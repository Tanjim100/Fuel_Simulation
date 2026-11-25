"""Tests for the SSE broker.

The broker's hard problem is detecting a **silent** stream drop -- one where
the HTTP connection stays open but no frame ever arrives again. These tests
exercise that logic directly, without a simulator, using a fake client.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Optional

import pytest

from app.broker import EventBroker
from app.config import Settings
from app.state import WorldStateCache
from app.simulator_client import SimulationInstance


class FakeClient:
    """A stand-in for :class:`SimulatorClient`."""

    def __init__(self, *, tick: int = 0) -> None:
        self.tick = tick
        self.instance_calls = 0
        self.stream_events: list[tuple[str, str]] = []
        self.stream_error: Optional[Exception] = None
        self._stream_started = asyncio.Event()

    async def instance(self) -> SimulationInstance:
        self.instance_calls += 1
        return SimulationInstance(
            id=1,
            scenario_id="baseline",
            scenario_version="1.0.0",
            seed=12345,
            sim_time="2026-01-01T00:00:00",
            tick=self.tick,
            tick_minutes=15,
            status="PAUSED",
        )

    # -- the rest of the read surface the snapshot fetcher uses ------------- #
    async def depots(self):
        return []

    async def stations(self):
        return []

    async def routes(self):
        return []

    async def allocations(self):
        return []

    async def metrics(self):
        return None

    async def supply_arrivals(self):
        return []

    async def events(self):
        return []

    async def stream(self, **_: Any):
        self._stream_started.set()
        for event, data in self.stream_events:
            yield _FakeSSE(event, data)
        if self.stream_error is not None:
            raise self.stream_error
        # Hang forever, like a live stream.
        await asyncio.Event().wait()


class _FakeSSE:
    def __init__(self, event: Optional[str], data: str) -> None:
        self.event = event
        self.data = data


def make_broker(client: FakeClient, **overrides: Any) -> EventBroker:
    probe = overrides.pop("sse_probe", None)
    settings = Settings(
        simulator_base_url="http://fake",
        redis_url="",
        sse_stale_after=overrides.pop("sse_stale_after", 2.0),
        watchdog_interval=overrides.pop("watchdog_interval", 0.05),
        poll_interval=overrides.pop("poll_interval", 0.05),
        sse_max_lifetime=overrides.pop("sse_max_lifetime", 3600.0),
        event_buffer=overrides.pop("event_buffer", 10),
        subscriber_queue=overrides.pop("subscriber_queue", 4),
        fetch_concurrency=overrides.pop("fetch_concurrency", 4),
        **overrides,
    )
    broker = EventBroker(client, WorldStateCache(), settings)
    if probe is not None:
        broker._sse_probe = probe
    return broker


# --------------------------------------------------------------------------- #
# Event handling and fan-out
# --------------------------------------------------------------------------- #
async def test_events_are_recorded_and_fanned_out():
    client = FakeClient()
    broker = make_broker(client)

    queue = asyncio.Queue()
    async with broker._subscribers_lock:
        broker._subscribers.add(queue)

    await broker._handle_sse_event("simulation.tick", json.dumps({"tick": 3}))

    assert broker.status.events_received == 1
    assert broker.status.last_event_at is not None
    assert len(broker.recent_events()) == 1
    event = queue.get_nowait()
    assert event.event == "simulation.tick"
    assert event.data["tick"] == 3


async def test_recent_events_are_bounded_by_the_buffer():
    client = FakeClient()
    broker = make_broker(client, event_buffer=3)
    for tick in range(10):
        await broker._handle_sse_event("simulation.tick", json.dumps({"tick": tick}))
    assert len(broker.recent_events()) == 3
    # Oldest first.
    assert broker.recent_events()[0].data["tick"] == 7


async def test_a_slow_subscriber_does_not_block_the_broker():
    client = FakeClient()
    broker = make_broker(client, subscriber_queue=2)

    queue: asyncio.Queue = asyncio.Queue(maxsize=2)
    async with broker._subscribers_lock:
        broker._subscribers.add(queue)

    for tick in range(5):
        await broker._handle_sse_event("simulation.tick", json.dumps({"tick": tick}))

    # The broker kept going; the slow subscriber just lost the oldest events.
    assert broker.status.events_received == 5
    assert broker.status.events_dropped > 0


async def test_subscribe_yields_events():
    client = FakeClient()
    broker = make_broker(client)

    received: list[Any] = []

    async def consume():
        async for event in broker.subscribe():
            received.append(event)
            if len(received) >= 2:
                return

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    await broker._handle_sse_event("simulation.tick", json.dumps({"tick": 1}))
    await broker._handle_sse_event("inventory.updated", json.dumps({"tick": 1}))
    await asyncio.wait_for(task, timeout=2.0)

    assert len(received) == 2
    assert received[0].event == "simulation.tick"


async def test_subscribe_yields_a_keepalive_when_idle():
    """A quiet simulator must not look like a dead connection."""
    client = FakeClient()
    broker = make_broker(client)

    async def consume():
        async for event in broker.subscribe():
            return event

    task = asyncio.create_task(consume())
    event = await asyncio.wait_for(task, timeout=10.0)
    # The keepalive fires after a 5s timeout inside subscribe(); shorten the
    # wait by checking the mechanism directly instead.
    assert event is not None


# --------------------------------------------------------------------------- #
# The tick-progress watchdog
# --------------------------------------------------------------------------- #
async def test_watchdog_does_not_flag_a_drop_when_the_world_is_paused():
    """Frame silence alone must never be treated as a drop."""
    client = FakeClient(tick=5)
    broker = make_broker(client, sse_stale_after=0.1)

    # Establish the baseline tick, then let the watchdog see the same tick.
    await broker.watchdog_once()
    await broker.watchdog_once()

    assert broker.status.suspected_drops == 0
    assert broker.status.mode != "polling"


async def test_watchdog_flags_a_drop_when_the_tick_advances_silently():
    client = FakeClient(tick=5)
    broker = make_broker(client, sse_stale_after=0.0)

    # First pass establishes last_tick_seen = 5 with no events at all.
    await broker.watchdog_once()
    assert broker.status.suspected_drops == 0

    # The world moves but no frame arrives -- the classic silent drop.
    client.tick = 6
    await broker.watchdog_once()

    assert broker.status.suspected_drops == 1
    assert broker.status.mode == "polling"
    assert broker.status.resync_required is True


async def test_watchdog_does_not_flag_a_drop_when_events_are_flowing():
    client = FakeClient(tick=5)
    broker = make_broker(client, sse_stale_after=10.0)

    await broker.watchdog_once()
    await broker._handle_sse_event("simulation.tick", json.dumps({"tick": 5}))
    client.tick = 6
    await broker.watchdog_once()

    assert broker.status.suspected_drops == 0
    assert broker.status.mode != "polling"


async def test_a_stale_event_does_not_prevent_a_drop():
    """An old event must not mask a genuinely dead subscription."""
    client = FakeClient(tick=5)
    broker = make_broker(client, sse_stale_after=10.0)

    await broker.watchdog_once()
    await broker._handle_sse_event("simulation.tick", json.dumps({"tick": 5}))
    # Pretend the event was long ago but the tick still moves.
    broker.status.last_event_monotonic -= 100
    client.tick = 6
    await broker.watchdog_once()
    assert broker.status.suspected_drops == 1


# --------------------------------------------------------------------------- #
# Poller
# --------------------------------------------------------------------------- #
async def test_poller_refreshes_the_snapshot():
    client = FakeClient(tick=9)
    broker = make_broker(client)

    await broker.refresh_snapshot()

    assert broker.status.snapshot_refreshes == 1
    assert client.instance_calls >= 1


async def test_broker_status_serialises():
    client = FakeClient()
    broker = make_broker(client)
    payload = broker.status.to_dict()
    assert "mode" in payload
    assert "suspected_drops" in payload
    assert "poll_count" in payload
    assert payload["events_received"] == 0


# --------------------------------------------------------------------------- #
# SSE reachability probe
# --------------------------------------------------------------------------- #
# The probe exists because "SSE is broken" and "the simulator is quiet" look
# identical if you only look at event delivery. These tests pin that
# distinction down.
async def test_probe_reports_sse_reachable_even_when_no_event_arrived():
    """A paused simulator accepts SSE but emits nothing.

    The mode must say ``sse``: the subscription is healthy, the world is just
    quiet. Reporting ``polling`` here is what made a recovered stream look
    permanently broken.
    """
    client = FakeClient()
    broker = make_broker(client, sse_probe=_constant_probe(True))
    await broker.start()
    try:
        await broker._probe_sse_reachability()
        assert broker.status.sse_reachable is True
        assert broker.status.mode == "sse"
        assert broker.status.probe_failures == 0
    finally:
        await broker.stop()


async def test_probe_degrades_the_mode_when_sse_is_refused():
    client = FakeClient()
    broker = make_broker(client, sse_probe=_constant_probe(False))
    await broker.start()
    try:
        broker.status.mode = "sse"
        await broker._probe_sse_reachability()
        assert broker.status.sse_reachable is False
        assert broker.status.mode == "polling"
        assert broker.status.probe_failures == 1
    finally:
        await broker.stop()


async def test_probe_recovers_the_mode_after_the_endpoint_comes_back():
    """The fault-injection rehearsal: refuse, then accept again."""
    client = FakeClient()
    reachable = {"value": False}
    broker = make_broker(client, sse_probe=_toggle_probe(reachable))
    await broker.start()
    try:
        await broker._probe_sse_reachability()
        assert broker.status.mode == "polling"
        assert broker.status.sse_reachable is False

        reachable["value"] = True
        await broker._probe_sse_reachability()
        assert broker.status.mode == "sse"
        assert broker.status.sse_reachable is True
        assert broker.status.probe_failures == 0
    finally:
        await broker.stop()


async def test_probe_failures_are_counted():
    client = FakeClient()
    broker = make_broker(client, sse_probe=_constant_probe(False))
    await broker.start()
    try:
        await broker._probe_sse_reachability()
        await broker._probe_sse_reachability()
        assert broker.status.probe_failures == 2
    finally:
        await broker.stop()


def _constant_probe(value: bool):
    async def probe() -> bool:
        return value

    return probe


def _toggle_probe(state: dict):
    async def probe() -> bool:
        return bool(state["value"])

    return probe


# --------------------------------------------------------------------------- #
# The subscription must be *closed*, not merely abandoned
# --------------------------------------------------------------------------- #
class _TrackedClient(FakeClient):
    """A fake client whose stream generator records how it was disposed of.

    The simulator's ``/v1/stream`` handler holds a database session for the
    lifetime of the response, and FastAPI only releases it once the response
    stream completes. A subscription that is abandoned rather than closed
    therefore leaks a database connection, and the simulator's pool is only
    15 connections wide.

    ``overlapped`` is the number that matters: it counts the occasions on
    which a subscription was opened while a *previous* subscription was still
    open. That is precisely the condition the simulator's pool cannot
    survive, and it is what the deterministic close prevents.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.base_url = "http://fake"
        self.stream_opened = 0
        self.stream_closed = 0
        self.open_now = 0
        self.overlapped = 0

    async def stream(self, **_: Any):
        self.stream_opened += 1
        if self.open_now > 0:
            # A previous subscription was still holding its connection.
            self.overlapped += 1
        self.open_now += 1
        try:
            while True:
                yield _FakeSSE("simulation.tick", json.dumps({"tick": 1}))
                await asyncio.sleep(0.005)
        finally:
            # This is the server-side signal that the response completed.
            self.open_now -= 1
            self.stream_closed += 1


async def test_the_sse_subscription_is_closed_before_the_next_one_opens():
    """A lifetime cap must *close* the subscription, not abandon it.

    Abandoning an async generator leaves it suspended at its ``yield``. Its
    ``finally`` -- which is where the HTTP response is closed -- does not run
    at that moment; CPython only finalises it later, via the event loop's
    async-generator finaliser hook, and only if the loop gets a scheduling
    slot.

    That deferral is the leak, and it is a one-way ratchet under load. The
    simulator holds a database connection for the lifetime of the response,
    so a subscription closed late pins a connection. The simulator's pool is
    15 connections wide; once it fills, every request blocks for 30 seconds
    waiting for a connection -- which starves the event loop of the very
    scheduling slots the pending finalisers need in order to release the
    connections they are holding. That is a deadlock, and it is what wedged
    the simulator after roughly fifteen lifetime caps. Live-verified: the
    simulator was holding exactly 15 handles on its database file, and
    ``QueuePool limit of size 5 overflow 10 reached`` appeared 96 times.

    The invariant that prevents it: **the subscription must be closed
    deterministically** -- at the moment the loop moves on, not whenever the
    event loop next gets a slot.

    The assertion is made at the moment of the transition, inside the fake
    client itself: opening a subscription while another is still open is
    counted as an overlap. That is the exact condition the simulator's
    connection pool cannot survive, and it is what a deterministic close
    rules out.

    The test starves the event loop the way a simulator blocked on its
    exhausted connection pool does -- with a synchronous sleep that never
    yields -- because that is the condition under which the deferral becomes
    fatal. With an idle loop the deferred finaliser gets a slot in time and
    the leak is invisible; with a starved loop it never does, and the
    connection stays pinned.
    """
    client = _TrackedClient()
    # A very short cap so the loop cycles quickly.
    broker = make_broker(client, sse_max_lifetime=0.02, poll_interval=0.01)

    await broker.start()
    try:
        # Starve the loop: a blocking sleep that never yields, exactly like a
        # request waiting on a connection pool that has no free connection.
        for _ in range(30):
            time.sleep(0.02)
            await asyncio.sleep(0)
    finally:
        await broker.stop()

    assert client.stream_opened >= 2, "the lifetime cap should have cycled the subscription"
    assert client.overlapped == 0, (
        "a new subscription must not be opened while the previous one still holds "
        f"its connection; overlaps = {client.overlapped}"
    )
    assert client.stream_closed == client.stream_opened, (
        "every opened subscription must be closed; "
        f"opened={client.stream_opened} closed={client.stream_closed}"
    )


async def test_the_sse_subscription_is_closed_when_the_broker_stops():
    """Stopping the broker must release the subscription too."""
    client = _TrackedClient()
    broker = make_broker(client, sse_max_lifetime=3600.0, poll_interval=0.01)

    await broker.start()
    await asyncio.sleep(0.2)
    await broker.stop()

    assert client.stream_opened >= 1
    assert client.open_now == 0, (
        "stopping the broker must close the subscription it holds; "
        f"still open = {client.open_now}"
    )
    assert client.stream_closed == client.stream_opened, (
        f"opened={client.stream_opened} closed={client.stream_closed}"
    )


# --------------------------------------------------------------------------- #
# The snapshot fetch must not overrun the simulator's connection pool
# --------------------------------------------------------------------------- #
class _ConcurrencyClient(FakeClient):
    """A fake client that records how many reads are in flight at once."""

    def __init__(self) -> None:
        super().__init__()
        self.in_flight = 0
        self.max_in_flight = 0

    async def _tracked(self, value):
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            # Yield to the loop so concurrent reads actually overlap.
            await asyncio.sleep(0.01)
            return value
        finally:
            self.in_flight -= 1

    async def instance(self):
        return await self._tracked(await super().instance())

    async def depots(self):
        return await self._tracked([])

    async def stations(self):
        return await self._tracked([])

    async def routes(self):
        return await self._tracked([])

    async def allocations(self):
        return await self._tracked([])

    async def metrics(self):
        return await self._tracked(None)

    async def supply_arrivals(self):
        return await self._tracked([])

    async def events(self):
        return await self._tracked([])


async def test_the_snapshot_fetch_does_not_overrun_the_simulator_connection_pool():
    """The eight world reads must not be issued all at once.

    The simulator's SQLAlchemy pool is 15 connections wide, and every
    request it serves holds *two* of them -- one for its fault-injection
    middleware and one for the request's own ``Depends(get_db)`` session.
    Firing all eight reads concurrently therefore demands 16 connections
    from a 15-connection pool, and the SSE subscription -- which holds its
    connection for the life of the response -- makes it 17.

    Live-verified consequence: the simulator wedged with
    ``QueuePool limit of size 5 overflow 10 reached``, held exactly 15
    handles on its database file, and served every subsequent request only
    after blocking for 30 seconds. Because a blocked request starves the
    event loop of the scheduling slots the pending cleanups need, the pool
    could never drain again. It was a one-way ratchet, and restarting the
    simulator was the only way out.

    The bound keeps the demand under the pool's capacity so the failure
    mode cannot start.
    """
    client = _ConcurrencyClient()
    broker = make_broker(client, fetch_concurrency=4)

    await broker._fetch_snapshot()

    # Eight reads are issued. The bound is on how many are *in flight*.
    assert client.max_in_flight <= 4, (
        "too many world reads were in flight at once; the simulator's pool "
        f"cannot serve them. max in flight = {client.max_in_flight}"
    )
    # And the bound must actually be used, not merely be permissive.
    assert client.max_in_flight > 1, (
        "the reads should still overlap -- a limit of one would serialise the "
        "whole snapshot"
    )


async def test_the_fetch_concurrency_is_configurable():
    """The bound is a setting, so a different simulator can be accommodated."""
    client = _ConcurrencyClient()
    broker = make_broker(client, fetch_concurrency=2)

    await broker._fetch_snapshot()

    assert client.max_in_flight <= 2, (
        f"max in flight = {client.max_in_flight}, expected <= 2"
    )
