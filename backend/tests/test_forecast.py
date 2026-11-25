"""Unit tests for the pure forecast module.

No simulator required -- the golden fixture was captured live and committed.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from app.forecast import (
    DEMAND_TOLERANCE,
    FORECAST_START_TIME,
    FUEL_TYPES,
    PROFILES,
    TICKS_PER_DAY,
    build_forecast,
    expected_demand_litres,
    forecast_series,
    hour_factor,
    inbound_schedule,
    project_world,
    seeded_rng,
    sim_time_for_tick,
    stockout_risk,
    supply_schedule,
)

FIXTURE = Path(__file__).parent / "fixtures" / "demand_golden.json"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def golden() -> dict:
    with FIXTURE.open() as handle:
        return json.load(handle)


@pytest.fixture(scope="module")
def golden_rows(golden: dict) -> list[dict]:
    return golden["rows"]


class _Station:
    """A minimal stand-in for the client's Station model."""

    def __init__(self, id, profile, region_id, multiplier=1.0, inventory=None, capacity=None):
        self.id = id
        self.demand_profile = profile
        self.region_id = region_id
        self.demand_multiplier = multiplier
        self.inventory = inventory or {f: 1000.0 for f in FUEL_TYPES}
        self.capacity = capacity or {f: 20000.0 for f in FUEL_TYPES}
        self.status = "OPEN"


class _Depot:
    def __init__(self, id, inventory=None, cap=12000.0, status="OPEN"):
        self.id = id
        self.inventory = inventory or {f: 50000.0 for f in FUEL_TYPES}
        self.capacity = {f: 100000.0 for f in FUEL_TYPES}
        self.dispatch_capacity_per_tick = cap
        self.status = status


class _Route:
    def __init__(self, id, src, dst, transit=2, max_shipment=7000.0, status="AVAILABLE"):
        self.id = id
        self.source_depot_id = src
        self.destination_station_id = dst
        self.transit_ticks = transit
        self.max_shipment = max_shipment
        self.status = status


class _Allocation:
    def __init__(self, id, key, src, dst, route, fuel, qty, created_tick, status="IN_TRANSIT"):
        self.id = id
        self.idempotency_key = key
        self.source_depot_id = src
        self.destination_station_id = dst
        self.route_id = route
        self.fuel_type = fuel
        self.quantity = qty
        self.created_tick = created_tick
        self.status = status
        self.departure_tick = created_tick
        self.expected_arrival_tick = created_tick + 2
        self.actual_arrival_tick = None


class _Supply:
    def __init__(self, id, depot, fuel, qty, planned_tick, actual_tick=None, status="SCHEDULED"):
        self.id = id
        self.depot_id = depot
        self.fuel_type = fuel
        self.quantity = qty
        self.planned_tick = planned_tick
        self.actual_tick = actual_tick
        self.status = status


class _Snapshot:
    def __init__(self, tick=0, stations=(), depots=(), routes=(), allocations=(), supplies=()):
        self.tick = tick
        self.stations = list(stations)
        self.depots = list(depots)
        self.routes = list(routes)
        self.allocations = list(allocations)
        self.supply_arrivals = list(supplies)
        self.events = []
        self.instance = None

    def depot(self, depot_id):
        return next((d for d in self.depots if d.id == depot_id), None)

    def station(self, station_id):
        return next((s for s in self.stations if s.id == station_id), None)

    def route(self, route_id):
        return next((r for r in self.routes if r.id == route_id), None)

    def routes_for_station(self, station_id):
        return [r for r in self.routes if r.destination_station_id == station_id]


# --------------------------------------------------------------------------- #
# The oracle: the formula reproduces the live simulator exactly
# --------------------------------------------------------------------------- #
def test_the_formula_matches_the_live_simulator_exactly(golden, golden_rows):
    """Every committed golden row must be reproduced to the last decimal."""
    seed = golden["seed"]
    start_time = golden["start_time"]

    for row in golden_rows:
        station_id = row["station_id"]
        fuel = row["fuel_type"]
        tick = row["tick"]

        # Recover the profile/region from the baseline scenario.
        if station_id == "station-mirpur":
            profile, region = "urban_high", "region-dhaka"
        elif station_id == "station-tongi":
            profile, region = "industrial", "region-dhaka"
        elif station_id == "station-karnaphuli":
            profile, region = "highway", "region-chattogram"
        else:
            profile, region = "regional", "region-chattogram"

        predicted = expected_demand_litres(
            station_id=station_id,
            fuel_type=fuel,
            profile=profile,
            region_id=region,
            demand_multiplier=1.0,
            tick=tick,
            seed=seed,
            start_time=start_time,
        )

        assert abs(predicted - row["demand_liters"]) <= DEMAND_TOLERANCE, (
            f"{station_id}/{fuel}/tick{tick}: predicted {predicted}, "
            f"simulator said {row['demand_liters']}"
        )


def test_the_golden_fixture_covers_every_station_and_fuel(golden_rows):
    stations = {r["station_id"] for r in golden_rows}
    fuels = {r["fuel_type"] for r in golden_rows}
    assert stations == {
        "station-mirpur",
        "station-tongi",
        "station-karnaphuli",
        "station-coxsbazar",
    }
    assert fuels == set(FUEL_TYPES)
    assert len(golden_rows) == 60


# --------------------------------------------------------------------------- #
# The deterministic core
# --------------------------------------------------------------------------- #
def test_the_jitter_is_deterministic():
    a = seeded_rng(12345, 7, "station-mirpur", "demand:DIESEL")
    b = seeded_rng(12345, 7, "station-mirpur", "demand:DIESEL")
    assert a.uniform(-0.1, 0.1) == b.uniform(-0.1, 0.1)


def test_the_jitter_differs_by_tick_entity_and_purpose():
    base = seeded_rng(12345, 7, "station-mirpur", "demand:DIESEL").uniform(-0.1, 0.1)
    assert base != seeded_rng(12345, 8, "station-mirpur", "demand:DIESEL").uniform(-0.1, 0.1)
    assert base != seeded_rng(12345, 7, "station-tongi", "demand:DIESEL").uniform(-0.1, 0.1)
    assert base != seeded_rng(12345, 7, "station-mirpur", "demand:PETROL").uniform(-0.1, 0.1)
    assert base != seeded_rng(54321, 7, "station-mirpur", "demand:DIESEL").uniform(-0.1, 0.1)


def test_hour_factor_boundaries():
    # industrial: 1.55 for 06:00-17:59, 0.45 otherwise
    assert hour_factor(5, "industrial") == 0.45
    assert hour_factor(6, "industrial") == 1.55
    assert hour_factor(17, "industrial") == 1.55
    assert hour_factor(18, "industrial") == 0.45

    # highway: 1.35 for 06:00-09:59 and 16:00-20:59
    assert hour_factor(5, "highway") == 0.75
    assert hour_factor(6, "highway") == 1.35
    assert hour_factor(10, "highway") == 0.75
    assert hour_factor(16, "highway") == 1.35
    assert hour_factor(21, "highway") == 0.75

    # urban_high: 1.45 for 07:00-09:59 and 16:00-20:59
    assert hour_factor(6, "urban_high") == 0.70
    assert hour_factor(7, "urban_high") == 1.45
    assert hour_factor(10, "urban_high") == 0.70
    assert hour_factor(16, "urban_high") == 1.45
    assert hour_factor(21, "urban_high") == 0.70

    # regional: 1.25 for 07:00-20:59
    assert hour_factor(6, "regional") == 0.65
    assert hour_factor(7, "regional") == 1.25
    assert hour_factor(20, "regional") == 1.25
    assert hour_factor(21, "regional") == 0.65


def test_sim_time_advances_by_tick_minutes():
    assert sim_time_for_tick(FORECAST_START_TIME, 0).hour == 0
    assert sim_time_for_tick(FORECAST_START_TIME, 2).hour == 0
    assert sim_time_for_tick(FORECAST_START_TIME, 2).minute == 30
    assert sim_time_for_tick(FORECAST_START_TIME, 4).hour == 1


def test_demand_multiplier_scales_the_forecast_linearly():
    """The simulator computes demand unrounded, so the scaling is exact.

    ``raw=True`` returns the unrounded value, matching the simulator's own
    arithmetic (it only rounds when it stores the observation).
    """
    common = dict(
        station_id="station-mirpur",
        fuel_type="DIESEL",
        profile="urban_high",
        region_id="region-dhaka",
        tick=32,
        seed=12345,
    )
    single = expected_demand_litres(demand_multiplier=1.0, raw=True, **common)
    double = expected_demand_litres(demand_multiplier=2.0, raw=True, **common)
    assert double == pytest.approx(single * 2.0, rel=1e-12)


def test_the_forecast_is_not_scaled_from_daily_liters():
    """Pin the measured realised-daily ratios.

    The Phase 1 brief claimed ~0.89 / 1.00 / 0.875 / 0.93. Live measurement
    over a full 96-tick day gave 0.944 / 1.001 / 0.966 / 0.997. The forecast
    must reproduce the *formula*, which is what these numbers assert.
    """
    stations = [
        _Station("station-mirpur", "urban_high", "region-dhaka"),
        _Station("station-tongi", "industrial", "region-dhaka"),
        _Station("station-karnaphuli", "highway", "region-chattogram"),
        _Station("station-coxsbazar", "regional", "region-chattogram"),
    ]
    series = forecast_series(
        stations=stations, seed=12345, start_tick=0, horizon=TICKS_PER_DAY
    )

    # mirpur DIESEL: measured 8022.3 over a day, daily_liters 8500 -> 0.944
    realised = sum(series[("station-mirpur", "DIESEL")])
    assert realised == pytest.approx(8022.3, abs=5.0)
    assert realised / PROFILES["urban_high"].daily_liters["DIESEL"] == pytest.approx(
        0.944, abs=0.01
    )

    # tongi DIESEL: measured 14002.9 -> 1.001
    realised = sum(series[("station-tongi", "DIESEL")])
    assert realised == pytest.approx(14002.9, abs=5.0)
    assert realised / PROFILES["industrial"].daily_liters["DIESEL"] == pytest.approx(
        1.001, abs=0.01
    )

    # karnaphuli DIESEL: measured 11110.3, and 0.980 vs daily_liters*1.08
    realised = sum(series[("station-karnaphuli", "DIESEL")])
    assert realised == pytest.approx(11110.3, abs=5.0)
    assert realised / (
        PROFILES["highway"].daily_liters["DIESEL"] * 1.08
    ) == pytest.approx(0.980, abs=0.01)

    # coxsbazar DIESEL: measured 7750.9, 0.997 vs daily_liters*1.08
    realised = sum(series[("station-coxsbazar", "DIESEL")])
    assert realised == pytest.approx(7750.9, abs=5.0)
    assert realised / (
        PROFILES["regional"].daily_liters["DIESEL"] * 1.08
    ) == pytest.approx(0.997, abs=0.01)


def test_forecast_is_pure():
    stations = [_Station("station-mirpur", "urban_high", "region-dhaka")]
    a = forecast_series(stations=stations, seed=12345, start_tick=3, horizon=8)
    b = forecast_series(stations=stations, seed=12345, start_tick=3, horizon=8)
    assert a == b


def test_forecast_series_has_exactly_horizon_entries():
    stations = [_Station("station-mirpur", "urban_high", "region-dhaka")]
    series = forecast_series(stations=stations, seed=12345, start_tick=5, horizon=13)
    assert len(series[("station-mirpur", "DIESEL")]) == 13


# --------------------------------------------------------------------------- #
# Stockout risk
# --------------------------------------------------------------------------- #
def test_stockout_risk_finds_the_stockout_tick():
    risk = stockout_risk(
        station_id="station-tongi",
        fuel_type="DIESEL",
        current_inventory=100.0,
        demand_series=[40.0] * 10,
        horizon=10,
    )
    assert risk.ticks_to_stockout == 2  # 40 + 40 consume 80, the third needs 40
    assert risk.stockout_probability > 0.9
    assert risk.expected_unmet_litres > 0


def test_stockout_probability_is_monotone_in_the_shortfall():
    """More stock must never make a stockout *more* likely.

    The probability is only informative near the boundary -- with a 10% noise
    band, a shortfall of several hundred litres is many standard deviations
    and is certain. So the probe follows the boundary.
    """
    def prob(inventory: float) -> float:
        return stockout_risk(
            station_id="s",
            fuel_type="DIESEL",
            current_inventory=inventory,
            demand_series=[100.0] * 8,
            horizon=8,
        ).stockout_probability

    # 800 L exactly meets 8 x 100 L: right on the boundary.
    assert prob(700.0) > prob(750.0) > prob(790.0) > prob(800.0)
    assert prob(900.0) < prob(800.0)
    assert prob(1200.0) == 0.0


def test_stockout_probability_is_zero_when_cover_is_far_in_excess():
    risk = stockout_risk(
        station_id="s",
        fuel_type="DIESEL",
        current_inventory=100000.0,
        demand_series=[10.0] * 8,
        horizon=8,
    )
    assert risk.stockout_probability < 0.01
    assert risk.ticks_to_stockout is None


def test_stockout_risk_confidence_labels():
    # HIGH: a stockout within 4 ticks.
    close = stockout_risk(
        station_id="s", fuel_type="DIESEL", current_inventory=10.0,
        demand_series=[100.0] * 10, horizon=10,
    )
    assert close.ticks_to_stockout == 0
    assert close.confidence == "HIGH"

    # MEDIUM: a stockout between 5 and 12 ticks out.
    mid = stockout_risk(
        station_id="s", fuel_type="DIESEL", current_inventory=650.0,
        demand_series=[100.0] * 20, horizon=20,
    )
    assert mid.ticks_to_stockout == 6
    assert mid.confidence == "MEDIUM"

    # LOW: no stockout within a short horizon.
    far = stockout_risk(
        station_id="s", fuel_type="DIESEL", current_inventory=100000.0,
        demand_series=[1.0] * 10, horizon=10,
    )
    assert far.confidence == "LOW"


def test_inbound_fuel_prevents_a_stockout():
    without = stockout_risk(
        station_id="s", fuel_type="DIESEL", current_inventory=100.0,
        demand_series=[100.0] * 6, horizon=6,
    )
    with_inbound = stockout_risk(
        station_id="s", fuel_type="DIESEL", current_inventory=100.0,
        demand_series=[100.0] * 6, inbound_by_tick={1: 500.0}, horizon=6,
    )
    assert without.ticks_to_stockout is not None
    assert with_inbound.ticks_to_stockout is None


# --------------------------------------------------------------------------- #
# The projection model -- the measured +1 lags
# --------------------------------------------------------------------------- #
def test_inbound_schedule_uses_the_arrival_lag():
    """created_tick 0 + transit 2 -> applied at tick 3, not 2."""
    snap = _Snapshot(
        tick=0,
        stations=[_Station("station-mirpur", "urban_high", "region-dhaka")],
        depots=[_Depot("depot-gazipur")],
        routes=[_Route("route-gazipur-mirpur", "depot-gazipur", "station-mirpur", 2)],
        allocations=[
            _Allocation(1, "k", "depot-gazipur", "station-mirpur",
                        "route-gazipur-mirpur", "DIESEL", 1000.0, created_tick=0)
        ],
    )
    schedule = inbound_schedule(snap)
    assert schedule[("station-mirpur", "DIESEL")] == {3: 1000.0}


def test_inbound_schedule_ignores_settled_allocations():
    snap = _Snapshot(
        tick=5,
        routes=[_Route("r", "d", "station-mirpur", 2)],
        allocations=[
            _Allocation(1, "k", "d", "station-mirpur", "r", "DIESEL", 1000.0, 0,
                        status="ARRIVED"),
            _Allocation(2, "k2", "d", "station-mirpur", "r", "DIESEL", 500.0, 4,
                        status="PENDING"),
        ],
    )
    schedule = inbound_schedule(snap)
    assert schedule[("station-mirpur", "DIESEL")] == {7: 500.0}


def test_supply_arrivals_are_applied_one_tick_late():
    """planned_tick 12 is credited at tick 13."""
    snap = _Snapshot(
        tick=0,
        depots=[_Depot("depot-gazipur")],
        supplies=[_Supply("supply-001", "depot-gazipur", "DIESEL", 18000.0, 12)],
    )
    schedule = supply_schedule(snap)
    assert schedule[("depot-gazipur", "DIESEL")] == {13: 18000.0}


def test_the_projection_applies_arrivals_one_tick_late():
    """A shipment dispatched on tick 0 lands in the station's tank at tick 3."""
    stations = [
        _Station("station-mirpur", "urban_high", "region-dhaka",
                 inventory={f: 0.0 for f in FUEL_TYPES}),
    ]
    snap = _Snapshot(
        tick=0,
        stations=stations,
        depots=[_Depot("depot-gazipur")],
        routes=[_Route("route-gazipur-mirpur", "depot-gazipur",
                       "station-mirpur", 2)],
    )
    forecast = build_forecast(snap, horizon=6)

    class _Shipment:
        dispatch_tick = 0
        route_id = "route-gazipur-mirpur"
        destination_station_id = "station-mirpur"
        fuel_type = "DIESEL"
        quantity = 1000.0

    projection = project_world(snap, forecast, [_Shipment()], horizon=6)

    inv = projection.station_inventory[("station-mirpur", "DIESEL")]
    # The fuel is available from tick 3 onwards, and demand eats it there.
    assert inv[0] <= 0.0
    assert inv[1] <= 0.0
    assert inv[2] <= 0.0
    # By tick 3 the 1000 L has landed (and demand has consumed some of it).
    assert inv[3] < 1000.0
    assert inv[3] >= 0.0


def test_the_projection_credits_arrivals_before_demand_in_the_same_tick():
    """Fuel arriving on tick T can serve tick T's demand."""
    station = _Station(
        "station-mirpur", "urban_high", "region-dhaka",
        inventory={f: 0.0 for f in FUEL_TYPES},
    )
    snap = _Snapshot(tick=0, stations=[station], depots=[_Depot("depot-gazipur")])
    forecast = build_forecast(snap, horizon=4)

    class _Shipment:
        dispatch_tick = 0
        route_id = None
        destination_station_id = "station-mirpur"
        fuel_type = "DIESEL"
        quantity = 1000.0

    projection = project_world(snap, forecast, [_Shipment()], horizon=4)
    served = projection.served[("station-mirpur", "DIESEL")]
    # No arrival before tick 3, so nothing is served in ticks 0-2.
    assert served[0] == 0.0
    assert served[1] == 0.0
    assert served[2] == 0.0


def test_the_projection_counts_spill_but_does_not_reject_it():
    """A tank too small for the delivery spills the excess -- and counts it."""
    station = _Station(
        "station-mirpur", "urban_high", "region-dhaka",
        inventory={"DIESEL": 0.0, "PETROL": 0.0, "OCTANE": 0.0},
        capacity={"DIESEL": 500.0, "PETROL": 500.0, "OCTANE": 500.0},
    )
    snap = _Snapshot(
        tick=0,
        stations=[station],
        depots=[_Depot("depot-gazipur")],
        routes=[_Route("route-gazipur-mirpur", "depot-gazipur",
                       "station-mirpur", 2)],
    )
    forecast = build_forecast(snap, horizon=6)

    class _Shipment:
        dispatch_tick = 0
        route_id = "route-gazipur-mirpur"
        destination_station_id = "station-mirpur"
        fuel_type = "DIESEL"
        quantity = 1000.0

    projection = project_world(snap, forecast, [_Shipment()], horizon=6)
    assert projection.spilled_litres == pytest.approx(500.0, abs=1.0)
    # The station never holds more than its tank.
    assert max(projection.station_inventory[("station-mirpur", "DIESEL")]) <= 500.0


def test_depot_inventory_drops_at_dispatch_time():
    """The simulator decrements depot inventory when the shipment is created."""
    depot = _Depot("depot-gazipur", inventory={f: 10000.0 for f in FUEL_TYPES})
    snap = _Snapshot(
        tick=0,
        stations=[_Station("station-mirpur", "urban_high", "region-dhaka")],
        depots=[depot],
        routes=[_Route("route-gazipur-mirpur", "depot-gazipur", "station-mirpur")],
    )
    forecast = build_forecast(snap, horizon=3)

    class _Shipment:
        dispatch_tick = 0
        route_id = "route-gazipur-mirpur"
        destination_station_id = "station-mirpur"
        fuel_type = "DIESEL"
        quantity = 2000.0

    projection = project_world(snap, forecast, [_Shipment()], horizon=3)
    depot_series = projection.depot_inventory[("depot-gazipur", "DIESEL")]
    assert depot_series[0] == pytest.approx(8000.0)
    assert depot_series[1] == pytest.approx(8000.0)


def test_supply_is_applied_one_tick_late_in_the_projection():
    depot = _Depot("depot-gazipur", inventory={f: 10000.0 for f in FUEL_TYPES})
    snap = _Snapshot(
        tick=0,
        stations=[_Station("station-mirpur", "urban_high", "region-dhaka")],
        depots=[depot],
        supplies=[_Supply("s1", "depot-gazipur", "DIESEL", 18000.0, 2)],
    )
    forecast = build_forecast(snap, horizon=6)
    projection = project_world(snap, forecast, [], horizon=6)
    series = projection.depot_inventory[("depot-gazipur", "DIESEL")]
    assert series[0] == pytest.approx(10000.0)
    assert series[1] == pytest.approx(10000.0)
    assert series[2] == pytest.approx(10000.0)  # planned_tick 2 -> applied at 3
    assert series[3] == pytest.approx(28000.0)


# --------------------------------------------------------------------------- #
# The bundle
# --------------------------------------------------------------------------- #
def test_build_forecast_produces_a_risk_for_every_station_and_fuel():
    stations = [
        _Station("station-mirpur", "urban_high", "region-dhaka"),
        _Station("station-tongi", "industrial", "region-dhaka"),
    ]
    snap = _Snapshot(tick=0, stations=stations, depots=[_Depot("depot-gazipur")])
    bundle = build_forecast(snap, horizon=8)
    assert len(bundle.risk) == 2 * len(FUEL_TYPES)
    assert bundle.horizon == 8
    assert bundle.start_tick == 0


def test_the_bundle_answers_demand_queries():
    stations = [_Station("station-mirpur", "urban_high", "region-dhaka")]
    snap = _Snapshot(tick=0, stations=stations, depots=[_Depot("depot-gazipur")])
    bundle = build_forecast(snap, horizon=8)

    assert bundle.demand_at("station-mirpur", "DIESEL", 0) > 0
    assert bundle.demand_at("station-mirpur", "DIESEL", 99) == 0.0
    assert bundle.demand_at("station-mirpur", "DIESEL", -1) == 0.0

    total = bundle.cumulative_demand("station-mirpur", "DIESEL", 0, 4)
    per_tick = sum(bundle.demand_at("station-mirpur", "DIESEL", t) for t in range(4))
    assert total == pytest.approx(per_tick)


def test_tick_where_cumulative_exceeds_finds_the_breach():
    stations = [_Station("station-mirpur", "urban_high", "region-dhaka")]
    snap = _Snapshot(tick=0, stations=stations, depots=[_Depot("depot-gazipur")])
    bundle = build_forecast(snap, horizon=8)

    breach = bundle.tick_where_cumulative_exceeds("station-mirpur", "DIESEL", 0.0, 0, 8)
    assert breach == 0  # any demand at all exceeds zero

    huge = bundle.tick_where_cumulative_exceeds(
        "station-mirpur", "DIESEL", 1e12, 0, 8
    )
    assert huge == 8  # never breached -> the horizon end


def test_the_projection_service_level_is_bounded():
    stations = [_Station("station-mirpur", "urban_high", "region-dhaka")]
    snap = _Snapshot(tick=0, stations=stations, depots=[_Depot("depot-gazipur")])
    bundle = build_forecast(snap, horizon=8)
    projection = project_world(snap, bundle, [], horizon=8)
    assert 0.0 <= projection.service_level <= 1.0
    assert projection.total_demand > 0
