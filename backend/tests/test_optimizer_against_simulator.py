"""Differential tests: the optimiser against the live simulator.

The simulator is the **oracle**. These tests prove the property that matters
most in Phase 2:

    **Every shipment the optimiser proposes, after gating, is accepted by the
    simulator.** No 409 we did not predict.

That is what makes it safe to let the optimiser write to the simulator. The
optimiser decides what is *optimal*; the validator decides what is *legal*;
the simulator decides what is *true*. A disagreement between the first two
and the third is the failure mode this file exists to catch.

The tests need a running simulator. They are skipped automatically when
``SIMULATOR_BASE_URL`` is unreachable, so the suite still runs in isolation.

Run them with::

    SIMULATOR_BASE_URL=http://simulator-api:8000 pytest \
        tests/test_optimizer_against_simulator.py -v
"""

from __future__ import annotations

import os
import random
import uuid
from typing import Any, Optional

import pytest

from app.dispatch import gate_plan
from app.forecast import build_forecast
from app.optimizer import OptimizerConfig, optimize
from app.simulator_client import (
    AllocationRequest,
    SimulatorClient,
    SimulatorError,
)
from app.state import WorldSnapshot

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


async def submit_gated(client: SimulatorClient, gated: Any) -> list[Any]:
    """Submit gated shipments to the simulator."""
    results = []
    for item in gated:
        shipment = item.shipment
        allocation, created = await client.create_allocation(
            AllocationRequest(
                idempotency_key=shipment.idempotency_key,
                source_depot_id=shipment.source_depot_id,
                destination_station_id=shipment.destination_station_id,
                route_id=shipment.route_id,
                fuel_type=shipment.fuel_type,
                quantity=shipment.quantity,
            )
        )
        results.append((allocation, created))
    return results


def plan_for(snapshot: WorldSnapshot, **kwargs) -> Any:
    """Build a forecast and optimise against it."""
    horizon = kwargs.pop("horizon", 16)
    forecast = build_forecast(snapshot, horizon=horizon)
    return optimize(snapshot, forecast, **kwargs)


async def advance(client: SimulatorClient, ticks: int = 64) -> WorldSnapshot:
    """Step the simulator so demand actually depletes the stations.

    At tick 0 every station is full, so there is genuinely nothing to
    dispatch -- the optimiser returning an empty plan there is *correct*, and
    a test that asserted otherwise would be asserting a bug.

    The baseline scenario is well-stocked for the first ~60 ticks (measured:
    the first station falls short at tick 64), so the world must be stepped
    well past that before there is anything to optimise.
    """
    for _ in range(ticks):
        await client.admin_step()
    return await fetch_snapshot(client)


async def snapshot_with_demand(client: SimulatorClient) -> WorldSnapshot:
    """A snapshot in which at least one station actually needs fuel."""
    snapshot = await advance(client, 64)
    forecast = build_forecast(snapshot, horizon=16)
    short = [
        r for r in forecast.risk if r.expected_unmet_litres > 1.0
    ]
    if not short:
        pytest.skip("no station is short of fuel after 64 ticks")
    return snapshot


# --------------------------------------------------------------------------- #
# The core soundness property
# --------------------------------------------------------------------------- #
async def test_every_gated_shipment_is_accepted_by_the_simulator(live_client):
    """The optimiser's plan, after gating, is always accepted.

    This is the single most important test in Phase 2. If it fails, the
    optimiser must not be allowed to write to the simulator.
    """
    await live_client.admin_reset()
    snapshot = await snapshot_with_demand(live_client)

    result = plan_for(snapshot)
    assert result.shipments, "the optimiser found nothing to do"

    gated = gate_plan(snapshot, result.shipments_now)
    assert gated.rejected_count == 0, (
        "the gate rejected shipments the optimiser proposed: "
        f"{[r.code for r in gated.rejected]}"
    )
    assert gated.accepted_count > 0

    results = await submit_gated(live_client, gated.accepted)
    assert len(results) == gated.accepted_count
    for allocation, _created in results:
        assert allocation.status == "PENDING"


async def test_the_whole_plan_is_accepted_not_just_the_first_shipment(live_client):
    """Every shipment, including the later waves."""
    await live_client.admin_reset()
    snapshot = await snapshot_with_demand(live_client)

    result = plan_for(snapshot, horizon=32)
    gated = gate_plan(snapshot, result.shipments)

    if not gated.accepted:
        pytest.skip("the optimiser produced no executable shipment")

    results = await submit_gated(live_client, gated.accepted)
    assert len(results) == len(gated.accepted)


async def test_the_dispatch_cap_is_never_exceeded_in_one_tick(live_client):
    """No ``DISPATCH_CAPACITY_EXCEEDED`` -- the cap is per depot, per tick."""
    await live_client.admin_reset()
    snapshot = await snapshot_with_demand(live_client)

    result = plan_for(snapshot)
    gated = gate_plan(snapshot, result.shipments_now)
    assert gated.rejected_count == 0

    results = await submit_gated(live_client, gated.accepted)
    assert len(results) == gated.accepted_count


async def test_the_depot_inventory_is_never_oversubscribed(live_client):
    """No ``INSUFFICIENT_INVENTORY``."""
    await live_client.admin_reset()
    snapshot = await snapshot_with_demand(live_client)

    result = plan_for(snapshot, horizon=32)
    gated = gate_plan(snapshot, result.shipments)
    assert gated.rejected_count == 0

    results = await submit_gated(live_client, gated.accepted)
    assert len(results) == len(gated.accepted)


# --------------------------------------------------------------------------- #
# Resilience
# --------------------------------------------------------------------------- #
async def test_a_disrupted_route_is_never_used(live_client):
    """Inject a disruption; the plan must route around it."""
    await live_client.admin_reset()
    snapshot = await snapshot_with_demand(live_client)
    routes = await live_client.routes()
    target = routes[0].id

    await live_client.admin_inject_event(
        type="route_disruption",
        start_tick=0,
        duration_ticks=50,
        parameters={"route_ids": [target]},
    )
    await live_client.admin_step()
    snapshot = await fetch_snapshot(live_client)

    disrupted = {r.id for r in snapshot.routes if r.status != "AVAILABLE"}
    if not disrupted:
        pytest.skip("the disruption did not take effect in time")

    result = plan_for(snapshot)
    for shipment in result.shipments:
        assert shipment.route_id not in disrupted

    gated = gate_plan(snapshot, result.shipments_now)
    assert gated.rejected_count == 0
    results = await submit_gated(live_client, gated.accepted)
    assert len(results) == gated.accepted_count


async def test_a_dual_connected_station_is_rerouted(live_client):
    """Disrupt a station's primary route; the plan must use the other one."""
    await live_client.admin_reset()
    snapshot = await snapshot_with_demand(live_client)

    # Find a station that is BOTH served by more than one route AND short of
    # fuel -- a station with headroom needs no shipment at all, so asserting
    # that it was rerouted would be asserting nothing.
    forecast = build_forecast(snapshot, horizon=16)
    short_ids = {
        r.station_id for r in forecast.risk if r.expected_unmet_litres > 1.0
    }

    dual = None
    for station in snapshot.stations:
        if station.id not in short_ids:
            continue
        serving = snapshot.routes_for_station(station.id)
        if len(serving) >= 2:
            dual = (station, serving)
            break
    if dual is None:
        pytest.skip(
            "no short dual-connected station in the baseline scenario"
        )

    station, serving = dual
    # Disrupt the *fastest* route -- the one the optimiser would prefer.
    primary = min(serving, key=lambda r: (r.transit_ticks, r.id))

    await live_client.admin_inject_event(
        type="route_disruption",
        start_tick=0,
        duration_ticks=50,
        parameters={"route_ids": [primary.id]},
    )
    await live_client.admin_step()
    snapshot = await fetch_snapshot(live_client)

    disrupted = {r.id for r in snapshot.routes if r.status != "AVAILABLE"}
    if primary.id not in disrupted:
        pytest.skip("the disruption did not take effect in time")

    result = plan_for(snapshot)
    used = {s.route_id for s in result.shipments}
    assert primary.id not in used, "the optimiser used the disrupted route"

    alternatives = {r.id for r in serving} - disrupted
    assert alternatives, "the station has no alternative route to test"
    assert used & alternatives, (
        "the optimiser found no alternative route for the short station"
    )

    # And the rerouted shipments must still be legal.
    gated = gate_plan(snapshot, result.shipments_now)
    assert gated.rejected_count == 0


async def test_a_demand_spike_does_not_break_the_plan(live_client):
    """A spike changes the forecast; the plan must still be legal."""
    await live_client.admin_reset()
    await live_client.admin_inject_event(
        type="demand_spike",
        start_tick=0,
        duration_ticks=5,
        parameters={"multiplier": 1.8},
    )
    snapshot = await snapshot_with_demand(live_client)

    result = plan_for(snapshot)
    gated = gate_plan(snapshot, result.shipments_now)
    assert gated.rejected_count == 0

    results = await submit_gated(live_client, gated.accepted)
    assert len(results) == gated.accepted_count


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #
async def test_the_multi_tick_keys_are_unique_and_replayable(live_client):
    """Two shipments in one tick differ; a replay returns the same allocation."""
    await live_client.admin_reset()
    snapshot = await snapshot_with_demand(live_client)

    result = plan_for(snapshot)
    gated = gate_plan(snapshot, result.shipments_now)
    if len(gated.accepted) < 1:
        pytest.skip("the optimiser produced no shipment")

    keys = [item.shipment.idempotency_key for item in gated.accepted]
    assert len(keys) == len(set(keys)), "two shipments share an idempotency key"

    # Replay the first shipment -- the simulator must return the same
    # allocation rather than creating a second one.
    first = gated.accepted[0].shipment
    request = AllocationRequest(
        idempotency_key=first.idempotency_key,
        source_depot_id=first.source_depot_id,
        destination_station_id=first.destination_station_id,
        route_id=first.route_id,
        fuel_type=first.fuel_type,
        quantity=first.quantity,
    )
    allocation_a, _ = await live_client.create_allocation(request)
    allocation_b, _ = await live_client.create_allocation(request)
    assert allocation_a.id == allocation_b.id


async def test_the_key_embeds_the_dispatch_tick(live_client):
    """A shipment for tick N+1 uses ``alloc-t{N+1}-...``."""
    await live_client.admin_reset()
    snapshot = await snapshot_with_demand(live_client)

    result = plan_for(snapshot, horizon=32)
    gated = gate_plan(snapshot, result.shipments)

    for item in gated.accepted:
        expected = f"alloc-t{item.planned.dispatch_tick}-"
        assert item.shipment.idempotency_key.startswith(expected), (
            f"key {item.shipment.idempotency_key} does not embed the "
            f"dispatch tick {item.planned.dispatch_tick}"
        )


async def test_a_future_tick_shipment_is_not_written_now(live_client):
    """Only current-tick shipments may be written."""
    await live_client.admin_reset()
    snapshot = await snapshot_with_demand(live_client)

    result = plan_for(snapshot, horizon=32)
    now_tick = snapshot.tick

    for shipment in result.shipments_now:
        assert shipment.dispatch_tick == now_tick
    for shipment in result.shipments_later:
        assert shipment.dispatch_tick != now_tick


# --------------------------------------------------------------------------- #
# The randomised differential test
# --------------------------------------------------------------------------- #
async def test_the_randomised_differential_test(live_client):
    """12 seeded random configurations; every gated shipment is accepted.

    The simulator is reset before every iteration so each iteration is judged
    against a world the optimiser actually saw. Each iteration also steps the
    world forward, so the optimiser faces a *depleted* station rather than a
    full one -- an empty plan against a full station is correct but proves
    nothing.

    Twelve iterations rather than thirty: each one advances the simulator by
    up to 80 ticks over REST, so thirty would dominate the suite's runtime
    for no extra coverage.
    """
    rng = random.Random(20261124)
    planned_count = 0
    shipped = 0

    for attempt in range(12):
        await live_client.admin_reset()
        # Step forward by a random number of ticks so the world varies.
        for _ in range(rng.choice([64, 72, 80, 88])):
            await live_client.admin_step()
        snapshot = await fetch_snapshot(live_client)

        # Randomise the horizon so the plan spans different tick counts.
        horizon = rng.choice([4, 8, 16, 24])
        result = plan_for(snapshot, horizon=horizon)
        planned_count += 1

        gated = gate_plan(snapshot, result.shipments_now)
        if gated.rejected_count:
            pytest.fail(
                f"the gate rejected {gated.rejected_count} shipment(s) the "
                f"optimiser proposed: "
                f"{[(r.code, r.quantity) for r in gated.rejected]}"
            )

        if not gated.accepted:
            continue

        try:
            results = await submit_gated(live_client, gated.accepted)
        except SimulatorError as exc:
            pytest.fail(
                f"the simulator rejected a gated shipment "
                f"({getattr(exc, 'code', None)}) on attempt {attempt}: "
                f"{[(i.shipment.route_id, i.shipment.quantity) for i in gated.accepted]}"
            )
        shipped += len(results)

    assert planned_count == 12
    assert shipped > 0, "no shipment was ever sent -- the test is vacuous"


async def test_the_randomised_differential_test_with_several_horizons(live_client):
    """The same property across a spread of horizons."""
    for horizon in (4, 8, 16, 32, 48):
        await live_client.admin_reset()
        for _ in range(64):
            await live_client.admin_step()
        snapshot = await fetch_snapshot(live_client)

        result = plan_for(snapshot, horizon=horizon)
        gated = gate_plan(snapshot, result.shipments_now)
        assert gated.rejected_count == 0, (
            f"horizon {horizon}: the gate rejected "
            f"{[r.code for r in gated.rejected]}"
        )

        if gated.accepted:
            results = await submit_gated(live_client, gated.accepted)
            assert len(results) == len(gated.accepted)


async def test_the_plan_is_deterministic_against_the_live_world(live_client):
    """Two runs against the same world produce the same plan."""
    await live_client.admin_reset()
    snapshot = await fetch_snapshot(live_client)

    first = plan_for(snapshot)
    second = plan_for(snapshot)
    assert first.to_dict() == second.to_dict()
