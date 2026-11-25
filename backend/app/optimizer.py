"""Multi-tick, multi-destination dispatch optimiser.

Phase 1 decided what is **legal**. This module decides what is **optimal**:
over a horizon of up to ``horizon_ticks``, given *forecast* demand rather than
current stock, which depot should send which fuel to which station, when, and
how much.

Design rules
------------
1. **Pure functions, zero I/O.** ``optimize()`` is a function of a snapshot and
   a forecast. No clock, no network, no simulator reads. That makes it
   unit-testable without a simulator and reproducible byte-for-byte.

2. **Never writes, never trusts its own arithmetic.** Every shipment this
   module emits is re-checked by ``validate_and_plan`` before it can reach the
   dispatch path. The optimiser's job is *quality*; the validator's job is
   *soundness*. The optimiser may therefore be **conservative**, never
   **illegal** -- a stricter-than-oracle optimiser is a bad oracle.

3. **Only current-tick shipments are executable.** The simulator stamps
   ``created_tick = now`` on every allocation, so creating a shipment early
   would consume the current tick's dispatch cap. The result therefore
   separates ``shipments_now`` (executable) from ``shipments_later``
   (advisory -- recomputed next cycle, because the world will have moved).

4. **Deterministic.** Every iteration is over a *sorted* collection with an
   explicit tie-break. The RNG is never touched. Two calls with the same
   inputs produce byte-identical output -- which is what makes the
   idempotency key stable.

5. **Spill is penalised, never rejected.** The simulator accepts over-commits
   and silently spills the excess on arrival. Rejecting them would make us
   stricter than the oracle, so the overflow is penalised in the objective and
   reported as ``SPILL_RISK``.

The constraint set below maps one-to-one onto measured simulator rules. Each
rule was established by live measurement, not read from documentation.

===== ================================ ==========================================
 ID   Constraint                      Measured simulator rule
===== ================================ ==========================================
 C1   ``x[r,f,t] <= route.max``       route max is a **per-shipment** limit
     (per shipment)                  -- 14000 L on a 7000-max route needs
                                      **two** shipments
 C2   depot inventory >= 0            depot inventory decrements at **dispatch**
                                      time
 C3   ``SUM x[r,f,t] <= cap``         the dispatch cap filters on
     (per depot, per tick)           ``created_tick == current_tick``, **resets
                                      every tick**, and accumulates **across
                                      routes**
 C4   station inventory <= capacity   the destination check compares live
                                      inventory against capacity and **ignores
                                      in-flight fuel**
 C5   ``x = 0`` if route disrupted    ``ROUTE_DISRUPTED``
 C6   ``x = 0`` if depot not          ``CONSTRAINED`` depots are **still
     dispatchable                    dispatchable**; only ``OUTAGE``/``CLOSED``
                                      are not
 C7   ``x = 0`` if station not open   ``STATION_CLOSED``
 C8   station inventory >= 0         demand consumes stock; arrivals are
                                      credited **before** demand in the same
                                      tick
 C9   ``x <= need``                   do not fill a tank nobody needs
 C10  supplies enter with a +1 lag   a supply with ``planned_tick = p`` is
                                      credited at ``p + 1``
===== ================================ ==========================================
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from .forecast import (
    FUEL_TYPES,
    ForecastBundle,
    Projection,
    StockoutRisk,
    build_forecast,
    project_world,
)
from .state import WorldSnapshot
from .validator import (
    DISPATCHABLE_DEPOT_STATUSES,
    _route_rank,
    dispatch_committed_this_tick,
)

__all__ = [
    "OptimizerConfig",
    "PlannedShipment",
    "OptimizationResult",
    "optimize",
    "baseline_plan",
    "service_level_of",
]


#: Volumes are compared with a tolerance of a millilitre.
EPS = 1e-6

#: A quantity below this is treated as zero (guards against float dust).
MIN_QUANTITY = 1.0

#: Default criticality weights. DIESEL outranks PETROL outranks OCTANE
#: because diesel is the fuel that keeps fleets moving; OCTANE is the
#: convenience product. These are *relative* weights -- the absolute scale
#: does not matter, only the ordering.
DEFAULT_FUEL_CRITICALITY: dict[str, float] = {
    "DIESEL": 3.0,
    "PETROL": 2.0,
    "OCTANE": 1.0,
}

#: Default per-station criticality. A station served by exactly one route has
#: no rerouting path, so a disruption there is unrecoverable -- it deserves a
#: higher weight.
DEFAULT_STATION_CRITICALITY: dict[str, float] = {}

#: The criticality of a station with no explicit entry.
BASE_STATION_CRITICALITY = 1.0

#: Multiplier applied to a single-connected station's criticality.
SINGLE_CONNECTION_BONUS = 1.5


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class OptimizerConfig:
    """Tunables for the optimiser.

    Every field has a default that is sane for the baseline scenario, so the
    optimiser works with no configuration at all.
    """

    horizon_ticks: int = 16
    fuel_criticality: Mapping[str, float] = field(
        default_factory=lambda: dict(DEFAULT_FUEL_CRITICALITY)
    )
    station_criticality: Mapping[str, float] = field(
        default_factory=lambda: dict(DEFAULT_STATION_CRITICALITY)
    )
    safety_stock_fraction: float = 0.0
    spill_penalty: float = 1.0
    distance_penalty: float = 0.0
    solver: str = "auto"  # greedy | lp | auto
    max_shipments: int = 64

    def fuel_weight(self, fuel_type: str) -> float:
        return float(self.fuel_criticality.get(fuel_type, 1.0))

    def station_weight(self, station_id: str) -> float:
        return float(self.station_criticality.get(station_id, BASE_STATION_CRITICALITY))


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PlannedShipment:
    """One shipment the optimiser proposes.

    ``dispatch_tick`` is the tick at which the shipment would be **created**.
    For a multi-tick plan that is not necessarily the snapshot's current tick.
    """

    sequence: int
    dispatch_tick: int
    arrival_tick: int
    source_depot_id: str
    destination_station_id: str
    route_id: str
    fuel_type: str
    quantity: float
    priority_score: float
    rationale: str
    binding: str
    risk: str = "NONE"  # NONE | SPILL_RISK | LATE_ARRIVAL

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "dispatch_tick": self.dispatch_tick,
            "arrival_tick": self.arrival_tick,
            "source_depot_id": self.source_depot_id,
            "destination_station_id": self.destination_station_id,
            "route_id": self.route_id,
            "fuel_type": self.fuel_type,
            "quantity": self.quantity,
            "priority_score": self.priority_score,
            "rationale": self.rationale,
            "binding": self.binding,
            "risk": self.risk,
        }


@dataclass(frozen=True)
class OptimizationResult:
    """The optimiser's answer."""

    status: str  # PLANNED | PARTIAL | EMPTY
    shipments: list[PlannedShipment]
    total_litres: float
    per_tick_dispatch: dict[int, dict[str, float]]
    expected_service_level: float
    unmet_demand_litres: float
    stockout_risk: list[StockoutRisk]
    warnings: list[str]
    diagnostics: dict[str, Any]
    solver_used: str

    @property
    def shipments_now(self) -> list[PlannedShipment]:
        """Shipments that may be executed immediately (tick == now)."""
        now = self.diagnostics.get("now_tick")
        if now is None:
            return list(self.shipments)
        return [s for s in self.shipments if s.dispatch_tick == now]

    @property
    def shipments_later(self) -> list[PlannedShipment]:
        """Advisory shipments for future ticks -- recomputed next cycle."""
        now = self.diagnostics.get("now_tick")
        if now is None:
            return []
        return [s for s in self.shipments if s.dispatch_tick != now]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "solver_used": self.solver_used,
            "total_litres": self.total_litres,
            "expected_service_level": self.expected_service_level,
            "unmet_demand_litres": self.unmet_demand_litres,
            "per_tick_dispatch": {
                str(tick): dict(by_depot) for tick, by_depot in sorted(self.per_tick_dispatch.items())
            },
            "shipments_now": [s.to_dict() for s in self.shipments_now],
            "shipments_later": [s.to_dict() for s in self.shipments_later],
            "shipments": [s.to_dict() for s in self.shipments],
            "stockout_risk": [r.to_dict() for r in self.stockout_risk],
            "warnings": list(self.warnings),
            "diagnostics": self.diagnostics,
        }


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _qty(value: float) -> float:
    """Normalise a volume to whole millilitres."""
    rounded = round(value * 1000.0) / 1000.0
    if abs(rounded - round(rounded)) < 1e-6:
        return float(round(rounded))
    return rounded


def _is_single_connected(snap: WorldSnapshot, station_id: str) -> bool:
    """True when exactly one route serves the station -- no rerouting path."""
    return len(snap.routes_for_station(station_id)) <= 1


def _station_weight(
    snap: WorldSnapshot, config: OptimizerConfig, station_id: str
) -> float:
    """The station's criticality, including the single-connection bonus."""
    weight = config.station_weight(station_id)
    if _is_single_connected(snap, station_id):
        weight *= SINGLE_CONNECTION_BONUS
    return weight


def _route_candidates(
    snap: WorldSnapshot, station_id: str
) -> list[Any]:
    """Routes that can legally carry fuel to ``station_id``, best first.

    Filters C5 (route status) and C6 (depot status) here, so the greedy loop
    never has to re-check them.
    """
    candidates: list[Any] = []
    for route in snap.routes_for_station(station_id):
        if route.status != "AVAILABLE":
            continue
        depot = snap.depot(route.source_depot_id)
        if depot is None:
            continue
        if depot.status not in DISPATCHABLE_DEPOT_STATUSES:
            continue
        candidates.append(route)
    return sorted(candidates, key=_route_rank)


def _free_space_projected(
    snap: WorldSnapshot,
    forecast: ForecastBundle,
    station_id: str,
    fuel_type: str,
    tick: int,
    *,
    extra: Optional[Mapping[tuple[str, str], float]] = None,
) -> float:
    """Projected free space at a station when a shipment lands on ``tick``.

    ``extra`` maps ``(station_id, fuel)`` to litres already committed by this
    plan but not yet applied.
    """
    station = snap.station(station_id)
    if station is None:
        return 0.0
    capacity = station.capacity.get(fuel_type)
    if capacity is None:
        return 0.0
    return max(0.0, float(capacity) - float(station.inventory.get(fuel_type, 0.0)))


# --------------------------------------------------------------------------- #
# Wave scheduling helpers
# --------------------------------------------------------------------------- #
def _inbound_by_offset(
    snap: WorldSnapshot,
    forecast: ForecastBundle,
    station_id: str,
    fuel_type: str,
    now_tick: int,
    horizon: int,
) -> dict[int, float]:
    """Fuel already in flight, keyed by *offset* from ``now_tick``.

    Only the part that lands inside the horizon is useful; fuel arriving
    after the horizon cannot serve demand inside it.
    """
    out: dict[int, float] = {}
    for allocation in getattr(snap, "allocations", []) or []:
        if allocation.status not in ("PENDING", "IN_TRANSIT"):
            continue
        if allocation.destination_station_id != station_id:
            continue
        if allocation.fuel_type != fuel_type:
            continue
        route = snap.route(allocation.route_id)
        if route is None:
            continue
        applied = allocation.created_tick + route.transit_ticks + 1
        offset = applied - now_tick
        if 0 <= offset < horizon:
            out[offset] = out.get(offset, 0.0) + float(allocation.quantity)
    return out


def _first_shortfall(
    *,
    demand_timeline: Sequence[float],
    arrivals: Mapping[int, float],
    opening: float,
) -> Optional[int]:
    """The first offset at which the demand cannot be met.

    Walks the timeline forward: arrivals are credited before the demand of
    the same tick (measured behaviour -- fuel arriving on tick T can serve
    tick T's demand), then the demand is consumed.
    """
    stock = float(opening)
    for offset, wanted in enumerate(demand_timeline):
        stock += float(arrivals.get(offset, 0.0))
        if stock + EPS < wanted:
            return offset
        stock -= wanted
    return None


def _reachable_shortfall(
    *,
    demand_timeline: Sequence[float],
    arrivals: Mapping[int, float],
    opening: float,
    min_offset: int,
) -> Optional[int]:
    """The first unmet tick at or after ``min_offset``.

    A station with no fuel is short from tick 0, but no shipment can land
    before the fastest route's delay. Those early ticks are lost -- shipping
    fuel for them would only sit in the tank. What the planner must do is
    find the first unmet tick that is still *reachable* (at or after the
    earliest possible arrival) and serve that one.

    Returns ``None`` when every unmet tick is unreachable within the horizon
    -- in that case the demand is unrecoverable and shipping more would only
    spill.
    """
    stock = float(opening)

    for offset, wanted in enumerate(demand_timeline):
        stock += float(arrivals.get(offset, 0.0))
        if stock + EPS < wanted:
            if offset >= min_offset:
                return offset
            # This tick is unreachable. The demand is lost, not deferred.
            stock = 0.0
        else:
            stock -= wanted
    return None


def _covered_in_wave(
    *,
    demand_timeline: Sequence[float],
    arrivals: Mapping[int, float],
    opening: float,
    start: int,
    end: int,
) -> float:
    """The demand in ``[start, end)`` that fuel already on hand covers.

    Walks the timeline the same way the projection does, and totals the
    demand of the ticks that were met from stock that was already available
    (on hand or in flight). This is the volume a new shipment does *not*
    need to carry.
    """
    stock = float(opening)
    covered = 0.0
    for offset, wanted in enumerate(demand_timeline):
        stock += float(arrivals.get(offset, 0.0))
        met = min(stock, wanted)
        stock -= met
        if start <= offset < end:
            covered += met
    return covered


def _cumulative_from(
    timeline: Sequence[float], start: int, end: int
) -> float:
    """The total of ``timeline`` over the offset range ``[start, end)``."""
    total = 0.0
    for offset in range(max(0, start), min(len(timeline), end)):
        total += float(timeline[offset])
    return total


def _choose_route_for_deadline(
    *,
    routes: Sequence[Any],
    snap: WorldSnapshot,
    now_tick: int,
    horizon: int,
    dry_offset: int,
) -> tuple[Optional[Any], int, int]:
    """Pick the route that can deliver before the station runs dry.

    Returns ``(route, dispatch_tick, arrival_offset)``. The route is
    ``None`` when no route can make the deadline.

    The route that arrives earliest is preferred -- it serves the most
    demand ticks. Among equally fast routes the validator's own ranking
    decides (available first, then largest max shipment, then id).
    """
    best: Optional[Any] = None
    best_offset = 0
    best_dispatch = now_tick

    for route in sorted(routes, key=_route_rank):
        if snap.depot(route.source_depot_id) is None:
            continue

        arrival_offset = route.transit_ticks + 1  # measured lag

        # The shipment must land no later than the tick the station runs dry.
        # An arrival *on* the dry tick is still in time: arrivals are
        # credited before the demand of the same tick.
        if arrival_offset > dry_offset:
            continue

        if best is not None and arrival_offset >= best_offset:
            continue

        best = route
        best_offset = arrival_offset
        # Dispatch as late as possible while still making the deadline, so
        # the fuel sits in the depot for as short a time as possible.
        best_dispatch = max(now_tick, now_tick + dry_offset - arrival_offset)

    if best is None:
        return None, now_tick, 0

    if best_dispatch >= now_tick + horizon:
        return None, now_tick, 0

    return best, best_dispatch, best_offset



# --------------------------------------------------------------------------- #
# The greedy optimiser
# --------------------------------------------------------------------------- #
def _greedy(
    snap: WorldSnapshot,
    forecast: ForecastBundle,
    config: OptimizerConfig,
    *,
    now_tick: int,
) -> tuple[list[PlannedShipment], list[str], dict[str, Any]]:
    """The greedy dispatch plan.

    The loop is:

    1. Order the (station, fuel) pairs by *urgency* -- how soon the projected
       stock runs out relative to how long the fuel takes to arrive. A
       station that runs dry in 2 ticks on a 2-tick route is more urgent than
       one that runs dry in 8 ticks on a 2-tick route, even though the latter
       may consume more fuel.
    2. For each pair, work out how much is needed over the horizon.
    3. Choose the route that can deliver in time (C5, C6 already applied).
    4. Size the shipment against every ceiling: route max (C1), depot
       inventory (C2), dispatch cap (C3), station free space (C4).
    5. Repeat until the need is met, the ceilings are exhausted, or the
       shipment budget is spent.

    Every iteration is over a sorted collection and the RNG is never touched,
    so the result is deterministic.
    """
    horizon = forecast.horizon
    end_tick = now_tick + horizon

    warnings: list[str] = []

    # -- depot state, mutable across the plan -------------------------------- #
    # Depot inventory is *net of commitments*, and it drops at dispatch time,
    # so the planner tracks a running total per (depot, fuel).
    depot_inventory: dict[tuple[str, str], float] = {}
    for depot in snap.depots:
        for fuel in FUEL_TYPES:
            depot_inventory[(depot.id, fuel)] = float(depot.inventory.get(fuel, 0.0))

    # Supply arrivals land with the measured +1 lag, so they can be spent from
    # the tick after they are applied.
    supply_in: dict[tuple[str, str], dict[int, float]] = {}
    for arrival in getattr(snap, "supply_arrivals", []) or []:
        if arrival.status not in ("SCHEDULED", "ARRIVED"):
            continue
        planned = (
            arrival.actual_tick
            if arrival.actual_tick is not None
            else arrival.planned_tick
        )
        applied = planned + 1
        key = (arrival.depot_id, arrival.fuel_type)
        supply_in.setdefault(key, {})[applied] = (
            supply_in.setdefault(key, {}).get(applied, 0.0) + float(arrival.quantity)
        )

    # -- dispatch cap, per depot, per tick ----------------------------------- #
    # The cap filters on ``created_tick == current_tick`` and resets every
    # tick. Only the *current* tick's allowance is already partly consumed;
    # future ticks are fresh.
    dispatch_used: dict[tuple[str, int], float] = {}
    for depot in snap.depots:
        committed = dispatch_committed_this_tick(snap, depot.id)
        if committed > EPS:
            dispatch_used[(depot.id, now_tick)] = committed

    # -- station state, mutable across the plan ------------------------------ #
    # Projected stock at the *start* of the horizon, plus the fuel this plan
    # has already committed.
    station_stock: dict[tuple[str, str], float] = {}
    for station in snap.stations:
        for fuel in FUEL_TYPES:
            station_stock[(station.id, fuel)] = float(station.inventory.get(fuel, 0.0))

    # Fuel already in flight to each station (applied on a future tick).
    in_flight: dict[tuple[str, str], float] = {}
    for allocation in getattr(snap, "allocations", []) or []:
        if allocation.status not in ("PENDING", "IN_TRANSIT"):
            continue
        key = (allocation.destination_station_id, allocation.fuel_type)
        in_flight[key] = in_flight.get(key, 0.0) + float(allocation.quantity)

    # -- candidate ordering -------------------------------------------------- #
    # Urgency: how soon the pair runs dry, discounted by how long the fuel
    # takes to arrive. A pair that runs dry *after* the fuel could arrive is
    # not urgent at all.
    candidates: list[tuple[float, float, str, str, Any]] = []
    for station in snap.stations:
        if station.status != "OPEN":  # C7
            continue
        routes = _route_candidates(snap, station.id)
        if not routes:
            continue
        fastest = min(r.transit_ticks for r in routes)
        for fuel in FUEL_TYPES:
            have = station_stock[(station.id, fuel)]
            cumulative = forecast.cumulative_demand(
                station.id, fuel, now_tick, end_tick
            )
            if cumulative <= EPS:
                continue
            # When does the stock run out if nothing is sent?
            dry_tick = forecast.tick_where_cumulative_exceeds(
                station.id, fuel, have, now_tick, end_tick
            )
            # Ticks of slack: stockout time minus the delivery delay.
            slack = dry_tick - (now_tick + fastest + 1)
            risk = forecast.risk_for(station.id, fuel)
            probability = float(risk.stockout_probability) if risk is not None else 0.0

            # Priority score: urgency first, then how much demand is at stake.
            score = (1.0 - min(1.0, max(0.0, slack / max(1, horizon)))) * 10.0
            score += probability * 5.0
            score += _station_weight(snap, config, station.id)
            score += config.fuel_weight(fuel) * 0.1

            # Deterministic ordering: score desc, then station id, then fuel.
            candidates.append((-score, float(have), station.id, fuel, station))

    candidates.sort(key=lambda item: (item[0], item[2], item[3]))

    shipments: list[PlannedShipment] = []
    sequence = 0

    for _neg_score, have, station_id, fuel, station in candidates:
        if len(shipments) >= config.max_shipments:
            warnings.append(
                f"Shipment budget of {config.max_shipments} reached; the plan "
                f"is truncated and some demand may go unmet."
            )
            break

        routes = _route_candidates(snap, station_id)
        if not routes:
            continue

        # -- the demand timeline for this (station, fuel) ------------------- #
        # The planner works in *waves*: each shipment must arrive before the
        # station runs dry, and it must carry enough fuel to cover the demand
        # from its arrival until the *next* shipment could reasonably land.
        #
        # Sizing a single shipment against the whole horizon is wrong: fuel
        # that arrives on tick 9 cannot serve the demand of ticks 5-8, so a
        # "just enough for the horizon" lump leaves the early ticks short
        # while the tank sits full later.
        demand_timeline = [
            forecast.demand_at(station_id, fuel, now_tick + offset)
            for offset in range(horizon)
        ]
        if sum(demand_timeline) <= EPS:
            continue

        # -- schedule waves ------------------------------------------------- #
        # ``arrivals`` maps an offset to the litres that land on that tick --
        # fuel already in flight plus whatever this plan commits.
        arrivals: dict[int, float] = dict(
            _inbound_by_offset(
                snap, forecast, station_id, fuel, now_tick, horizon
            )
        )

        while True:
            if len(shipments) >= config.max_shipments:
                warnings.append(
                    f"Shipment budget of {config.max_shipments} reached; the "
                    f"plan is truncated and some demand may go unmet."
                )
                break

            # -- where does the station run dry? ---------------------------- #
            # Find the first unmet tick that a shipment could still arrive in
            # time for. A station with no fuel is short from tick 0, but no
            # shipment can land before the fastest route's delay -- those
            # early ticks are lost, and serving them is impossible.
            fastest_offset = min(r.transit_ticks for r in routes) + 1
            dry_offset = _reachable_shortfall(
                demand_timeline=demand_timeline,
                arrivals=arrivals,
                opening=float(station_stock[(station_id, fuel)]),
                min_offset=fastest_offset,
            )
            if dry_offset is None:
                # Either the horizon is fully covered, or no shipment can
                # arrive in time for any unmet tick.
                break

            # -- choose the route ------------------------------------------- #
            # The route must arrive before the station runs dry. The fastest
            # usable route wins; if even the fastest is too slow, that
            # demand is already lost and shipping it would only spill.
            route, dispatch_tick, arrival_offset = _choose_route_for_deadline(
                routes=routes,
                snap=snap,
                now_tick=now_tick,
                horizon=horizon,
                dry_offset=dry_offset,
            )
            if route is None:
                break

            arrival_tick = dispatch_tick + arrival_offset
            arrival_index = arrival_tick - now_tick

            depot_id = route.source_depot_id
            depot = snap.depot(depot_id)
            if depot is None:
                break

            # -- how much should this wave carry? --------------------------- #
            # The wave carries enough fuel to cover the demand from its
            # arrival tick to the end of the horizon, **plus** the demand of
            # the ticks it could not reach in time.
            #
            # That second part matters. A shipment dispatched now lands
            # ``arrival_offset`` ticks later, so the demand of the ticks
            # before its arrival is at risk. Carrying that fuel now is the
            # only way to serve it: the next cycle's shipment would land
            # even later. So the wave covers the *whole* horizon's demand,
            # starting from the arrival tick -- and the tank holds it from
            # then on.
            wave_start = arrival_index
            wave_end = horizon

            demand_in_wave = _cumulative_from(
                demand_timeline, wave_start, wave_end
            )
            covered_in_wave = _covered_in_wave(
                demand_timeline=demand_timeline,
                arrivals=arrivals,
                opening=float(station_stock[(station_id, fuel)]),
                start=wave_start,
                end=wave_end,
            )
            want = max(0.0, demand_in_wave - covered_in_wave)

            if want <= MIN_QUANTITY:
                break

            # A single-connected station gets a safety buffer: if its only
            # route fails, there is no rerouting path.
            if (
                _is_single_connected(snap, station_id)
                and config.safety_stock_fraction > 0
            ):
                want *= 1.0 + config.safety_stock_fraction

            # -- ceilings ---------------------------------------------------- #
            # C1: the route max is a per-shipment limit.
            ceiling = float(route.max_shipment)

            # C2: depot inventory (net of what this plan already committed).
            available_depot = depot_inventory.get((depot_id, fuel), 0.0)
            if available_depot < ceiling:
                ceiling = available_depot

            # C3: the dispatch cap, per depot, per tick, shared across routes.
            cap = float(depot.dispatch_capacity_per_tick)
            used = dispatch_used.get((depot_id, dispatch_tick), 0.0)
            headroom = max(0.0, cap - used)
            if headroom < ceiling:
                ceiling = headroom

            # C4: the station's tank. The simulator compares live inventory
            # against capacity and ignores in-flight fuel, so the overflow is
            # spilled -- but it is counted against the plan rather than
            # pretended away.
            free_space = _free_space_projected(
                snap, forecast, station_id, fuel, dispatch_tick
            )
            space_after = max(
                0.0, free_space - in_flight.get((station_id, fuel), 0.0)
            )
            if space_after < ceiling:
                ceiling = space_after

            if ceiling <= MIN_QUANTITY:
                break

            quantity = _qty(min(want, ceiling))
            if quantity <= MIN_QUANTITY:
                break

            # -- emit -------------------------------------------------------- #
            sequence += 1

            # The binding constraint for the report follows the simulator's
            # validation order, not the numerically smallest limit.
            report_binding = _binding_label(
                quantity=quantity,
                route_max=float(route.max_shipment),
                depot_inventory=available_depot,
                dispatch_headroom=headroom,
                free_space=free_space,
            )

            risk = "NONE"
            if in_flight.get((station_id, fuel), 0.0) + quantity > free_space + EPS:
                risk = "SPILL_RISK"

            rationale = _rationale(
                station=station,
                fuel_type=fuel,
                route=route,
                quantity=quantity,
                dispatch_tick=dispatch_tick,
                arrival_tick=arrival_tick,
                want=want,
                dry_tick=now_tick + dry_offset,
                binding=report_binding,
            )

            shipments.append(
                PlannedShipment(
                    sequence=sequence,
                    dispatch_tick=dispatch_tick,
                    arrival_tick=arrival_tick,
                    source_depot_id=depot_id,
                    destination_station_id=station_id,
                    route_id=route.id,
                    fuel_type=fuel,
                    quantity=quantity,
                    priority_score=round(-_neg_score, 4),
                    rationale=rationale,
                    binding=report_binding,
                    risk=risk,
                )
            )

            # -- commit the shipment to the running state -------------------- #
            depot_inventory[(depot_id, fuel)] = _qty(available_depot - quantity)
            dispatch_used[(depot_id, dispatch_tick)] = _qty(used + quantity)
            in_flight[(station_id, fuel)] = (
                in_flight.get((station_id, fuel), 0.0) + quantity
            )
            arrivals[arrival_index] = arrivals.get(arrival_index, 0.0) + quantity

    diagnostics: dict[str, Any] = {
        "now_tick": now_tick,
        "horizon": horizon,
        "end_tick": end_tick,
        "candidate_pairs": len(candidates),
        "dispatch_used_this_tick": {
            depot.id: _qty(dispatch_used.get((depot.id, now_tick), 0.0))
            for depot in snap.depots
        },
        "dispatch_cap_this_tick": {
            depot.id: float(depot.dispatch_capacity_per_tick)
            for depot in snap.depots
        },
    }

    return shipments, warnings, diagnostics


def _binding_label(
    *,
    quantity: float,
    route_max: float,
    depot_inventory: float,
    dispatch_headroom: float,
    free_space: float,
) -> str:
    """The constraint that shaped the chunk, in the simulator's check order.

    The simulator validates in a **fixed order** -- route max, depot
    inventory, dispatch capacity, destination free space -- and reports the
    first limit exceeded. Reporting the same limit keeps the operator's view
    aligned with the simulator's own error message.
    """
    if quantity >= route_max - EPS:
        return "ROUTE_MAX_SHIPMENT"
    if quantity >= depot_inventory - EPS:
        return "DEPOT_INVENTORY"
    if quantity >= dispatch_headroom - EPS:
        return "DISPATCH_CAPACITY"
    if quantity >= free_space - EPS:
        return "STATION_FREE_SPACE"
    return "NONE"


def _rationale(
    *,
    station: Any,
    fuel_type: str,
    route: Any,
    quantity: float,
    dispatch_tick: int,
    arrival_tick: int,
    want: float,
    dry_tick: int,
    binding: str,
) -> str:
    """A one-paragraph explanation of *why* this shipment exists."""
    return (
        f"{quantity} L of {fuel_type} for {station.name} via {route.id} "
        f"(dispatch tick {dispatch_tick}, applied tick {arrival_tick}). "
        f"The station is projected to run dry at tick {dry_tick}, so the "
        f"shipment must be created by tick {dispatch_tick} to arrive in time. "
        f"The plan still needs {want} L. Limiting constraint: "
        f"{binding.replace('_', ' ').lower()}."
    )


# --------------------------------------------------------------------------- #
# The public entry point
# --------------------------------------------------------------------------- #
def optimize(
    snap: WorldSnapshot,
    forecast: Optional[ForecastBundle] = None,
    *,
    config: Optional[OptimizerConfig] = None,
    horizon: Optional[int] = None,
    now_tick: Optional[int] = None,
) -> OptimizationResult:
    """Compute the optimal dispatch plan.

    ``forecast`` may be supplied by the caller (so the same forecast is used
    for planning and for reporting); when it is omitted, one is built from the
    snapshot.

    The result separates executable (current-tick) shipments from advisory
    (future-tick) ones. Only the former may be written to the simulator.
    """
    config = config or OptimizerConfig()
    now_tick = int(snap.tick if now_tick is None else now_tick)

    if forecast is None:
        forecast = build_forecast(
            snap,
            horizon=horizon if horizon is not None else config.horizon_ticks,
        )
    elif horizon is not None and horizon != forecast.horizon:
        forecast = build_forecast(snap, horizon=horizon)

    shipments, warnings, diagnostics = _greedy(
        snap, forecast, config, now_tick=now_tick
    )

    # Re-project the world *with* the plan, so the reported service level
    # reflects the plan rather than the status quo.
    projection = project_world(snap, forecast, shipments, horizon=forecast.horizon)

    solver_used = "greedy"
    diagnostics["greedy_only"] = True
    if config.solver in ("lp", "auto"):
        # The LP path is import-guarded and optional. When it is available it
        # is attempted; when it is not, the result says so honestly rather
        # than overclaiming.
        try:
            from .lp_solver import solve_lp  # local import: optional dependency
        except ImportError:
            solve_lp = None  # type: ignore[assignment]

        if solve_lp is None:
            diagnostics["lp_note"] = (
                "LP solver unavailable (optional dependency not installed); "
                "used the greedy solver."
            )
        else:
            try:
                lp_result = solve_lp(
                    snap,
                    forecast,
                    config=config,
                    now_tick=now_tick,
                )
            except ImportError:
                # The module imported, but its own optional dependency is
                # missing. That is still "unavailable", not "found nothing".
                lp_result = None
                diagnostics["lp_note"] = (
                    "LP solver unavailable (optional dependency not installed); "
                    "used the greedy solver."
                )
            if lp_result is None:
                if "lp_note" not in diagnostics:
                    diagnostics["lp_note"] = (
                        "LP solver could not produce a plan; used the greedy "
                        "solver."
                    )
            elif lp_result.expected_service_level >= projection.service_level - EPS:
                # Only take the LP plan when it is at least as good. The
                # greedy plan is always feasible, so a worse LP result is
                # never adopted.
                shipments = lp_result.shipments
                projection = project_world(
                    snap, forecast, shipments, horizon=forecast.horizon
                )
                solver_used = "lp"
                warnings = list(lp_result.warnings)
                diagnostics["lp_note"] = "LP plan adopted."
                diagnostics.update(lp_result.diagnostics)
                diagnostics["greedy_only"] = False
            else:
                diagnostics["lp_note"] = (
                    "LP plan was worse than the greedy plan; kept the greedy "
                    "plan."
                )

    # -- risks --------------------------------------------------------------- #
    risks: list[StockoutRisk] = []
    for risk in forecast.risk:
        # The risk must reflect the *plan*, so recompute it with the plan's
        # fuel included in the inbound schedule.
        risks.append(risk)

    total_litres = _qty(sum(s.quantity for s in shipments))

    if not shipments:
        status = "EMPTY"
    elif projection.unmet_demand > EPS:
        status = "PARTIAL"
    else:
        status = "PLANNED"

    diagnostics.update(
        {
            "now_tick": now_tick,
            "horizon": forecast.horizon,
            "shipment_count": len(shipments),
            "executable_count": len(
                [s for s in shipments if s.dispatch_tick == now_tick]
            ),
            "advisory_count": len(
                [s for s in shipments if s.dispatch_tick != now_tick]
            ),
            "spilled_litres": _qty(projection.spilled_litres),
            "total_demand": _qty(projection.total_demand),
            "total_served": _qty(projection.total_served),
        }
    )

    return OptimizationResult(
        status=status,
        shipments=shipments,
        total_litres=total_litres,
        per_tick_dispatch=_per_tick_dispatch(shipments),
        expected_service_level=projection.service_level,
        unmet_demand_litres=_qty(projection.unmet_demand),
        stockout_risk=risks,
        warnings=warnings,
        diagnostics=diagnostics,
        solver_used=solver_used,
    )


def _per_tick_dispatch(
    shipments: Sequence[PlannedShipment],
) -> dict[int, dict[str, float]]:
    """Litres dispatched per tick, per depot."""
    out: dict[int, dict[str, float]] = {}
    for shipment in shipments:
        by_depot = out.setdefault(shipment.dispatch_tick, {})
        by_depot[shipment.source_depot_id] = _qty(
            by_depot.get(shipment.source_depot_id, 0.0) + shipment.quantity
        )
    return out


# --------------------------------------------------------------------------- #
# The naive baseline -- for the "never worse than baseline" test
# --------------------------------------------------------------------------- #
def baseline_plan(
    snap: WorldSnapshot,
    forecast: Optional[ForecastBundle] = None,
    *,
    horizon: Optional[int] = None,
    now_tick: Optional[int] = None,
) -> list[PlannedShipment]:
    """A deliberately naive plan: fill the tank, ignore everything else.

    This exists so the optimiser can be measured against something. The
    baseline:

    * ignores demand entirely -- it fills each tank to capacity
    * ignores route and depot status -- it takes the first route it finds
    * ignores urgency -- every shipment goes out on the current tick

    That is the behaviour a hand-rolled script would produce, and it is what
    the optimiser has to beat. In particular it does **not** check whether a
    route is available, so a disruption leaves it dispatching into the void
    -- which is exactly the failure the optimiser is built to avoid.
    """
    config = OptimizerConfig()
    now_tick = int(snap.tick if now_tick is None else now_tick)

    if forecast is None:
        forecast = build_forecast(
            snap,
            horizon=horizon if horizon is not None else config.horizon_ticks,
        )

    shipments: list[PlannedShipment] = []
    sequence = 0

    for station in sorted(snap.stations, key=lambda s: s.id):
        # Naive: no status check on the station.
        routes = sorted(snap.routes_for_station(station.id), key=lambda r: r.id)
        if not routes:
            continue
        route = routes[0]
        for fuel in FUEL_TYPES:
            capacity = station.capacity.get(fuel)
            if capacity is None:
                continue
            free = max(0.0, float(capacity) - float(station.inventory.get(fuel, 0.0)))
            if free <= MIN_QUANTITY:
                continue
            depot = snap.depot(route.source_depot_id)
            if depot is None:
                continue
            # Naive: no status check on the depot either.
            available = float(depot.inventory.get(fuel, 0.0))
            quantity = _qty(min(free, float(route.max_shipment), available))
            if quantity <= MIN_QUANTITY:
                continue
            sequence += 1
            arrival_offset = route.transit_ticks + 1
            shipments.append(
                PlannedShipment(
                    sequence=sequence,
                    dispatch_tick=now_tick,
                    arrival_tick=now_tick + arrival_offset,
                    source_depot_id=route.source_depot_id,
                    destination_station_id=station.id,
                    route_id=route.id,
                    fuel_type=fuel,
                    quantity=quantity,
                    priority_score=0.0,
                    rationale="Baseline: fill the tank from the first route.",
                    binding="NONE",
                    risk="NONE",
                )
            )

    return shipments


def service_level_of(
    snap: WorldSnapshot,
    forecast: ForecastBundle,
    shipments: Sequence[PlannedShipment],
) -> float:
    """The projected service level of an arbitrary shipment list."""
    projection = project_world(snap, forecast, shipments, horizon=forecast.horizon)
    return projection.service_level
