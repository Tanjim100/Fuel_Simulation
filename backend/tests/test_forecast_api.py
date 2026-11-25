"""HTTP surface tests for the forecast API.

The forecast is the optimiser's *input*, so an operator asking "will station X
run dry, and when?" should be able to get that answer without running an
optimisation. These tests check the contract of that endpoint surface.

The forecast's *correctness* -- that it reproduces the simulator's demand
exactly -- is proven separately by ``test_forecast.py`` against the golden
fixture, and live by ``GET /api/forecast/validate``.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest
from fastapi.testclient import TestClient

from app.broker import EventBroker
from app.config import Settings
from app.main import create_app
from app.state import WorldStateCache
from conftest import make_depot, make_route, make_snapshot, make_station

FUELS = ("DIESEL", "PETROL", "OCTANE")


class FakeUpstream:
    """A simulator stand-in with a controllable demand history."""

    def __init__(self, *, tick: int = 40, history: Optional[list[dict[str, Any]]] = None):
        self.tick = tick
        self.history = history or []

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
        return [make_depot()]

    async def stations(self):
        return [
            make_station(
                "station-mirpur",
                capacity={f: 20000.0 for f in FUELS},
                inventory={f: 5000.0 for f in FUELS},
            )
        ]

    async def routes(self):
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

    async def demand_history(self, *, limit: int = 200):
        return list(self.history[:limit])

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
        forecast_start_time="2026-01-01T00:00:00+00:00",
        journal_path=str(tmp_path / "journal.sqlite3"),
    )
    app = create_app(settings)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def patched_lifespan(app_):
        app_.state.client = upstream
        app_.state.world = WorldStateCache(max_age=0.0)
        app_.state.broker = EventBroker(upstream, app_.state.world, settings)
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


# --------------------------------------------------------------------------- #
# GET /api/forecast
# --------------------------------------------------------------------------- #
def test_forecast_returns_a_series_per_station_and_fuel(client):
    body = client.get("/api/forecast").json()
    assert body["status"] == "OK"
    assert body["tick"] == 40
    assert body["horizon"] == 16

    key = "station-mirpur:DIESEL"
    assert key in body["demand"]
    assert len(body["demand"][key]) == 16
    assert all(v >= 0.0 for v in body["demand"][key])


def test_forecast_reports_risk(client):
    body = client.get("/api/forecast").json()
    assert body["risk"], "a forecast with no risk rows is not a forecast"
    for risk in body["risk"]:
        assert risk["station_id"] == "station-mirpur"
        assert "ticks_to_stockout" in risk
        assert 0.0 <= risk["stockout_probability"] <= 1.0


def test_forecast_honours_an_explicit_horizon(client):
    body = client.get("/api/forecast?horizon=4").json()
    assert body["horizon"] == 4
    assert len(body["demand"]["station-mirpur:DIESEL"]) == 4


def test_forecast_is_deterministic(client):
    first = client.get("/api/forecast").json()
    second = client.get("/api/forecast").json()
    assert first["demand"] == second["demand"]


# --------------------------------------------------------------------------- #
# GET /api/forecast/summary
# --------------------------------------------------------------------------- #
def test_forecast_summary_reports_headline_numbers(client):
    body = client.get("/api/forecast/summary").json()
    assert body["status"] == "OK"
    assert body["horizon"] == 16
    assert body["total_demand_litres"] >= 0.0
    assert "at_risk_count" in body
    assert len(body["stations"]) >= 1


def test_forecast_summary_station_rows_carry_the_numbers(client):
    body = client.get("/api/forecast/summary").json()
    row = body["stations"][0]
    for field in (
        "station_id",
        "fuel_type",
        "current_inventory_litres",
        "total_demand_litres",
        "ticks_to_stockout",
        "stockout_probability",
        "expected_unmet_litres",
        "coverage_ticks",
        "confidence",
    ):
        assert field in row


# --------------------------------------------------------------------------- #
# GET /api/forecast/{station_id}
# --------------------------------------------------------------------------- #
def test_station_forecast_returns_that_station(client):
    body = client.get("/api/forecast/station-mirpur").json()
    assert body["status"] == "OK"
    assert body["station"]["id"] == "station-mirpur"
    assert "DIESEL" in body["demand"]
    assert len(body["demand"]["DIESEL"]) == 16
    assert body["routes"], "the serving route should be listed"


def test_station_forecast_reports_an_unknown_station(client):
    response = client.get("/api/forecast/station-nope")
    assert response.status_code == 404
    body = response.json()
    assert body["status"] == "ERROR"
    assert body["error"]["http_status"] == 404


# --------------------------------------------------------------------------- #
# GET /api/forecast/validate
# --------------------------------------------------------------------------- #
def test_validate_reports_unavailable_without_history(client):
    body = client.get("/api/forecast/validate").json()
    assert body["status"] == "OK"
    assert body["verdict"] == "UNAVAILABLE"
    assert body["compared"] == 0


def test_validate_is_exact_against_a_matching_history(tmp_path):
    """The forecast must reproduce the simulator's demand exactly.

    The history here is generated from the forecast's own formula, so an
    exact match is the expected outcome -- and any drift in the formula
    shows up as a mismatch.
    """
    from app.forecast import build_forecast

    upstream = FakeUpstream(tick=40)
    snapshot = make_snapshot(
        tick=40,
        stations=[
            make_station(
                "station-mirpur",
                capacity={f: 20000.0 for f in FUELS},
                inventory={f: 5000.0 for f in FUELS},
            )
        ],
    )
    forecast = build_forecast(snapshot, horizon=8)

    rows: list[dict[str, Any]] = []
    for offset in range(8):
        tick = 40 + offset
        for fuel in FUELS:
            rows.append(
                {
                    "tick": tick,
                    "station_id": "station-mirpur",
                    "fuel_type": fuel,
                    "demand_liters": forecast.demand_at("station-mirpur", fuel, tick),
                }
            )
    upstream.history = rows

    with build_client(tmp_path, upstream=upstream) as client:
        body = client.get("/api/forecast/validate").json()

    assert body["verdict"] == "EXACT"
    assert body["mismatched"] == 0
    assert body["compared"] == len(rows)
    assert body["largest_error_litres"] <= 1e-6


def test_validate_reports_a_mismatch_honestly(tmp_path):
    upstream = FakeUpstream(tick=40)
    upstream.history = [
        {
            "tick": 40,
            "station_id": "station-mirpur",
            "fuel_type": "DIESEL",
            "demand_liters": 999999.0,
        }
    ]

    with build_client(tmp_path, upstream=upstream) as client:
        body = client.get("/api/forecast/validate").json()

    assert body["verdict"] == "MISMATCH"
    assert body["mismatched"] == 1
    assert body["failures"]
    assert body["failures"][0]["fuel_type"] == "DIESEL"


def test_validate_anchors_at_the_histories_earliest_tick(tmp_path):
    """A history that predates the current tick must still validate.

    The simulator's demand history is behind the current tick. The
    comparison rebuilds the forecast anchored at the history's earliest
    tick, so the numbers line up.
    """
    from app.forecast import build_forecast

    upstream = FakeUpstream(tick=40)
    snapshot = make_snapshot(
        tick=40,
        stations=[
            make_station(
                "station-mirpur",
                capacity={f: 20000.0 for f in FUELS},
                inventory={f: 5000.0 for f in FUELS},
            )
        ],
    )
    anchored = build_forecast(snapshot, horizon=4, start_tick=10)

    rows = [
        {
            "tick": 10 + offset,
            "station_id": "station-mirpur",
            "fuel_type": fuel,
            "demand_liters": anchored.demand_at("station-mirpur", fuel, 10 + offset),
        }
        for offset in range(4)
        for fuel in FUELS
    ]
    upstream.history = rows

    with build_client(tmp_path, upstream=upstream) as client:
        body = client.get("/api/forecast/validate").json()

    assert body["verdict"] == "EXACT", body
    assert body["compared"] == len(rows)


def test_validate_survives_an_unreadable_history(tmp_path):
    class BrokenUpstream(FakeUpstream):
        async def demand_history(self, *, limit: int = 200):
            raise RuntimeError("the upstream is unreachable")

    with build_client(tmp_path, upstream=BrokenUpstream()) as client:
        body = client.get("/api/forecast/validate").json()

    assert body["status"] == "OK"
    assert body["verdict"] == "UNAVAILABLE"
    assert "unreachable" in body["reason"]


def test_validate_accepts_an_envelope_shaped_history(tmp_path):
    """The upstream's shape has varied; the endpoint must tolerate it."""
    from app.forecast import build_forecast

    upstream = FakeUpstream(tick=40)
    snapshot = make_snapshot(
        tick=40,
        stations=[
            make_station(
                "station-mirpur",
                capacity={f: 20000.0 for f in FUELS},
                inventory={f: 5000.0 for f in FUELS},
            )
        ],
    )
    forecast = build_forecast(snapshot, horizon=2)
    items = [
        {
            "tick": 40 + offset,
            "station_id": "station-mirpur",
            "fuel_type": fuel,
            "demand_liters": forecast.demand_at("station-mirpur", fuel, 40 + offset),
        }
        for offset in range(2)
        for fuel in FUELS
    ]

    class EnvelopeUpstream(FakeUpstream):
        async def demand_history(self, *, limit: int = 200):
            return {"items": items}

    upstream = EnvelopeUpstream(tick=40)
    with build_client(tmp_path, upstream=upstream) as client:
        body = client.get("/api/forecast/validate").json()

    assert body["verdict"] == "EXACT", body
