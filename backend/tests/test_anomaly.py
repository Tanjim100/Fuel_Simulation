"""Unit tests for the anomaly detectors.

An anomaly that fires on every tick is noise; one that never fires is a lie.
These tests pin both directions: the detectors catch the conditions they are
supposed to catch, and they stay quiet on a healthy world.

Determinism matters as much as detection here -- the same history must
always produce the same anomaly ids, so an anomaly can be referenced in a
ticket and found again later.
"""

from __future__ import annotations

import pytest

from app.anomaly import (
    DEPOT_BOTTLENECK,
    DEMAND_NORMALISED,
    DEMAND_SPIKE,
    INVENTORY_DROP,
    ROUTE_BOTTLENECK,
    STOCKOUT_RISK,
    SUPPLY_SHORTFALL,
    AnomalyConfig,
    AnomalyDetector,
    anomaly_id,
    detect_snapshot_anomalies,
    group_incidents,
)
from app.state import Observation
from conftest import (
    make_allocation,
    make_depot,
    make_route,
    make_snapshot,
    make_station,
)

FUELS = ("DIESEL", "PETROL", "OCTANE")


def observation(
    *,
    tick: int = 10,
    station_id: str = "station-mirpur",
    fuel_type: str = "DIESEL",
    demand: float = 100.0,
    served: float = 100.0,
    unmet: float = 0.0,
    inventory: float = 5000.0,
    depot_inventory: float = 50000.0,
    multiplier: float = 1.0,
    inbound_litres: float | None = None,
) -> Observation:
    kwargs = {}
    if inbound_litres is not None:
        # The detector reads this as an optional hint. ``Observation`` is a
        # frozen dataclass, so the hint is attached by the caller; the
        # detector uses ``getattr`` and tolerates its absence.
        kwargs["inbound_litres"] = inbound_litres
    obs = Observation(
        tick=tick,
        station_id=station_id,
        fuel_type=fuel_type,
        demand_litres=demand,
        served_litres=served,
        unmet_litres=unmet,
        inventory_litres=inventory,
        depot_inventory_litres=depot_inventory,
        demand_multiplier=multiplier,
    )
    if inbound_litres is not None:
        object.__setattr__(obs, "inbound_litres", inbound_litres)
    return obs


# --------------------------------------------------------------------------- #
# A healthy world produces nothing
# --------------------------------------------------------------------------- #
def test_a_healthy_history_produces_no_anomalies():
    detector = AnomalyDetector()
    found = []
    for tick in range(10, 20):
        found.extend(
            detector.observe(
                observation(tick=tick, demand=100.0, served=100.0, inventory=5000.0)
            )
        )
    assert found == []


def test_a_steady_inventory_is_not_a_drop():
    """A tank that falls exactly as fast as it is served is normal."""
    detector = AnomalyDetector()
    found = []
    inventory = 5000.0
    for tick in range(10, 20):
        found.extend(
            detector.observe(
                observation(
                    tick=tick,
                    demand=100.0,
                    served=100.0,
                    inventory=inventory,
                )
            )
        )
        inventory -= 100.0
    assert [a for a in found if a.kind == INVENTORY_DROP] == []


# --------------------------------------------------------------------------- #
# D1: demand spike
# --------------------------------------------------------------------------- #
def test_a_raised_multiplier_is_a_demand_spike():
    """The event-driven path needs no history at all.

    The simulator's ``demand_multiplier`` is *observable*, so a spike is
    reported on the very tick it appears -- not after enough history has
    accumulated to compute a z-score.
    """
    detector = AnomalyDetector()
    found = detector.observe(observation(tick=12, multiplier=1.8))

    spikes = [a for a in found if a.kind == DEMAND_SPIKE]
    assert len(spikes) == 1
    spike = spikes[0]
    assert spike.severity == "HIGH"
    assert spike.entity_id == "station-mirpur:DIESEL"
    assert spike.metrics["demand_multiplier"] == 1.8
    assert spike.recommended_action


def test_a_small_multiplier_above_the_threshold_is_medium():
    detector = AnomalyDetector()
    found = detector.observe(observation(tick=12, multiplier=1.1))
    spikes = [a for a in found if a.kind == DEMAND_SPIKE]
    assert spikes and spikes[0].severity == "MEDIUM"


def test_a_multiplier_at_or_below_the_threshold_is_not_a_spike():
    detector = AnomalyDetector()
    found = detector.observe(observation(tick=12, multiplier=1.05))
    assert [a for a in found if a.kind == DEMAND_SPIKE] == []


def test_a_statistical_spike_needs_history():
    """With no history there is no baseline, so no z-score spike."""
    detector = AnomalyDetector()
    found = detector.observe(observation(tick=1, demand=10000.0))
    assert [a for a in found if a.kind == DEMAND_SPIKE] == []


def test_a_statistical_spike_fires_once_there_is_history():
    """The z-score path needs a baseline with genuine spread.

    A baseline of five *identical* values has a standard deviation of zero,
    and a zero-spread baseline makes the z-score meaningless -- the detector
    returns ``None`` rather than infinity, because "infinitely unusual" is
    not a useful answer.
    """
    detector = AnomalyDetector()
    baseline_demands = [90.0, 95.0, 100.0, 105.0, 110.0]
    for index, demand in enumerate(baseline_demands):
        detector.observe(
            observation(
                tick=1 + index,
                demand=demand,
                served=demand,
                inventory=5000.0,
            )
        )
    found = detector.observe(
        observation(tick=6, demand=1000.0, served=100.0, inventory=5000.0)
    )
    spikes = [a for a in found if a.kind == DEMAND_SPIKE]
    assert len(spikes) == 1
    assert spikes[0].metrics["z_score"] > 3.0


def test_a_zero_spread_baseline_does_not_produce_an_infinite_z_score():
    """Identical history is not evidence of a spike."""
    detector = AnomalyDetector()
    for tick in range(1, 6):
        detector.observe(
            observation(tick=tick, demand=100.0, served=100.0, inventory=5000.0)
        )
    found = detector.observe(
        observation(tick=6, demand=1000.0, served=100.0, inventory=5000.0)
    )
    assert [a for a in found if a.kind == DEMAND_SPIKE] == []


# --------------------------------------------------------------------------- #
# D1b: the multiplier reverts
# --------------------------------------------------------------------------- #
def test_a_reverting_multiplier_is_reported_as_normalised():
    """The measured behaviour: the multiplier reverts to 1.0 on resolve."""
    detector = AnomalyDetector()
    detector.observe(observation(tick=12, multiplier=1.8))
    found = detector.observe(observation(tick=16, multiplier=1.0))

    normalised = [a for a in found if a.kind == DEMAND_NORMALISED]
    assert len(normalised) == 1
    assert normalised[0].severity == "LOW"
    assert normalised[0].metrics["previous_multiplier"] == 1.8


def test_normalisation_is_not_reported_twice():
    detector = AnomalyDetector()
    detector.observe(observation(tick=12, multiplier=1.8))
    detector.observe(observation(tick=16, multiplier=1.0))
    found = detector.observe(observation(tick=17, multiplier=1.0))
    assert [a for a in found if a.kind == DEMAND_NORMALISED] == []


def test_a_station_that_never_spiked_is_not_reported_as_normalised():
    detector = AnomalyDetector()
    found = detector.observe(observation(tick=12, multiplier=1.0))
    assert [a for a in found if a.kind == DEMAND_NORMALISED] == []


# --------------------------------------------------------------------------- #
# D2: inventory drop -- the silent spill
# --------------------------------------------------------------------------- #
def test_fuel_that_vanishes_without_being_served_is_an_inventory_drop():
    detector = AnomalyDetector()
    detector.observe(
        observation(tick=10, demand=100.0, served=100.0, inventory=10000.0)
    )
    # 10000 -> 4000 is a 6000 L drop, but only 100 L was served.
    found = detector.observe(
        observation(tick=11, demand=100.0, served=100.0, inventory=4000.0)
    )

    drops = [a for a in found if a.kind == INVENTORY_DROP]
    assert len(drops) == 1
    drop = drops[0]
    assert drop.metrics["excess_litres"] == pytest.approx(5900.0)
    assert drop.metrics["loss_ratio"] == pytest.approx(0.59)
    assert drop.severity == "HIGH"


def test_the_drop_detector_names_the_silent_spill_when_fuel_is_in_flight():
    detector = AnomalyDetector()
    detector.observe(
        observation(tick=10, demand=100.0, served=100.0, inventory=10000.0)
    )
    found = detector.observe(
        observation(
            tick=11,
            demand=100.0,
            served=100.0,
            inventory=4000.0,
            inbound_litres=5500.0,
        )
    )
    drop = [a for a in found if a.kind == INVENTORY_DROP][0]
    assert "spilled" in drop.detail


def test_a_small_absolute_drop_is_ignored():
    """A 200 L drop is below the 250 L threshold."""
    detector = AnomalyDetector()
    detector.observe(
        observation(tick=10, demand=100.0, served=100.0, inventory=10000.0)
    )
    found = detector.observe(
        observation(tick=11, demand=100.0, served=100.0, inventory=9800.0)
    )
    assert [a for a in found if a.kind == INVENTORY_DROP] == []


def test_a_small_relative_drop_is_ignored():
    """A 300 L drop from a 10000 L tank is only 3% -- noise."""
    detector = AnomalyDetector()
    detector.observe(
        observation(tick=10, demand=100.0, served=100.0, inventory=10000.0)
    )
    found = detector.observe(
        observation(tick=11, demand=100.0, served=100.0, inventory=9700.0)
    )
    assert [a for a in found if a.kind == INVENTORY_DROP] == []


def test_a_drop_with_no_history_is_not_reported():
    """Without a previous observation there is no baseline to compare to."""
    detector = AnomalyDetector()
    found = detector.observe(
        observation(tick=10, demand=100.0, served=100.0, inventory=4000.0)
    )
    assert [a for a in found if a.kind == INVENTORY_DROP] == []


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #
def test_the_same_history_produces_the_same_anomaly_ids():
    def run():
        detector = AnomalyDetector()
        found = []
        for tick in range(10, 16):
            found.extend(
                detector.observe(
                    observation(tick=tick, multiplier=1.8 if tick == 14 else 1.0)
                )
            )
        return [(a.id, a.kind, a.tick) for a in found]

    assert run() == run()


def test_the_anomaly_id_is_a_deterministic_digest():
    assert anomaly_id("DEMAND_SPIKE", "station-mirpur:DIESEL", 12) == anomaly_id(
        "DEMAND_SPIKE", "station-mirpur:DIESEL", 12
    )
    assert anomaly_id("DEMAND_SPIKE", "station-mirpur:DIESEL", 12) != anomaly_id(
        "DEMAND_SPIKE", "station-mirpur:DIESEL", 13
    )


def test_reset_clears_the_history():
    detector = AnomalyDetector()
    detector.observe(observation(tick=10, multiplier=1.8))
    detector.reset()
    found = detector.observe(observation(tick=11, multiplier=1.0))
    assert [a for a in found if a.kind == DEMAND_NORMALISED] == []


# --------------------------------------------------------------------------- #
# D3: depot bottleneck
# --------------------------------------------------------------------------- #
def test_a_depot_with_no_dispatch_allowance_is_a_bottleneck():
    snapshot = make_snapshot(
        tick=10,
        depots=[make_depot("depot-gazipur", dispatch_capacity_per_tick=1000.0)],
        allocations=[
            make_allocation(
                1,
                depot_id="depot-gazipur",
                quantity=1000.0,
                created_tick=10,
                status="IN_TRANSIT",
            )
        ],
    )
    found = detect_snapshot_anomalies(snapshot)
    bottlenecks = [a for a in found if a.kind == DEPOT_BOTTLENECK]
    assert len(bottlenecks) == 1
    assert bottlenecks[0].entity_id == "depot-gazipur"
    assert bottlenecks[0].metrics["dispatch_remaining_litres"] == 0.0


def test_a_depot_with_headroom_is_not_a_bottleneck():
    snapshot = make_snapshot(
        depots=[make_depot("depot-gazipur", dispatch_capacity_per_tick=12000.0)],
    )
    found = detect_snapshot_anomalies(snapshot)
    assert [a for a in found if a.kind == DEPOT_BOTTLENECK] == []


# --------------------------------------------------------------------------- #
# D4: route bottleneck
# --------------------------------------------------------------------------- #
def test_a_disrupted_route_on_a_single_connected_station_is_critical():
    snapshot = make_snapshot(
        routes=[
            make_route(
                "route-gazipur-mirpur",
                destination_station_id="station-mirpur",
                status="ROUTE_DISRUPTED",
            )
        ],
    )
    found = detect_snapshot_anomalies(snapshot)
    bottlenecks = [a for a in found if a.kind == ROUTE_BOTTLENECK]
    assert len(bottlenecks) == 1
    assert bottlenecks[0].severity == "CRITICAL"
    assert "no rerouting path" in bottlenecks[0].detail


def test_a_disrupted_route_with_an_alternative_is_high_not_critical():
    snapshot = make_snapshot(
        routes=[
            make_route(
                "route-gazipur-mirpur",
                destination_station_id="station-mirpur",
                status="ROUTE_DISRUPTED",
            ),
            make_route(
                "route-patiya-mirpur",
                source_depot_id="depot-patiya",
                destination_station_id="station-mirpur",
            ),
        ],
        depots=[
            make_depot("depot-gazipur"),
            make_depot("depot-patiya"),
        ],
    )
    found = detect_snapshot_anomalies(snapshot)
    bottlenecks = [a for a in found if a.kind == ROUTE_BOTTLENECK]
    assert len(bottlenecks) == 1
    assert bottlenecks[0].severity == "HIGH"
    assert "another route" in bottlenecks[0].detail.lower()


# --------------------------------------------------------------------------- #
# D6: projected stockout
# --------------------------------------------------------------------------- #
def test_a_projected_stockout_is_reported():
    class FakeRisk:
        station_id = "station-mirpur"
        fuel_type = "DIESEL"
        stockout_probability = 0.9
        ticks_to_stockout = 3
        expected_unmet_litres = 500.0
        current_inventory = 100.0

    class FakeForecast:
        horizon = 16
        risk = [FakeRisk()]

    snapshot = make_snapshot()
    found = detect_snapshot_anomalies(snapshot, forecast=FakeForecast())
    stockouts = [a for a in found if a.kind == STOCKOUT_RISK]
    assert len(stockouts) == 1
    assert stockouts[0].severity == "CRITICAL"
    assert stockouts[0].metrics["ticks_to_stockout"] == 3.0


def test_a_low_probability_stockout_is_not_reported():
    class FakeRisk:
        station_id = "station-mirpur"
        fuel_type = "DIESEL"
        stockout_probability = 0.1
        ticks_to_stockout = 3
        expected_unmet_litres = 50.0
        current_inventory = 100.0

    class FakeForecast:
        horizon = 16
        risk = [FakeRisk()]

    snapshot = make_snapshot()
    found = detect_snapshot_anomalies(snapshot, forecast=FakeForecast())
    assert [a for a in found if a.kind == STOCKOUT_RISK] == []


def test_a_stockout_beyond_the_horizon_is_not_reported():
    class FakeRisk:
        station_id = "station-mirpur"
        fuel_type = "DIESEL"
        stockout_probability = 0.9
        ticks_to_stockout = 40
        expected_unmet_litres = 500.0
        current_inventory = 100.0

    class FakeForecast:
        horizon = 16
        risk = [FakeRisk()]

    snapshot = make_snapshot()
    found = detect_snapshot_anomalies(snapshot, forecast=FakeForecast())
    assert [a for a in found if a.kind == STOCKOUT_RISK] == []


# --------------------------------------------------------------------------- #
# D5: supply shortfall
# --------------------------------------------------------------------------- #
def test_a_depot_with_no_resupply_and_no_stock_is_a_shortfall():
    from app.simulator_client import SupplyArrival

    snapshot = make_snapshot(
        tick=10,
        depots=[
            make_depot("depot-gazipur", inventory={"DIESEL": 0.0, "PETROL": 100.0, "OCTANE": 100.0}),
        ],
        supply_arrivals=[],
    )
    found = detect_snapshot_anomalies(snapshot)
    shortfalls = [a for a in found if a.kind == SUPPLY_SHORTFALL]
    assert len(shortfalls) == 1
    assert shortfalls[0].entity_id == "depot-gazipur:DIESEL"
    assert shortfalls[0].severity == "HIGH"


# --------------------------------------------------------------------------- #
# Incidents
# --------------------------------------------------------------------------- #
def test_related_anomalies_are_grouped_into_one_incident():
    detector = AnomalyDetector()
    found = []
    for tick in range(10, 14):
        found.extend(
            detector.observe(
                observation(tick=tick, multiplier=1.8, station_id="station-mirpur")
            )
        )

    incidents = group_incidents(found, window_ticks=8)
    assert len(incidents) == 1
    incident = incidents[0]
    assert incident.entity_id == "station-mirpur:DIESEL"
    assert incident.start_tick == 10
    assert incident.end_tick == 13
    assert len(incident.anomaly_ids) == 4
    assert incident.severity == "HIGH"
    assert incident.summary


def test_distant_anomalies_are_separate_incidents():
    detector = AnomalyDetector()
    found = []
    for tick in (10, 40):
        found.extend(
            detector.observe(
                observation(tick=tick, multiplier=1.8, station_id="station-mirpur")
            )
        )
    incidents = group_incidents(found, window_ticks=8)
    assert len(incidents) == 2


def test_anomalies_on_different_entities_are_separate_incidents():
    detector = AnomalyDetector()
    found = []
    found.extend(detector.observe(observation(tick=10, multiplier=1.8)))
    found.extend(
        detector.observe(
            observation(tick=10, multiplier=1.8, station_id="station-tongi")
        )
    )
    incidents = group_incidents(found, window_ticks=8)
    assert len(incidents) == 2


def test_grouping_nothing_gives_nothing():
    assert group_incidents([]) == []


# --------------------------------------------------------------------------- #
# Serialisation
# --------------------------------------------------------------------------- #
def test_an_anomaly_serialises():
    detector = AnomalyDetector()
    found = detector.observe(observation(tick=12, multiplier=1.8))
    payload = found[0].to_dict()
    for key in (
        "id",
        "kind",
        "severity",
        "entity_type",
        "entity_id",
        "tick",
        "title",
        "detail",
        "metrics",
        "recommended_action",
    ):
        assert key in payload


def test_an_incident_serialises():
    detector = AnomalyDetector()
    found = detector.observe(observation(tick=12, multiplier=1.8))
    incident = group_incidents(found)[0]
    payload = incident.to_dict()
    for key in ("id", "title", "severity", "start_tick", "end_tick", "kinds"):
        assert key in payload
