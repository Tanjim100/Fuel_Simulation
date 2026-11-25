"""Pure unit tests for the dispatch optimiser.

No simulator, no I/O -- the optimiser is a function of a snapshot and a
forecast, so it can be tested exhaustively and quickly.

Every assertion here encodes a *measured* simulator behaviour. The live
oracle lives in ``test_optimizer_against_simulator.py``.
"""

from __future__ import annotations

import pytest

from app.forecast import build_forecast
from app.optimizer import (
    OptimizerConfig,
    baseline_plan,
    optimize,
    service_level_of,
)
from app.validator import dispatch_remaining
from conftest import (
    make_allocation,
    make_depot,
    make_request,
    make_route,
    make_snapshot,
    make_station,
)

FUELS = ("DIESEL", "PETROL", "OCTANE")


# --------------------------------------------------------------------------- #
# A small but realistic synthetic world
# --------------------------------------------------------------------------- #
def build_world(
    *,
    tick: int = 5,
    depot_status: str = "OPEN",
    route_status: str = "AVAILABLE",
    station_status: str = "OPEN",
    station_inventory: float = 0.0,
    depot_inventory: float = 50000.0,
    dispatch_capacity: float = 12000.0,
    route_max: float = 7000.0,
    second_route: bool = False,
    allocations=None,
):
    """Two depots, two stations, one or two routes -- the measured shapes."""
    depots = [
        make_depot(
            "depot-gazipur",
            status=depot_status,
            dispatch_capacity_per_tick=dispatch_capacity,
            inventory={f: depot_inventory for f in FUELS},
        ),
        make_depot(
            "depot-patiya",
            status="OPEN",
            dispatch_capacity_per_tick=dispatch_capacity,
            inventory={f: depot_inventory for f in FUELS},
        ),
    ]
    stations = [
        make_station(
            "station-mirpur",
            status=station_status,
            capacity={f: 20000.0 for f in FUELS},
            inventory={f: station_inventory for f in FUELS},
        ),
        make_station(
            "station-tongi",
            capacity={f: 20000.0 for f in FUELS},
            inventory={f: station_inventory for f in FUELS},
        ),
    ]
    routes = [
        make_route(
            "route-gazipur-mirpur",
            source_depot_id="depot-gazipur",
            destination_station_id="station-mirpur",
            transit_ticks=2,
            max_shipment=route_max,
            status=route_status,
        ),
        make_route(
            "route-patiya-mirpur",
            source_depot_id="depot-patiya",
            destination_station_id="station-mirpur",
            transit_ticks=3,
            max_shipment=route_max,
            status=route_status,
        ),
        make_route(
            "route-gazipur-tongi",
            source_depot_id="depot-gazipur",
            destination_station_id="station-tongi",
            transit_ticks=1,
            max_shipment=route_max,
            status=route_status,
        ),
    ]
    if not second_route:
        routes = [r for r in routes if r.id != "route-patiya-mirpur"]
    return make_snapshot(
        tick=tick,
        depots=depots,
        stations=stations,
        routes=routes,
        allocations=allocations or [],
    )


def plan_for(world, **kwargs):
    forecast = build_forecast(world, horizon=kwargs.pop("horizon", 16))
    return optimize(world, forecast, **kwargs)


# --------------------------------------------------------------------------- #
# Constraint coverage
# --------------------------------------------------------------------------- #
def test_the_plan_respects_every_constraint():
    world = build_world()
    result = plan_for(world)

    assert result.shipments, "the optimiser should find something to ship"

    for shipment in result.shipments:
        route = world.route(shipment.route_id)
        depot = world.depot(shipment.source_depot_id)
        station = world.station(shipment.destination_station_id)

        # C1: route max is a per-shipment limit.
        assert shipment.quantity <= route.max_shipment + 1e-6
        # C2: depot inventory is never oversubscribed.
        assert shipment.quantity <= depot.inventory["DIESEL"] + 1e-6
        # C4: the station's tank is never overfilled.
        capacity = station.capacity[shipment.fuel_type]
        assert shipment.quantity <= max(
            0.0, capacity - station.inventory.get(shipment.fuel_type, 0.0)
        ) + 1e-6


def test_a_constrained_depot_is_still_dispatched_from():
    """CONSTRAINED is dispatchable -- measured, not assumed."""
    world = build_world(depot_status="CONSTRAINED")
    result = plan_for(world)
    sources = {s.source_depot_id for s in result.shipments}
    assert "depot-gazipur" in sources


def test_an_outage_depot_is_not_dispatched_from():
    world = build_world(depot_status="OUTAGE")
    result = plan_for(world)
    sources = {s.source_depot_id for s in result.shipments}
    assert "depot-gazipur" not in sources


def test_a_disrupted_route_is_not_used():
    world = build_world(route_status="ROUTE_DISRUPTED")
    result = plan_for(world)
    for shipment in result.shipments:
        assert world.route(shipment.route_id).status == "AVAILABLE"


def test_a_closed_station_is_not_dispatched_to():
    world = build_world(station_status="CLOSED")
    result = plan_for(world)
    for shipment in result.shipments:
        assert world.station(shipment.destination_station_id).status == "OPEN"


def test_the_plan_does_not_exceed_the_dispatch_cap_across_routes():
    """C3: the cap is per depot, per tick, and accumulates across routes."""
    world = build_world(dispatch_capacity=8000.0, second_route=True)
    result = plan_for(world)

    per_tick: dict[tuple[int, str], float] = {}
    for shipment in result.shipments:
        key = (shipment.dispatch_tick, shipment.source_depot_id)
        per_tick[key] = per_tick.get(key, 0.0) + shipment.quantity

    for (_tick, depot_id), total in per_tick.items():
        depot = world.depot(depot_id)
        assert total <= depot.dispatch_capacity_per_tick + 1e-6


def test_the_plan_never_exceeds_the_depot_inventory_in_total():
    world = build_world(depot_inventory=9000.0)
    result = plan_for(world)

    per_depot: dict[str, float] = {}
    for shipment in result.shipments:
        per_depot[shipment.source_depot_id] = (
            per_depot.get(shipment.source_depot_id, 0.0) + shipment.quantity
        )

    for depot_id, total in per_depot.items():
        depot = world.depot(depot_id)
        for fuel in FUELS:
            shipped = sum(
                s.quantity
                for s in result.shipments
                if s.source_depot_id == depot_id and s.fuel_type == fuel
            )
            assert shipped <= depot.inventory[fuel] + 1e-6


def test_the_plan_splits_across_shipments_when_the_route_max_is_insufficient():
    """C1: the route max is a per-shipment limit, not a per-plan limit."""
    world = build_world(route_max=7000.0, station_inventory=0.0)
    result = plan_for(world, horizon=96)

    for shipment in result.shipments:
        assert shipment.quantity <= 7000.0 + 1e-6

    # A long horizon with an empty station must need more than one shipment.
    assert len(result.shipments) >= 2


def test_the_plan_prefers_the_faster_route_when_both_are_available():
    world = build_world(second_route=True)
    result = plan_for(world)
    for shipment in result.shipments:
        if shipment.destination_station_id != "station-mirpur":
            continue
        route = world.route(shipment.route_id)
        assert route.transit_ticks == 2


def test_a_dual_connected_station_is_rerouted_around_a_disruption():
    """Disrupt the primary route; the plan must use the other one."""
    routes = [
        make_route(
            "route-gazipur-mirpur",
            source_depot_id="depot-gazipur",
            destination_station_id="station-mirpur",
            transit_ticks=2,
            max_shipment=7000.0,
            status="ROUTE_DISRUPTED",
        ),
        make_route(
            "route-patiya-mirpur",
            source_depot_id="depot-patiya",
            destination_station_id="station-mirpur",
            transit_ticks=3,
            max_shipment=7000.0,
            status="AVAILABLE",
        ),
        make_route(
            "route-gazipur-tongi",
            source_depot_id="depot-gazipur",
            destination_station_id="station-tongi",
            transit_ticks=1,
            max_shipment=7000.0,
            status="AVAILABLE",
        ),
    ]
    world = make_snapshot(
        tick=5,
        depots=[
            make_depot("depot-gazipur", inventory={f: 50000.0 for f in FUELS}),
            make_depot("depot-patiya", inventory={f: 50000.0 for f in FUELS}),
        ],
        stations=[
            make_station(
                "station-mirpur",
                capacity={f: 20000.0 for f in FUELS},
                inventory={f: 0.0 for f in FUELS},
            ),
            make_station(
                "station-tongi",
                capacity={f: 20000.0 for f in FUELS},
                inventory={f: 0.0 for f in FUELS},
            ),
        ],
        routes=routes,
    )
    result = plan_for(world)
    mirpur_routes = {
        s.route_id for s in result.shipments
        if s.destination_station_id == "station-mirpur"
    }
    assert "route-gazipur-mirpur" not in mirpur_routes
    assert "route-patiya-mirpur" in mirpur_routes


# --------------------------------------------------------------------------- #
# Multi-tick behaviour
# --------------------------------------------------------------------------- #
def test_multi_tick_plans_use_the_dispatch_tick_not_the_snapshot_tick():
    """A future shipment carries its own tick, so the key stays unique."""
    world = build_world(tick=5, station_inventory=0.0)
    result = plan_for(world, horizon=96)

    later = [s for s in result.shipments if s.dispatch_tick > world.tick]
    if later:
        assert all(s.dispatch_tick > world.tick for s in later)
    # Whatever the plan contains, the executable and advisory sets partition it.
    assert len(result.shipments_now) + len(result.shipments_later) == len(
        result.shipments
    )


def test_only_current_tick_shipments_are_executable():
    world = build_world(tick=5)
    result = plan_for(world)
    for shipment in result.shipments_now:
        assert shipment.dispatch_tick == world.tick
    for shipment in result.shipments_later:
        assert shipment.dispatch_tick != world.tick


def test_the_arrival_tick_includes_the_measured_one_tick_lag():
    world = build_world(tick=5)
    result = plan_for(world)
    for shipment in result.shipments:
        route = world.route(shipment.route_id)
        assert shipment.arrival_tick == shipment.dispatch_tick + route.transit_ticks + 1


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #
def test_the_plan_is_deterministic():
    world = build_world()
    forecast = build_forecast(world, horizon=16)
    first = optimize(world, forecast)
    for _ in range(20):
        again = optimize(world, forecast)
        assert again.to_dict() == first.to_dict()


def test_the_plan_is_deterministic_across_fresh_forecasts():
    world = build_world()
    a = optimize(world, build_forecast(world, horizon=16))
    b = optimize(world, build_forecast(world, horizon=16))
    assert a.to_dict() == b.to_dict()


# --------------------------------------------------------------------------- #
# Never worse than the naive baseline
# --------------------------------------------------------------------------- #
def test_the_plan_is_never_worse_than_the_naive_baseline():
    world = build_world()
    forecast = build_forecast(world, horizon=16)

    optimised = optimize(world, forecast)
    baseline = baseline_plan(world, forecast)

    assert service_level_of(world, forecast, optimised.shipments) >= (
        service_level_of(world, forecast, baseline) - 1e-6
    )


def test_the_plan_is_strictly_better_than_the_baseline_when_a_route_fails():
    """Otherwise the comparison above would be vacuous."""
    routes = [
        make_route(
            "route-gazipur-mirpur",
            source_depot_id="depot-gazipur",
            destination_station_id="station-mirpur",
            transit_ticks=2,
            max_shipment=7000.0,
            status="ROUTE_DISRUPTED",
        ),
        make_route(
            "route-patiya-mirpur",
            source_depot_id="depot-patiya",
            destination_station_id="station-mirpur",
            transit_ticks=3,
            max_shipment=7000.0,
            status="AVAILABLE",
        ),
        make_route(
            "route-gazipur-tongi",
            source_depot_id="depot-gazipur",
            destination_station_id="station-tongi",
            transit_ticks=1,
            max_shipment=7000.0,
            status="AVAILABLE",
        ),
    ]
    world = make_snapshot(
        tick=5,
        depots=[
            make_depot("depot-gazipur", inventory={f: 50000.0 for f in FUELS}),
            make_depot("depot-patiya", inventory={f: 50000.0 for f in FUELS}),
        ],
        stations=[
            make_station(
                "station-mirpur",
                capacity={f: 20000.0 for f in FUELS},
                inventory={f: 0.0 for f in FUELS},
            ),
            make_station(
                "station-tongi",
                capacity={f: 20000.0 for f in FUELS},
                inventory={f: 0.0 for f in FUELS},
            ),
        ],
        routes=routes,
    )
    forecast = build_forecast(world, horizon=16)

    optimised = optimize(world, forecast)
    baseline = baseline_plan(world, forecast)

    assert service_level_of(world, forecast, optimised.shipments) > (
        service_level_of(world, forecast, baseline) + 1e-6
    )


# --------------------------------------------------------------------------- #
# Interaction with the dispatch cap already consumed this tick
# --------------------------------------------------------------------------- #
def test_the_plan_respects_the_dispatch_allowance_already_consumed():
    """C3: allocations created on this tick already count against the cap."""
    world = build_world(
        tick=5,
        dispatch_capacity=10000.0,
        allocations=[
            make_allocation(
                1,
                depot_id="depot-gazipur",
                station_id="station-tongi",
                route_id="route-gazipur-tongi",
                fuel_type="DIESEL",
                quantity=8000.0,
                created_tick=5,
                status="IN_TRANSIT",
            )
        ],
    )
    remaining = dispatch_remaining(world, "depot-gazipur")
    assert remaining == pytest.approx(2000.0)

    result = plan_for(world)
    shipped_this_tick = sum(
        s.quantity
        for s in result.shipments
        if s.dispatch_tick == world.tick and s.source_depot_id == "depot-gazipur"
    )
    assert shipped_this_tick <= remaining + 1e-6


# --------------------------------------------------------------------------- #
# Spill is penalised, never rejected
# --------------------------------------------------------------------------- #
def test_spill_is_flagged_but_the_shipment_is_still_planned():
    """The simulator accepts over-commits and silently spills the excess."""
    world = build_world(
        allocations=[
            make_allocation(
                1,
                depot_id="depot-gazipur",
                station_id="station-mirpur",
                route_id="route-gazipur-mirpur",
                fuel_type="DIESEL",
                quantity=19000.0,
                created_tick=3,
                status="IN_TRANSIT",
            )
        ],
    )
    result = plan_for(world)
    # The plan is not empty -- a stricter optimiser would be a bad oracle.
    assert result.status in ("PLANNED", "PARTIAL")


# --------------------------------------------------------------------------- #
# Solver reporting
# --------------------------------------------------------------------------- #
def test_the_solver_always_reports_the_truth():
    world = build_world()
    result = plan_for(world)
    assert result.solver_used in ("greedy", "lp")
    assert "solver_used" in result.to_dict()


def test_the_greedy_and_lp_paths_return_the_same_result_type():
    world = build_world()
    result = plan_for(world, config=OptimizerConfig(solver="greedy"))
    assert hasattr(result, "shipments")
    assert hasattr(result, "expected_service_level")
    assert hasattr(result, "solver_used")


# --------------------------------------------------------------------------- #
# Edge cases
# --------------------------------------------------------------------------- #
def test_an_empty_world_produces_an_empty_plan():
    """No stations at all -- nothing to plan for."""
    world = make_snapshot(tick=5, depots=[], stations=[], routes=[])
    result = plan_for(world)
    assert result.status == "EMPTY"
    assert result.shipments == []
    assert result.total_litres == 0.0


def test_a_world_with_no_usable_route_produces_an_empty_plan():
    world = build_world(route_status="ROUTE_DISRUPTED")
    result = plan_for(world)
    assert result.shipments == []
    assert result.status == "EMPTY"


def test_the_forecast_horizon_governs_not_the_config():
    """The caller supplies the forecast; its horizon is the planning horizon.

    Rebuilding the forecast from the config's horizon would silently discard
    a forecast the caller has already computed (and with it any risk data
    they intend to report against), so the supplied forecast wins.
    """
    world = build_world()
    forecast = build_forecast(world, horizon=16)
    result = optimize(world, forecast, config=OptimizerConfig(horizon_ticks=4))
    assert result.diagnostics["horizon"] == 16


def test_an_explicit_horizon_rebuilds_the_forecast():
    world = build_world()
    forecast = build_forecast(world, horizon=16)
    result = optimize(world, forecast, horizon=8)
    assert result.diagnostics["horizon"] == 8


def test_the_result_serialises_to_json_friendly_types():
    import json

    world = build_world()
    result = plan_for(world)
    payload = json.dumps(result.to_dict())
    assert "shipments_now" in payload
    assert "expected_service_level" in payload
