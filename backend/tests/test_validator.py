"""Pure unit tests for the pre-flight validator.

No simulator, no I/O -- the validator is a function of a snapshot and a
request, so it can be tested exhaustively and quickly.

Every assertion here encodes a *measured* simulator behaviour. The live
oracle lives in ``test_validator_against_simulator.py``.
"""

from __future__ import annotations

import pytest

from app.keys import make_key, request_hash
from app.validator import (
    dispatch_committed_this_tick,
    dispatch_remaining,
    select_route,
    station_free_space,
    station_inbound,
    validate_and_plan,
)
from conftest import (
    make_allocation,
    make_depot,
    make_request,
    make_route,
    make_snapshot,
    make_station,
)


# --------------------------------------------------------------------------- #
# Request sanity
# --------------------------------------------------------------------------- #
def test_zero_quantity_is_rejected():
    plan = validate_and_plan(make_snapshot(), make_request(quantity=0))
    assert plan.status == "REJECTED"
    assert plan.reject_code == "INVALID_QUANTITY"


def test_negative_quantity_is_rejected():
    plan = validate_and_plan(make_snapshot(), make_request(quantity=-5))
    assert plan.status == "REJECTED"
    assert plan.reject_code == "INVALID_QUANTITY"


def test_unknown_fuel_type_is_rejected():
    # Pydantic blocks an unknown fuel type at the schema level; bypass the
    # schema to prove the validator also guards it.
    request = make_request()
    object.__setattr__(request, "fuel_type", "GASOLINE")
    plan = validate_and_plan(make_snapshot(), request)
    assert plan.status == "REJECTED"
    assert plan.reject_code == "INVALID_FUEL_TYPE"


# --------------------------------------------------------------------------- #
# Entity existence
# --------------------------------------------------------------------------- #
def test_unknown_depot_is_not_found():
    plan = validate_and_plan(make_snapshot(), make_request(depot_id="depot-nope"))
    assert plan.status == "REJECTED"
    assert plan.reject_code == "NOT_FOUND"


def test_unknown_station_is_not_found():
    plan = validate_and_plan(make_snapshot(), make_request(station_id="station-nope"))
    assert plan.status == "REJECTED"
    assert plan.reject_code == "NOT_FOUND"


# --------------------------------------------------------------------------- #
# Route matching
# --------------------------------------------------------------------------- #
def test_explicit_route_with_wrong_endpoints_is_route_mismatch():
    snapshot = make_snapshot(
        routes=[make_route("route-other", destination_station_id="station-tongi")]
    )
    plan = validate_and_plan(
        snapshot, make_request(route_id="route-other")
    )
    assert plan.status == "REJECTED"
    assert plan.reject_code == "ROUTE_MISMATCH"


def test_route_selection_prefers_available_then_fastest():
    snapshot = make_snapshot(
        stations=[make_station("station-mirpur")],
        routes=[
            make_route("route-a", transit_ticks=1, max_shipment=5000.0),
            make_route("route-b", transit_ticks=3, max_shipment=9000.0),
            make_route(
                "route-c", transit_ticks=1, max_shipment=5000.0, status="DISRUPTED"
            ),
        ],
    )
    route, alternatives, is_only = select_route(
        snapshot,
        source_depot_id="depot-gazipur",
        destination_station_id="station-mirpur",
    )
    assert route.id == "route-a"
    assert is_only is False
    assert {a.route_id for a in alternatives} == {"route-a", "route-b", "route-c"}
    disrupted = next(a for a in alternatives if a.route_id == "route-c")
    assert disrupted.selected is False


def test_route_selection_is_deterministic():
    snapshot = make_snapshot(
        routes=[
            make_route("route-b", transit_ticks=2, max_shipment=7000.0),
            make_route("route-a", transit_ticks=2, max_shipment=7000.0),
        ]
    )
    first, _, _ = select_route(
        snapshot,
        source_depot_id="depot-gazipur",
        destination_station_id="station-mirpur",
    )
    second, _, _ = select_route(
        snapshot,
        source_depot_id="depot-gazipur",
        destination_station_id="station-mirpur",
    )
    assert first.id == second.id == "route-a"


def test_single_connected_station_is_detected():
    snapshot = make_snapshot(routes=[make_route("route-only")])
    plan = validate_and_plan(snapshot, make_request(quantity=1000.0))
    assert plan.explanation.is_only_route is True
    assert plan.explanation.rerouting_possible is False
    assert any(w.code == "SINGLE_CONNECTED_STATION" for w in plan.warnings)


def test_dual_connected_station_can_reroute():
    snapshot = make_snapshot(
        depots=[make_depot("depot-gazipur"), make_depot("depot-chattogram")],
        stations=[make_station("station-karnaphuli")],
        routes=[
            make_route(
                "route-gazipur-karnaphuli",
                source_depot_id="depot-gazipur",
                destination_station_id="station-karnaphuli",
            ),
            make_route(
                "route-chattogram-karnaphuli",
                source_depot_id="depot-chattogram",
                destination_station_id="station-karnaphuli",
            ),
        ],
    )
    plan = validate_and_plan(
        snapshot,
        make_request(
            depot_id="depot-gazipur",
            station_id="station-karnaphuli",
            quantity=1000.0,
        ),
    )
    assert plan.status == "PLANNED"
    assert plan.explanation.is_only_route is False
    assert plan.explanation.rerouting_possible is True


# --------------------------------------------------------------------------- #
# Status rules
# --------------------------------------------------------------------------- #
def test_constrained_depot_is_still_dispatchable():
    """CONSTRAINED is dispatchable -- measured, not assumed."""
    snapshot = make_snapshot(depots=[make_depot(status="CONSTRAINED")])
    plan = validate_and_plan(snapshot, make_request(quantity=1000.0))
    assert plan.status == "PLANNED"


def test_outage_depot_is_rejected():
    snapshot = make_snapshot(depots=[make_depot(status="OUTAGE")])
    plan = validate_and_plan(snapshot, make_request(quantity=1000.0))
    assert plan.status == "REJECTED"
    assert plan.reject_code == "DEPOT_CLOSED"


def test_closed_station_is_rejected():
    snapshot = make_snapshot(stations=[make_station(status="CLOSED")])
    plan = validate_and_plan(snapshot, make_request(quantity=1000.0))
    assert plan.status == "REJECTED"
    assert plan.reject_code == "STATION_CLOSED"


def test_disrupted_route_is_rejected():
    snapshot = make_snapshot(routes=[make_route(status="DISRUPTED")])
    plan = validate_and_plan(snapshot, make_request(quantity=1000.0))
    assert plan.status == "REJECTED"
    assert plan.reject_code == "ROUTE_DISRUPTED"


def test_disrupted_route_rejection_names_alternatives():
    snapshot = make_snapshot(
        routes=[
            make_route("route-a", status="DISRUPTED"),
            make_route("route-b"),
        ]
    )
    plan = validate_and_plan(snapshot, make_request(quantity=1000.0))
    assert plan.status == "PLANNED"
    assert plan.shipments[0].route_id == "route-b"


# --------------------------------------------------------------------------- #
# Capacity ceilings
# --------------------------------------------------------------------------- #
def test_request_above_route_max_is_split():
    snapshot = make_snapshot(
        stations=[make_station(capacity={"DIESEL": 20000.0, "PETROL": 6000.0,
                                          "OCTANE": 6000.0})],
        routes=[make_route(max_shipment=7000.0)],
    )
    plan = validate_and_plan(snapshot, make_request(quantity=12000.0))
    assert plan.status == "PLANNED"
    assert len(plan.shipments) == 2
    assert plan.shipments[0].quantity == 7000.0
    assert plan.shipments[1].quantity == 5000.0
    assert plan.total_planned_litres == 12000.0


def test_quantity_exactly_at_route_max_is_accepted():
    """Boundary checks are `>`, not `>=` -- equality passes."""
    snapshot = make_snapshot(
        stations=[make_station(capacity={"DIESEL": 20000.0, "PETROL": 6000.0,
                                          "OCTANE": 6000.0})],
        routes=[make_route(max_shipment=7000.0)],
    )
    plan = validate_and_plan(snapshot, make_request(quantity=7000.0))
    assert plan.status == "PLANNED"
    assert len(plan.shipments) == 1
    assert plan.shipments[0].quantity == 7000.0


def test_split_disabled_rejects_a_partial_plan():
    snapshot = make_snapshot(
        stations=[make_station(capacity={"DIESEL": 20000.0, "PETROL": 6000.0,
                                          "OCTANE": 6000.0})],
        routes=[make_route(max_shipment=7000.0)],
    )
    plan = validate_and_plan(
        snapshot, make_request(quantity=12000.0, allow_split=False)
    )
    assert plan.status == "REJECTED"
    # The route max shipment is the binding limit for a single shipment.
    assert plan.reject_code == "ROUTE_MAX_SHIPMENT"
    assert plan.shipments == []
    assert plan.binding_constraint.kind == "ROUTE_MAX_SHIPMENT"


def test_split_disabled_accepts_a_request_that_fits_one_shipment():
    snapshot = make_snapshot(
        stations=[make_station(capacity={"DIESEL": 20000.0, "PETROL": 6000.0,
                                          "OCTANE": 6000.0})],
        routes=[make_route(max_shipment=7000.0)],
    )
    plan = validate_and_plan(
        snapshot, make_request(quantity=5000.0, allow_split=False)
    )
    assert plan.status == "PLANNED"
    assert len(plan.shipments) == 1
    assert plan.shipments[0].quantity == 5000.0


def test_dispatch_capacity_limits_the_plan():
    snapshot = make_snapshot(
        depots=[make_depot(dispatch_capacity_per_tick=12000.0)],
        stations=[make_station(capacity={"DIESEL": 40000.0, "PETROL": 6000.0,
                                          "OCTANE": 6000.0})],
        routes=[make_route(max_shipment=7000.0)],
    )
    plan = validate_and_plan(snapshot, make_request(quantity=20000.0))
    assert plan.status == "PARTIAL"
    assert plan.total_planned_litres == 12000.0
    assert plan.deferred_litres == 8000.0


def test_depot_inventory_limits_the_plan():
    snapshot = make_snapshot(
        depots=[make_depot(inventory={"DIESEL": 9000.0, "PETROL": 100.0,
                                      "OCTANE": 100.0})],
        stations=[make_station(capacity={"DIESEL": 40000.0, "PETROL": 6000.0,
                                          "OCTANE": 6000.0})],
        routes=[make_route(max_shipment=7000.0)],
    )
    plan = validate_and_plan(snapshot, make_request(quantity=12000.0))
    assert plan.status == "PARTIAL"
    assert plan.total_planned_litres == 9000.0
    assert plan.deferred_litres == 3000.0


def test_station_free_space_limits_the_plan():
    snapshot = make_snapshot(
        stations=[make_station(capacity={"DIESEL": 6000.0, "PETROL": 6000.0,
                                          "OCTANE": 6000.0})],
        routes=[make_route(max_shipment=7000.0)],
    )
    plan = validate_and_plan(snapshot, make_request(quantity=12000.0))
    assert plan.status == "PARTIAL"
    assert plan.total_planned_litres == 6000.0
    assert plan.deferred_litres == 6000.0


def test_quantity_exactly_at_station_free_space_is_accepted():
    snapshot = make_snapshot(
        stations=[make_station(capacity={"DIESEL": 6000.0, "PETROL": 6000.0,
                                          "OCTANE": 6000.0})],
    )
    plan = validate_and_plan(snapshot, make_request(quantity=6000.0))
    assert plan.status == "PLANNED"


# --------------------------------------------------------------------------- #
# Dispatch accumulation -- the created_tick == current_tick rule
# --------------------------------------------------------------------------- #
def test_dispatch_committed_only_counts_the_current_tick():
    snapshot = make_snapshot(
        tick=5,
        allocations=[
            make_allocation(1, quantity=3000.0, created_tick=5, status="PENDING"),
            make_allocation(2, quantity=4000.0, created_tick=4, status="IN_TRANSIT"),
        ],
    )
    committed = dispatch_committed_this_tick(snapshot, "depot-gazipur")
    assert committed == 3000.0


def test_dispatch_accumulates_across_routes():
    """6000 + 6000 on two different routes in one tick uses the whole cap."""
    snapshot = make_snapshot(
        tick=5,
        allocations=[
            make_allocation(
                1,
                quantity=6000.0,
                created_tick=5,
                status="PENDING",
                route_id="route-a",
            ),
            make_allocation(
                2,
                quantity=6000.0,
                created_tick=5,
                status="PENDING",
                route_id="route-b",
            ),
        ],
    )
    assert dispatch_committed_this_tick(snapshot, "depot-gazipur") == 12000.0
    assert dispatch_remaining(snapshot, "depot-gazipur") == 0.0


def test_dispatch_cap_resets_on_the_next_tick():
    snapshot = make_snapshot(
        tick=6,
        allocations=[
            make_allocation(1, quantity=6000.0, created_tick=5, status="PENDING"),
        ],
    )
    # Dispatched on tick 5, so it does not count against tick 6.
    assert dispatch_remaining(snapshot, "depot-gazipur") == 12000.0


def test_cancelled_allocations_do_not_consume_dispatch_capacity():
    snapshot = make_snapshot(
        tick=5,
        allocations=[
            make_allocation(1, quantity=6000.0, created_tick=5, status="CANCELLED"),
        ],
    )
    assert dispatch_committed_this_tick(snapshot, "depot-gazipur") == 0.0


def test_arrived_allocations_do_not_consume_dispatch_capacity():
    snapshot = make_snapshot(
        tick=5,
        allocations=[
            make_allocation(1, quantity=6000.0, created_tick=5, status="ARRIVED"),
        ],
    )
    assert dispatch_committed_this_tick(snapshot, "depot-gazipur") == 0.0


def test_plan_accounts_for_committed_dispatch_this_tick():
    snapshot = make_snapshot(
        tick=5,
        allocations=[
            make_allocation(1, quantity=6000.0, created_tick=5, status="PENDING"),
        ],
        stations=[make_station(capacity={"DIESEL": 40000.0, "PETROL": 6000.0,
                                          "OCTANE": 6000.0})],
        routes=[make_route(max_shipment=7000.0)],
    )
    plan = validate_and_plan(snapshot, make_request(quantity=12000.0))
    # 12000 cap - 6000 already committed = 6000 left this tick.
    assert plan.total_planned_litres == 6000.0
    assert plan.status == "PARTIAL"


# --------------------------------------------------------------------------- #
# Spill risk (soft rule)
# --------------------------------------------------------------------------- #
def test_inbound_fuel_produces_a_spill_risk_warning():
    snapshot = make_snapshot(
        stations=[make_station(capacity={"DIESEL": 6000.0, "PETROL": 6000.0,
                                          "OCTANE": 6000.0})],
        allocations=[
            make_allocation(1, quantity=5000.0, created_tick=4, status="IN_TRANSIT"),
        ],
        routes=[make_route(max_shipment=7000.0)],
    )
    plan = validate_and_plan(snapshot, make_request(quantity=6000.0))
    # The simulator would accept this -- the validator must not be stricter.
    assert plan.status == "PLANNED"
    spill = [w for w in plan.warnings if w.code == "SPILL_RISK"]
    assert len(spill) == 1
    assert spill[0].spill_risk_litres == 5000.0


def test_no_spill_warning_when_the_station_has_room():
    snapshot = make_snapshot(
        stations=[make_station(
            capacity={"DIESEL": 20000.0, "PETROL": 6000.0, "OCTANE": 6000.0},
            inventory={"DIESEL": 0.0, "PETROL": 0.0, "OCTANE": 0.0},
        )],
    )
    plan = validate_and_plan(snapshot, make_request(quantity=6000.0))
    assert not [w for w in plan.warnings if w.code == "SPILL_RISK"]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def test_station_free_space_is_measured_against_live_inventory_only():
    snapshot = make_snapshot(
        stations=[make_station(
            capacity={"DIESEL": 6000.0, "PETROL": 6000.0, "OCTANE": 6000.0},
            inventory={"DIESEL": 1000.0, "PETROL": 0.0, "OCTANE": 0.0},
        )],
    )
    assert station_free_space(snapshot, "station-mirpur", "DIESEL") == 5000.0


def test_station_inbound_counts_only_in_flight_shipments():
    snapshot = make_snapshot(
        allocations=[
            make_allocation(1, quantity=1000.0, created_tick=4, status="IN_TRANSIT"),
            make_allocation(2, quantity=2000.0, created_tick=4, status="PENDING"),
            make_allocation(3, quantity=4000.0, created_tick=4, status="ARRIVED"),
            make_allocation(4, quantity=500.0, created_tick=4, status="CANCELLED"),
        ],
    )
    assert station_inbound(snapshot, "station-mirpur", "DIESEL") == 3000.0


# --------------------------------------------------------------------------- #
# Idempotency keys
# --------------------------------------------------------------------------- #
def test_keys_are_stable_for_the_same_request():
    snapshot = make_snapshot()
    request = make_request(quantity=1000.0)
    assert make_key(snapshot, request, 1) == make_key(snapshot, request, 1)


def test_keys_differ_per_sequence():
    snapshot = make_snapshot()
    request = make_request(quantity=12000.0)
    assert make_key(snapshot, request, 1) != make_key(snapshot, request, 2)


def test_keys_differ_for_different_quantities():
    snapshot = make_snapshot()
    assert request_hash(make_request(quantity=1000.0)) != request_hash(
        make_request(quantity=2000.0)
    )


def test_keys_differ_for_different_ticks():
    snapshot_a = make_snapshot(tick=5)
    snapshot_b = make_snapshot(tick=6)
    request = make_request(quantity=1000.0)
    assert make_key(snapshot_a, request, 1) != make_key(snapshot_b, request, 1)


def test_keys_embed_the_tick():
    snapshot = make_snapshot(tick=7)
    key = make_key(snapshot, make_request(quantity=1000.0), 1)
    assert key.startswith("alloc-t7-")
    assert "station-mirpur" in key
    assert "diesel" in key


# --------------------------------------------------------------------------- #
# Explanation quality
# --------------------------------------------------------------------------- #
def test_every_shipment_carries_reasoning():
    snapshot = make_snapshot(
        stations=[make_station(capacity={"DIESEL": 40000.0, "PETROL": 6000.0,
                                          "OCTANE": 6000.0})],
        routes=[make_route(max_shipment=7000.0)],
    )
    plan = validate_and_plan(snapshot, make_request(quantity=12000.0))
    for shipment in plan.shipments:
        assert shipment.reasoning
        assert shipment.binding_constraints
        assert shipment.binding_constraints[0].kind


def test_plan_has_a_human_summary():
    snapshot = make_snapshot()
    plan = validate_and_plan(snapshot, make_request(quantity=1000.0))
    assert plan.explanation.human_summary
    assert "Dispatch" in plan.explanation.human_summary


def test_rejection_identifies_the_binding_constraint():
    """When nothing can be shipped at all, name the limit that blocked it.

    The reported limit follows the *simulator's* validation order, not the
    numeric size: the route max is checked before the destination's free
    space, so a request above both is reported as ROUTE_MAX_SHIPMENT.
    """
    snapshot = make_snapshot(
        stations=[make_station(capacity={"DIESEL": 0.0, "PETROL": 0.0,
                                          "OCTANE": 0.0})],
        routes=[make_route(max_shipment=100.0)],
    )
    plan = validate_and_plan(snapshot, make_request(quantity=5000.0))
    assert plan.status == "REJECTED"
    assert plan.binding_constraint is not None
    # The route max (100) is checked before the station's free space (0).
    assert plan.binding_constraint.kind == "ROUTE_MAX_SHIPMENT"
    assert plan.diagnostics["station_free_space"] == 0.0


def test_destination_capacity_is_reported_when_only_it_is_exceeded():
    """With the route max satisfied, the station's free space is reported."""
    snapshot = make_snapshot(
        stations=[make_station(capacity={"DIESEL": 500.0, "PETROL": 6000.0,
                                          "OCTANE": 6000.0})],
        routes=[make_route(max_shipment=7000.0)],
    )
    plan = validate_and_plan(
        snapshot, make_request(quantity=1000.0, allow_split=False)
    )
    assert plan.status == "REJECTED"
    assert plan.reject_code == "STATION_FREE_SPACE"


def test_a_request_above_the_free_space_is_split_when_splitting_is_on():
    """Splitting turns an over-capacity request into a partial shipment."""
    snapshot = make_snapshot(
        stations=[make_station(capacity={"DIESEL": 500.0, "PETROL": 6000.0,
                                          "OCTANE": 6000.0})],
        routes=[make_route(max_shipment=7000.0)],
    )
    plan = validate_and_plan(snapshot, make_request(quantity=1000.0))
    assert plan.status == "PARTIAL"
    assert plan.total_planned_litres == 500.0
    assert plan.deferred_litres == 500.0


def test_split_respects_every_ceiling():
    snapshot = make_snapshot(
        depots=[make_depot(dispatch_capacity_per_tick=9000.0)],
        stations=[make_station(capacity={"DIESEL": 8000.0, "PETROL": 6000.0,
                                          "OCTANE": 6000.0})],
        routes=[make_route(max_shipment=4000.0)],
    )
    plan = validate_and_plan(snapshot, make_request(quantity=20000.0))
    assert plan.status == "PARTIAL"
    for shipment in plan.shipments:
        assert shipment.quantity <= 4000.0  # route max
    assert plan.total_planned_litres <= 8000.0  # station free space
    assert plan.total_planned_litres <= 9000.0  # dispatch capacity
    assert plan.total_planned_litres <= 50000.0  # depot inventory


# --------------------------------------------------------------------------- #
# The simulator's validation ORDER (measured, not assumed)
# --------------------------------------------------------------------------- #
def test_the_route_max_is_reported_before_a_tighter_destination_limit():
    """The simulator checks limits in a fixed order, not by magnitude.

    A 5000 L request against a route with a 1000 L max and a station with
    100 L of free space is rejected by the simulator with
    ``ROUTE_CAPACITY_EXCEEDED`` -- not ``DESTINATION_CAPACITY_EXCEEDED`` --
    even though the station is far tighter.

    Splitting is disabled here so the request is judged as one shipment.
    """
    snapshot = make_snapshot(
        stations=[make_station(capacity={"DIESEL": 100.0, "PETROL": 6000.0,
                                          "OCTANE": 6000.0})],
        routes=[make_route(max_shipment=1000.0)],
    )
    plan = validate_and_plan(
        snapshot, make_request(quantity=5000.0, allow_split=False)
    )
    assert plan.status == "REJECTED"
    assert plan.reject_code == "ROUTE_MAX_SHIPMENT"


def test_the_destination_limit_is_reported_when_the_route_max_is_satisfied():
    snapshot = make_snapshot(
        stations=[make_station(capacity={"DIESEL": 100.0, "PETROL": 6000.0,
                                          "OCTANE": 6000.0})],
        routes=[make_route(max_shipment=7000.0)],
    )
    plan = validate_and_plan(
        snapshot, make_request(quantity=5000.0, allow_split=False)
    )
    assert plan.status == "REJECTED"
    assert plan.reject_code == "STATION_FREE_SPACE"


def test_dispatch_capacity_is_reported_before_the_destination_limit():
    snapshot = make_snapshot(
        depots=[make_depot(dispatch_capacity_per_tick=1000.0)],
        stations=[make_station(capacity={"DIESEL": 100.0, "PETROL": 6000.0,
                                          "OCTANE": 6000.0})],
        routes=[make_route(max_shipment=7000.0)],
    )
    plan = validate_and_plan(
        snapshot, make_request(quantity=5000.0, allow_split=False)
    )
    assert plan.status == "REJECTED"
    assert plan.reject_code == "DISPATCH_CAPACITY"


def test_a_request_above_the_route_max_is_split_not_rejected():
    """With splitting on, an oversized request becomes several shipments."""
    snapshot = make_snapshot(
        stations=[make_station(capacity={"DIESEL": 40000.0, "PETROL": 6000.0,
                                          "OCTANE": 6000.0})],
        routes=[make_route(max_shipment=1000.0)],
    )
    plan = validate_and_plan(snapshot, make_request(quantity=5000.0))
    assert plan.status == "PLANNED"
    assert len(plan.shipments) == 5
    assert all(s.quantity <= 1000.0 for s in plan.shipments)
