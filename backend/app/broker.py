"""SSE broker: owns the simulator's event stream, fans it out, and never lies.

Why a broker
------------
The simulator's SSE endpoint has a **silent failure mode**. Each subscriber has
its own ``asyncio.Queue(maxsize=200)``. When that queue overflows, the
simulator removes the subscriber from its subscriber set but **leaves the HTTP
connection open**, with the generator blocked awaiting a queue that will never
be filled again.

Live-verified consequence: after 240+ events queued while the client did not
read, the client read **nothing at all** -- no domain events and no keepalives.
The connection looks healthy to an HTTP client, and the simulator itself stays
healthy. It is a permanently silent subscription.

Why frame-silence cannot detect it
----------------------------------
A *paused* simulator also emits nothing for minutes. Any liveness check based
on "have I seen a frame recently?" would false-positive constantly. So the
broker measures **tick progress**, not frame arrival:

    watchdog (every 5s):
        tick_now = GET /v1/instance -> tick
        if tick_now != last_tick_seen:          # the world MOVED
            if now - last_sse_event_at > 20s:   # ...but we heard nothing
                suspected_drop = True
                -> cancel the orphaned SSE task
                -> open a NEW subscription (re-registers us)
                -> poller covers the gap
            else:
                suspected_drop = False
        last_tick_seen = tick_now

The old subscription can never recover on its own -- its queue is orphaned
forever -- so reconnecting is the only cure. A poller runs at all times and
reconciles world state regardless, which means a drop degrades freshness, never
correctness. A forced reconnect every ``SSE_MAX_LIFETIME`` (900 s) bounds the
blast radius of an undetected drop.

Modes
-----
* ``sse``      -- subscribed and receiving events.
* ``polling``  -- SSE is unavailable, suspected dead, or being retried; the
  poller keeps the world fresh and the broker keeps trying to reclaim SSE.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

import httpx

from .config import Settings
from .circuit_breaker import CircuitOpen
from .metrics import (
    BROKER_MODE,
    BROKER_PROBE_FAILURES,
    BROKER_RECONNECTS,
    BROKER_SUSPECTED_DROPS,
    SIMULATOR_CONCURRENCY_LIMIT,
    SIMULATOR_INFLIGHT,
    SIMULATOR_QUEUE_DEPTH,
    SIMULATOR_SATURATION,
    SNAPSHOT_AGE,
    SNAPSHOT_REFRESH,
    SSE_DROPPED,
    SSE_EVENTS,
    SSE_FANOUT,
)
from .simulator_client import SimulatorClient, SimulatorError
from .state import (
    Observation,
    ObservationLog,
    WorldSnapshot,
    WorldStateCache,
    build_observations,
)

__all__ = ["BrokerEvent", "BrokerStatus", "EventBroker"]

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class BrokerEvent:
    """A normalised event handed to dashboard subscribers."""

    event: str
    data: dict[str, Any]
    received_at: float = field(default_factory=time.time)
    tick: Optional[int] = None

    def to_sse(self) -> str:
        """Render as an SSE frame."""
        payload = json.dumps(self.data, separators=(",", ":"), default=str)
        return f"event: {self.event}\ndata: {payload}\n\n"


@dataclass
class BrokerStatus:
    """A snapshot of the broker's health, served by ``/api/stream-health``."""

    mode: str = "starting"
    subscribers: int = 0
    events_received: int = 0
    events_fanned_out: int = 0
    events_dropped: int = 0
    reconnects: int = 0
    suspected_drops: int = 0
    poll_count: int = 0
    snapshot_refreshes: int = 0
    last_event_at: Optional[float] = None
    last_event_monotonic: Optional[float] = None
    last_tick_seen: int = 0
    last_poll_at: Optional[float] = None
    last_error: Optional[str] = None
    sse_connected_since: Optional[float] = None
    resync_required: bool = False
    forced_reconnects: int = 0
    #: True when the simulator accepted an SSE subscription. This is tracked
    #: *separately* from event delivery: a paused simulator accepts the
    #: connection but emits nothing, so "events flowing" would wrongly read
    #: as "SSE broken".
    sse_reachable: bool = False
    #: When the last reachability probe ran.
    last_probe_at: Optional[float] = None
    #: Consecutive failed reachability probes.
    probe_failures: int = 0

    def to_dict(self) -> dict[str, Any]:
        now = time.monotonic()
        silence = (
            now - self.last_event_monotonic
            if self.last_event_monotonic is not None
            else None
        )
        return {
            "mode": self.mode,
            "subscribers": self.subscribers,
            "events_received": self.events_received,
            "events_fanned_out": self.events_fanned_out,
            "events_dropped": self.events_dropped,
            "reconnects": self.reconnects,
            "suspected_drops": self.suspected_drops,
            "poll_count": self.poll_count,
            "snapshot_refreshes": self.snapshot_refreshes,
            "last_tick_seen": self.last_tick_seen,
            "last_event_at": self.last_event_at,
            "seconds_since_last_event": (
                round(silence, 2) if silence is not None else None
            ),
            "last_poll_at": self.last_poll_at,
            "last_error": self.last_error,
            "sse_connected_since": self.sse_connected_since,
            "sse_reachable": self.sse_reachable,
            "last_probe_at": self.last_probe_at,
            "probe_failures": self.probe_failures,
            "resync_required": self.resync_required,
            "forced_reconnects": self.forced_reconnects,
            "uptime_seconds": round(time.time() - _START_TIME, 2),
        }


_START_TIME = time.time()


class EventBroker:
    """Owns the simulator SSE subscription and fans events out to subscribers."""

    def __init__(
        self,
        client: SimulatorClient,
        state: WorldStateCache,
        settings: Settings,
        *,
        snapshot_fetcher: Optional[
            Callable[[], Awaitable[WorldSnapshot]]
        ] = None,
        sse_probe: Optional[Callable[[], Awaitable[bool]]] = None,
        observations: Optional[ObservationLog] = None,
        breaker: Optional[Any] = None,
        dlq: Optional[Any] = None,
    ) -> None:
        #: The **raw** simulator client.
        #:
        #: The SSE subscription is a long-lived async generator, not a
        #: request-shaped call, so it must not go through the circuit
        #: breaker: the breaker mediates individual calls, and a subscription
        #: is neither individual nor finite. It also has no latency to record
        #: until it ends.
        self.client = client
        #: The circuit breaker, used for the *request-shaped* reads that make
        #: up a snapshot. Those are the calls that saturate the simulator's
        #: connection pool, so they are the ones the breaker must protect.
        self.breaker = breaker
        self.state = state
        self.settings = settings
        self.status = BrokerStatus()
        self._snapshot_fetcher = snapshot_fetcher
        #: Injectable SSE reachability probe. Defaults to a real HTTP check
        #: against the simulator; tests substitute a fake.
        self._sse_probe = sse_probe or self._http_sse_probe
        #: The rolling observation window the anomaly detectors read.
        #: Optional so the broker stays usable without it.
        self.observations = observations
        #: The dead-letter queue. Payloads the broker cannot use are
        #: quarantined here instead of being dropped or raised. Optional so
        #: the broker stays usable without it -- but with it, a malformed SSE
        #: frame is survivable.
        self.dlq = dlq

        #: Bounds how many world reads may be in flight at once.
        #:
        #: The simulator's connection pool is 15 wide and every request it
        #: serves holds two of those connections, so a snapshot that fired all
        #: eight reads at once would demand 16 -- more than the pool has.
        #: Exceeding it is not a slow-down; it is a deadlock, because the
        #: blocked requests starve the event loop of the slots the pending
        #: cleanups need and the pool can never drain again.
        #:
        #: Created lazily: an ``asyncio.Semaphore`` binds to the running loop
        #: when it is constructed, and the broker may be built before the loop
        #: exists.
        self._fetch_concurrency = settings.fetch_concurrency
        self._fetch_semaphore: Optional[asyncio.Semaphore] = None
        # Publish the bound, so the saturation ratio has a denominator even
        # before the first read happens.
        SIMULATOR_CONCURRENCY_LIMIT.set(float(self._fetch_concurrency))

        self._recent: deque[BrokerEvent] = deque(maxlen=settings.event_buffer)
        self._subscribers: set[asyncio.Queue[BrokerEvent]] = set()
        self._subscribers_lock = asyncio.Lock()

        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._started_at = time.monotonic()
        self._baseline_established = False
        self._sse_task: Optional[asyncio.Task[None]] = None
        self._poll_task: Optional[asyncio.Task[None]] = None
        self._watchdog_task: Optional[asyncio.Task[None]] = None
        self._started = False

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        """Start the SSE consumer, the poller and the watchdog."""
        if self._started:
            return
        self._started = True
        self._started_at = time.monotonic()
        self._stop.clear()
        self._sse_task = asyncio.create_task(
            self._sse_loop(), name="broker-sse"
        )
        self._poll_task = asyncio.create_task(
            self._poll_loop(), name="broker-poll"
        )
        self._watchdog_task = asyncio.create_task(
            self._watchdog_loop(), name="broker-watchdog"
        )
        LOGGER.info("Event broker started (mode=%s)", self.status.mode)

    async def stop(self) -> None:
        """Stop every background task."""
        self._started = False
        self._stop.set()
        self._wake.set()
        for task in (self._sse_task, self._poll_task, self._watchdog_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._sse_task = None
        self._poll_task = None
        self._watchdog_task = None
        self.status.mode = "stopped"
        LOGGER.info("Event broker stopped")

    # ------------------------------------------------------------------ #
    # Snapshot refresh
    # ------------------------------------------------------------------ #
    async def refresh_snapshot(self) -> Optional[WorldSnapshot]:
        """Pull a fresh world snapshot over REST and publish it.

        A failed refresh returns ``None`` and leaves the cached world in
        place -- that is the degraded-mode contract for the *reads*.

        The one exception is :class:`CircuitOpen`, which is re-raised rather
        than swallowed. The breaker rejecting a call is not "the refresh
        failed, here is the old world" -- it is "we did not even try". A
        caller that gets ``None`` back believes it is looking at a fresh
        snapshot, and would then plan a write against a world that is
        minutes old. Swallowing it would turn the breaker's safety into a
        lie exactly where it matters most: the write path.
        """
        try:
            if self._snapshot_fetcher is not None:
                snapshot = await self._snapshot_fetcher()
            else:
                snapshot = await self._fetch_snapshot()
            await self.state.replace(snapshot)
            self.status.snapshot_refreshes += 1
            self.status.last_tick_seen = snapshot.tick
            SNAPSHOT_REFRESH.inc(result="ok")
            SNAPSHOT_AGE.set(self.state.current().age())
            await self._record_observations(snapshot)
            return snapshot
        except CircuitOpen:
            # Not a failed refresh -- a rejected one. Let it through.
            SNAPSHOT_REFRESH.inc(result="error")
            raise
        except SimulatorError as exc:
            LOGGER.warning("Snapshot refresh failed: %s", exc)
            self.status.last_error = str(exc)
            SNAPSHOT_REFRESH.inc(result="error")
            await self.state.mark_error(str(exc))
            return None
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.warning("Snapshot refresh failed: %s", exc)
            self.status.last_error = str(exc)
            SNAPSHOT_REFRESH.inc(result="error")
            await self.state.mark_error(str(exc))
            return None

    async def _record_observations(self, snapshot: WorldSnapshot) -> None:
        """Append this tick's observations to the rolling log.

        One hook call, no change to the broker's own logic. The log is what
        the anomaly detectors compare against -- the simulator's
        ``/v1/demand-history`` reports *demand* but not *inventory*, so the
        inventory anomalies can only be found locally.

        A snapshot that repeats the previous tick adds nothing, so it is
        skipped: the poller can refresh several times within one tick, and
        recording the same tick repeatedly would make the history look
        longer than it is.
        """
        if self.observations is None:
            return

        try:
            observations = build_observations(snapshot)
        except Exception as exc:  # pragma: no cover - defensive
            # A warning, not a debug line: a silent failure here would
            # disable every observation-driven anomaly detector while
            # leaving the rest of the backend looking healthy.
            LOGGER.warning("Could not build observations: %s", exc)
            return

        if not observations:
            return

        latest = await self.observations.latest_for(
            observations[0].station_id, observations[0].fuel_type
        )
        if latest is not None and latest.tick == snapshot.tick:
            return

        await self.observations.extend(observations)

    async def _fetch_snapshot(self) -> WorldSnapshot:
        """Fetch every world resource over REST (the source of truth).

        The reads are issued with bounded concurrency, not all at once. The
        simulator's database pool is small (SQLAlchemy ``QueuePool`` of 5 +
        10 overflow = 15 connections), and **every** request it handles holds
        *two* connections: one for its fault-injection middleware and one for
        the request's own ``Depends(get_db)`` session. Firing all eight reads
        concurrently therefore demands 16 connections from a 15-connection
        pool -- and the SSE subscription, which holds its connection for the
        life of the response, makes it 17.

        That is not a theoretical limit. Live-verified: the simulator wedged
        with ``QueuePool limit of size 5 overflow 10 reached``, holding
        exactly 15 handles on its database file, and every subsequent request
        blocked for 30 seconds before failing. The pool filled, the requests
        blocked, and because a blocked request starves the event loop of the
        scheduling slots the pending cleanups need, the pool could never
        drain again. It was a one-way ratchet.

        Bounding the concurrency keeps the demand under the pool's capacity,
        so the simulator stays responsive and the failure mode cannot start.

        The bound is on the *individual reads*, not on the snapshot as a
        whole. Gating the whole snapshot would still let its first batch of
        six run at once, which is more than the bound allows.
        """
        semaphore = self._fetch_semaphore
        if semaphore is None:
            semaphore = asyncio.Semaphore(self._fetch_concurrency)
            self._fetch_semaphore = semaphore

        # The reads go through the circuit breaker when there is one. These
        # are the calls that saturate the simulator's connection pool, so
        # they are exactly the ones that must fail fast when the upstream
        # is down -- instead of each one burning the client's full retry
        # budget (3 attempts, up to 4s of jitter each) before giving up.
        client = self.breaker if self.breaker is not None else self.client

        # -- pool saturation instrumentation -------------------------------- #
        # The semaphore is *ours*, so the saturation of the simulator's
        # connection pool is directly observable from our side: the number of
        # reads in flight divided by the bound we set.
        #
        # This is the signal that would have made the pool wedge visible in
        # seconds rather than hours. Before the incident the only evidence
        # was a line in the simulator's log, read much later.
        inflight = 0
        queue_depth = 0

        async def read(make_coro):
            nonlocal inflight, queue_depth
            queue_depth += 1
            SIMULATOR_QUEUE_DEPTH.set(queue_depth)
            async with semaphore:
                queue_depth -= 1
                SIMULATOR_QUEUE_DEPTH.set(queue_depth)
                inflight += 1
                SIMULATOR_INFLIGHT.set(inflight)
                SIMULATOR_SATURATION.set(inflight / float(self._fetch_concurrency))
                try:
                    return await make_coro()
                finally:
                    inflight -= 1
                    SIMULATOR_INFLIGHT.set(inflight)
                    SIMULATOR_SATURATION.set(
                        inflight / float(self._fetch_concurrency)
                    )

        # -- gather the reads ------------------------------------------------ #
        # The attribute lookups happen *before* the gather, so a client
        # that is missing one of these methods would raise here and strand
        # the coroutines already built -- an unawaited-coroutine warning
        # and a leaked coroutine object. Look the methods up first, then
        # build the coroutines, so a failing lookup cannot abandon the
        # ones that already succeeded.
        methods = (
            client.instance,
            client.depots,
            client.stations,
            client.routes,
            client.allocations,
            client.metrics,
            client.supply_arrivals,
            client.events,
        )
        reads = await asyncio.gather(
            *(read(method) for method in methods),
            return_exceptions=True,
        )
        if any(isinstance(r, BaseException) for r in reads):
            # Surface the first real failure, but let the others settle
            # first so no read is left dangling when we raise.
            for r in reads:
                if isinstance(r, BaseException):
                    raise r
        (
            instance,
            depots,
            stations,
            routes,
            allocations,
            metrics,
            supply_arrivals,
            events,
        ) = reads
        return WorldSnapshot(
            tick=instance.tick,
            sim_time=instance.sim_time,
            instance=instance,
            depots=list(depots),
            stations=list(stations),
            routes=list(routes),
            allocations=list(allocations),
            supply_arrivals=list(supply_arrivals),
            events=list(events),
            metrics=metrics,
        )

    # ------------------------------------------------------------------ #
    # SSE consumer
    # ------------------------------------------------------------------ #
    async def _sse_loop(self) -> None:
        """Consume the simulator SSE stream, forever.

        The client's ``stream()`` generator already handles 503s and transport
        errors with backoff. This loop adds the lifetime cap: after
        ``SSE_MAX_LIFETIME`` the subscription is dropped and reopened, bounding
        the blast radius of an undetected silent drop.

        The subscription is wrapped in ``contextlib.aclosing`` because
        abandoning an async generator does not close it. A bare ``break`` out
        of ``async for`` leaves the generator suspended at its ``yield``; its
        ``finally`` never runs, the ``async with httpx`` block inside it never
        exits, and the HTTP response stays open on the server.

        That leak is not cosmetic. The simulator's ``/v1/stream`` handler
        takes its database session as a FastAPI dependency, and FastAPI only
        finalises that dependency *after* the response stream completes. A
        subscription that is never closed therefore holds its database
        connection for the life of the process. The simulator's SQLAlchemy
        pool is 5 + 10 overflow = 15 connections, so after fifteen leaked
        subscriptions the pool is exhausted and every subsequent simulator
        request blocks for 30s and then fails with
        ``QueuePool limit of size 5 overflow 10 reached``.

        Live-verified: the simulator held exactly 15 open handles on its
        database file, and the wedge recurred every time the lifetime cap
        cycled. ``aclose()`` makes the cleanup deterministic.
        """
        while not self._stop.is_set():
            try:
                started = time.monotonic()
                subscription = self.client.stream(reconnect=False)
                try:
                    async for event in subscription:
                        if self._stop.is_set():
                            break
                        # Only now do we know the subscription actually works.
                        self.status.mode = "sse"
                        BROKER_MODE.set(0)
                        self.status.sse_connected_since = time.time()
                        await self._handle_sse_event(event.event or "message", event.data)

                        if time.monotonic() - started > self.settings.sse_max_lifetime:
                            LOGGER.info(
                                "SSE subscription hit its %ss lifetime cap; "
                                "reconnecting", self.settings.sse_max_lifetime)
                            self.status.forced_reconnects += 1
                            self.status.reconnects += 1
                            BROKER_RECONNECTS.inc()
                            break
                finally:
                    # Close the subscription deterministically. Walking away
                    # from an async generator does not close it: its
                    # ``finally`` -- where the HTTP response is closed -- is
                    # deferred to the event loop's async-generator finaliser,
                    # which only runs if the loop gets a scheduling slot.
                    # Under load it never does, and the simulator's database
                    # connection is pinned for the life of the process.
                    await self._close_subscription(subscription)
            except asyncio.CancelledError:
                raise
            except SimulatorError as exc:
                self.status.last_error = str(exc)
                self.status.mode = "polling"
                LOGGER.warning("SSE stream error: %s", exc)
                await asyncio.sleep(self.settings.poll_interval)
            except Exception as exc:  # pragma: no cover - defensive
                self.status.last_error = str(exc)
                self.status.mode = "polling"
                LOGGER.warning("SSE stream failed unexpectedly: %s", exc)
                await asyncio.sleep(self.settings.poll_interval)
            else:
                # The stream ended without an error (the generator was
                # exhausted). Nothing has been received, so we are not in SSE
                # mode. Reconnect after a short pause.
                if not self._stop.is_set():
                    self.status.mode = "polling"
                    await asyncio.sleep(self.settings.poll_interval)

    async def _close_subscription(self, subscription: Any) -> None:
        """Close an SSE subscription, swallowing the ways closing can fail.

        Closing is what releases the simulator's database connection, so it
        must be attempted even when the task is being cancelled. Three
        outcomes are all acceptable:

        * the generator closes cleanly;
        * it is already closed;
        * it refuses to close because the task was cancelled mid-flight --
          in which case the event loop's own finaliser will handle it.

        The one outcome that must not escape is a ``RuntimeError`` about a
        coroutine ignoring ``GeneratorExit``, which would mask a cancellation
        and leave the caller believing the subscription was tidied up.
        """
        aclose = getattr(subscription, "aclose", None)
        if aclose is None:
            return
        try:
            await aclose()
        except (asyncio.CancelledError, StopAsyncIteration):
            # Cancelled while closing: the loop's finaliser will finish it.
            raise
        except RuntimeError as exc:
            # "coroutine ignored GeneratorExit" -- the generator was already
            # being finalised elsewhere. Nothing left to do.
            if "GeneratorExit" not in str(exc):
                raise
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.debug("SSE subscription close failed: %s", exc)

    async def replay_dead_letter(self, entry: Any) -> None:
        """Replay a dead-lettered payload through its original handler.

        The payload is handed back to the frame handler. If the frame is
        still malformed the handler will dead-letter it again -- which is the
        correct outcome: the entry has not been handled, so it must stay in
        the queue.
        """
        source = getattr(entry, "source", "") or ""
        event_name = source.split(":", 1)[1] if ":" in source else "message"
        payload = getattr(entry, "payload", "") or ""
        await self._handle_sse_event(event_name, payload)

    def _dead_letter(
        self,
        payload: Any,
        *,
        reason: str,
        source: str,
        detail: str = "",
    ) -> None:
        """Quarantine a payload the broker could not use.

        Never raises. A dead-letter queue that can itself fail would turn a
        recoverable bad frame into an outage -- the exact opposite of what
        it is for.
        """
        if self.dlq is None:
            LOGGER.warning(
                "Discarding a %s payload from %s (no dead-letter queue): %s",
                reason, source, detail,
            )
            return
        try:
            entry = self.dlq.put(payload, reason=reason, source=source)
            LOGGER.warning(
                "Dead-lettered a %s payload from %s (id=%s): %s",
                reason, source, entry.id, detail,
            )
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.warning(
                "Could not dead-letter a %s payload from %s: %s",
                reason, source, exc,
            )

    async def _handle_sse_event(self, event_name: str, raw_data: str) -> None:
        """Normalise, record and fan out one SSE event.

        The event payload is **advisory**. The state update is the REST
        re-fetch that follows; the payload is never trusted as world state.

        A frame that cannot be parsed is **dead-lettered, not raised**. The
        subscription must survive a single bad frame: an event feed that
        dies on one malformed message is an event feed that stops
        delivering events, which is far worse than a feed that quarantines
        the bad message and keeps going. The operator can see the entry and
        replay it.
        """
        try:
            data = json.loads(raw_data)
        except (ValueError, TypeError) as exc:
            self._dead_letter(
                raw_data,
                reason="malformed_frame",
                source=f"sse:{event_name}",
                detail=str(exc),
            )
            return

        if not isinstance(data, dict):
            self._dead_letter(
                raw_data,
                reason="unparseable_payload",
                source=f"sse:{event_name}",
                detail="the frame's JSON was not an object",
            )
            return

        tick = data.get("tick")
        broker_event = BrokerEvent(
            event=event_name,
            data=data,
            tick=tick if isinstance(tick, int) else None,
        )

        self.status.events_received += 1
        SSE_EVENTS.inc()
        self.status.last_event_at = time.time()
        self.status.last_event_monotonic = time.monotonic()
        self.status.resync_required = False
        self._recent.append(broker_event)
        await self._fanout(broker_event)

        # SSE is only a hint that something changed -- refresh over REST.
        try:
            await self.refresh_snapshot()
        except CircuitOpen:
            # The breaker rejected the refresh. The event itself was still
            # delivered to the subscribers; only the REST reconciliation was
            # skipped.
            pass

    # ------------------------------------------------------------------ #
    # Fan-out
    # ------------------------------------------------------------------ #
    async def _fanout(self, event: BrokerEvent) -> None:
        async with self._subscribers_lock:
            targets = list(self._subscribers)
        for queue in targets:
            try:
                queue.put_nowait(event)
                self.status.events_fanned_out += 1
                SSE_FANOUT.inc()
            except asyncio.QueueFull:
                # Drop the oldest event for this slow subscriber and flag a
                # resync -- the subscriber will pull fresh state on reconnect.
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                    self.status.events_dropped += 1
                    SSE_DROPPED.inc()
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait(event)

    async def subscribe(self) -> AsyncIterator[BrokerEvent]:
        """Subscribe to the event fan-out.

        The subscriber gets its own bounded queue. A slow consumer loses the
        oldest events rather than blocking the broker.
        """
        queue: asyncio.Queue[BrokerEvent] = asyncio.Queue(
            maxsize=self.settings.subscriber_queue
        )
        async with self._subscribers_lock:
            self._subscribers.add(queue)
            self.status.subscribers = len(self._subscribers)
        try:
            while not self._stop.is_set():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=5.0)
                except asyncio.TimeoutError:
                    # A keepalive frame keeps the HTTP connection alive through
                    # proxies that would otherwise time it out.
                    yield BrokerEvent(event="keepalive", data={"ts": time.time()})
                    continue
                yield event
        finally:
            async with self._subscribers_lock:
                self._subscribers.discard(queue)
                self.status.subscribers = len(self._subscribers)

    def recent_events(self, limit: int = 200) -> list[BrokerEvent]:
        """The most recent retained events, oldest first."""
        events = list(self._recent)
        if limit <= 0:
            return []
        return events[-limit:]

    # ------------------------------------------------------------------ #
    # Poller
    # ------------------------------------------------------------------ #
    async def _poll_loop(self) -> None:
        """Reconcile world state over REST, always.

        This runs whether or not SSE is healthy. It is what makes a silent SSE
        drop a *freshness* problem rather than a correctness one.

        It is also the breaker's **probe**. When the breaker is OPEN the
        poller keeps calling ``refresh_snapshot()``; the breaker rejects
        those calls until the cooldown elapses, then lets exactly one
        through as a HALF_OPEN probe. A successful probe closes the breaker
        and the world resumes on its own -- no operator action, no manual
        reset. That is why the loop must survive ``CircuitOpen``: it is the
        expected condition during an outage, not an error.
        """
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.settings.poll_interval
                )
                return
            except asyncio.TimeoutError:
                pass

            try:
                await self.refresh_snapshot()
            except CircuitOpen:
                # The breaker is open. The cached world stands; the next
                # poll is the next probe.
                pass
            self.status.poll_count += 1
            self.status.last_poll_at = time.time()
            await self._maybe_reclaim_sse()

    async def _maybe_reclaim_sse(self) -> None:
        """Try to return to SSE mode after a suspected drop.

        The poller keeps the world fresh; this only decides whether we are
        *listening* again.
        """
        if self.status.mode != "polling":
            return
        if self._stop.is_set():
            return
        # The SSE loop owns reconnection; it flips the mode back on success.
        # Nudge it awake so a backoff sleep does not delay recovery.
        self._wake.set()
        self._wake.clear()

    # ------------------------------------------------------------------ #
    # Watchdog
    # ------------------------------------------------------------------ #
    async def _watchdog_loop(self) -> None:
        """Detect the silent SSE drop by comparing tick progress to events.

        This is the only reliable detector: frame silence is ambiguous (a
        paused simulator is also silent), but "the tick advanced and we heard
        nothing" is unambiguous.
        """
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.settings.watchdog_interval
                )
                return
            except asyncio.TimeoutError:
                pass

            await self.watchdog_once()
            await self._probe_sse_reachability()

    async def _http_sse_probe(self) -> bool:
        """Open a short-lived SSE connection and close it immediately.

        A ``200`` means the simulator is willing to stream; a ``503`` means it
        is refusing (typically an injected ``stream_disconnect`` fault).
        """
        url = f"{self.client.base_url}/v1/stream"
        try:
            async with httpx.AsyncClient(timeout=5.0) as probe:
                async with probe.stream("GET", url) as response:
                    return response.status_code < 400
        except Exception as exc:  # pragma: no cover - network failure
            LOGGER.debug("SSE reachability probe failed: %s", exc)
            return False

    async def _probe_sse_reachability(self) -> None:
        """Check whether the simulator would accept an SSE subscription.

        Why this exists
        ---------------
        The broker's ``mode`` used to flip to ``sse`` only when an *event*
        arrived. That conflates two different things:

        * can the simulator accept an SSE connection right now?
        * are events flowing right now?

        A **paused** simulator accepts the connection but emits nothing, so
        the old logic reported ``polling`` while SSE was perfectly healthy --
        the operator could not tell a healthy-but-quiet stream from a broken
        one. The recovery from a ``stream_disconnect`` fault looked like a
        permanent failure for exactly this reason.

        The probe is independent of event delivery, so it stays truthful
        while the simulator is paused.
        """
        self.status.last_probe_at = time.time()
        reachable = await self._sse_probe()

        if reachable:
            if not self.status.sse_reachable:
                LOGGER.info("SSE endpoint is reachable again")
            self.status.sse_reachable = True
            self.status.probe_failures = 0
            # The subscription may be healthy while no event has arrived yet.
            # Report the truth instead of waiting for the first event.
            if self.status.mode in ("polling", "starting") \
                    and self._sse_task is not None \
                    and not self._sse_task.done():
                self.status.mode = "sse"
                BROKER_MODE.set(1)
                if self.status.sse_connected_since is None:
                    self.status.sse_connected_since = time.time()
        else:
            self.status.probe_failures += 1
            BROKER_PROBE_FAILURES.inc()
            self.status.sse_reachable = False
            if self.status.mode != "polling":
                self.status.mode = "polling"
                BROKER_MODE.set(1)
                LOGGER.warning(
                    "SSE endpoint is refusing connections; degrading to polling")

    async def watchdog_once(self) -> None:
        """One watchdog pass. Extracted so it can be tested deterministically."""
        try:
            instance = await self.client.instance()
        except SimulatorError as exc:
            self.status.last_error = str(exc)
            return

        tick_now = instance.tick
        if not self._baseline_established:
            # The first successful sample only establishes the baseline. A
            # fresh broker has never seen a tick before, so it cannot yet say
            # whether the world moved.
            self._baseline_established = True
            self.status.last_tick_seen = tick_now
            return

        tick_moved = tick_now != self.status.last_tick_seen
        self.status.last_tick_seen = tick_now

        if not tick_moved:
            # The world is paused or slow. Silence is expected and is not
            # evidence of a drop.
            return

        last_event = self.status.last_event_monotonic
        if last_event is None:
            # We have *never* received an event. If the world is moving, the
            # subscription is already dead -- this is the silent drop in its
            # purest form. Treat the silence as having started when the
            # broker came up.
            silence = time.monotonic() - self._started_at
        else:
            silence = time.monotonic() - last_event

        if silence > self.settings.sse_stale_after:
            # The world moved but we heard nothing for too long -> the
            # subscription is orphaned. Only reconnecting can fix it.
            self.status.suspected_drops += 1
            BROKER_SUSPECTED_DROPS.inc()
            self.status.mode = "polling"
            BROKER_MODE.set(1)
            self.status.resync_required = True
            self.status.last_error = (
                f"SSE silent for {silence:.1f}s while the tick advanced to "
                f"{tick_now}; reconnecting"
            )
            LOGGER.warning(
                "Suspected silent SSE drop: no event for %.1fs while tick "
                "advanced to %s. Reconnecting.",
                silence, tick_now)
            await self._force_reconnect()

    async def _force_reconnect(self) -> None:
        """Cancel the orphaned SSE task and start a fresh subscription.

        The mode stays ``polling`` until an event actually arrives on the new
        subscription -- opening a socket is not proof that it works, and the
        whole point of the watchdog is that a *seemingly* open connection can
        be dead.
        """
        task = self._sse_task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.status.reconnects += 1
        BROKER_RECONNECTS.inc()
        self.status.mode = "polling"
        BROKER_MODE.set(1)
        self._sse_task = asyncio.create_task(self._sse_loop(), name="broker-sse")
        # Refresh immediately so the gap is as short as possible.
        try:
            await self.refresh_snapshot()
        except CircuitOpen:
            # The breaker is open -- the poller will probe instead.
            pass
