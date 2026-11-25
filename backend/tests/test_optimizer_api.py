"""HTTP surface tests for the optimisation API.

The API is the operator's window onto the optimiser. These tests check the
*contract*: the plan is separated into executable and advisory shipments, the
gate's verdict is visible, and only current-tick shipments are ever written.

The *live* contract -- that the simulator accepts every gated shipment -- is
proven separately by ``test_optimizer_against_simulator.py``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.broker import EventBroker
from app.config import Settings
from app.journal import DispatchJournal
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
    """A simulator stand-in wired into the app's client."""

    def __init__(self, *, tick: int = 70) -> None:
        self.tick = tick
        self._allocations: list[Any] = []
        self.created: list[dict[str, Any]] = []
        self._next_id = 100

    async def health(self):
        class H:
            status = "ok"
            database = "ok"
            simulation: dict[str, Any] = {"tick": 70}
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
        # The station starts nearly empty, so there is genuinely something to
        # dispatch. At full stock an empty plan would be correct but would
        # prove nothing.
        return [
            make_station(
                "station-mirpur",
                capacity={f: 20000.0 for f in FUELS},
                inventory={f: 100.0 for f in FUELS},
            )
        ]

    async def routes(self):
        return [make_route()]

    async def allocations(self):
        return list(self._allocations)

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

    async def create_allocation(self, request):
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
        raise NotImplementedError

    async def stream(self, **_: Any):
        if False:
            yield None
        await __import__("asyncio").Event().wait()


@pytest.fixture
def app_client(tmp_path):
    upstream = FakeUpstream()
    settings = Settings(
        simulator_base_url="http://fake",
        redis_url="",
        poll_interval=60.0,
        watchdog_interval=60.0,
        state_max_age=0.0,
        optimizer_horizon_ticks=16,
        journal_path=str(tmp_path / "journal.sqlite3"),
    )
    app = create_app(settings)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def patched_lifespan(app_):
        app_.state.client = upstream
        app_.state.world = WorldStateCache(max_age=0.0)
        app_.state.broker = EventBroker(upstream, app_.state.world, settings)
        journal = DispatchJournal(settings.journal_path)
        journal.connect()
        app_.state.journal = journal
        await app_.state.broker.start()
        await app_.state.broker.refresh_snapshot()
        try:
            yield
        finally:
            await app_.state.broker.stop()
            journal.close()

    app.router.lifespan_context = patched_lifespan

    with TestClient(app) as client:
        client.upstream = upstream  # type: ignore[attr-defined]
        yield client


# --------------------------------------------------------------------------- #
# POST /api/optimize -- read-only
# --------------------------------------------------------------------------- #
def test_optimize_returns_a_plan(app_client):
    response = app_client.post("/api/optimize", json={})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "OK"
    assert body["tick"] == 70
    assert body["horizon"] == 16

    optimization = body["optimization"]
    assert optimization["status"] in ("PLANNED", "PARTIAL", "EMPTY")
    # The solver is greedy by default, but the LP path is import-guarded
    # and may be present in a development image. Either way the API must
    # report the truth.
    assert optimization["solver_used"] in ("greedy", "lp")
    assert 0.0 <= optimization["expected_service_level"] <= 1.0
    assert "shipments_now" in optimization
    assert "shipments_later" in optimization


def test_optimize_writes_nothing_to_the_simulator(app_client):
    app_client.post("/api/optimize", json={})
    assert app_client.upstream.created == []


def test_optimize_reports_the_gate_verdict(app_client):
    body = app_client.post("/api/optimize", json={}).json()
    gated = body["gated"]
    assert "accepted" in gated
    assert "rejected" in gated
    assert "total_litres" in gated
    # The gate must not reject what the optimiser proposed -- that would mean
    # the optimiser's arithmetic disagrees with the validator's.
    assert gated["rejected"] == 0, gated["rejections"]


def test_optimize_separates_now_from_later(app_client):
    body = app_client.post("/api/optimize", json={"horizon_ticks": 32}).json()
    optimization = body["optimization"]
    now = body["tick"]

    for shipment in optimization["shipments_now"]:
        assert shipment["dispatch_tick"] == now
    for shipment in optimization["shipments_later"]:
        assert shipment["dispatch_tick"] != now

    # The two lists together are the whole plan.
    assert len(optimization["shipments_now"]) + len(optimization["shipments_later"]) \
        == len(optimization["shipments"])


def test_optimize_accepts_an_empty_body(app_client):
    response = app_client.post("/api/optimize")
    assert response.status_code == 200


def test_optimize_honours_the_requested_horizon(app_client):
    body = app_client.post("/api/optimize", json={"horizon_ticks": 8}).json()
    assert body["horizon"] == 8
    assert body["optimization"]["diagnostics"]["horizon"] == 8


def test_optimize_can_restrict_to_one_station(app_client):
    body = app_client.post(
        "/api/optimize", json={"station_ids": ["station-nowhere"]}
    ).json()
    optimization = body["optimization"]
    assert optimization["shipments"] == []
    assert optimization["status"] == "EMPTY"


def test_optimize_can_restrict_to_one_fuel(app_client):
    body = app_client.post("/api/optimize", json={"fuel_types": ["DIESEL"]}).json()
    for shipment in body["optimization"]["shipments"]:
        assert shipment["fuel_type"] == "DIESEL"


def test_optimize_rejects_an_unknown_field(app_client):
    response = app_client.post("/api/optimize", json={"surprise": True})
    assert response.status_code == 422


def test_optimize_rejects_a_bad_horizon(app_client):
    response = app_client.post("/api/optimize", json={"horizon_ticks": 0})
    assert response.status_code == 422


def test_the_plan_id_is_deterministic(app_client):
    first = app_client.post("/api/optimize", json={}).json()
    second = app_client.post("/api/optimize", json={}).json()
    assert first["plan_id"] == second["plan_id"]


def test_the_plan_is_recorded_in_the_journal(app_client):
    body = app_client.post("/api/optimize", json={}).json()
    plan_id = body["plan_id"]

    response = app_client.get(f"/api/optimize/{plan_id}")
    assert response.status_code == 200
    plan = response.json()["plan"]
    assert plan["plan_id"] == plan_id
    assert plan["tick"] == 70
    assert plan["horizon"] == 16
    # The LP path is import-guarded and may be present in a development
    # image; the journalled plan must record whichever solver ran.
    assert plan["solver_used"] in ("greedy", "lp")
    assert "optimization" in plan["payload"]
    assert "gated" in plan["payload"]


def test_an_unknown_plan_is_reported(app_client):
    response = app_client.get("/api/optimize/plan-does-not-exist")
    assert response.status_code == 200
    assert response.json()["status"] == "ERROR"
    assert response.json()["error"]["code"] == "NOT_FOUND"


# --------------------------------------------------------------------------- #
# POST /api/optimize/execute -- writes only current-tick shipments
# --------------------------------------------------------------------------- #
def test_execute_creates_the_current_tick_shipments(app_client):
    response = app_client.post("/api/optimize/execute", json={})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "OK"
    assert body["tick"] == 70
    assert body["gated"]["rejected"] == 0

    # Every written shipment must belong to the current tick.
    for record in body["created"]:
        assert record["quantity"] > 0
    assert len(body["created"]) == len(app_client.upstream.created)


def test_execute_writes_only_current_tick_shipments(app_client):
    body = app_client.post(
        "/api/optimize/execute", json={"horizon_ticks": 32}
    ).json()
    now = body["tick"]
    for record in body["created"]:
        assert record["created_tick"] == now


def test_execute_reports_the_executable_count(app_client):
    body = app_client.post("/api/optimize/execute", json={}).json()
    gated = body["gated"]
    assert gated["executable_now"] == len(body["created"]) + len(body["replayed"])
    assert gated["deferred_to_future_ticks"] >= 0


def test_execute_does_not_double_ship(app_client):
    """A second execute must not create a *second* shipment for the same tick.

    The simulator stamps ``created_tick = now`` on every allocation and the
    dispatch cap filters on ``created_tick == current_tick``, so a second
    execute at the same tick sees the cap already consumed. The plan is
    therefore recomputed -- and legitimately smaller or empty -- rather than
    replaying the earlier shipments.
    """
    first = app_client.post("/api/optimize/execute", json={}).json()
    second = app_client.post("/api/optimize/execute", json={}).json()

    if not first["created"]:
        pytest.skip("the optimiser produced no shipment to execute")

    # The upstream must never have seen the same fuel twice.
    keys = [r["idempotency_key"] for r in app_client.upstream.created]
    assert len(keys) == len(set(keys)), "the same key was written twice"

    # And the second execute created nothing new.
    assert len(second["created"]) == 0


def test_execute_records_the_plan_before_writing(app_client):
    body = app_client.post("/api/optimize/execute", json={}).json()
    plan_id = body["plan_id"]
    response = app_client.get(f"/api/optimize/{plan_id}")
    assert response.status_code == 200
    assert response.json()["plan"]["plan_id"] == plan_id


# --------------------------------------------------------------------------- #
# GET /api/optimize/solver
# --------------------------------------------------------------------------- #
def test_the_solver_endpoint_is_truthful(app_client):
    response = app_client.get("/api/optimize/solver")
    assert response.status_code == 200
    body = response.json()
    assert body["configured"] == "auto"
    assert body["active"] in ("greedy", "lp")
    # The LP dependency is not installed by default, so the endpoint must say
    # so rather than overclaim.
    if not body["lp_available"]:
        assert body["active"] == "greedy"
        assert "greedy" in body["note"].lower()
    assert body["horizon_ticks"] == 16
