"""Degraded mode: what the backend serves when the simulator is gone.

The contract this file pins is the one PDF §12 asks for -- *backend dependency
unavailable -> retry / cached state / degraded mode* -- and the one the
console depends on:

* the **read** endpoints keep answering with the last-known-good world and
  say plainly that it is cached (``degraded: true``);
* the **write** endpoints keep failing, with the canonical error envelope and
  the code ``CIRCUIT_OPEN`` -- because a plan computed against a stale world
  is a wrong plan, and silently writing it would be worse than an error.

The failure mode these tests guard against is the expensive one: a backend
that *appears* healthy while serving a world from five minutes ago. The
``degraded`` flag is what turns that from invisible into obvious.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.broker import EventBroker
from app.circuit_breaker import BreakerState, CircuitBreaker
from app.config import Settings
from app.main import create_app
from app.simulator_client import SimulatorTimeout
from app.state import WorldStateCache
from conftest import make_snapshot

# A schema-valid plan body. It has to be valid, or FastAPI rejects it with
# a 422 before the request ever reaches the breaker -- and a test that
# asserts 503 while the body fails validation is asserting nothing.
VALID_PLAN_BODY = {
    "source_depot_id": "depot-gazipur",
    "destination_station_id": "station-mirpur",
    "fuel_type": "DIESEL",
    "quantity": 1000.0,
}


class FlakyUpstream:
    """A simulator that answers normally, then goes silent on demand.

    ``healthy`` is the switch. When it flips to false every call raises a
    ``SimulatorTimeout`` -- the retryable class, the one that trips the
    breaker.
    """

    def __init__(self) -> None:
        self.healthy = True
        self.calls = 0

    def _check(self) -> None:
        self.calls += 1
        if not self.healthy:
            raise SimulatorTimeout("the simulator is not answering")

    async def health(self):
        self._check()
        class H:
            status = "ok"
            database = "ok"
            simulation: dict[str, Any] = {"tick": 3}
        return H()

    async def instance(self):
        self._check()
        from app.simulator_client import SimulationInstance

        return SimulationInstance(
            id=1,
            scenario_id="baseline",
            scenario_version="1.0.0",
            seed=12345,
            sim_time="2026-01-01T08:00:00",
            tick=3,
            tick_minutes=15,
            status="PAUSED",
        )

    async def depots(self):
        self._check()
        from conftest import make_depot

        return [make_depot()]

    async def stations(self):
        self._check()
        from conftest import make_station

        return [make_station()]

    async def routes(self):
        self._check()
        from conftest import make_route

        return [make_route()]

    async def allocations(self):
        self._check()
        return []

    async def metrics(self):
        self._check()
        from app.simulator_client import Metrics

        return Metrics(
            served_demand_liters=1000.0,
            unmet_demand_liters=0.0,
            service_level=1.0,
            allocation_liters=1000.0,
            allocation_failures=0,
        )

    async def supply_arrivals(self):
        self._check()
        return []

    async def events(self):
        self._check()
        return []

    async def create_allocation(self, request):
        self._check()
        raise AssertionError("the write path must not reach the upstream")

    async def cancel_allocation(self, allocation_id: int):
        self._check()
        raise AssertionError("the write path must not reach the upstream")

    async def stream(self, **_: Any):
        if False:
            yield None
        await asyncio.Event().wait()


@pytest.fixture
def degraded_app():
    """An app whose upstream can be knocked over, with a tripped breaker."""
    upstream = FlakyUpstream()
    settings = Settings(
        simulator_base_url="http://fake",
        redis_url="",
        poll_interval=60.0,
        watchdog_interval=60.0,
        state_max_age=0.0,
        breaker_failure_threshold=2,
        breaker_cooldown_seconds=60.0,
    )
    app = create_app(settings)

    from contextlib import asynccontextmanager

    # Hoisted so the test can inspect the breaker's state directly.
    breaker = CircuitBreaker(
        upstream,
        failure_threshold=settings.breaker_failure_threshold,
        cooldown_seconds=settings.breaker_cooldown_seconds,
    )

    @asynccontextmanager
    async def patched_lifespan(app_):
        app_.state.client = breaker
        app_.state.simulator_client = upstream
        app_.state.breaker = breaker
        app_.state.world = WorldStateCache(max_age=0.0)
        app_.state.broker = EventBroker(
            upstream, app_.state.world, settings, breaker=breaker
        )
        app_.state.journal = None
        await app_.state.broker.start()
        await app_.state.broker.refresh_snapshot()
        try:
            yield
        finally:
            await app_.state.broker.stop()

    app.router.lifespan_context = patched_lifespan

    with TestClient(app) as client:
        client.upstream = upstream  # type: ignore[attr-defined]
        client.breaker = breaker  # type: ignore[attr-defined]
        yield client


# --------------------------------------------------------------------------- #
# The reads survive
# --------------------------------------------------------------------------- #
def test_reads_are_healthy_before_the_fault(degraded_app):
    response = degraded_app.get("/api/state")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "OK"
    assert "degraded" not in body


def test_state_serves_the_cached_snapshot_when_the_breaker_is_open(degraded_app):
    """The core promise: a stale world, honestly labelled.

    The snapshot must still be the *real* one -- the last known good world,
    not an empty placeholder. A degraded read that returns nothing is not a
    fallback, it is a different kind of outage.
    """
    before = degraded_app.get("/api/state").json()
    assert before["state"]["stations"], "the healthy snapshot has stations"

    degraded_app.upstream.healthy = False
    # Trip the breaker: threshold is 2, so two failing calls do it.
    for _ in range(3):
        degraded_app.get("/api/state")
    assert degraded_app.breaker.state is BreakerState.OPEN

    response = degraded_app.get("/api/state")
    assert response.status_code == 200, "a degraded read is still a read"
    body = response.json()
    assert body["degraded"] is True
    assert "circuit breaker is OPEN" in body["degraded_reason"]
    # The same world, not an empty one.
    assert body["state"]["stations"] == before["state"]["stations"]
    assert body["state"]["tick"] == before["state"]["tick"]


def test_every_read_endpoint_carries_the_degraded_flag(degraded_app):
    """Not just ``/api/state`` -- every read the console polls."""
    degraded_app.upstream.healthy = False
    for _ in range(3):
        degraded_app.get("/api/state")
    assert degraded_app.breaker.state is BreakerState.OPEN

    for path in (
        "/api/state",
        "/api/stations",
        "/api/depots",
        "/api/routes",
        "/api/allocations",
        "/api/metrics",
    ):
        response = degraded_app.get(path)
        assert response.status_code == 200, f"{path} should still answer"
        body = response.json()
        assert body.get("degraded") is True, f"{path} must flag the degradation"
        assert body.get("degraded_reason"), f"{path} must explain itself"


def test_a_degraded_read_does_not_hit_the_upstream(degraded_app):
    """Serving the cache must not re-spend the retry budget.

    This is the difference between a fallback and a stall: an open breaker
    that still called the upstream on every read would be paying the full
    ~13.75s retry budget per request -- the exact failure the breaker
    exists to prevent.
    """
    degraded_app.upstream.healthy = False
    for _ in range(3):
        degraded_app.get("/api/state")
    assert degraded_app.breaker.state is BreakerState.OPEN

    calls_before = degraded_app.upstream.calls
    for _ in range(5):
        degraded_app.get("/api/state")
    assert degraded_app.upstream.calls == calls_before, (
        "a degraded read must be served entirely from the cache"
    )


def test_health_reports_degraded_but_stays_200(degraded_app):
    """The container healthcheck must keep working through an outage.

    A health endpoint that 500s during an outage is useless: the container
    gets killed for being unhealthy at exactly the moment it should be
    serving the cached world.
    """
    degraded_app.upstream.healthy = False
    for _ in range(3):
        degraded_app.get("/api/state")

    response = degraded_app.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["circuit"]["state"] == "OPEN"


# --------------------------------------------------------------------------- #
# The writes do not survive
# --------------------------------------------------------------------------- #
def test_a_write_fails_with_the_circuit_open_code(degraded_app):
    """A plan against a stale world is a wrong plan.

    The write must fail with the canonical envelope and the code
    ``CIRCUIT_OPEN`` -- not a 500, and not a silent success computed against
    a world that no longer exists.
    """
    degraded_app.upstream.healthy = False
    for _ in range(3):
        degraded_app.get("/api/state")
    assert degraded_app.breaker.state is BreakerState.OPEN

    response = degraded_app.post("/api/plan", json=VALID_PLAN_BODY)
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "ERROR"
    assert body["error"]["code"] == "CIRCUIT_OPEN"
    assert body["error"]["http_status"] == 503


def test_a_rejected_write_reports_a_retry_after(degraded_app):
    """The client is told when to come back."""
    degraded_app.upstream.healthy = False
    for _ in range(3):
        degraded_app.get("/api/state")

    response = degraded_app.post("/api/plan", json=VALID_PLAN_BODY)
    assert response.status_code == 503
    assert response.headers.get("Retry-After") is not None


def test_the_breaker_rejects_a_write_without_touching_the_upstream(degraded_app):
    """The write path must be rejected by the gate, not by the upstream."""
    degraded_app.upstream.healthy = False
    for _ in range(3):
        degraded_app.get("/api/state")
    assert degraded_app.breaker.state is BreakerState.OPEN

    calls_before = degraded_app.upstream.calls
    response = degraded_app.post("/api/plan", json=VALID_PLAN_BODY)
    assert response.status_code == 503
    assert degraded_app.upstream.calls == calls_before


# --------------------------------------------------------------------------- #
# Recovery
# --------------------------------------------------------------------------- #
def test_the_breaker_recovers_on_its_own(degraded_app):
    """An outage must not need a human to clear it.

    The cooldown elapses, a probe is allowed through, the probe succeeds, and
    the breaker closes. Then the reads stop flagging themselves.
    """
    degraded_app.upstream.healthy = False
    for _ in range(3):
        degraded_app.get("/api/state")
    assert degraded_app.breaker.state is BreakerState.OPEN

    # The upstream comes back.
    degraded_app.upstream.healthy = True
    # Force the cooldown to have elapsed rather than sleeping 60s. The
    # timestamp is relative to now, because the breaker compares against
    # ``time.monotonic()`` -- an absolute epoch value would not work.
    import time as _time

    degraded_app.breaker._opened_at = (
        _time.monotonic() - degraded_app.breaker.cooldown_seconds - 1.0
    )

    response = degraded_app.get("/api/state")
    assert response.status_code == 200
    assert degraded_app.breaker.state is BreakerState.CLOSED
    assert "degraded" not in response.json()
