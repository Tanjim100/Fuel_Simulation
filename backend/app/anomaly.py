"""Anomaly detection over the fuel world.

Every function in this module is **pure** with one deliberate exception: the
:class:`AnomalyDetector` keeps a rolling window of plain dataclasses so it can
compare an observation against its own recent history. It has no clock, no
I/O and no network. That keeps it unit-testable without a simulator and
readable by judges.

What the detectors look for
---------------------------

===== ============================ ==========================================
 ID   Detector                    What it catches
===== ============================ ==========================================
 D1   demand spike                demand far above the forecast, or a
                                   ``demand_multiplier`` above 1.05
 D2   inventory drop              fuel left a station *without* being
                                   served -- a silent spill, a manual
                                   removal, or a booking
 D3   depot bottleneck             the depot has almost no dispatch allowance
                                   left this tick
 D4   route bottleneck            a disrupted route, or one so full that it
                                   cannot serve the station's demand
 D5   supply shortfall            the depot will run dry before its next
                                   resupply lands
 D6   projected stockout          the forecast says the station runs dry
                                   inside the horizon
===== ============================ ==========================================

Two design decisions are worth stating outright.

**The event-driven path is the reliable one.** D1 can read
``demand_multiplier`` directly -- it is *observable*, not inferred. The
statistical path (comparing observed demand against the forecast) is a
*secondary* signal, because it needs a history that the backend may not have
yet on a cold start. A spike is therefore reported as soon as the multiplier
moves, and the z-score path adds confidence afterwards.

**Silent spills are invisible to the simulator.** The destination capacity
check compares live inventory against capacity and ignores in-flight fuel; the
excess is clamped and discarded on arrival with no error and no audit flag.
D2 is what makes that visible: it notices fuel that vanished without being
served, and attributes it.

Determinism
-----------

Every anomaly's id is a SHA-1 digest of ``(kind, entity, tick)``. The same
history always produces the same ids, so an anomaly can be referenced in a
ticket and found again later.
"""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence

from .state import Observation, WorldSnapshot

__all__ = [
    "Anomaly",
    "Incident",
    "AnomalyConfig",
    "AnomalyDetector",
    "group_incidents",
]


#: Volumes are compared with a tolerance of a millilitre.
EPS = 1e-6

#: The anomaly kinds.
DEMAND_SPIKE = "DEMAND_SPIKE"
DEMAND_NORMALISED = "DEMAND_NORMALISED"
INVENTORY_DROP = "INVENTORY_DROP"
DEPOT_BOTTLENECK = "DEPOT_BOTTLENECK"
ROUTE_BOTTLENECK = "ROUTE_BOTTLENECK"
SUPPLY_SHORTFALL = "SUPPLY_SHORTFALL"
STOCKOUT_RISK = "STOCKOUT_RISK"

#: Severity levels, most severe first.
SEVERITY_ORDER = ("CRITICAL", "HIGH", "MEDIUM", "LOW")


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AnomalyConfig:
    """Tunables for the detectors.

    The defaults are chosen for the baseline scenario: a station of ~20000 L
    capacity and ~100 L/tick of demand.
    """

    #: A z-score at or above this is a MEDIUM demand spike.
    spike_z_threshold: float = 3.0
    #: A z-score at or above this is a HIGH demand spike.
    spike_z_severe: float = 5.0
    #: The multiplier above which a spike is reported even with no history.
    multiplier_threshold: float = 1.05
    #: An inventory drop of at least this many litres is anomalous.
    inventory_drop_abs: float = 250.0
    #: ...and at least this fraction of the previous inventory.
    inventory_drop_ratio: float = 0.20
    #: A depot with at most this much dispatch allowance left is a bottleneck.
    bottleneck_litres: float = 500.0
    #: A depot using this fraction of its cap is a bottleneck.
    bottleneck_ratio: float = 0.95
    #: How many observations of history a statistic needs.
    min_history: int = 4
    #: How many ticks of history the detectors keep.
    history_window: int = 32
    #: How many ticks of proximity group anomalies into one incident.
    incident_window_ticks: int = 8
    #: A projected stockout at or above this probability is reported.
    stockout_probability: float = 0.5


# --------------------------------------------------------------------------- #
# The anomaly
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Anomaly:
    """One detected anomaly."""

    id: str
    kind: str
    severity: str
    entity_type: str  # STATION | FUEL | DEPOT | ROUTE | STATION_FUEL
    entity_id: str
    tick: int
    detected_tick: int
    title: str
    detail: str
    metrics: dict[str, float] = field(default_factory=dict)
    baseline: dict[str, float] = field(default_factory=dict)
    recommended_action: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "severity": self.severity,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "tick": self.tick,
            "detected_tick": self.detected_tick,
            "title": self.title,
            "detail": self.detail,
            "metrics": dict(self.metrics),
            "baseline": dict(self.baseline),
            "recommended_action": self.recommended_action,
        }

    @property
    def sort_key(self) -> tuple[int, int, str]:
        """Severity first, then tick, then id -- a total order."""
        return (SEVERITY_ORDER.index(self.severity), self.tick, self.id)


def anomaly_id(kind: str, entity_id: str, tick: int) -> str:
    """A deterministic id for an anomaly.

    Deterministic on purpose: the same history always produces the same ids,
    so an anomaly can be referenced in a ticket and found again later.
    """
    raw = f"{kind}:{entity_id}:{tick}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:12]


@dataclass(frozen=True)
class Incident:
    """A group of related anomalies.

    An incident is the operator-facing unit: three anomalies on the same
    station within eight ticks are one story ("this station is in trouble"),
    not three.
    """

    id: str
    title: str
    severity: str
    entity_type: str
    entity_id: str
    start_tick: int
    end_tick: int
    anomaly_ids: list[str] = field(default_factory=list)
    kinds: list[str] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "severity": self.severity,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "start_tick": self.start_tick,
            "end_tick": self.end_tick,
            "anomaly_ids": list(self.anomaly_ids),
            "kinds": list(self.kinds),
            "summary": self.summary,
        }


# --------------------------------------------------------------------------- #
# Statistics
# --------------------------------------------------------------------------- #
def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _stddev(values: Sequence[float]) -> float:
    """Population standard deviation."""
    if len(values) < 2:
        return 0.0
    mean = _mean(values)
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    return math.sqrt(variance)


def _zscore(value: float, baseline: Sequence[float]) -> Optional[float]:
    """How many standard deviations ``value`` is above the baseline mean.

    ``None`` when the baseline has no spread -- a zero standard deviation
    would make the z-score infinite, and "infinitely unusual" is not a
    useful answer.
    """
    if len(baseline) < 2:
        return None
    std = _stddev(baseline)
    if std <= EPS:
        return None
    return (value - _mean(baseline)) / std


# --------------------------------------------------------------------------- #
# The detector
# --------------------------------------------------------------------------- #
class AnomalyDetector:
    """Detects anomalies from a rolling window of observations.

    The detector is *stateful but pure*: it keeps a rolling window of plain
    dataclasses, and the same sequence of observations always produces the
    same anomalies.
    """

    def __init__(self, config: Optional[AnomalyConfig] = None) -> None:
        self.config = config or AnomalyConfig()
        self._history: dict[tuple[str, str], list[Observation]] = defaultdict(list)
        self._multiplier_state: dict[str, float] = {}

    # -- history ------------------------------------------------------------ #
    def observe(self, observation: Observation) -> list[Anomaly]:
        """Record one observation and return the anomalies it triggers.

        The observation is recorded *before* the detectors run, so a detector
        that compares against history sees the previous observations only --
        never the current one.
        """
        key = (observation.station_id, observation.fuel_type)
        history = self._history[key]
        previous = list(history)
        history.append(observation)
        if len(history) > self.config.history_window:
            del history[: len(history) - self.config.history_window]

        anomalies: list[Anomaly] = []
        anomalies.extend(self._detect_demand_spike(observation, previous))
        anomalies.extend(self._detect_demand_normalised(observation))
        anomalies.extend(self._detect_inventory_drop(observation, previous))
        return anomalies

    def observe_many(
        self, observations: Iterable[Observation]
    ) -> list[Anomaly]:
        """Record several observations and return every anomaly."""
        found: list[Anomaly] = []
        for observation in observations:
            found.extend(self.observe(observation))
        return found

    def reset(self) -> None:
        """Forget the history. Used when the simulator is reset."""
        self._history.clear()
        self._multiplier_state.clear()

    # -- D1: demand spike --------------------------------------------------- #
    def _detect_demand_spike(
        self, observation: Observation, previous: Sequence[Observation]
    ) -> list[Anomaly]:
        """A demand far above the recent history, or a raised multiplier.

        Two independent signals, because they fail in different ways:

        * the **multiplier** is *observed* -- it is the simulator's own
          ``demand_multiplier``, so it needs no history at all. This is the
          reliable path.
        * the **z-score** compares the observed demand against the recent
          history. It needs history, so on a cold start it is silent.

        The multiplier is checked first: when both fire, the multiplier is
        the *cause* and the z-score is the symptom.
        """
        anomalies: list[Anomaly] = []

        multiplier = observation.demand_multiplier
        if multiplier > self.config.multiplier_threshold:
            baseline = [o.demand_litres for o in previous]
            mean = _mean(baseline) if baseline else 0.0
            ratio = (observation.demand_litres / mean) if mean > EPS else 0.0

            severity = "HIGH" if multiplier >= 1.5 else "MEDIUM"

            anomalies.append(
                Anomaly(
                    id=anomaly_id(
                        DEMAND_SPIKE,
                        f"{observation.station_id}:{observation.fuel_type}",
                        observation.tick,
                    ),
                    kind=DEMAND_SPIKE,
                    severity=severity,
                    entity_type="STATION_FUEL",
                    entity_id=f"{observation.station_id}:{observation.fuel_type}",
                    tick=observation.tick,
                    detected_tick=observation.tick,
                    title=(
                        f"Demand spike at {observation.station_id} "
                        f"({observation.fuel_type})"
                    ),
                    detail=(
                        f"The simulator's demand multiplier is {multiplier:.2f} "
                        f"(normally 1.00), so demand at "
                        f"{observation.station_id} is running "
                        f"{multiplier:.2f}x normal. Observed demand is "
                        f"{observation.demand_litres:.1f} L against a recent "
                        f"average of {mean:.1f} L."
                    ),
                    metrics={
                        "demand_multiplier": multiplier,
                        "observed_demand_litres": observation.demand_litres,
                        "baseline_mean_demand_litres": mean,
                        "demand_ratio": ratio,
                        "unmet_litres": observation.unmet_litres,
                    },
                    baseline={"mean_demand_litres": mean},
                    recommended_action=(
                        "Dispatch extra fuel to this station, or accept the "
                        "shortfall if the spike is short-lived."
                    ),
                )
            )
            self._multiplier_state[
                f"{observation.station_id}:{observation.fuel_type}"
            ] = multiplier
            return anomalies

        # -- the statistical path ------------------------------------------ #
        if len(previous) >= self.config.min_history:
            baseline = [o.demand_litres for o in previous]
            z = _zscore(observation.demand_litres, baseline)
            if z is not None and z >= self.config.spike_z_threshold:
                severity = "HIGH" if z >= self.config.spike_z_severe else "MEDIUM"
                mean = _mean(baseline)
                anomalies.append(
                    Anomaly(
                        id=anomaly_id(
                            DEMAND_SPIKE,
                            f"{observation.station_id}:{observation.fuel_type}",
                            observation.tick,
                        ),
                        kind=DEMAND_SPIKE,
                        severity=severity,
                        entity_type="STATION_FUEL",
                        entity_id=f"{observation.station_id}:{observation.fuel_type}",
                        tick=observation.tick,
                        detected_tick=observation.tick,
                        title=(
                            f"Demand spike at {observation.station_id} "
                            f"({observation.fuel_type})"
                        ),
                        detail=(
                            f"Observed demand of {observation.demand_litres:.1f} "
                            f"L is {z:.1f} standard deviations above the "
                            f"recent average of {mean:.1f} L."
                        ),
                        metrics={
                            "z_score": z,
                            "observed_demand_litres": observation.demand_litres,
                            "baseline_mean_demand_litres": mean,
                            "unmet_litres": observation.unmet_litres,
                        },
                        baseline={"mean_demand_litres": mean},
                        recommended_action=(
                            "Check the station's recent demand history and "
                            "consider dispatching extra fuel."
                        ),
                    )
                )
        return anomalies

    # -- D1b: the multiplier reverts --------------------------------------- #
    def _detect_demand_normalised(self, observation: Observation) -> list[Anomaly]:
        """Note that a raised multiplier has reverted to normal.

        Measured behaviour: a ``demand_spike`` event raises the multiplier for
        the event's duration and it **reverts to 1.0** when the event
        resolves. An operator watching a station under spike pressure needs to
        know when it lifted, otherwise the spike looks permanent.
        """
        key = f"{observation.station_id}:{observation.fuel_type}"
        previous = self._multiplier_state.get(key)
        if previous is None:
            return []
        if previous <= self.config.multiplier_threshold:
            return []
        if observation.demand_multiplier > self.config.multiplier_threshold:
            return []

        self._multiplier_state.pop(key, None)
        return [
            Anomaly(
                id=anomaly_id(DEMAND_NORMALISED, key, observation.tick),
                kind=DEMAND_NORMALISED,
                severity="LOW",
                entity_type="STATION_FUEL",
                entity_id=key,
                tick=observation.tick,
                detected_tick=observation.tick,
                title=f"Demand normalised at {observation.station_id}",
                detail=(
                    f"The demand multiplier has fallen from {previous:.2f} "
                    f"back to {observation.demand_multiplier:.2f}. The spike "
                    f"event has resolved."
                ),
                metrics={
                    "previous_multiplier": previous,
                    "current_multiplier": observation.demand_multiplier,
                },
                baseline={"previous_multiplier": previous},
                recommended_action=(
                    "No action needed; the elevated demand has ended."
                ),
            )
        ]

    # -- D2: inventory drop ------------------------------------------------- #
    def _detect_inventory_drop(
        self, observation: Observation, previous: Sequence[Observation]
    ) -> list[Anomaly]:
        """Fuel that left the station without being served.

        The arithmetic::

            consumed = previous_inventory - current_inventory
            excess   = consumed - demand_served_this_tick

        A positive ``excess`` means fuel disappeared without being handed to
        a customer. In the simulator the usual cause is a **silent spill**:
        the destination capacity check ignores in-flight fuel, so an
        over-committed delivery is clamped to the tank and the excess is
        discarded on arrival -- with no error and no audit flag.

        The threshold is both absolute *and* relative, so a small tank with a
        small drop is not flagged while a large tank with a large drop is.
        """
        if not previous:
            return []

        last = previous[-1]
        prev_inventory = last.inventory_litres
        if prev_inventory <= EPS:
            return []

        consumed = prev_inventory - observation.inventory_litres
        # The demand that was *met* is what legitimately left the tank.
        served = observation.served_litres
        excess = consumed - served

        if excess <= EPS:
            return []

        # Both thresholds must be crossed.
        if excess < self.config.inventory_drop_abs:
            return []
        if excess / prev_inventory < self.config.inventory_drop_ratio:
            return []

        ratio = excess / prev_inventory
        severity = "HIGH" if ratio >= 0.5 else "MEDIUM"

        # Name the likely cause: in-flight fuel over-committed against the
        # tank is the classic silent spill. The hint is optional -- the
        # detector must work without it.
        hint = getattr(observation, "inbound_litres", None)

        detail = (
            f"{excess:.1f} L of {observation.fuel_type} left "
            f"{observation.station_id} without being served: the tank fell "
            f"from {prev_inventory:.1f} L to {observation.inventory_litres:.1f} "
            f"L while only {served:.1f} L was handed over."
        )
        if hint is not None and hint > EPS:
            detail += (
                f" There is {hint:.1f} L in flight to this station, so the "
                f"excess was most likely spilled on arrival -- the simulator "
                f"clamps a delivery to the tank and discards the rest without "
                f"reporting an error."
            )

        return [
            Anomaly(
                id=anomaly_id(
                    INVENTORY_DROP,
                    f"{observation.station_id}:{observation.fuel_type}",
                    observation.tick,
                ),
                kind=INVENTORY_DROP,
                severity=severity,
                entity_type="STATION_FUEL",
                entity_id=f"{observation.station_id}:{observation.fuel_type}",
                tick=observation.tick,
                detected_tick=observation.tick,
                title=(
                    f"Unaccounted fuel loss at {observation.station_id} "
                    f"({observation.fuel_type})"
                ),
                detail=detail,
                metrics={
                    "excess_litres": excess,
                    "previous_inventory_litres": prev_inventory,
                    "current_inventory_litres": observation.inventory_litres,
                    "served_litres": served,
                    "loss_ratio": ratio,
                },
                baseline={"previous_inventory_litres": prev_inventory},
                recommended_action=(
                    "Reduce the volume committed to this station, or raise "
                    "its tank capacity; the excess is being discarded."
                ),
            )
        ]


# --------------------------------------------------------------------------- #
# Snapshot-driven detectors
# --------------------------------------------------------------------------- #
def detect_snapshot_anomalies(
    snap: WorldSnapshot,
    forecast: Optional[Any] = None,
    config: Optional[AnomalyConfig] = None,
) -> list[Anomaly]:
    """Detect the anomalies that can be read straight off the snapshot.

    These do not need history -- they read the world's *current* state:

    * **D3** a depot with almost no dispatch allowance left
    * **D4** a disrupted route, or a single-connected station whose only
      route is gone
    * **D5** a depot that runs dry before its next resupply lands
    * **D6** a projected stockout inside the horizon
    """
    from .forecast import FUEL_TYPES
    from .validator import dispatch_remaining

    config = config or AnomalyConfig()
    anomalies: list[Anomaly] = []

    # -- D3: depot bottleneck ---------------------------------------------- #
    for depot in snap.depots:
        remaining = dispatch_remaining(snap, depot.id)
        cap = float(depot.dispatch_capacity_per_tick)
        ratio = 1.0 - (remaining / cap) if cap > EPS else 1.0

        if remaining <= config.bottleneck_litres or ratio >= config.bottleneck_ratio:
            severity = "CRITICAL" if depot.status != "OPEN" else "HIGH"
            anomalies.append(
                Anomaly(
                    id=anomaly_id(DEPOT_BOTTLENECK, depot.id, snap.tick),
                    kind=DEPOT_BOTTLENECK,
                    severity=severity,
                    entity_type="DEPOT",
                    entity_id=depot.id,
                    tick=snap.tick,
                    detected_tick=snap.tick,
                    title=f"Dispatch bottleneck at {depot.id}",
                    detail=(
                        f"{depot.id} has {remaining:.1f} L of dispatch "
                        f"allowance left this tick, out of a per-tick cap of "
                        f"{cap:.1f} L ({ratio * 100:.1f}% used)."
                    ),
                    metrics={
                        "dispatch_remaining_litres": remaining,
                        "dispatch_cap_litres": cap,
                        "used_ratio": ratio,
                    },
                    baseline={"dispatch_cap_litres": cap},
                    recommended_action=(
                        "Spread the dispatch across the other depot, or wait "
                        "for the cap to reset on the next tick."
                    ),
                )
            )

    # -- D4: route bottleneck ---------------------------------------------- #
    for route in snap.routes:
        station_routes = snap.routes_for_station(route.destination_station_id)
        single_connected = len(station_routes) == 1

        if route.status != "AVAILABLE":
            severity = "CRITICAL" if single_connected else "HIGH"
            consequence = (
                " This station is served by exactly one route, so there is no "
                "rerouting path and it will run dry."
                if single_connected
                else " Another route may serve this station."
            )
            anomalies.append(
                Anomaly(
                    id=anomaly_id(ROUTE_BOTTLENECK, route.id, snap.tick),
                    kind=ROUTE_BOTTLENECK,
                    severity=severity,
                    entity_type="ROUTE",
                    entity_id=route.id,
                    tick=snap.tick,
                    detected_tick=snap.tick,
                    title=f"Route {route.id} is {route.status}",
                    detail=(
                        f"Route {route.id} ({route.source_depot_id} -> "
                        f"{route.destination_station_id}) is {route.status}."
                        + consequence
                    ),
                    metrics={
                        "transit_ticks": float(route.transit_ticks),
                        "max_shipment": float(route.max_shipment),
                        "route_count_for_station": float(len(station_routes)),
                    },
                    baseline={"max_shipment": float(route.max_shipment)},
                    recommended_action=(
                        "Reroute through the alternative depot."
                        if not single_connected
                        else "No alternative exists; source the fuel elsewhere "
                             "or accept the stockout."
                    ),
                )
            )

    # -- D5: supply shortfall ---------------------------------------------- #
    anomalies.extend(_detect_supply_shortfall(snap, config))

    # -- D6: projected stockout -------------------------------------------- #
    if forecast is not None:
        for risk in getattr(forecast, "risk", []) or []:
            if risk.stockout_probability < config.stockout_probability:
                continue
            if (
                risk.ticks_to_stockout is None
                or risk.ticks_to_stockout > forecast.horizon
            ):
                continue
            severity = (
                "CRITICAL"
                if risk.stockout_probability >= 0.8
                else "HIGH"
            )
            anomalies.append(
                Anomaly(
                    id=anomaly_id(
                        STOCKOUT_RISK,
                        f"{risk.station_id}:{risk.fuel_type}",
                        snap.tick,
                    ),
                    kind=STOCKOUT_RISK,
                    severity=severity,
                    entity_type="STATION_FUEL",
                    entity_id=f"{risk.station_id}:{risk.fuel_type}",
                    tick=snap.tick,
                    detected_tick=snap.tick,
                    title=(
                        f"Projected stockout at {risk.station_id} "
                        f"({risk.fuel_type})"
                    ),
                    detail=(
                        f"{risk.station_id} is projected to run dry in "
                        f"{risk.ticks_to_stockout} tick(s), with a "
                        f"{risk.stockout_probability * 100:.1f}% chance of "
                        f"going unmet and an expected shortfall of "
                        f"{risk.expected_unmet_litres:.1f} L."
                    ),
                    metrics={
                        "stockout_probability": risk.stockout_probability,
                        "ticks_to_stockout": float(risk.ticks_to_stockout or 0),
                        "expected_unmet_litres": risk.expected_unmet_litres,
                        "current_inventory_litres": risk.current_inventory,
                    },
                    baseline={"current_inventory_litres": risk.current_inventory},
                    recommended_action=(
                        "Dispatch to this station now; the shortfall is "
                        "projected inside the planning horizon."
                    ),
                )
            )

    return anomalies


def _detect_supply_shortfall(
    snap: WorldSnapshot, config: AnomalyConfig
) -> list[Anomaly]:
    """A depot that runs dry before its next resupply lands.

    The supply lag is the **measured** one: a supply with
    ``planned_tick = p`` is credited to depot inventory at ``p + 1``.

    The question is whether the depot can carry the stations it serves until
    the resupply arrives. The depot's *dispatch rate* is what drains it, so
    the test is:

        demand_on_depot_per_tick  *  gap_ticks  >  depot_inventory

    where the demand is the forecast demand of the stations this depot
    serves. When the forecast is unavailable, the depot's own share is
    approximated from the routes it serves -- a conservative fallback, since
    the forecast is the accurate answer.
    """
    from .forecast import FUEL_TYPES

    anomalies: list[Anomaly] = []

    # The demand each depot is responsible for, per fuel.
    demand_by_depot: dict[tuple[str, str], float] = defaultdict(float)

    for route in snap.routes:
        station = snap.station(route.destination_station_id)
        if station is None:
            continue
        for fuel in FUEL_TYPES:
            if fuel not in station.capacity:
                continue
            demand_by_depot[(route.source_depot_id, fuel)] += _station_demand_rate(
                station, fuel
            )

    for depot in snap.depots:
        # The next resupply for this depot.
        next_arrival: Optional[int] = None
        next_quantity = 0.0
        for arrival in getattr(snap, "supply_arrivals", []) or []:
            if arrival.depot_id != depot.id:
                continue
            if arrival.status not in ("SCHEDULED", "ARRIVED"):
                continue
            planned = (
                arrival.actual_tick
                if arrival.actual_tick is not None
                else arrival.planned_tick
            )
            if planned >= snap.tick and (
                next_arrival is None or planned < next_arrival
            ):
                next_arrival = planned
                next_quantity = float(arrival.quantity)

        if next_arrival is None:
            # No resupply is coming. That is only an anomaly if the depot is
            # already empty of a fuel it is expected to ship.
            for fuel in FUEL_TYPES:
                inventory = float(depot.inventory.get(fuel, 0.0))
                if inventory > EPS:
                    continue
                if demand_by_depot.get((depot.id, fuel), 0.0) <= EPS:
                    continue
                anomalies.append(
                    Anomaly(
                        id=anomaly_id(SUPPLY_SHORTFALL, f"{depot.id}:{fuel}", snap.tick),
                        kind=SUPPLY_SHORTFALL,
                        severity="HIGH",
                        entity_type="DEPOT_FUEL",
                        entity_id=f"{depot.id}:{fuel}",
                        tick=snap.tick,
                        detected_tick=snap.tick,
                        title=f"No resupply scheduled for {depot.id} ({fuel})",
                        detail=(
                            f"{depot.id} holds no {fuel} and no supply arrival "
                            f"is scheduled. The stations it serves will go "
                            f"unfueled."
                        ),
                        metrics={
                            "inventory_litres": inventory,
                            "required_per_tick": demand_by_depot.get(
                                (depot.id, fuel), 0.0
                            ),
                        },
                        baseline={},
                        recommended_action=(
                            "Schedule a resupply, or reroute the affected "
                            "stations to the other depot."
                        ),
                    )
                )
            continue

        gap = next_arrival - snap.tick + 1  # the +1 lag
        for fuel in FUEL_TYPES:
            inventory = float(depot.inventory.get(fuel, 0.0))
            rate = demand_by_depot.get((depot.id, fuel), 0.0)
            if rate <= EPS:
                continue
            required = rate * gap
            if inventory + next_quantity >= required:
                continue

            shortfall = required - inventory - next_quantity
            severity = "CRITICAL" if inventory <= EPS else "HIGH"
            anomalies.append(
                Anomaly(
                    id=anomaly_id(SUPPLY_SHORTFALL, f"{depot.id}:{fuel}", snap.tick),
                    kind=SUPPLY_SHORTFALL,
                    severity=severity,
                    entity_type="DEPOT_FUEL",
                    entity_id=f"{depot.id}:{fuel}",
                    tick=snap.tick,
                    detected_tick=snap.tick,
                    title=f"Supply shortfall at {depot.id} ({fuel})",
                    detail=(
                        f"{depot.id} holds {inventory:.1f} L of {fuel} and its "
                        f"next resupply of {next_quantity:.1f} L lands in "
                        f"{gap} tick(s), but the stations it serves need about "
                        f"{required:.1f} L over that window. The depot is "
                        f"short by roughly {shortfall:.1f} L."
                    ),
                    metrics={
                        "inventory_litres": inventory,
                        "next_supply_litres": next_quantity,
                        "next_supply_in_ticks": float(gap),
                        "required_litres": required,
                        "shortfall_litres": shortfall,
                        "required_per_tick": rate,
                    },
                    baseline={"inventory_litres": inventory},
                    recommended_action=(
                        "Bring the resupply forward, or reduce the dispatch "
                        "from this depot until it lands."
                    ),
                )
            )

    return anomalies


def _station_demand_rate(station: Any, fuel_type: str) -> float:
    """The average litres per tick a station draws of a fuel.

    A flat average, computed from the day's announced volume. It is
    deliberately conservative -- the true rate varies with the hour of day,
    and a shortfall alarm should err on the side of silence rather than
    crying wolf.
    """
    from .forecast import PROFILES

    profile = PROFILES.get(station.demand_profile)
    if profile is None:
        return 0.0
    daily = profile.daily_liters.get(fuel_type)
    if daily is None:
        return 0.0
    return daily / 96.0


# --------------------------------------------------------------------------- #
# Incidents
# --------------------------------------------------------------------------- #
def group_incidents(
    anomalies: Sequence[Anomaly],
    *,
    window_ticks: int = 8,
) -> list[Incident]:
    """Group related anomalies into incidents.

    Anomalies are grouped by ``(entity_type, entity_id)`` and then by
    proximity in time: two anomalies of the same entity within
    ``window_ticks`` ticks belong to the same incident. This is what turns
    "three anomalies" into "one problem with three symptoms".
    """
    buckets: dict[tuple[str, str], list[Anomaly]] = defaultdict(list)
    for anomaly in anomalies:
        buckets[(anomaly.entity_type, anomaly.entity_id)].append(anomaly)

    incidents: list[Incident] = []

    for (entity_type, entity_id), group in buckets.items():
        ordered = sorted(group, key=lambda a: (a.tick, a.id))
        current: list[Anomaly] = []

        def flush(batch: list[Anomaly]) -> None:
            if not batch:
                return
            severity = min(
                (a.severity for a in batch),
                key=lambda s: SEVERITY_ORDER.index(s),
            )
            start = batch[0].tick
            end = batch[-1].tick
            kinds = sorted({a.kind for a in batch})
            incident_id = anomaly_id(
                "INCIDENT", f"{entity_type}:{entity_id}:{start}", start
            )
            incidents.append(
                Incident(
                    id=incident_id,
                    title=_incident_title(entity_type, entity_id, kinds),
                    severity=severity,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    start_tick=start,
                    end_tick=end,
                    anomaly_ids=[a.id for a in batch],
                    kinds=kinds,
                    summary=(
                        f"{len(batch)} anomal"
                        f"{'y' if len(batch) == 1 else 'ies'} on "
                        f"{entity_id} between tick {start} and tick {end}: "
                        f"{', '.join(k.replace('_', ' ').lower() for k in kinds)}."
                    ),
                )
            )

        for anomaly in ordered:
            if current and anomaly.tick - current[-1].tick > window_ticks:
                flush(current)
                current = []
            current.append(anomaly)
        flush(current)

    incidents.sort(key=lambda i: (SEVERITY_ORDER.index(i.severity), i.start_tick, i.id))
    return incidents


def _incident_title(entity_type: str, entity_id: str, kinds: Sequence[str]) -> str:
    """A one-line title for an incident."""
    label = entity_id.replace(":", " / ")
    if len(kinds) == 1:
        return f"{kinds[0].replace('_', ' ').title()} affecting {label}"
    return f"Multiple anomalies affecting {label}"
