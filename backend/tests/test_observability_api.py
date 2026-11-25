"""The health-status table the brief's §16 asks for.

PDF §16 "Health and Status" names a specific table: the state of the Backend
API, the Database, the Fuel Simulator, the Prediction Service and the Decision
Engine, plus **p95 latency** and **error rate**. A judge reads that table.

These tests pin it:

* every named component is present with a state;
* the p95 and the error rate are there, and they are *numbers*;
* the circuit state is reported alongside them;
* the pre-existing keys are **preserved** -- ``/api/health`` was already
  consumed by the console and the container healthcheck, and extending it
  must never break either.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

#: The components PDF §16 names. The key names are ours; the set is not.
REQUIRED_COMPONENTS = {
    "backend_api",
    "redis",
    "fuel_simulator",
    "prediction_service",
    "decision_engine",
}


@pytest.fixture
def client():
    from app.broker import EventBroker
    from app.config import Settings
    from app.main import create_app
    from app.state import WorldStateCache

    class FakeUpstream:
        async def health(self):
            class H:
                status = "ok"
                database = "ok"
                simulation: dict[str, Any] = {"tick": 3}
            return H()

        async def instance(self):
            from app.simulator_client import SimulationInstance

            return SimulationInstance(
                id=1, scenario_id="baseline", scenario_version="1.0.0",
                seed=12345, sim_time="2026-01-01T08:00:00", tick=3,
                tick_minutes=15, status="PAUSED",
            )

        async def depots(self):
            from conftest import make_depot

            return [make_depot()]

        async def stations(self):
            from conftest import make_station

            return [make_station()]

        async def routes(self):
            from conftest import make_route

            return [make_route()]

        async def allocations(self):
            return []

        async def metrics(self):
            from app.simulator_client import Metrics

            return Metrics(
                served_demand_liters=1000.0, unmet_demand_liters=0.0,
                service_level=1.0, allocation_liters=1000.0,
                allocation_failures=0,
            )

        async def supply_arrivals(self):
            return []

        async def events(self):
            return []

        async def stream(self, **_: Any):
            if False:
                yield None
            import asyncio

            await asyncio.Event().wait()

    upstream = FakeUpstream()
    settings = Settings(
        simulator_base_url="http://fake", redis_url="", poll_interval=60.0,
        watchdog_interval=60.0, state_max_age=0.0,
    )
    app = create_app(settings)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def patched_lifespan(app_):
        from app.circuit_breaker import CircuitBreaker

        breaker = CircuitBreaker(
            upstream,
            failure_threshold=settings.breaker_failure_threshold,
            cooldown_seconds=settings.breaker_cooldown_seconds,
        )
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

    with TestClient(app) as test_client:
        yield test_client


def test_health_is_always_200(client) -> None:
    """The container healthcheck must never see a non-200.

    A health endpoint that fails during an outage gets the container killed
    for being unhealthy at exactly the moment it should be serving the
    cached world.
    """
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] in ("ok", "degraded")


def test_every_named_component_is_present(client) -> None:
    body = client.get("/api/health").json()
    components = body["components"]
    assert REQUIRED_COMPONENTS <= set(components)
    for name, component in components.items():
        assert "state" in component, f"{name} has no state"
        assert isinstance(component["state"], str)


def test_the_component_states_are_from_a_known_set(client) -> None:
    """An unknown state string would break a dashboard's colour mapping."""
    body = client.get("/api/health").json()
    allowed = {"HEALTHY", "DEGRADED", "UNHEALTHY", "UNKNOWN", "DISABLED"}
    for name, component in body["components"].items():
        assert component["state"] in allowed, f"{name}: {component['state']}"


def test_the_p95_and_the_error_rate_are_present(client) -> None:
    body = client.get("/api/health").json()
    assert isinstance(body["latency"]["p95_seconds"], (int, float))
    assert isinstance(body["latency"]["p50_seconds"], (int, float))
    assert isinstance(body["requests"]["error_rate"], (int, float))
    assert 0.0 <= body["requests"]["error_rate"] <= 1.0
    assert body["requests"]["total"] >= 0


def test_the_p95_reflects_the_traffic_that_happened(client) -> None:
    """The reported p95 must be the *system's* p95, not zero.

    The regression this guards against is subtle and easy to reintroduce:
    the duration histogram is observed with ``method`` and ``path``
    labels, so its samples live under labelled keys. Reading the
    percentile with no labels returns the unlabelled key -- which holds
    nothing -- and the health panel reports ``p95_seconds: 0.0`` while
    the system is serving requests. A dashboard that says "p95 latency:
    0ms" for a busy backend is worse than one that says nothing.
    """
    from app.metrics import HTTP_DURATION

    before = client.get("/api/health").json()["requests"]["total"]

    # Generate some traffic through the instrumented middleware.
    for _ in range(5):
        client.get("/api/health")

    after = client.get("/api/health").json()["requests"]["total"]
    assert after > before, "the request counter did not move"

    body = client.get("/api/health").json()
    assert body["latency"]["p95_seconds"] > 0.0, (
        "the p95 is zero despite traffic -- the aggregate percentile is "
        "not pooling the labelled samples"
    )
    assert HTTP_DURATION.count_across() > 0


def test_the_circuit_state_is_reported(client) -> None:
    body = client.get("/api/health").json()
    circuit = body["circuit"]
    assert circuit["state"] in ("CLOSED", "HALF_OPEN", "OPEN", "DISABLED")
    for key in ("consecutive_failures", "total_failures", "total_rejections"):
        assert key in circuit


def test_the_pre_existing_keys_are_preserved(client) -> None:
    """``/api/health`` was already a contract. Extending it must not break it.

    The console and the container healthcheck both read the existing keys.
    """
    body = client.get("/api/health").json()
    for key in (
        "status", "service", "version", "upstream", "broker", "journal",
    ):
        assert key in body, f"the pre-existing key {key!r} disappeared"
    assert body["service"] == "buetcsefest-backend"
    assert "reachable" in body["upstream"]
    assert "mode" in body["broker"]


def test_stream_health_reports_the_broker_diagnostics(client) -> None:
    body = client.get("/api/stream-health").json()
    assert body["status"] == "OK"
    assert "mode" in body["broker"]
    assert "snapshot" in body
    assert "tick" in body["snapshot"]
    assert "is_stale" in body["snapshot"]


def test_the_observability_endpoint_carries_the_same_table(client) -> None:
    """The console's System tab reads this, and it must agree with health."""
    observability = client.get("/api/observability").json()
    health = client.get("/api/health").json()
    assert set(observability["components"]) == set(health["components"])
    assert observability["components"] == health["components"]
    assert observability["circuit"]["state"] == health["circuit"]["state"]


def test_an_open_circuit_degrades_the_status_even_when_the_probe_answers(client) -> None:
    """The health probe and the data path are different claims.

    The simulator's ``/health`` is a liveness check on the *process*. A fault
    injected against the data endpoints leaves it answering while every read
    the backend cares about fails. Reporting "ok" then would be the worst
    possible answer: the backend is up and the world is unreachable.

    So an open circuit degrades the status and the simulator's component
    state, regardless of what the probe says.
    """
    from app.circuit_breaker import CircuitOpen

    breaker = client.app.state.breaker

    # The probe still answers (the fake upstream's health is fine)...
    assert client.app.state.simulator_client is not None

    # ...but the breaker is open, so the data path is broken.
    breaker._state = breaker._state.__class__.OPEN
    breaker._opened_at = 1.0  # a value that is certainly in the past

    body = client.get("/api/health").json()
    assert body["status"] == "degraded"
    assert body["circuit"]["state"] == "OPEN"
    assert body["components"]["fuel_simulator"]["state"] == "DEGRADED"
    assert body["components"]["decision_engine"]["state"] == "DEGRADED"

    observability = client.get("/api/observability").json()
    assert observability["components"]["fuel_simulator"]["state"] == "DEGRADED"
