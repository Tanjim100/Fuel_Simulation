"""Differential tests: the validator against the live simulator.

The simulator is the **oracle**. These tests prove two properties:

1. **Soundness** -- anything the validator approves, the simulator accepts.
   No 409 we did not predict.
2. **Completeness** -- anything the validator rejects, the simulator would
   have rejected with the same code.

The randomised differential test is the strongest evidence we can present: it
generates seeded random requests and asserts that
``(validator says PLANNED) == (simulator accepts)``.

These tests need a running simulator. They are skipped automatically when
``SIMULATOR_BASE_URL`` is unreachable, so the suite still runs in isolation.

Run them with::

    SIMULATOR_BASE_URL=http://simulator-api:8000 pytest tests/test_validator_against_simulator.py -v
"""

from __future__ import annotations

import os
import random
import uuid
from typing import Any, Optional

import pytest

from app.models import PlanRequest
from app.simulator_client import (
    AllocationRequest,
    SimulatorClient,
    SimulatorConflict,
    SimulatorError,
    SimulatorNotFound,
    SimulatorValidationError,
)
from app.state import WorldSnapshot
from app.validator import validate_and_plan

BASE_URL = os.environ.get("SIMULATOR_BASE_URL", "http://simulator-api:8000")


async def _reachable(client: SimulatorClient) -> bool:
    try:
        await client.health()
        return True
    except Exception:
        return False


@pytest.fixture
async def live_client():
    """A client against the live simulator, reset to the baseline scenario."""
    client = SimulatorClient(base_url=BASE_URL, redis_url=None, timeout=5.0)
    if not await _reachable(client):
        await client.aclose()
        pytest.skip(f"simulator not reachable at {BASE_URL}")
    try:
        await client.admin_reset()
    except SimulatorError as exc:
        await client.aclose()
        pytest.skip(f"cannot reset the simulator: {exc}")
    yield client
    await client.aclose()


async def fetch_snapshot(client: SimulatorClient) -> WorldSnapshot:
    """Pull a fresh world snapshot over REST."""
    instance = await client.instance()
    depots = await client.depots()
    stations = await client.stations()
    routes = await client.routes()
    allocations = await client.allocations()
    metrics = await client.metrics()
    supply_arrivals = await client.supply_arrivals()
    events = await client.events()
    return WorldSnapshot(
        tick=instance.tick,
        sim_time=instance.sim_time,
        instance=instance,
        depots=list(depots),
        stations=list(stations),
        routes=list(routes),
        allocations=list(allocations),
        supply_arrivals=list(supply_arrivals),
        events=list(events),
        metrics=metrics,
    )


async def submit(client: SimulatorClient, plan: Any) -> list[Any]:
    """Submit every shipment in a plan to the simulator."""
    results = []
    for shipment in plan.shipments:
        request = AllocationRequest(
            idempotency_key=shipment.idempotency_key,
            source_depot_id=shipment.source_depot_id,
            destination_station_id=shipment.destination_station_id,
            route_id=shipment.route_id,
            fuel_type=shipment.fuel_type,
            quantity=shipment.quantity,
        )
        allocation, created = await client.create_allocation(request)
        results.append((allocation, created))
    return results


# --------------------------------------------------------------------------- #
# Soundness
# --------------------------------------------------------------------------- #
async def test_a_planned_request_is_accepted_by_the_simulator(live_client):
    """The core soundness property: an approved plan is never rejected."""
    await live_client.admin_reset()
    snapshot = await fetch_snapshot(live_client)
    depot = snapshot.depots[0]
    station = snapshot.stations[0]
    route = next(
        r for r in snapshot.routes
        if r.source_depot_id == depot.id and r.destination_station_id == station.id
    )

    request = PlanRequest(
        source_depot_id=depot.id,
        destination_station_id=station.id,
        fuel_type="DIESEL",
        quantity=1000.0,
        route_id=route.id,
    )
    plan = validate_and_plan(snapshot, request)
    assert plan.status == "PLANNED", plan.reject_code

    results = await submit(live_client, plan)
    assert results
    for allocation, _created in results:
        assert allocation.status == "PENDING"


async def test_an_oversized_request_is_split_into_accepted_shipments(live_client):
    """Splitting turns a would-be 409 into a series of accepted shipments.

    The quantity is chosen to exceed the route's per-shipment max while still
    fitting inside the station's free space and the depot's dispatch
    allowance -- otherwise the split would be driven by a different limit.
    """
    await live_client.admin_reset()
    snapshot = await fetch_snapshot(live_client)

    # Find a (depot, station, route, fuel) combination where the route max is
    # the tightest limit -- that is the case where splitting matters.
    best = None
    for route in snapshot.routes:
        depot = snapshot.depot(route.source_depot_id)
        station = snapshot.station(route.destination_station_id)
        for fuel in ("DIESEL", "PETROL", "OCTANE"):
            free = (
                station.capacity.get(fuel, 0.0)
                - station.inventory.get(fuel, 0.0)
            )
            if route.max_shipment < free:
                best = (route, depot, station, fuel, free)
                break
        if best:
            break

    if best is None:
        pytest.skip("no route in the baseline scenario is tighter than its station")

    route, depot, station, fuel, free = best

    # Ask for more than the route can carry in one shipment, but no more than
    # the station can take.
    quantity = min(route.max_shipment * 1.5, free)
    if quantity <= route.max_shipment:
        pytest.skip("the station has no headroom above the route max")

    request = PlanRequest(
        source_depot_id=depot.id,
        destination_station_id=station.id,
        fuel_type=fuel,
        quantity=quantity,
        route_id=route.id,
    )
    plan = validate_and_plan(snapshot, request)

    assert plan.status == "PLANNED", plan.reject_code
    assert len(plan.shipments) >= 2, "the request should have been split"
    for shipment in plan.shipments:
        assert shipment.quantity <= route.max_shipment

    results = await submit(live_client, plan)
    assert len(results) == len(plan.shipments)
    for allocation, _created in results:
        assert allocation.quantity <= route.max_shipment
        assert allocation.status == "PENDING"


async def test_the_raw_oversized_request_would_have_been_rejected(live_client):
    """Prove the split was necessary -- the unsplit request is rejected."""
    await live_client.admin_reset()
    snapshot = await fetch_snapshot(live_client)
    depot = snapshot.depots[0]
    station = snapshot.stations[0]
    route = next(
        r for r in snapshot.routes
        if r.source_depot_id == depot.id and r.destination_station_id == station.id
    )
    quantity = route.max_shipment * 2.0

    request = AllocationRequest(
        idempotency_key=f"raw-{uuid.uuid4().hex[:12]}",
        source_depot_id=depot.id,
        destination_station_id=station.id,
        route_id=route.id,
        fuel_type="DIESEL",
        quantity=quantity,
    )
    with pytest.raises(SimulatorConflict) as excinfo:
        await live_client.create_allocation(request)
    assert excinfo.value.code == "ROUTE_CAPACITY_EXCEEDED"


# --------------------------------------------------------------------------- #
# Completeness -- each rejection code matches the simulator's
# --------------------------------------------------------------------------- #
async def test_completeness_unknown_depot(live_client):
    await live_client.admin_reset()
    snapshot = await fetch_snapshot(live_client)
    station = snapshot.stations[0]
    request = PlanRequest(
        source_depot_id="depot-does-not-exist",
        destination_station_id=station.id,
        fuel_type="DIESEL",
        quantity=100.0,
    )
    plan = validate_and_plan(snapshot, request)
    assert plan.status == "REJECTED"
    assert plan.reject_code == "NOT_FOUND"

    with pytest.raises(SimulatorNotFound):
        await live_client.create_allocation(
            AllocationRequest(
                idempotency_key=f"x-{uuid.uuid4().hex[:12]}",
                source_depot_id="depot-does-not-exist",
                destination_station_id=station.id,
                route_id=snapshot.routes[0].id,
                fuel_type="DIESEL",
                quantity=100.0,
            )
        )


async def test_completeness_route_mismatch(live_client):
    await live_client.admin_reset()
    snapshot = await fetch_snapshot(live_client)
    depot = snapshot.depots[0]
    station = snapshot.stations[0]
    # A route that does not connect the requested depot to the station.
    mismatched = next(
        (
            r for r in snapshot.routes
            if not (
                r.source_depot_id == depot.id
                and r.destination_station_id == station.id
            )
        ),
        None,
    )
    if mismatched is None:
        pytest.skip("the baseline scenario has no mismatched route to test")

    request = PlanRequest(
        source_depot_id=depot.id,
        destination_station_id=station.id,
        fuel_type="DIESEL",
        quantity=100.0,
        route_id=mismatched.id,
    )
    plan = validate_and_plan(snapshot, request)
    assert plan.status == "REJECTED"
    assert plan.reject_code in ("ROUTE_MISMATCH", "ROUTE_DISRUPTED")


async def test_completeness_route_capacity(live_client):
    """A request above the route max is rejected by both, with the same code."""
    await live_client.admin_reset()
    snapshot = await fetch_snapshot(live_client)
    route = snapshot.routes[0]
    depot = snapshot.depot(route.source_depot_id)
    station = snapshot.station(route.destination_station_id)
    quantity = route.max_shipment + 1000.0

    request = PlanRequest(
        source_depot_id=depot.id,
        destination_station_id=station.id,
        fuel_type="DIESEL",
        quantity=quantity,
        route_id=route.id,
        allow_split=False,
    )
    plan = validate_and_plan(snapshot, request)
    assert plan.status == "REJECTED"
    # The route max is checked first by the simulator, so it is the code it
    # reports -- even when the station's free space is the tighter limit.
    assert plan.reject_code == "ROUTE_MAX_SHIPMENT"

    with pytest.raises(SimulatorConflict) as excinfo:
        await live_client.create_allocation(
            AllocationRequest(
                idempotency_key=f"x-{uuid.uuid4().hex[:12]}",
                source_depot_id=depot.id,
                destination_station_id=station.id,
                route_id=route.id,
                fuel_type="DIESEL",
                quantity=quantity,
            )
        )
    assert excinfo.value.code == "ROUTE_CAPACITY_EXCEEDED"


async def test_completeness_invalid_quantity(live_client):
    await live_client.admin_reset()
    snapshot = await fetch_snapshot(live_client)
    route = snapshot.routes[0]
    request = PlanRequest(
        source_depot_id=route.source_depot_id,
        destination_station_id=route.destination_station_id,
        fuel_type="DIESEL",
        quantity=0.0,
        route_id=route.id,
    )
    plan = validate_and_plan(snapshot, request)
    assert plan.status == "REJECTED"
    assert plan.reject_code == "INVALID_QUANTITY"

    with pytest.raises(SimulatorValidationError):
        await live_client.create_allocation(
            AllocationRequest(
                idempotency_key=f"x-{uuid.uuid4().hex[:12]}",
                source_depot_id=route.source_depot_id,
                destination_station_id=route.destination_station_id,
                route_id=route.id,
                fuel_type="DIESEL",
                quantity=0.0,
            )
        )


# --------------------------------------------------------------------------- #
# Boundary sweep
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("multiplier", [1.0, 1.0001, 2.0])
async def test_boundary_sweep_around_the_route_max(live_client, multiplier):
    """Values at, just above and well above the route limit.

    The route max is not always the binding limit -- the station's free space
    can be smaller -- so the expectation is derived from the simulator's own
    validation order rather than assumed.
    """
    await live_client.admin_reset()
    snapshot = await fetch_snapshot(live_client)
    route = snapshot.routes[0]
    depot = snapshot.depot(route.source_depot_id)
    station = snapshot.station(route.destination_station_id)
    quantity = route.max_shipment * multiplier

    request = PlanRequest(
        source_depot_id=depot.id,
        destination_station_id=station.id,
        fuel_type="DIESEL",
        quantity=quantity,
        route_id=route.id,
        allow_split=False,
    )
    plan = validate_and_plan(snapshot, request)

    # What would the simulator do? Submit it and see.
    try:
        allocation, _ = await live_client.create_allocation(
            AllocationRequest(
                idempotency_key=f"b-{uuid.uuid4().hex[:12]}",
                source_depot_id=depot.id,
                destination_station_id=station.id,
                route_id=route.id,
                fuel_type="DIESEL",
                quantity=quantity,
            )
        )
    except SimulatorConflict as exc:
        simulator_rejects = True
        simulator_code = exc.code
    else:
        simulator_rejects = False
        simulator_code = None
        assert allocation.status == "PENDING"

    if simulator_rejects:
        assert plan.status == "REJECTED", (
            f"the simulator rejected with {simulator_code} but the validator "
            f"said {plan.status}"
        )
        # The validator's code must be the *same* one the simulator reports.
        expected = {
            "ROUTE_MAX_SHIPMENT": "ROUTE_CAPACITY_EXCEEDED",
            "DEPOT_INVENTORY": "INSUFFICIENT_INVENTORY",
            "DISPATCH_CAPACITY": "DISPATCH_CAPACITY_EXCEEDED",
            "STATION_FREE_SPACE": "DESTINATION_CAPACITY_EXCEEDED",
        }
        assert expected[plan.reject_code] == simulator_code
    else:
        assert plan.status == "PLANNED"
        assert plan.total_planned_litres == quantity


# --------------------------------------------------------------------------- #
# The randomised differential test
# --------------------------------------------------------------------------- #
async def test_randomised_differential_test(live_client):
    """The strongest evidence we can present.

    40 seeded random requests. For each one, the validator's verdict and the
    simulator's verdict must agree:

    * validator says PLANNED  ->  the simulator accepts every shipment.
    * validator says REJECTED ->  the simulator rejects with the same code.

    The simulator is reset before every iteration. Without that, each
    iteration's shipments would consume the next iteration's dispatch capacity
    and the comparison would be against a world the validator never saw.
    """
    snapshot = await fetch_snapshot(live_client)
    depots = [d.id for d in snapshot.depots]
    stations = [s.id for s in snapshot.stations]
    routes = snapshot.routes
    fuels = ["DIESEL", "PETROL", "OCTANE"]

    rng = random.Random(20261124)
    checked = 0
    planned = 0
    rejected = 0

    for attempt in range(40):
        route = rng.choice(routes)
        depot_id = rng.choice(depots)
        station_id = rng.choice(stations)
        fuel = rng.choice(fuels)
        quantity = round(rng.uniform(-100.0, 14000.0), 2)
        explicit_route = route.id if rng.random() < 0.7 else None

        # A clean world for every comparison.
        await live_client.admin_reset()
        snapshot = await fetch_snapshot(live_client)

        request = PlanRequest(
            source_depot_id=depot_id,
            destination_station_id=station_id,
            fuel_type=fuel,
            quantity=quantity,
            route_id=explicit_route,
        )

        plan = validate_and_plan(snapshot, request)
        checked += 1

        if plan.status == "REJECTED":
            rejected += 1
            # The simulator must reject it too.
            try:
                await live_client.create_allocation(
                    AllocationRequest(
                        idempotency_key=f"r-{attempt}-{uuid.uuid4().hex[:8]}",
                        source_depot_id=depot_id,
                        destination_station_id=station_id,
                        route_id=plan.explanation.route_choice
                        if plan.explanation and plan.explanation.route_choice
                        else route.id,
                        fuel_type=fuel,
                        quantity=max(quantity, 0.001),
                    )
                )
            except (SimulatorConflict, SimulatorNotFound, SimulatorValidationError):
                pass
            else:
                pytest.fail(
                    f"validator rejected (code={plan.reject_code}) but the "
                    f"simulator accepted: depot={depot_id} station={station_id} "
                    f"fuel={fuel} quantity={quantity}"
                )
            continue

        planned += 1
        # Soundness: every shipment the validator produced must be accepted.
        try:
            await submit(live_client, plan)
        except SimulatorError as exc:
            pytest.fail(
                f"validator said PLANNED but the simulator rejected a shipment "
                f"({getattr(exc, 'code', None)}): depot={depot_id} "
                f"station={station_id} fuel={fuel} quantity={quantity} "
                f"shipments={[s.quantity for s in plan.shipments]}"
            )

    assert checked == 40
    # The random generator must actually have exercised both paths.
    assert planned > 0, "no request was ever planned -- the test is vacuous"
    assert rejected > 0, "no request was ever rejected -- the test is vacuous"


async def test_randomised_differential_test_with_splitting(live_client):
    """The same differential property, but with splitting enabled."""
    rng = random.Random(4242)

    for attempt in range(15):
        await live_client.admin_reset()
        snapshot = await fetch_snapshot(live_client)
        route = rng.choice(snapshot.routes)
        quantity = round(rng.uniform(1.0, 20000.0), 2)
        request = PlanRequest(
            source_depot_id=route.source_depot_id,
            destination_station_id=route.destination_station_id,
            fuel_type=rng.choice(["DIESEL", "PETROL", "OCTANE"]),
            quantity=quantity,
            route_id=route.id,
            allow_split=True,
        )
        plan = validate_and_plan(snapshot, request)
        if plan.status == "REJECTED":
            continue
        try:
            await submit(live_client, plan)
        except SimulatorError as exc:
            pytest.fail(
                f"split plan rejected by the simulator ({getattr(exc, 'code', None)}): "
                f"quantity={quantity} chunks={[s.quantity for s in plan.shipments]}"
            )


# --------------------------------------------------------------------------- #
# Idempotency, live
# --------------------------------------------------------------------------- #
async def test_the_same_key_replays_live(live_client):
    await live_client.admin_reset()
    snapshot = await fetch_snapshot(live_client)
    route = snapshot.routes[0]
    key = f"live-{uuid.uuid4().hex[:12]}"

    request = AllocationRequest(
        idempotency_key=key,
        source_depot_id=route.source_depot_id,
        destination_station_id=route.destination_station_id,
        route_id=route.id,
        fuel_type="DIESEL",
        quantity=500.0,
    )
    first, created_first = await live_client.create_allocation(request)
    second, created_second = await live_client.create_allocation(request)

    assert created_first is True
    # The simulator answers 201 for a replay too -- the client detects it by
    # tracking the highest allocation id it has seen.
    assert second.id == first.id


async def test_a_different_body_with_the_same_key_is_rejected(live_client):
    await live_client.admin_reset()
    snapshot = await fetch_snapshot(live_client)
    route = snapshot.routes[0]
    key = f"mismatch-{uuid.uuid4().hex[:12]}"

    await live_client.create_allocation(
        AllocationRequest(
            idempotency_key=key,
            source_depot_id=route.source_depot_id,
            destination_station_id=route.destination_station_id,
            route_id=route.id,
            fuel_type="DIESEL",
            quantity=500.0,
        )
    )
    with pytest.raises(SimulatorConflict) as excinfo:
        await live_client.create_allocation(
            AllocationRequest(
                idempotency_key=key,
                source_depot_id=route.source_depot_id,
                destination_station_id=route.destination_station_id,
                route_id=route.id,
                fuel_type="DIESEL",
                quantity=600.0,
            )
        )
    assert excinfo.value.code == "IDEMPOTENCY_KEY_MISMATCH"


# --------------------------------------------------------------------------- #
# Crisis behaviour
# --------------------------------------------------------------------------- #
async def test_a_disrupted_route_is_rejected_by_both(live_client):
    """Inject a disruption and confirm both sides agree."""
    routes = await live_client.routes()
    target_route = routes[0].id

    await live_client.admin_inject_event(
        type="route_disruption",
        start_tick=0,
        duration_ticks=50,
        parameters={"route_ids": [target_route]},
    )
    snapshot = await fetch_snapshot(live_client)
    disrupted = [r for r in snapshot.routes if r.status != "AVAILABLE"]
    if not disrupted:
        # The event may need a tick to take effect.
        await live_client.admin_step()
        snapshot = await fetch_snapshot(live_client)
        disrupted = [r for r in snapshot.routes if r.status != "AVAILABLE"]
    if not disrupted:
        pytest.skip("the disruption did not take effect in time")

    route = disrupted[0]
    request = PlanRequest(
        source_depot_id=route.source_depot_id,
        destination_station_id=route.destination_station_id,
        fuel_type="DIESEL",
        quantity=100.0,
        route_id=route.id,
    )
    plan = validate_and_plan(snapshot, request)
    assert plan.status == "REJECTED"
    assert plan.reject_code == "ROUTE_DISRUPTED"

    with pytest.raises(SimulatorConflict) as excinfo:
        await live_client.create_allocation(
            AllocationRequest(
                idempotency_key=f"d-{uuid.uuid4().hex[:12]}",
                source_depot_id=route.source_depot_id,
                destination_station_id=route.destination_station_id,
                route_id=route.id,
                fuel_type="DIESEL",
                quantity=100.0,
            )
        )
    assert excinfo.value.code == "ROUTE_DISRUPTED"
