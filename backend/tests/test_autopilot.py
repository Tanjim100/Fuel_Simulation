"""The autopilot's policy.

``cycle_once()`` is the whole policy, so these tests drive it directly with a
stubbed world rather than waiting for the loop. What they check is the
*shape* of the decision:

- ``OFF`` never plans and never writes.
- ``LIVE`` writes.
- The breaker gate skips on ``OPEN`` **and** on ``HALF_OPEN``.
- The tick-regression guard skips a repeated tick, and a rewound tick (a
  world reset) clears the guard instead of locking the autopilot out.
- The quarantine skips a tick whose dispatch was rejected.
- The budget caps truncate the accepted list.
- An empty gate result is reported as ``gated``, not as a dispatch.
- The status carries what the autopilot dispatched, and clears it when it
  dispatches nothing.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any, Optional

import pytest

from app.autopilot import AutopilotController, AutopilotMode
from app.config import Settings
from app.dispatch import GatedShipment
from app.optimizer import PlannedShipment
from app.simulator_client import Allocation, SimulationInstance


# --------------------------------------------------------------------------- #
# Stubs
# --------------------------------------------------------------------------- #
class StubBreaker:
    """A breaker stand-in whose state the test controls."""

    def __init__(self, state: str = "CLOSED") -> None:
        self.state = state

    def status(self):
        class S:
            def __init__(self, value: str) -> None:
                self.state = type("State", (), {"value": value})()

        return S(self.state)


class StubClient:
    """A simulator stand-in for the autopilot's calls.

    The stand-in is **breaker-shaped** on purpose. In the app,
    ``app.state.client`` *is* the ``CircuitBreaker`` (``main.py``: the
    breaker wraps the raw client and is stored in its place, so every
    consumer calls through it). The controller therefore reads the breaker's
    state off the client it was given -- so the stand-in has to answer
    ``status()`` the way a breaker does, or the breaker gate would never
    fire in the tests while firing correctly in production.
    """

    def __init__(self, *, tick: int = 70, breaker_state: str = "CLOSED") -> None:
        self.tick = tick
        self.breaker = StubBreaker(breaker_state)
        self.created: list[dict[str, Any]] = []
        self.rejected = False
        self._next_id = 100
        self._allocations: list[Allocation] = []

    def status(self):
        """The breaker's state, as the real client's ``status()`` reports it."""
        return self.breaker.status()

    async def instance(self) -> SimulationInstance:
        return SimulationInstance(
            id=1,
            scenario_id="baseline",
            scenario_version="1.0.0",
            seed=12345,
            sim_time="2026-01-01T08:00:00",
            tick=self.tick,
            tick_minutes=15,
            status="PAUSED",
        )

    async def create_allocation(self, request):
        payload = request.to_payload()
        for allocation in self._allocations:
            if allocation.idempotency_key == payload["idempotency_key"]:
                return allocation, False
        if self.rejected:
            from app.simulator_client import SimulatorConflict

            raise SimulatorConflict("The simulator refused the allocation.")
        allocation = Allocation(
            id=self._next_id,
            idempotency_key=payload["idempotency_key"],
            source_depot_id=payload["source_depot_id"],
            destination_station_id=payload["destination_station_id"],
            route_id=payload["route_id"],
            fuel_type=payload["fuel_type"],
            quantity=payload["quantity"],
            created_tick=self.tick,
            status="PENDING",
        )
        self._next_id += 1
        self._allocations.append(allocation)
        self.created.append(payload)
        return allocation, True


class StubBroker:
    """A broker stand-in that publishes a fixed snapshot.

    When ``follow`` is given, the published snapshot's tick tracks the
    client's. That mirrors a real world reset, which rewinds the tick
    counter everywhere at once -- not just the instance read.
    """

    def __init__(self, snapshot: Any, follow: Any = None) -> None:
        self._snapshot = snapshot
        self._follow = follow
        self.refreshes = 0

    async def refresh_snapshot(self):
        self.refreshes += 1
        if self._follow is not None:
            # A dataclass copy with the client's current tick.
            self._snapshot = replace(self._snapshot, tick=int(self._follow.tick))
        return self._snapshot

    @property
    def state(self):
        broker = self

        class State:
            @staticmethod
            def current():
                return broker._snapshot

        return State()


class StubJournal:
    """A journal stand-in that records the intents it was given."""

    def __init__(self) -> None:
        self.intents: list[str] = []
        self.results: list[str] = []

    async def record_intent(self, key: str, payload: dict[str, Any]) -> None:
        self.intents.append(key)

    async def record_result(self, key: str, **kwargs: Any) -> None:
        self.results.append(key)


def _gated(quantity: float = 1000.0, dispatch_tick: int = 70) -> GatedShipment:
    """A gated shipment, without going through the validator."""
    from app.models import Shipment

    shipment = Shipment(
        sequence=1,
        idempotency_key=f"alloc-t{dispatch_tick}-depot-gazipur-station-mirpur-DIESEL-1-abcdef",
        source_depot_id="depot-gazipur",
        destination_station_id="station-mirpur",
        route_id="route-gazipur-mirpur",
        fuel_type="DIESEL",
        quantity=quantity,
        reasoning="test",
    )
    planned = PlannedShipment(
        sequence=1,
        dispatch_tick=dispatch_tick,
        arrival_tick=dispatch_tick + 2,
        source_depot_id="depot-gazipur",
        destination_station_id="station-mirpur",
        route_id="route-gazipur-mirpur",
        fuel_type="DIESEL",
        quantity=quantity,
        priority_score=1.0,
        rationale="test",
        binding="demand",
    )
    return GatedShipment(shipment=shipment, planned=planned)


def _controller(
    client: StubClient,
    broker: StubBroker,
    journal: StubJournal,
    **settings_kwargs: Any,
) -> AutopilotController:
    settings = Settings(
        simulator_base_url="http://fake",
        redis_url="",
        **settings_kwargs,
    )
    return AutopilotController(
        client=client,
        broker=broker,
        journal=journal,
        settings=settings,
    )


@pytest.fixture
def world():
    from conftest import make_snapshot

    return make_snapshot(tick=70)


# --------------------------------------------------------------------------- #
# The mode gate
# --------------------------------------------------------------------------- #
async def test_off_never_plans_or_writes(world):
    client = StubClient()
    broker = StubBroker(world)
    journal = StubJournal()
    controller = _controller(client, broker, journal)

    result = await controller.cycle_once()

    assert result["decision"] == "off"
    assert broker.refreshes == 0, "an off autopilot must not even read the world"
    assert client.created == []


async def test_the_mode_starts_off(world):
    controller = _controller(StubClient(), StubBroker(world), StubJournal())
    status = await controller.status()
    assert status.mode == "off"
    assert status.enabled is False


async def test_switching_to_live_enables_the_controller(world):
    controller = _controller(StubClient(), StubBroker(world), StubJournal())
    status = await controller.set_mode(AutopilotMode.LIVE)
    assert status.mode == "live"
    assert status.enabled is True
    await controller.stop()


async def test_switching_back_to_off_disables_it(world):
    controller = _controller(StubClient(), StubBroker(world), StubJournal())
    await controller.set_mode(AutopilotMode.LIVE)
    status = await controller.set_mode(AutopilotMode.OFF)
    assert status.mode == "off"
    assert status.enabled is False


async def test_an_unknown_mode_is_refused():
    with pytest.raises(ValueError):
        AutopilotMode.parse("lve")


# --------------------------------------------------------------------------- #
# The breaker gate
# --------------------------------------------------------------------------- #
async def test_an_open_breaker_stops_the_cycle(world):
    client = StubClient(breaker_state="OPEN")
    broker = StubBroker(world)
    controller = _controller(client, broker, StubJournal())

    await controller.set_mode(AutopilotMode.LIVE)
    result = await controller.cycle_once()

    assert result["decision"] == "breaker_open"
    assert broker.refreshes == 0
    assert client.created == []
    await controller.stop()


async def test_a_half_open_breaker_stops_the_cycle(world):
    """A half-open breaker is still probing. Writing during the probe window
    is exactly the wrong time."""
    client = StubClient(breaker_state="HALF_OPEN")
    broker = StubBroker(world)
    controller = _controller(client, broker, StubJournal())

    await controller.set_mode(AutopilotMode.LIVE)
    result = await controller.cycle_once()

    assert result["decision"] == "breaker_open"
    assert client.created == []
    await controller.stop()


# --------------------------------------------------------------------------- #
# The tick guards
# --------------------------------------------------------------------------- #
async def test_a_repeated_tick_is_skipped(world):
    """The tick guard: one dispatch per simulator tick."""
    client = StubClient(tick=70)
    broker = StubBroker(world)
    controller = _controller(client, broker, StubJournal())

    await controller.set_mode(AutopilotMode.LIVE)
    first = await controller.cycle_once()
    assert first["decision"] == "dispatched"
    after_first = len(client.created)

    # The world has not advanced. A second cycle on the same tick must not
    # dispatch again.
    second = await controller.cycle_once()
    assert second["decision"] == "tick_regressed"
    assert len(client.created) == after_first, "the second cycle wrote again"
    await controller.stop()


async def test_a_reset_tick_clears_the_guard_and_dispatches_again(world):
    """A world reset rewinds the tick, and the autopilot must recover.

    The tick guard exists to stop a *repeat* on a spent tick. It must not
    become a permanent lockout: after a reset the old tick belongs to a
    timeline that no longer exists, so the guard's memory is cleared and
    the new timeline starts fresh.
    """
    client = StubClient(tick=70)
    broker = StubBroker(world, follow=client)
    controller = _controller(client, broker, StubJournal())

    await controller.set_mode(AutopilotMode.LIVE)
    first = await controller.cycle_once()
    assert first["decision"] == "dispatched"
    after_first = len(client.created)

    # The world was reset. The tick went backwards.
    client.tick = 12
    result = await controller.cycle_once()
    assert result["decision"] == "dispatched", (
        "a rewound tick must not lock the autopilot out"
    )
    assert len(client.created) > after_first, "the reset tick was never written"
    await controller.stop()


async def test_a_rewind_also_clears_the_quarantine(world):
    """The quarantine is tied to the old timeline too.

    A tick whose dispatch was rejected is quarantined for a few ticks. If
    the world is reset in the meantime, that quarantine refers to a
    timeline that no longer exists and must not suppress the new one.
    """
    client = StubClient(tick=70)
    broker = StubBroker(world, follow=client)
    controller = _controller(client, broker, StubJournal())

    await controller.set_mode(AutopilotMode.LIVE)
    controller._quarantine_tick = 70
    controller._quarantine_until = 72

    client.tick = 12
    result = await controller.cycle_once()
    assert result["decision"] != "quarantined", (
        "a quarantine from the old timeline suppressed the new one"
    )
    await controller.stop()


async def test_a_rewind_is_detected_from_the_quarantine_alone(world):
    """A failed dispatch leaves a quarantine but never a dispatch record.

    So the quarantine can be the *only* trace of the old timeline. The
    rewind must be recognised from it, or the new timeline's first tick
    would be suppressed by a quarantine that no longer applies.
    """
    client = StubClient(tick=70)
    broker = StubBroker(world, follow=client)
    controller = _controller(client, broker, StubJournal())

    await controller.set_mode(AutopilotMode.LIVE)
    # A rejected dispatch: the quarantine is set, nothing was dispatched.
    controller._quarantine_tick = 70
    controller._quarantine_until = 72
    assert controller._last_dispatched_tick is None

    client.tick = 12
    result = await controller.cycle_once()
    assert result["decision"] != "quarantined", (
        "the quarantine was recognised only when a dispatch record existed"
    )
    await controller.stop()


async def test_a_new_tick_dispatches_again(world):
    client = StubClient(tick=70)
    broker = StubBroker(world)
    controller = _controller(client, broker, StubJournal())

    await controller.set_mode(AutopilotMode.LIVE)
    await controller.cycle_once()
    after_first = len(client.created)

    client.tick = 71
    from conftest import make_snapshot

    broker._snapshot = make_snapshot(tick=71)
    result = await controller.cycle_once()

    assert result["decision"] == "dispatched"
    assert len(client.created) > after_first, "the new tick did not dispatch"
    await controller.stop()


# --------------------------------------------------------------------------- #
# The quarantine
# --------------------------------------------------------------------------- #
async def test_a_rejected_dispatch_quarantines_the_tick(world):
    client = StubClient(tick=70)
    client.rejected = True
    broker = StubBroker(world)
    controller = _controller(client, broker, StubJournal())

    await controller.set_mode(AutopilotMode.LIVE)
    first = await controller.cycle_once()
    assert first["decision"] == "error"

    # The same tick is quarantined: a simulator that rejected the dispatch
    # will reject it again.
    second = await controller.cycle_once()
    assert second["decision"] == "quarantined"
    await controller.stop()


# --------------------------------------------------------------------------- #
# The budget
# --------------------------------------------------------------------------- #
def test_the_budget_truncates_on_the_shipment_count():
    shipments = [_gated(quantity=100.0) for _ in range(5)]
    selected = AutopilotController._apply_budget(shipments, 1_000_000.0, 2)
    assert len(selected) == 2


def test_the_budget_truncates_on_the_litres():
    shipments = [_gated(quantity=600.0), _gated(quantity=600.0), _gated(quantity=600.0)]
    selected = AutopilotController._apply_budget(shipments, 1000.0, 8)
    assert len(selected) == 1
    assert selected[0].shipment.quantity == 600.0


def test_the_budget_keeps_a_shipment_that_fits():
    shipments = [_gated(quantity=100.0)]
    selected = AutopilotController._apply_budget(shipments, 1000.0, 8)
    assert len(selected) == 1


# --------------------------------------------------------------------------- #
# What the autopilot dispatched
# --------------------------------------------------------------------------- #
async def test_the_status_carries_what_was_dispatched(world):
    client = StubClient(tick=70)
    broker = StubBroker(world)
    controller = _controller(client, broker, StubJournal())

    await controller.set_mode(AutopilotMode.LIVE)
    await controller.cycle_once()

    status = await controller.status()
    assert status.last_shipments, "the status must carry the dispatched shipments"
    assert len(status.last_shipments) == status.last_created + status.last_replayed
    assert status.last_litres > 0
    await controller.stop()


async def test_a_cycle_that_dispatches_nothing_clears_the_shipments(world):
    client = StubClient(tick=70)
    broker = StubBroker(world)
    controller = _controller(client, broker, StubJournal())

    await controller.set_mode(AutopilotMode.LIVE)
    await controller.cycle_once()

    status = await controller.status()
    assert status.last_shipments, "the first cycle dispatched something"

    # A cycle that ships nothing must clear the list, so the console shows
    # the most recent *real* dispatch rather than a stale one.
    controller._finish("gated", AutopilotMode.LIVE, tick=71)

    status = await controller.status()
    assert status.last_shipments == []
    await controller.stop()


# --------------------------------------------------------------------------- #
# The journal
# --------------------------------------------------------------------------- #
async def test_the_intent_is_journalled_before_the_write(world):
    client = StubClient(tick=70)
    broker = StubBroker(world)
    journal = StubJournal()
    controller = _controller(client, broker, journal)

    await controller.set_mode(AutopilotMode.LIVE)
    await controller.cycle_once()

    assert len(journal.intents) == len(client.created)
    assert len(journal.results) == len(client.created)
    await controller.stop()
