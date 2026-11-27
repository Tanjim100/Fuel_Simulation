"""The autopilot: dispatch without an operator pressing the button.

The platform's manual dispatch path is safe but *reactive* -- the operator has
to notice a card and click it inside the tick window. The autopilot makes the
platform *proactive*: once it is on, every tick it re-plans against the live
world and dispatches what the gate accepts.

**What it is not.** It is not a new planner and not a new gate. It calls
``optimize()`` and ``gate_plan()`` -- the exact functions the operator's
button calls -- and it writes through ``execute_shipments()``, the exact
shared write path. The autopilot is *when* to call them, not *how* to plan.
A second planner would be a second set of arithmetic to be wrong.

**Two modes.**

- ``OFF`` -- no planning, no writing. This is the shipped default, and it is
  what makes the feature safe to exist: ``docker compose up -d`` behaves
  exactly as it did before this module was written. With the autopilot off,
  the console tells the operator when there is something to dispatch.
- ``LIVE`` -- plans, gates, and dispatches on its own, once per tick. The
  console shows what it dispatched; the operator can still dispatch by hand.

There is deliberately no third, rehearsal mode. A mode that plans without
writing needs its own status, its own metric outcome, its own console
treatment and its own tests -- and it buys nothing that the ``LIVE`` mode's
capabilities cannot show: the console reports the plan, the gate's verdict and
the resulting allocations either way.

**Why the autopilot is built the way it is.** The autopilot writes to the same
simulator that the 49-check verifier suite asserts against. If it filled a
station's free space, ``STATION_FREE_SPACE`` would bind before
``ROUTE_MAX_SHIPMENT`` and a passing check would start failing. Three
independent defences stand in the way, and they are deliberately redundant:

1. **The tick guard.** The autopilot dispatches at most once per simulator
   tick. A world reset (which rewinds the tick) therefore cannot replay a
   tick that was already dispatched.
2. **The idempotency key.** The gate mints a deterministic key per shipment
   (``alloc-t{tick}-{depot}-{station}-{fuel}-{seq}-{hash8}``). The simulator
   returns the *original* allocation for a repeated key, so a repeat is a
   REPLAY, not a second shipment. Unlike the tick guard, this one survives a
   backend restart.
3. **The simulator's own per-tick dispatch cap.** The simulator counts the
   allocations created on the current tick, per depot, and refuses to exceed
   its cap. The optimiser already subtracts that committed volume, so the
   autopilot's plan respects it too.

There is deliberately **no** durable "already dispatched" table. A table
would need its own schema, its own reconciliation, and its own recovery
story -- to duplicate what the idempotency key and the simulator's cap
already guarantee.

**One lock, two callers.** The autopilot and the operator's
``POST /api/optimize/execute`` share a single ``asyncio.Lock``
(``app.state.dispatch_lock``). That makes "the autopilot and a manual click
cannot interleave" a structural property rather than a convention.

**Only the pure detectors are used.** The anomaly escalation reads
``detect_snapshot_anomalies()``, which is a pure function of the snapshot.
``AnomalyDetector.observe()`` is *not* called, because it mutates the
detector's rolling history -- an autopilot running on its own cadence must
not perturb the anomaly pipeline's state.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from .anomaly import detect_snapshot_anomalies
from .circuit_breaker import BreakerState, CircuitOpen
from .dispatch import GatedShipment, execute_shipments, gate_plan
from .forecast import build_forecast
from .metrics import (
    AUTOPILOT_CYCLES,
    AUTOPILOT_DISPATCHES,
    AUTOPILOT_ENABLED,
    AUTOPILOT_LITRES,
    AUTOPILOT_MODE,
    DECISIONS,
)
from .optimizer import OptimizerConfig, optimize
from .simulator_client import SimulatorError

__all__ = [
    "AutopilotMode",
    "AutopilotStatus",
    "AutopilotController",
]

LOGGER = logging.getLogger(__name__)


class AutopilotMode(str, Enum):
    """The two modes the autopilot can be in."""

    OFF = "off"
    LIVE = "live"

    @classmethod
    def parse(cls, value: str) -> "AutopilotMode":
        """Parse a mode string, raising ``ValueError`` on an unknown one.

        The parse is strict on purpose. Silently clamping an unknown mode to
        ``OFF`` would leave an operator believing the autopilot was running
        when it was not -- the worst possible failure for a feature whose
        whole job is to act without supervision.
        """
        try:
            return cls(str(value).strip().lower())
        except ValueError:
            valid = ", ".join(m.value for m in cls)
            raise ValueError(
                f"Unknown autopilot mode {value!r}. Valid modes: {valid}."
            ) from None

    @property
    def gauge_value(self) -> int:
        """The numeric value published as ``backend_autopilot_mode``.

        A gauge cannot carry a string, so the mode is encoded as an integer.
        ``OFF`` is 0 because it is the healthy default -- a dashboard reading
        "0" is unambiguously the one that is not dispatching.
        """
        return {"off": 0, "live": 1}[self.value]


@dataclass(frozen=True)
class AutopilotStatus:
    """A snapshot of the autopilot's state.

    Frozen because it is handed to callers -- the console renders it, the
    endpoint serialises it -- and a status that could change underneath a
    reader would be a lie.
    """

    mode: str = "off"
    enabled: bool = False
    running: bool = False
    cycle: int = 0
    last_cycle_tick: Optional[int] = None
    last_decision: Optional[str] = None
    last_created: int = 0
    last_replayed: int = 0
    last_failed: int = 0
    last_litres: float = 0.0
    last_escalated: bool = False
    quarantine_until_tick: Optional[int] = None
    last_error: Optional[str] = None
    updated_at: str = ""
    last_shipments: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "enabled": self.enabled,
            "running": self.running,
            "cycle": self.cycle,
            "last_cycle_tick": self.last_cycle_tick,
            "last_decision": self.last_decision,
            "last_created": self.last_created,
            "last_replayed": self.last_replayed,
            "last_failed": self.last_failed,
            "last_litres": self.last_litres,
            "last_escalated": self.last_escalated,
            "quarantine_until_tick": self.quarantine_until_tick,
            "last_error": self.last_error,
            "updated_at": self.updated_at,
            "last_shipments": self.last_shipments,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class AutopilotController:
    """Owns the autopilot's mode, its loop, and its decision policy.

    The controller holds no planner and no gate of its own. It decides
    *whether* to plan and *when* to write; the planner and the gate remain
    the shared, already-tested components.
    """

    def __init__(
        self,
        *,
        client: Any,
        broker: Any,
        journal: Any = None,
        settings: Any = None,
        dispatch_lock: Optional[asyncio.Lock] = None,
    ) -> None:
        """
        :param client: the breaker-wrapped simulator client. The autopilot
            calls through the breaker like everything else, so an open
            circuit stops the autopilot before it plans anything.
        :param broker: the event broker, for ``refresh_snapshot()``.
        :param journal: the dispatch journal. The autopilot writes through
            the shared ``execute_shipments()``, so the journal is that
            function's journal -- not a second one.
        :param settings: the resolved :class:`~app.config.Settings`.
        :param dispatch_lock: the lock shared with
            ``POST /api/optimize/execute``.
        """
        self._client = client
        self._broker = broker
        self._journal = journal
        self._settings = settings
        self._dispatch_lock = dispatch_lock or asyncio.Lock()

        self._lock = asyncio.Lock()
        self._mode = AutopilotMode.OFF
        self._status = AutopilotStatus(updated_at=_now_iso())
        self._task: Optional[asyncio.Task[None]] = None

        #: The last tick the autopilot dispatched on. The tick guard compares
        #: against this: a tick that is not strictly greater is a repeat.
        self._last_dispatched_tick: Optional[int] = None
        #: The tick whose dispatch failed, and the tick it is quarantined
        #: until. A simulator that rejected a dispatch will reject the same
        #: one again; retrying every two seconds would only spam the
        #: journal.
        self._quarantine_tick: Optional[int] = None
        self._quarantine_until: Optional[int] = None

        if settings is not None:
            configured = getattr(settings, "autopilot_mode", "off")
            with contextlib.suppress(ValueError):
                self._mode = AutopilotMode.parse(str(configured))

        self._publish_mode_metrics()

    # ------------------------------------------------------------------ #
    # Mode
    # ------------------------------------------------------------------ #
    async def set_mode(self, mode: AutopilotMode | str) -> AutopilotStatus:
        """Change the autopilot's mode.

        The loop is **not** restarted when moving to ``LIVE``: the mode is read
        at the top of every cycle, so the change takes effect on the next cycle
        without losing any in-flight state.

        Moving to ``OFF`` *does* stop the loop, because a controller that is
        off should not be running cycles that immediately do nothing.
        """
        resolved = (
            mode if isinstance(mode, AutopilotMode) else AutopilotMode.parse(str(mode))
        )
        async with self._lock:
            previous = self._mode
            self._mode = resolved
            self._publish_mode_metrics()

            if resolved is AutopilotMode.OFF:
                await self._stop_task_locked()
            elif self._task is None or self._task.done():
                await self._start_task_locked()

            self._status = replace(
                self._status,
                mode=resolved.value,
                enabled=resolved is not AutopilotMode.OFF,
                running=self._task is not None and not self._task.done(),
                updated_at=_now_iso(),
            )
            LOGGER.info(
                "Autopilot mode %s -> %s", previous.value, resolved.value
            )
            return self._status

    @property
    def mode(self) -> AutopilotMode:
        return self._mode

    async def status(self) -> AutopilotStatus:
        """The autopilot's current state."""
        async with self._lock:
            return replace(
                self._status,
                running=self._task is not None and not self._task.done(),
            )

    # ------------------------------------------------------------------ #
    # The loop
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        """Start the loop, if the configured mode is not ``OFF``."""
        async with self._lock:
            if self._mode is AutopilotMode.OFF:
                return
            if self._task is None or self._task.done():
                await self._start_task_locked()
            self._status = replace(
                self._status,
                running=True,
                enabled=True,
                updated_at=_now_iso(),
            )

    async def stop(self) -> None:
        """Stop the loop and wait for the in-flight cycle to finish."""
        async with self._lock:
            await self._stop_task_locked()
            self._status = replace(
                self._status, running=False, updated_at=_now_iso()
            )

    async def _start_task_locked(self) -> None:
        self._task = asyncio.create_task(self._loop())
        LOGGER.info("Autopilot loop started (mode=%s)", self._mode.value)

    async def _stop_task_locked(self) -> None:
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _loop(self) -> None:
        """Run a cycle every ``autopilot_interval_seconds``.

        The interval is sized against the simulator's tick: at the default
        2-second tick and a 2-second interval, the autopilot plans once per
        tick. A slower world means slower cycles, which is correct -- there
        is nothing to dispatch between ticks anyway.
        """
        interval = float(getattr(self._settings, "autopilot_interval_seconds", 2.0))
        interval = max(1.0, interval)
        while True:
            try:
                await asyncio.sleep(interval)
                await self.cycle_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - the loop must never die
                LOGGER.exception("Autopilot cycle failed")

    # ------------------------------------------------------------------ #
    # The cycle -- the whole policy, in one function
    # ------------------------------------------------------------------ #
    def _is_rewind(self, now_tick: int) -> bool:
        """Has the world been reset out from under us?

        The simulator's ``/admin/reset`` rewinds the tick counter to zero.
        The autopilot's tick guard remembers the last tick it dispatched on,
        which after a reset refers to a timeline that no longer exists.

        Telling a *rewind* apart from a *repeat* is the whole point of this
        method. Both look like "a tick that is not greater than the one we
        dispatched on", but they demand opposite responses:

        - a **repeat** (the same tick) means the tick is spent and the
          autopilot must not write to it again;
        - a **rewind** (a strictly lower tick) means the world restarted and
          the autopilot must forget its old bookkeeping so it can dispatch
          again.

        A strictly-lower tick is only possible after a reset, because the
        world's tick counter only ever moves forward within a run.

        Both pieces of timeline-scoped memory are consulted: the last
        dispatched tick **and** the quarantined tick. A failed dispatch
        sets the quarantine without ever having recorded a dispatch, so
        the quarantine can be the only trace of the old timeline.
        """
        marks = [
            tick
            for tick in (self._last_dispatched_tick, self._quarantine_tick)
            if tick is not None
        ]
        if not marks:
            return False
        return now_tick < max(marks)

    async def cycle_once(self) -> dict[str, Any]:
        """Run one autopilot cycle.

        The ten steps below are the entire policy. Keeping them in one
        function means the policy can be tested without the loop, and there
        is exactly one place where "should we dispatch?" is decided.
        """
        mode = self._mode

        # -- 1. the mode gate ------------------------------------------- #
        if mode is AutopilotMode.OFF:
            return self._finish("off", mode)

        # -- 2. the breaker gate ----------------------------------------- #
        # ``OPEN`` means the upstream is known-bad. ``HALF_OPEN`` means the
        # breaker is still probing it -- writing during the probe window is
        # exactly the wrong time, because the probe is about to tell us
        # whether the upstream recovered at all.
        breaker_state = self._breaker_state()
        if breaker_state in ("OPEN", "HALF_OPEN"):
            AUTOPILOT_CYCLES.inc(outcome="breaker_open")
            return self._finish("breaker_open", mode)

        # -- 3. the tick probe ------------------------------------------- #
        # One cheap read to learn the world's tick. No plan, no write. If
        # this fails the upstream is not answering and there is nothing to
        # do about it.
        try:
            instance = await self._client.instance()
            now_tick = int(instance.tick)
        except (CircuitOpen, SimulatorError, OSError, asyncio.TimeoutError) as exc:
            LOGGER.warning("Autopilot could not read the world: %s", exc)
            AUTOPILOT_CYCLES.inc(outcome="error")
            return self._finish("error", mode, error=str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.exception("Autopilot tick probe failed")
            AUTOPILOT_CYCLES.inc(outcome="error")
            return self._finish("error", mode, error=str(exc))

        # -- 4. the tick-regression guard -------------------------------- #
        # The guard exists to stop the autopilot dispatching twice on the
        # same tick. It must not stop it from dispatching *at all*.
        #
        # A tick that is not strictly greater than the one we last dispatched
        # on is either a repeat (same tick) or a rewind (the world was
        # reset and the tick counter restarted). Those are opposite
        # outcomes, so they are told apart by the direction of the move:
        #
        #   equal  -> a repeat. Skip: the tick is spent.
        #   lower  -> a rewind. The old tick belongs to a timeline that no
        #             longer exists, so the guard's memory is cleared and
        #             the new timeline starts fresh.
        #
        # Without the rewind branch a reset would lock the autopilot out
        # permanently: the world would have to climb back past the old
        # high-water mark before a single shipment could move again.
        if self._is_rewind(now_tick):
            LOGGER.info(
                "The world was reset (tick %d -> %d). Clearing the tick guard.",
                self._last_dispatched_tick,
                now_tick,
            )
            self._last_dispatched_tick = None
            self._quarantine_tick = None
            self._quarantine_until = None
        elif (
            self._last_dispatched_tick is not None
            and now_tick <= self._last_dispatched_tick
        ):
            AUTOPILOT_CYCLES.inc(outcome="tick_regressed")
            return self._finish("tick_regressed", mode, tick=now_tick)

        # -- 5. the quarantine -------------------------------------------- #
        if (
            self._quarantine_tick == now_tick
            and self._quarantine_until is not None
            and now_tick <= self._quarantine_until
        ):
            AUTOPILOT_CYCLES.inc(outcome="quarantined")
            return self._finish("quarantined", mode, tick=now_tick)

        # -- 6. one dispatch per tick ------------------------------------ #
        # The lock is shared with the manual execute endpoint, so an
        # operator's click and an autopilot cycle cannot interleave.
        async with self._dispatch_lock:
            # The same guard as step 4, re-checked under the lock: an
            # operator's manual dispatch may have spent this tick while
            # this cycle was waiting for the lock. A rewind is still a
            # rewind, and it clears the memory rather than skipping.
            if self._is_rewind(now_tick):
                self._last_dispatched_tick = None
                self._quarantine_tick = None
                self._quarantine_until = None
            elif (
                self._last_dispatched_tick is not None
                and now_tick <= self._last_dispatched_tick
            ):
                AUTOPILOT_CYCLES.inc(outcome="tick_regressed")
                return self._finish("tick_regressed", mode, tick=now_tick)

            # -- 7. a fresh snapshot ------------------------------------- #
            # The plan must be against a *current* world. A cached snapshot
            # is a stale plan, and a stale plan is a wrong write.
            try:
                await self._broker.refresh_snapshot()
            except CircuitOpen:
                AUTOPILOT_CYCLES.inc(outcome="breaker_open")
                return self._finish("breaker_open", mode, tick=now_tick)
            except Exception as exc:  # pragma: no cover - defensive
                LOGGER.warning("Autopilot could not refresh the world: %s", exc)
                AUTOPILOT_CYCLES.inc(outcome="error")
                return self._finish("error", mode, tick=now_tick, error=str(exc))

            snapshot = self._current_snapshot()
            if snapshot is None:
                AUTOPILOT_CYCLES.inc(outcome="error")
                return self._finish("error", mode, tick=now_tick)

            # The snapshot's own tick is authoritative for the plan -- it is
            # the world the plan will be written against.
            plan_tick = int(snapshot.tick)

            # -- 8. plan and gate --------------------------------------- #
            config = self._optimizer_config()
            try:
                forecast = build_forecast(snapshot, horizon=config.horizon_ticks)
                result = optimize(snapshot, forecast, config=config)
                gated = gate_plan(snapshot, result.shipments)
            except Exception as exc:
                LOGGER.exception("Autopilot could not build a plan")
                AUTOPILOT_CYCLES.inc(outcome="error")
                return self._finish("error", mode, tick=plan_tick, error=str(exc))

            executable = [
                g for g in gated.accepted if g.planned.dispatch_tick == plan_tick
            ]

            # -- 9. the budget tier -------------------------------------- #
            # Anomalies *escalate* the budget; they do not gate dispatch.
            # A detected problem is a reason to move more fuel, not a
            # reason to stop moving it.
            anomalies = detect_snapshot_anomalies(snapshot, forecast)
            escalated = bool(anomalies)
            max_litres = (
                float(getattr(self._settings, "autopilot_max_litres_per_tick_escalated", 24000.0))
                if escalated
                else float(getattr(self._settings, "autopilot_max_litres_per_tick", 12000.0))
            )
            max_shipments = int(
                getattr(self._settings, "autopilot_max_shipments_per_tick", 8)
            )

            selected = self._apply_budget(executable, max_litres, max_shipments)

            if not selected:
                # Nothing to send. Either the gate refused everything, or
                # the plan had no shipment for this tick.
                outcome = "gated" if gated.accepted else "no_demand"
                AUTOPILOT_CYCLES.inc(outcome=outcome)
                return self._finish(
                    outcome,
                    mode,
                    tick=plan_tick,
                    escalated=escalated,
                    gated=gated,
                )

            # -- 10. the write -------------------------------------------- #
            try:
                outcome_result = await execute_shipments(
                    self._client, self._journal, selected
                )
            except Exception as exc:
                LOGGER.exception("Autopilot dispatch failed")
                self._quarantine_tick = plan_tick
                self._quarantine_until = plan_tick + int(
                    getattr(self._settings, "autopilot_reset_quarantine_ticks", 2)
                )
                AUTOPILOT_CYCLES.inc(outcome="error")
                return self._finish(
                    "error", mode, tick=plan_tick, error=str(exc), escalated=escalated
                )

            created = len(outcome_result.created)
            replayed = len(outcome_result.replayed)
            failed = len(outcome_result.failed)

            # What the autopilot actually put on the wire. The console shows
            # this so an operator can see the system's own dispatch without
            # having to read the journal. Every value in it came from the
            # simulator's response.
            shipments = list(outcome_result.created) + list(outcome_result.replayed)

            AUTOPILOT_DISPATCHES.inc(float(created), outcome="created")
            AUTOPILOT_DISPATCHES.inc(float(replayed), outcome="replayed")
            AUTOPILOT_DISPATCHES.inc(float(failed), outcome="failed")
            if outcome_result.total_litres:
                AUTOPILOT_LITRES.inc(outcome_result.total_litres)

            # The decision frequency the brief's §15 intelligence layer
            # asks for. The autopilot gets its **own** label value: these
            # are decisions the operator did not make, and folding them
            # into ``kind="dispatch"`` would make the human-decision
            # counter dishonest.
            if created:
                DECISIONS.inc(float(created), kind="autopilot")

            if failed:
                # A rejected allocation means the world moved under the plan
                # -- or the simulator refused. Either way, retrying the same
                # tick immediately would fail identically.
                self._quarantine_tick = plan_tick
                self._quarantine_until = plan_tick + int(
                    getattr(self._settings, "autopilot_reset_quarantine_ticks", 2)
                )
                AUTOPILOT_CYCLES.inc(outcome="error")
                return self._finish(
                    "error",
                    mode,
                    tick=plan_tick,
                    created=created,
                    replayed=replayed,
                    failed=failed,
                    litres=outcome_result.total_litres,
                    escalated=escalated,
                    error=f"{failed} shipment(s) were rejected by the simulator.",
                    shipments=shipments,
                )

            # The tick is now spent. This is the tick guard's write-down.
            self._last_dispatched_tick = plan_tick
            AUTOPILOT_CYCLES.inc(outcome="dispatched")
            LOGGER.info(
                "Autopilot dispatched %d shipment(s), %.1f L on tick %d%s",
                created + replayed,
                outcome_result.total_litres,
                plan_tick,
                " (escalated)" if escalated else "",
            )
            return self._finish(
                "dispatched",
                mode,
                tick=plan_tick,
                created=created,
                replayed=replayed,
                failed=failed,
                litres=outcome_result.total_litres,
                escalated=escalated,
                gated=gated,
                shipments=shipments,
            )

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _apply_budget(
        executable: list[GatedShipment], max_litres: float, max_shipments: int
    ) -> list[GatedShipment]:
        """Truncate the accepted shipments to the tick's budget.

        The cap is applied to the gate's **accepted** list, not to the
        planner's input. The plan itself is unchanged -- the optimiser still
        sees the whole world -- and only the *dispatched subset* is bounded.
        Bounding the planner's input instead would produce a different plan
        for the same world, which is exactly the kind of invisible
        divergence between the autopilot's decisions and the operator's that
        makes a system hard to reason about.
        """
        selected: list[GatedShipment] = []
        litres = 0.0
        for gated_shipment in executable:
            if len(selected) >= max_shipments:
                break
            quantity = float(gated_shipment.shipment.quantity)
            if litres + quantity > max_litres:
                continue
            selected.append(gated_shipment)
            litres += quantity
        return selected

    def _breaker_state(self) -> str:
        """The breaker's state as a string, or ``CLOSED`` if unavailable.

        The controller is given the **breaker** as its client -- the breaker
        wraps the raw client, and the app stores the breaker at
        ``app.state.client``. So the state is read off the client itself
        when it *is* a breaker.

        A raw client (no ``status()``, or a ``status()`` that is not a
        breaker's) means there is no breaker to gate on, and the answer is
        ``CLOSED`` -- the safe default, because with no breaker there is no
        upstream health signal to consult.
        """
        candidate = getattr(self._client, "_breaker", None) or self._client
        status = getattr(candidate, "status", None)
        if not callable(status):
            return BreakerState.CLOSED.value
        try:
            snapshot = status()
        except Exception:  # pragma: no cover - defensive
            return BreakerState.CLOSED.value
        state = getattr(snapshot, "state", None)
        value = getattr(state, "value", state)
        if not isinstance(value, str):
            return BreakerState.CLOSED.value
        return value

    def _current_snapshot(self) -> Any:
        """The broker's current snapshot, or ``None``."""
        state = getattr(self._broker, "state", None)
        if state is None:
            return None
        try:
            return state.current()
        except Exception:  # pragma: no cover - defensive
            return None

    def _optimizer_config(self) -> OptimizerConfig:
        """The optimiser configuration, from the settings."""
        settings = self._settings
        if settings is None:  # pragma: no cover - the app always has settings
            return OptimizerConfig()
        return OptimizerConfig(
            horizon_ticks=settings.optimizer_horizon_ticks,
            safety_stock_fraction=settings.optimizer_safety_stock_fraction,
            solver=settings.optimizer_solver,
            max_shipments=settings.optimizer_max_shipments,
        )

    def _publish_mode_metrics(self) -> None:
        AUTOPILOT_MODE.set(self._mode.gauge_value)
        AUTOPILOT_ENABLED.set(1.0 if self._mode is not AutopilotMode.OFF else 0.0)

    def _finish(
        self,
        decision: str,
        mode: AutopilotMode,
        *,
        tick: Optional[int] = None,
        created: int = 0,
        replayed: int = 0,
        failed: int = 0,
        litres: float = 0.0,
        escalated: bool = False,
        error: Optional[str] = None,
        gated: Any = None,
        selected: Optional[list[GatedShipment]] = None,
        shipments: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        """Record the cycle's outcome and return the cycle result.

        ``shipments`` is what the autopilot actually dispatched. It is kept
        only when something was dispatched: a cycle that sent nothing clears
        the list, so the console shows the most recent *real* dispatch rather
        than a stale one from several ticks ago.
        """
        status = replace(
            self._status,
            mode=mode.value,
            enabled=mode is not AutopilotMode.OFF,
            cycle=self._status.cycle + 1,
            last_cycle_tick=tick if tick is not None else self._status.last_cycle_tick,
            last_decision=decision,
            last_created=created,
            last_replayed=replayed,
            last_failed=failed,
            last_litres=round(litres, 3),
            last_escalated=escalated,
            quarantine_until_tick=self._quarantine_until,
            last_error=error,
            updated_at=_now_iso(),
            last_shipments=list(shipments) if shipments else [],
        )
        self._status = status

        result: dict[str, Any] = {
            "status": "OK",
            "decision": decision,
            "mode": mode.value,
            "tick": tick,
            "created": created,
            "replayed": replayed,
            "failed": failed,
            "total_litres": round(litres, 3),
            "escalated": escalated,
            "error": error,
            "shipments": list(shipments) if shipments else [],
        }
        if gated is not None:
            result["gated"] = gated.to_dict()
        if selected is not None:
            result["would_dispatch"] = [g.to_dict() for g in selected]
        return result
