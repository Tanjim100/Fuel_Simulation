"""HTTP surface tests for the anomaly and incident API.

The detectors themselves are unit-tested in ``test_anomaly.py``. These tests
check the *contract* of the HTTP surface: the filters work, the ids resolve,
the incidents group, and the payload shape is what a dashboard expects.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest
from fastapi.testclient import TestClient

from app.anomaly import AnomalyConfig, AnomalyDetector
from app.broker import EventBroker
from app.config import Settings
from app.main import create_app
from app.state import ObservationLog, WorldStateCache
from conftest import make_depot, make_route, make_station

FUELS = ("DIESEL", "PETROL", "OCTANE")


class FakeUpstream:
    """A simulator stand-in whose world the test can shape."""

    def __init__(
        self,
        *,
        tick: int = 40,
        stations: Optional[list[Any]] = None,
        depots: Optional[list[Any]] = None,
        routes: Optional[list[Any]] = None,
    ) -> None:
        self.tick = tick
        self._stations = stations
        self._depots = depots
        self._routes = routes

    async def health(self):
        class H:
            status = "ok"
            database = "ok"
            simulation: dict[str, Any] = {"tick": self.tick}
        return H()

    async def instance(self):
        from app.simulator_client import SimulationInstance

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

    async def depots(self):
        if self._depots is not None:
            return self._depots
        return [make_depot()]

    async def stations(self):
        if self._stations is not None:
            return self._stations
        return [
            make_station(
                "station-mirpur",
                capacity={f: 20000.0 for f in FUELS},
                inventory={f: 5000.0 for f in FUELS},
            )
        ]

    async def routes(self):
        if self._routes is not None:
            return self._routes
        return [make_route()]

    async def allocations(self):
        return []

    async def metrics(self):
        from app.simulator_client import Metrics

        return Metrics(
            served_demand_liters=0.0,
            unmet_demand_liters=0.0,
            service_level=1.0,
            allocation_liters=0.0,
            allocation_failures=0,
        )

    async def supply_arrivals(self):
        return []

    async def events(self):
        return []

    async def create_allocation(self, request):  # pragma: no cover - unused
        raise NotImplementedError

    async def cancel_allocation(self, allocation_id: int):  # pragma: no cover
        raise NotImplementedError

    async def stream(self, **_: Any):
        if False:
            yield None
        await __import__("asyncio").Event().wait()


def build_client(tmp_path, *, upstream: FakeUpstream):
    settings = Settings(
        simulator_base_url="http://fake",
        redis_url="",
        poll_interval=60.0,
        watchdog_interval=60.0,
        state_max_age=0.0,
        forecast_horizon_ticks=16,
        anomaly_incident_window_ticks=8,
        journal_path=str(tmp_path / "journal.sqlite3"),
    )
    app = create_app(settings)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def patched_lifespan(app_):
        app_.state.client = upstream
        app_.state.world = WorldStateCache(max_age=0.0)
        # The real lifespan builds these two singletons and hands the log to
        # the broker. The observation-driven detectors read the log, so a
        # test that skips this wiring would silently see no anomalies.
        observations = ObservationLog(
            max_entries=settings.anomaly_observation_log_max
        )
        app_.state.observations = observations
        app_.state.detector = AnomalyDetector(
            AnomalyConfig(
                spike_z_threshold=settings.anomaly_spike_z_threshold,
                spike_z_severe=settings.anomaly_spike_severe_z,
                inventory_drop_abs=settings.anomaly_inventory_drop_abs,
                inventory_drop_ratio=settings.anomaly_inventory_drop_ratio,
                bottleneck_litres=settings.anomaly_bottleneck_threshold,
                incident_window_ticks=settings.anomaly_incident_window_ticks,
            )
        )
        app_.state.broker = EventBroker(
            upstream, app_.state.world, settings, observations=observations
        )
        await app_.state.broker.start()
        await app_.state.broker.refresh_snapshot()
        try:
            yield
        finally:
            await app_.state.broker.stop()

    app.router.lifespan_context = patched_lifespan
    return TestClient(app)


@pytest.fixture
def client(tmp_path):
    with build_client(tmp_path, upstream=FakeUpstream()) as test_client:
        yield test_client


@pytest.fixture
def spike_client(tmp_path):
    """A world with an observable demand spike."""
    upstream = FakeUpstream(
        tick=40,
        stations=[
            make_station(
                "station-mirpur",
                capacity={f: 20000.0 for f in FUELS},
                inventory={f: 5000.0 for f in FUELS},
                demand_multiplier=1.8,
            )
        ],
    )
    with build_client(tmp_path, upstream=upstream) as test_client:
        yield test_client


# --------------------------------------------------------------------------- #
# GET /api/anomalies
# --------------------------------------------------------------------------- #
def test_anomalies_listing_has_a_stable_envelope(client):
    body = client.get("/api/anomalies").json()
    assert body["status"] == "OK"
    assert body["tick"] == 40
    assert isinstance(body["anomalies"], list)
    assert body["count"] == len(body["anomalies"])


def test_a_demand_spike_is_reported(spike_client):
    body = spike_client.get("/api/anomalies").json()
    kinds = {a["kind"] for a in body["anomalies"]}
    assert "DEMAND_SPIKE" in kinds, body["anomalies"]


def test_anomalies_can_be_filtered_by_kind(spike_client):
    body = spike_client.get("/api/anomalies?kind=DEMAND_SPIKE").json()
    assert body["anomalies"]
    for anomaly in body["anomalies"]:
        assert anomaly["kind"] == "DEMAND_SPIKE"


def test_anomalies_can_be_filtered_by_severity(spike_client):
    body = spike_client.get("/api/anomalies?severity=HIGH").json()
    for anomaly in body["anomalies"]:
        assert anomaly["severity"] == "HIGH"


def test_anomalies_can_be_filtered_by_tick(spike_client):
    body = spike_client.get("/api/anomalies?since_tick=40").json()
    for anomaly in body["anomalies"]:
        assert anomaly["tick"] >= 40


def test_anomaly_payload_carries_its_evidence(spike_client):
    body = spike_client.get("/api/anomalies").json()
    anomaly = next(a for a in body["anomalies"] if a["kind"] == "DEMAND_SPIKE")
    for field in (
        "id",
        "kind",
        "severity",
        "entity_type",
        "entity_id",
        "tick",
        "title",
        "detail",
        "metrics",
        "baseline",
        "recommended_action",
    ):
        assert field in anomaly
    assert anomaly["recommended_action"], "an anomaly with no advice is not actionable"


def test_one_anomaly_can_be_fetched_by_id(spike_client):
    listing = spike_client.get("/api/anomalies").json()
    anomaly_id = listing["anomalies"][0]["id"]
    body = spike_client.get(f"/api/anomalies/{anomaly_id}").json()
    assert body["status"] == "OK"
    assert body["anomaly"]["id"] == anomaly_id


def test_an_unknown_anomaly_is_reported_as_not_found(client):
    body = client.get("/api/anomalies/does-not-exist").json()
    assert body["status"] == "ERROR"
    assert body["error"]["code"] == "NOT_FOUND"


# --------------------------------------------------------------------------- #
# GET /api/anomalies/summary
# --------------------------------------------------------------------------- #
def test_anomaly_summary_counts_by_kind_and_severity(spike_client):
    body = spike_client.get("/api/anomalies/summary").json()
    assert body["status"] == "OK"
    assert body["total"] == sum(body["by_kind"].values())
    assert body["total"] == sum(body["by_severity"].values())
    assert "CRITICAL" in body["by_severity"]
    assert body["severity_order"][0] == "CRITICAL"


# --------------------------------------------------------------------------- #
# GET /api/incidents
# --------------------------------------------------------------------------- #
def test_incidents_are_listed(spike_client):
    body = spike_client.get("/api/incidents").json()
    assert body["status"] == "OK"
    assert body["count"] == len(body["incidents"])
    for incident in body["incidents"]:
        assert incident["anomaly_ids"]
        assert incident["severity"] in ("CRITICAL", "HIGH", "MEDIUM", "LOW")


def test_one_incident_can_be_fetched_by_id(spike_client):
    listing = spike_client.get("/api/incidents").json()
    if not listing["incidents"]:
        pytest.skip("no incidents in this world")
    incident_id = listing["incidents"][0]["id"]
    body = spike_client.get(f"/api/incidents/{incident_id}").json()
    assert body["status"] == "OK"
    assert body["incident"]["id"] == incident_id
    assert body["anomalies"]


def test_an_unknown_incident_is_reported_as_not_found(client):
    body = client.get("/api/incidents/does-not-exist").json()
    assert body["status"] == "ERROR"
    assert body["error"]["code"] == "NOT_FOUND"


# --------------------------------------------------------------------------- #
# A single-connected station's disrupted route is CRITICAL
# --------------------------------------------------------------------------- #
def test_a_disrupted_single_connected_route_is_critical(tmp_path):
    upstream = FakeUpstream(
        tick=40,
        stations=[
            make_station(
                "station-mirpur",
                capacity={f: 20000.0 for f in FUELS},
                inventory={f: 5000.0 for f in FUELS},
            )
        ],
        routes=[make_route(status="ROUTE_DISRUPTED")],
    )
    with build_client(tmp_path, upstream=upstream) as client:
        body = client.get("/api/anomalies").json()

    bottlenecks = [a for a in body["anomalies"] if a["kind"] == "ROUTE_BOTTLENECK"]
    assert bottlenecks
    assert bottlenecks[0]["severity"] == "CRITICAL"
    assert bottlenecks[0]["entity_id"] == "route-gazipur-mirpur"
