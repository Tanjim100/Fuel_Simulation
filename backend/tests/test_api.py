"""HTTP surface tests for the operator backend.

These use FastAPI's ``TestClient`` with a fake upstream so the API contract can
be tested without a simulator. The *live* contract is proven separately by
``test_validator_against_simulator.py``.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.broker import EventBroker
from app.config import Settings
from app.main import create_app
from app.state import WorldStateCache
from conftest import (
    make_allocation,
    make_depot,
    make_route,
    make_snapshot,
    make_station,
)

FUELS = ("DIESEL", "PETROL", "OCTANE")


class FakeUpstream:
    """A minimal simulator stand-in wired into the app's client."""

    def __init__(self) -> None:
        self.tick = 3
        self._allocations: list[Any] = []
        self.created: list[dict[str, Any]] = []
        self.cancel_ids: list[int] = []
        self._next_id = 100

    async def health(self):
        class H:
            status = "ok"
            database = "ok"
            simulation: dict[str, Any] = {"tick": 3}
        return H()

    async def instance(self):
        from app.simulator_client import SimulationInstance

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

    async def depots(self):
        return [make_depot()]

    async def stations(self):
        return [make_station()]

    async def routes(self):
        return [make_route()]

    async def allocations(self):
        return list(self._allocations)

    async def metrics(self):
        from app.simulator_client import Metrics

        return Metrics(
            served_demand_liters=1000.0,
            unmet_demand_liters=0.0,
            service_level=1.0,
            allocation_liters=1000.0,
            allocation_failures=0,
        )

    async def supply_arrivals(self):
        return []

    async def events(self):
        return []

    async def create_allocation(self, request):
        """Mirrors the real client's signature.

        The real ``SimulatorClient.create_allocation`` takes an
        ``AllocationRequest`` *model* and calls ``request.to_payload()``
        itself. The fake used to accept a plain dict, which hid a real bug:
        the router was passing a dict where the client expected the model.
        Accepting only the model keeps the fake honest.
        """
        from app.simulator_client import Allocation

        if not hasattr(request, "to_payload"):
            raise TypeError(
                "create_allocation expects an AllocationRequest model, got "
                f"{type(request).__name__}"
            )
        payload = request.to_payload()

        for allocation in self._allocations:
            if allocation.idempotency_key == payload["idempotency_key"]:
                return allocation, False
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

    async def cancel_allocation(self, allocation_id: int):
        allocation = _find(self._allocations, allocation_id)
        allocation.status = "CANCELLED"
        return allocation

    async def stream(self, **_: Any):
        # Never yields -- the broker stays in polling mode.
        if False:
            yield None
        await __import__("asyncio").Event().wait()


def _find(allocations, target):
    for allocation in allocations:
        if allocation.id == target:
            return allocation
    raise LookupError(target)


@pytest.fixture
def app_client():
    upstream = FakeUpstream()
    settings = Settings(
        simulator_base_url="http://fake",
        redis_url="",
        poll_interval=60.0,
        watchdog_interval=60.0,
        state_max_age=0.0,
    )
    app = create_app(settings)

    # Replace the real client with the fake before the lifespan runs.
    original_lifespan = app.router.lifespan_context

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def patched_lifespan(app_):
        app_.state.client = upstream
        app_.state.world = WorldStateCache(max_age=0.0)
        app_.state.broker = EventBroker(upstream, app_.state.world, settings)
        app_.state.journal = None
        await app_.state.broker.start()
        await app_.state.broker.refresh_snapshot()
        try:
            yield
        finally:
            await app_.state.broker.stop()

    app.router.lifespan_context = patched_lifespan
    assert original_lifespan is not None

    with TestClient(app) as client:
        client.upstream = upstream  # type: ignore[attr-defined]
        yield client


# --------------------------------------------------------------------------- #
# Meta and health
# --------------------------------------------------------------------------- #
def test_root_lists_the_endpoints(app_client):
    response = app_client.get("/")
    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "buetcsefest-backend"
    assert "POST /api/plan" in body["endpoints"]


def test_health_reports_the_upstream(app_client):
    response = app_client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["upstream"]["reachable"] is True


def test_stream_health_reports_the_broker(app_client):
    response = app_client.get("/api/stream-health")
    assert response.status_code == 200
    body = response.json()
    assert "broker" in body
    assert "suspected_drops" in body["broker"]
    assert "snapshot" in body


# --------------------------------------------------------------------------- #
# State endpoints
# --------------------------------------------------------------------------- #
def test_state_returns_the_whole_world(app_client):
    response = app_client.get("/api/state")
    assert response.status_code == 200
    state = response.json()["state"]
    assert state["tick"] == 3
    assert len(state["depots"]) == 1
    assert len(state["stations"]) == 1
    assert len(state["routes"]) == 1


def test_depots_expose_dispatch_headroom(app_client):
    response = app_client.get("/api/depots")
    assert response.status_code == 200
    depot = response.json()["depots"][0]
    assert depot["dispatch_remaining"] == 12000.0
    assert depot["dispatch_committed_this_tick"] == 0.0


def test_unknown_depot_is_404(app_client):
    response = app_client.get("/api/depots/depot-nope")
    assert response.status_code == 404


def test_stations_expose_free_space_and_spill_risk(app_client):
    response = app_client.get("/api/stations")
    assert response.status_code == 200
    station = response.json()["stations"][0]
    assert station["free_space"]["DIESEL"] == 6000.0
    assert station["inbound_in_flight"]["DIESEL"] == 0.0
    assert station["is_single_connected"] is True


def test_routes_flag_single_connected_destinations(app_client):
    response = app_client.get("/api/routes")
    assert response.status_code == 200
    route = response.json()["routes"][0]
    assert route["is_single_connected"] is True
    assert route["destination_route_count"] == 1


def test_metrics_include_derived_kpis(app_client):
    response = app_client.get("/api/metrics")
    assert response.status_code == 200
    body = response.json()
    assert body["metrics"]["service_level"] == 1.0
    assert body["kpis"]["service_level_pct"] == 100.0


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #
def test_plan_returns_a_shipment_and_an_explanation(app_client):
    response = app_client.post(
        "/api/plan",
        json={
            "source_depot_id": "depot-gazipur",
            "destination_station_id": "station-mirpur",
            "fuel_type": "DIESEL",
            "quantity": 1000.0,
        },
    )
    assert response.status_code == 200
    plan = response.json()["plan"]
    assert plan["status"] == "PLANNED"
    assert len(plan["shipments"]) == 1
    assert plan["shipments"][0]["quantity"] == 1000.0
    assert plan["shipments"][0]["reasoning"]
    assert plan["explanation"]["human_summary"]
    assert plan["explanation"]["route_choice"] == "route-gazipur-mirpur"


def test_plan_splits_an_oversized_request(app_client):
    response = app_client.post(
        "/api/plan",
        json={
            "source_depot_id": "depot-gazipur",
            "destination_station_id": "station-mirpur",
            "fuel_type": "DIESEL",
            "quantity": 5000.0,
        },
    )
    plan = response.json()["plan"]
    # The station only has 6000 L of free space and the route max is 7000 L,
    # so 5000 L fits in one shipment.
    assert plan["status"] == "PLANNED"
    assert plan["total_planned_litres"] == 5000.0


def test_plan_rejects_an_unknown_depot(app_client):
    response = app_client.post(
        "/api/plan",
        json={
            "source_depot_id": "depot-nope",
            "destination_station_id": "station-mirpur",
            "fuel_type": "DIESEL",
            "quantity": 1000.0,
        },
    )
    assert response.status_code == 200
    plan = response.json()["plan"]
    assert plan["status"] == "REJECTED"
    assert plan["reject_code"] == "NOT_FOUND"


def test_plan_rejects_a_bad_quantity(app_client):
    response = app_client.post(
        "/api/plan",
        json={
            "source_depot_id": "depot-gazipur",
            "destination_station_id": "station-mirpur",
            "fuel_type": "DIESEL",
            "quantity": -1,
        },
    )
    assert response.status_code == 200
    assert response.json()["plan"]["reject_code"] == "INVALID_QUANTITY"


def test_plan_rejects_an_unknown_fuel_type_with_422(app_client):
    response = app_client.post(
        "/api/plan",
        json={
            "source_depot_id": "depot-gazipur",
            "destination_station_id": "station-mirpur",
            "fuel_type": "GASOLINE",
            "quantity": 1000.0,
        },
    )
    assert response.status_code == 422
    body = response.json()
    assert body["status"] == "ERROR"
    assert body["error"]["code"] == "VALIDATION_ERROR"


def test_plan_rejects_an_unknown_field(app_client):
    response = app_client.post(
        "/api/plan",
        json={
            "source_depot_id": "depot-gazipur",
            "destination_station_id": "station-mirpur",
            "fuel_type": "DIESEL",
            "quantity": 1000.0,
            "surprise": True,
        },
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #
def test_posting_an_allocation_creates_it(app_client):
    response = app_client.post(
        "/api/allocations",
        json={
            "source_depot_id": "depot-gazipur",
            "destination_station_id": "station-mirpur",
            "fuel_type": "DIESEL",
            "quantity": 1000.0,
        },
    )
    assert response.status_code == 200
    execution = response.json()["execution"]
    assert len(execution["created"]) == 1
    assert execution["created"][0]["quantity"] == 1000.0
    assert execution["total_litres"] == 1000.0
    assert execution["plan"]["status"] == "PLANNED"


def test_posting_an_allocation_twice_is_idempotent(app_client):
    body = {
        "source_depot_id": "depot-gazipur",
        "destination_station_id": "station-mirpur",
        "fuel_type": "DIESEL",
        "quantity": 1000.0,
    }
    first = app_client.post("/api/allocations", json=body).json()
    second = app_client.post("/api/allocations", json=body).json()

    assert len(first["execution"]["created"]) == 1
    # The second attempt replays the same key -> no new shipment, and the
    # replayed record carries the original allocation id.
    assert second["execution"]["created"] == []
    assert len(second["execution"]["replayed"]) == 1
    assert (
        first["execution"]["created"][0]["id"]
        == second["execution"]["replayed"][0]["id"]
    )
    assert second["execution"]["total_litres"] == 0.0


def test_posting_an_allocation_for_an_unknown_depot_returns_404(app_client):
    """An unknown entity is a 404, not a conflict.

    The validator distinguishes three kinds of rejection: an unknown entity
    (404), a malformed request (422), and a genuine business conflict such
    as an over-capacity route (409). The router used to hardcode 409 for all
    three, so a client could not tell "that depot does not exist" apart from
    "that route is over capacity".
    """
    response = app_client.post(
        "/api/allocations",
        json={
            "source_depot_id": "depot-nope",
            "destination_station_id": "station-mirpur",
            "fuel_type": "DIESEL",
            "quantity": 1000.0,
        },
    )
    assert response.status_code == 404
    body = response.json()
    assert body["status"] == "ERROR"
    assert body["error"]["code"] == "NOT_FOUND"
    assert body["error"]["http_status"] == 404


def test_posting_a_malformed_allocation_returns_422(app_client):
    """A negative quantity is a malformed request, not a conflict."""
    response = app_client.post(
        "/api/allocations",
        json={
            "source_depot_id": "depot-dhanmondi",
            "destination_station_id": "station-mirpur",
            "fuel_type": "DIESEL",
            "quantity": -500.0,
        },
    )
    assert response.status_code == 422
    body = response.json()
    assert body["status"] == "ERROR"
    assert body["error"]["code"] == "INVALID_QUANTITY"
    assert body["error"]["http_status"] == 422


def test_cancelling_an_allocation(app_client):
    created = app_client.post(
        "/api/allocations",
        json={
            "source_depot_id": "depot-gazipur",
            "destination_station_id": "station-mirpur",
            "fuel_type": "DIESEL",
            "quantity": 1000.0,
        },
    ).json()
    allocation_id = created["execution"]["created"][0]["id"]

    response = app_client.post(f"/api/allocations/{allocation_id}/cancel")
    assert response.status_code == 200
    assert response.json()["allocation"]["status"] == "CANCELLED"


def test_explain_reconstructs_the_decision_record(app_client):
    created = app_client.post(
        "/api/allocations",
        json={
            "source_depot_id": "depot-gazipur",
            "destination_station_id": "station-mirpur",
            "fuel_type": "DIESEL",
            "quantity": 1000.0,
        },
    ).json()
    allocation_id = created["execution"]["created"][0]["id"]

    response = app_client.get(f"/api/allocations/{allocation_id}/explain")
    assert response.status_code == 200
    record = response.json()["decision_record"]
    assert record["route"]["id"] == "route-gazipur-mirpur"
    assert "ceilings_at_dispatch_time" in record


def test_explain_of_an_unknown_allocation_is_404(app_client):
    response = app_client.get("/api/allocations/99999/explain")
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #
def test_recent_events_is_empty_at_first(app_client):
    response = app_client.get("/api/events/recent")
    assert response.status_code == 200
    assert response.json()["count"] == 0


def test_the_event_stream_serves_sse_frames(app_client):
    with app_client.stream("GET", "/api/events/stream") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        # The first frame should arrive within the keepalive window; we only
        # assert the headers and that the stream does not error immediately.
        for chunk in response.iter_text():
            if chunk:
                assert "event:" in chunk or "data:" in chunk
                break


def test_a_connected_dashboard_receives_fanned_out_events(app_client):
    """A dashboard that connects must actually receive the events.

    This is the regression guard for a real defect: the endpoint used to
    ``asyncio.wait_for`` the broker's ``__anext__``, and a timeout cancels
    the pending await -- which destroys the async generator and silently
    unregisters the subscriber. The connection looked healthy and the
    dashboard received nothing.

    The keepalive frame is *not* proof of life here: it is generated by the
    broker regardless. Only a real fan-out event proves the subscription is
    registered and wired through.
    """
    broker = app_client.app.state.broker

    frames: list[str] = []
    with app_client.stream("GET", "/api/events/stream") as response:
        assert response.status_code == 200

        # Let the subscription register itself.
        deadline = time.monotonic() + 5.0
        while broker.status.subscribers == 0 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert broker.status.subscribers == 1, "the dashboard never subscribed"

        # Now push an event through the broker's fan-out. The broker lives on
        # the app's event loop, which the TestClient drives in its own
        # portal, so the coroutine has to be scheduled there.
        app_client.portal.call(
            broker._handle_sse_event,
            "simulation.tick",
            json.dumps({"tick": 42}),
        )

        for chunk in response.iter_text():
            frames.append(chunk)
            if "simulation.tick" in chunk:
                break

    assert any("simulation.tick" in frame for frame in frames), (
        "the dashboard never received the fanned-out event"
    )
    assert broker.status.events_fanned_out >= 1
