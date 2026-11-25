"""Unit tests for the dispatch gate.

The gate is the boundary between "the optimiser thinks this is a good idea"
and "the simulator is allowed to see it". A shipment that the gate lets
through must be one the validator would approve -- that is the soundness
property the whole Phase 1 contract rests on.
"""

from __future__ import annotations

import pytest

from app.dispatch import GateResult, gate_plan
from app.optimizer import PlannedShipment
from conftest import (
    make_allocation,
    make_depot,
    make_route,
    make_snapshot,
    make_station,
)

FUELS = ("DIESEL", "PETROL", "OCTANE")


def planned(
    *,
    sequence: int = 1,
    dispatch_tick: int = 5,
    source_depot_id: str = "depot-gazipur",
    destination_station_id: str = "station-mirpur",
    route_id: str = "route-gazipur-mirpur",
    fuel_type: str = "DIESEL",
    quantity: float = 1000.0,
) -> PlannedShipment:
    return PlannedShipment(
        sequence=sequence,
        dispatch_tick=dispatch_tick,
        arrival_tick=dispatch_tick + 3,
        source_depot_id=source_depot_id,
        destination_station_id=destination_station_id,
        route_id=route_id,
        fuel_type=fuel_type,
        quantity=quantity,
        priority_score=1.0,
        rationale="test shipment",
        binding="NONE",
        risk="NONE",
    )


def world(**kwargs):
    kwargs.setdefault("tick", 5)
    kwargs.setdefault(
        "depots",
        [make_depot("depot-gazipur", inventory={f: 50000.0 for f in FUELS})],
    )
    kwargs.setdefault(
        "stations",
        [
            make_station(
                "station-mirpur",
                capacity={f: 20000.0 for f in FUELS},
                inventory={f: 0.0 for f in FUELS},
            )
        ],
    )
    kwargs.setdefault(
        "routes",
        [
            make_route(
                "route-gazipur-mirpur",
                source_depot_id="depot-gazipur",
                destination_station_id="station-mirpur",
                transit_ticks=2,
                max_shipment=7000.0,
            )
        ],
    )
    return make_snapshot(**kwargs)


# --------------------------------------------------------------------------- #
# The happy path
# --------------------------------------------------------------------------- #
def test_a_legal_shipment_passes_the_gate():
    result = gate_plan(world(), [planned(quantity=1000.0)])
    assert result.accepted_count == 1
    assert result.rejected_count == 0
    assert result.total_litres == 1000.0


def test_the_gate_mints_the_idempotency_key():
    result = gate_plan(world(), [planned(quantity=1000.0)])
    key = result.accepted[0].shipment.idempotency_key
    assert key.startswith("alloc-t5-")
    assert "station-mirpur" in key
    assert "diesel" in key


# --------------------------------------------------------------------------- #
# The gate drops what the validator refuses
# --------------------------------------------------------------------------- #
def test_a_shipment_over_the_route_max_is_rejected():
    result = gate_plan(world(), [planned(quantity=9000.0)])
    assert result.accepted_count == 0
    assert result.rejected_count == 1
    assert result.rejected[0].code == "ROUTE_MAX_SHIPMENT"


def test_a_shipment_beyond_the_depot_inventory_is_rejected():
    result = gate_plan(world(), [planned(quantity=60000.0)])
    assert result.accepted_count == 0
    assert result.rejected[0].code in (
        "ROUTE_MAX_SHIPMENT",
        "DEPOT_INVENTORY",
    )


def test_a_shipment_on_a_disrupted_route_is_rejected():
    routes = [
        make_route(
            "route-gazipur-mirpur",
            source_depot_id="depot-gazipur",
            destination_station_id="station-mirpur",
            transit_ticks=2,
            max_shipment=7000.0,
            status="ROUTE_DISRUPTED",
        )
    ]
    result = gate_plan(world(routes=routes), [planned(quantity=1000.0)])
    assert result.accepted_count == 0
    assert result.rejected[0].code == "ROUTE_DISRUPTED"


def test_a_shipment_to_a_closed_station_is_rejected():
    stations = [
        make_station(
            "station-mirpur",
            status="CLOSED",
            capacity={f: 20000.0 for f in FUELS},
            inventory={f: 0.0 for f in FUELS},
        )
    ]
    result = gate_plan(world(stations=stations), [planned(quantity=1000.0)])
    assert result.accepted_count == 0
    assert result.rejected[0].code == "STATION_CLOSED"


def test_a_shipment_from_a_closed_depot_is_rejected():
    depots = [
        make_depot(
            "depot-gazipur",
            status="CLOSED",
            inventory={f: 50000.0 for f in FUELS},
        )
    ]
    result = gate_plan(world(depots=depots), [planned(quantity=1000.0)])
    assert result.accepted_count == 0
    assert result.rejected[0].code == "DEPOT_CLOSED"


def test_a_shipment_over_the_station_capacity_is_rejected():
    stations = [
        make_station(
            "station-mirpur",
            capacity={f: 500.0 for f in FUELS},
            inventory={f: 0.0 for f in FUELS},
        )
    ]
    result = gate_plan(world(stations=stations), [planned(quantity=1000.0)])
    assert result.accepted_count == 0
    assert result.rejected[0].code == "STATION_FREE_SPACE"


def test_a_shipment_over_the_dispatch_cap_is_rejected():
    depots = [
        make_depot(
            "depot-gazipur",
            dispatch_capacity_per_tick=1000.0,
            inventory={f: 50000.0 for f in FUELS},
        )
    ]
    result = gate_plan(world(depots=depots), [planned(quantity=5000.0)])
    assert result.accepted_count == 0
    assert result.rejected[0].code == "DISPATCH_CAPACITY"


def test_a_shipment_to_an_unknown_station_is_rejected():
    result = gate_plan(
        world(), [planned(destination_station_id="station-nowhere")]
    )
    assert result.accepted_count == 0
    assert result.rejected[0].code == "NOT_FOUND"


# --------------------------------------------------------------------------- #
# The route is never silently overridden
# --------------------------------------------------------------------------- #
def test_the_gate_does_not_reroute_around_a_disruption():
    """The optimiser chose that route; the gate must not substitute another.

    Silently rerouting would change when the fuel arrives, invalidating the
    optimiser's schedule.
    """
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
    ]
    depots = [
        make_depot("depot-gazipur", inventory={f: 50000.0 for f in FUELS}),
        make_depot("depot-patiya", inventory={f: 50000.0 for f in FUELS}),
    ]
    result = gate_plan(
        world(routes=routes, depots=depots),
        [planned(route_id="route-gazipur-mirpur", quantity=1000.0)],
    )
    assert result.accepted_count == 0
    assert result.rejected[0].code == "ROUTE_DISRUPTED"


# --------------------------------------------------------------------------- #
# Splitting is disabled
# --------------------------------------------------------------------------- #
def test_the_gate_does_not_split_a_shipment():
    """The optimiser computed a legal quantity; re-splitting would change it."""
    result = gate_plan(world(), [planned(quantity=5000.0)])
    assert result.accepted_count == 1
    # One shipment, not several chunks.
    assert result.accepted[0].shipment.quantity == 5000.0


# --------------------------------------------------------------------------- #
# Mixed batches
# --------------------------------------------------------------------------- #
def test_the_gate_keeps_the_legal_shipments_and_drops_the_rest():
    result = gate_plan(
        world(),
        [
            planned(sequence=1, quantity=1000.0),
            planned(sequence=2, quantity=9000.0),  # over the route max
            planned(sequence=3, quantity=2000.0),
        ],
    )
    assert result.accepted_count == 2
    assert result.rejected_count == 1
    assert result.total_litres == 3000.0


def test_the_gate_reports_the_rejection_reason():
    result = gate_plan(world(), [planned(quantity=9000.0)])
    rejection = result.rejected[0]
    assert rejection.code
    assert rejection.message
    assert rejection.diagnostics


def test_the_gate_result_serialises():
    import json

    result = gate_plan(world(), [planned(quantity=1000.0)])
    payload = json.dumps(result.to_dict())
    assert "accepted" in payload
    assert "rejections" in payload


def test_an_empty_plan_gates_to_an_empty_result():
    result = gate_plan(world(), [])
    assert isinstance(result, GateResult)
    assert result.accepted_count == 0
    assert result.rejected_count == 0
    assert result.total_litres == 0.0


# --------------------------------------------------------------------------- #
# The dispatch cap already consumed this tick
# --------------------------------------------------------------------------- #
def test_the_gate_accounts_for_the_dispatch_allowance_already_used():
    """An allocation created on this tick already counts against the cap."""
    allocations = [
        make_allocation(
            1,
            depot_id="depot-gazipur",
            station_id="station-mirpur",
            route_id="route-gazipur-mirpur",
            fuel_type="DIESEL",
            quantity=11000.0,
            created_tick=5,
            status="IN_TRANSIT",
        )
    ]
    depots = [
        make_depot(
            "depot-gazipur",
            dispatch_capacity_per_tick=12000.0,
            inventory={f: 50000.0 for f in FUELS},
        )
    ]
    result = gate_plan(
        world(depots=depots, allocations=allocations),
        [planned(quantity=2000.0)],
    )
    assert result.accepted_count == 0
    assert result.rejected[0].code == "DISPATCH_CAPACITY"
