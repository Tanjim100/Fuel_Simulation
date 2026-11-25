"""The simulator pool-saturation invariant, as an executable check.

This file exists because of a real incident. §18 of ``docs/BACKEND.md``
records it: the simulator wedged holding exactly 15 database handles --
its whole SQLAlchemy ``QueuePool`` -- with every request blocked for 30
seconds. It was found by reading logs hours later. The missing early-warning
signal was **pool saturation**: in-flight reads against the backend's own
``FETCH_CONCURRENCY`` semaphore.

Two things are asserted here:

* the **semaphore bound is never exceeded** -- the demand on the simulator's
  pool never exceeds what the backend decided it can safely ask for. This is
  the invariant that prevents the wedge from starting;
* the **saturation gauge never exceeds 1.0** -- so a dashboard reading "1.0"
  means "at the limit", and a value above it would mean the bound has been
  broken.

An incident that was found by reading logs hours later becomes an incident
that is caught by a test.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.broker import EventBroker
from app.config import Settings
from app.metrics import (
    SIMULATOR_CONCURRENCY_LIMIT,
    SIMULATOR_INFLIGHT,
    SIMULATOR_QUEUE_DEPTH,
    SIMULATOR_SATURATION,
)
from app.state import WorldStateCache


class SlowUpstream:
    """A simulator whose reads take a moment, so concurrency can be observed.

    The sleep is what makes the bound testable: without it every read would
    finish before the next one started and the semaphore would never be
    contended.
    """

    def __init__(self, delay: float = 0.05) -> None:
        self.delay = delay
        self.concurrent = 0
        self.peak_concurrent = 0
        self.calls = 0

    async def _read(self):
        self.calls += 1
        self.concurrent += 1
        self.peak_concurrent = max(self.peak_concurrent, self.concurrent)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.concurrent -= 1

    async def instance(self):
        await self._read()
        from app.simulator_client import SimulationInstance

        return SimulationInstance(
            id=1, scenario_id="baseline", scenario_version="1.0.0",
            seed=12345, sim_time="2026-01-01T08:00:00", tick=3,
            tick_minutes=15, status="PAUSED",
        )

    async def depots(self):
        await self._read()
        from conftest import make_depot

        return [make_depot()]

    async def stations(self):
        await self._read()
        from conftest import make_station

        return [make_station()]

    async def routes(self):
        await self._read()
        from conftest import make_route

        return [make_route()]

    async def allocations(self):
        await self._read()
        return []

    async def metrics(self):
        await self._read()
        from app.simulator_client import Metrics

        return Metrics(
            served_demand_liters=1000.0, unmet_demand_liters=0.0,
            service_level=1.0, allocation_liters=1000.0,
            allocation_failures=0,
        )

    async def supply_arrivals(self):
        await self._read()
        return []

    async def events(self):
        await self._read()
        return []


def _broker(upstream: Any, concurrency: int = 4) -> EventBroker:
    settings = Settings(
        simulator_base_url="http://fake",
        redis_url="",
        fetch_concurrency=concurrency,
        poll_interval=60.0,
        watchdog_interval=60.0,
        state_max_age=0.0,
    )
    return EventBroker(upstream, WorldStateCache(max_age=0.0), settings)


async def test_the_concurrency_bound_is_never_exceeded() -> None:
    """The demand on the simulator's pool stays within the bound.

    Eight reads are issued per snapshot; the bound is four. The upstream
    records the peak it actually saw, and that peak must not exceed the
    bound.
    """
    upstream = SlowUpstream()
    broker = _broker(upstream, concurrency=4)

    await broker.refresh_snapshot()
    await broker.refresh_snapshot()
    await broker.refresh_snapshot()

    assert upstream.peak_concurrent <= 4, (
        f"the upstream saw {upstream.peak_concurrent} concurrent reads, "
        f"above the bound of 4"
    )
    assert upstream.calls == 24, "three snapshots of eight reads each"


async def test_concurrent_snapshots_do_not_break_the_bound() -> None:
    """Ten simultaneous snapshot requests still respect the bound.

    This is the case the incident actually needed: many callers asking for
    the world at once. Each snapshot issues eight reads, so without the
    semaphore the upstream would see eighty.
    """
    upstream = SlowUpstream(delay=0.02)
    broker = _broker(upstream, concurrency=4)

    await asyncio.gather(*(broker.refresh_snapshot() for _ in range(10)))

    assert upstream.peak_concurrent <= 4, (
        f"the upstream saw {upstream.peak_concurrent} concurrent reads, "
        f"above the bound of 4"
    )


async def test_the_saturation_gauge_never_exceeds_one() -> None:
    """A saturation of 1.0 means "at the limit"; above it means broken."""
    upstream = SlowUpstream()
    broker = _broker(upstream, concurrency=4)

    await broker.refresh_snapshot()

    saturation = SIMULATOR_SATURATION.value()
    assert 0.0 <= saturation <= 1.0, (
        f"the saturation gauge read {saturation}, above the bound of 1.0"
    )
    # The gauge returns to zero once the reads are done.
    assert SIMULATOR_INFLIGHT.value() == 0.0


async def test_the_saturation_gauge_returns_to_zero_afterwards() -> None:
    """A gauge stuck above zero would read as a permanent saturation."""
    upstream = SlowUpstream()
    broker = _broker(upstream, concurrency=4)

    await broker.refresh_snapshot()
    assert SIMULATOR_INFLIGHT.value() == 0.0
    assert SIMULATOR_QUEUE_DEPTH.value() == 0.0


async def test_the_concurrency_limit_gauge_matches_the_setting() -> None:
    """The published limit must be the limit that is actually enforced."""
    upstream = SlowUpstream()
    broker = _broker(upstream, concurrency=4)
    assert SIMULATOR_CONCURRENCY_LIMIT.value() == 4.0


async def test_a_higher_bound_is_respected() -> None:
    """The bound is a setting, not a hardcoded constant."""
    upstream = SlowUpstream()
    broker = _broker(upstream, concurrency=2)

    await broker.refresh_snapshot()

    assert upstream.peak_concurrent <= 2
    assert SIMULATOR_CONCURRENCY_LIMIT.value() == 2.0
