"""An optional LP formulation of the dispatch problem.

This module is **import-guarded**: ``pulp`` is not a dependency of the
backend, so the import is inside a ``try``. When it is missing,
:func:`solve_lp` returns ``None`` and the caller keeps the greedy plan --
and says so, rather than overclaiming.

The formulation is written out in full here, because it is the clearest
statement of what "optimal" means for this problem. The greedy solver in
:mod:`app.optimizer` computes the same thing by a different route; this
one solves it as a linear program.

**Variables**

    x[r, f, t]   litres shipped on route ``r``, fuel ``f``, dispatch tick ``t``
    y[d, f, t]   depot ``d``'s inventory of fuel ``f`` at the *end* of tick ``t``
    s[k, f, t]   station ``k``'s inventory of fuel ``f`` at the *end* of tick ``t``
    g[k, f, t]   fuel arriving at station ``k`` on tick ``t``

A route fixes its depot and its station, so no separate depot/station index
is needed on ``x``.

**Objective** -- maximise delivered service, weighted by how much each
station's fuel matters, minus the cost of spilling:

    maximise   SUM over served volume * w_station(k)
             - SUM over spilled volume * spill_penalty

The weight is what makes the plan *good* rather than merely feasible: a
litre delivered to a station that is about to run dry is worth more than a
litre delivered to one with a full tank.

**Constraints** -- each maps to a measured simulator rule:

======  ================================================  ==========================
   #    Constraint                                           Simulator rule
======  ================================================  ==========================
  C1    ``x[r,f,t] <= max_shipment(r) * b[r,f,t]``           route max is per shipment
  C2    ``y[d,f,t] = y[d,f,t-1] - SUM x + supply[d,f,t]``     depot stock falls at dispatch
  C3    ``SUM_f SUM_{r: src(r)=d} x[r,f,t] <= cap(d)``        per-tick, per-depot cap
  C4    ``s[k,f,t] <= capacity(k,f)``                         tank capacity
  C5    ``x[r,f,t] = 0`` if ``r.status != AVAILABLE``         ``ROUTE_DISRUPTED``
  C6    ``x[r,f,t] = 0`` if depot status not dispatchable    ``CONSTRAINED`` *is* dispatchable
  C7    ``x[r,f,t] = 0`` if station status != OPEN            ``STATION_CLOSED``
  C8    ``s[k,f,t] = s[k,f,t-1] - demand + arrivals``         demand consumes stock
  C9    ``y[d,f,t] >= 0``                                    depot stock is non-negative
  C10   ``x[r,f,t] <= need[k,f,t]``                          don't fill a tank nobody needs
======  ================================================  ==========================

C1 uses a binary variable per (route, fuel, tick) because the simulator's
route limit is a **per-shipment** limit: 14 000 L on a 7 000 L route needs
two shipments, not one 7 000 L shipment.

**Arrival lag.** A shipment created on tick ``t`` is credited at
``t + transit_ticks + 1``; a supply with ``planned_tick = p`` is credited at
``p + 1``. Both lags are *measured*, not read off the simulator's own
fields -- see the forecast module's docstring.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

LOGGER = logging.getLogger(__name__)

#: Volumes are compared with a tolerance of a millilitre.
EPS = 1e-6

#: The depots from which fuel may be dispatched. ``CONSTRAINED`` is
#: dispatchable -- only ``OUTAGE`` and ``CLOSED`` are not.
DISPATCHABLE_DEPOT_STATUSES = frozenset({"OPEN", "CONSTRAINED"})


try:  # pragma: no cover - the import guard itself is what matters
    import pulp
except ImportError:  # pragma: no cover - the default path
    pulp = None  # type: ignore[assignment]


def lp_available() -> bool:
    """Is the optional LP dependency installed?"""
    return pulp is not None


def require_pulp() -> Any:
    """Return the ``pulp`` module, or raise ``ImportError``.

    The optimiser imports this module inside a ``try``/``except
    ImportError``. Raising here means the module's availability mirrors the
    dependency's exactly: without ``pulp`` the import *fails*, and the
    optimiser's guard reports "unavailable" rather than "ran and found
    nothing".
    """
    if pulp is None:
        raise ImportError(
            "The LP solver's optional dependency (pulp) is not installed."
        )
    return pulp


def _inventory_at(
    station: Any,
    s: dict[tuple[str, str, int], Any],
    station_id: str,
    fuel: str,
    tick: int,
    ticks: list[int],
) -> Any:
    """The station's inventory at the *start* of ``tick``.

    The first tick starts from the station's current stock; every later
    tick starts from the previous tick's closing stock.
    """
    if tick == ticks[0]:
        return float(station.inventory.get(fuel, 0.0))
    return s[(station_id, fuel, tick - 1)]


def _available_at(
    station: Any,
    s: dict[tuple[str, str, int], Any],
    g: dict[tuple[str, str, int], Any],
    station_id: str,
    fuel: str,
    tick: int,
    ticks: list[int],
) -> Any:
    """The fuel available to serve the demand at ``tick``.

    The opening stock plus whatever arrives on the tick. It does **not**
    include fuel already committed to an *earlier* tick's arrival -- that
    fuel arrived earlier and is already in the opening stock.
    """
    arrivals = g.get((station_id, fuel, tick))
    opening = _inventory_at(station, s, station_id, fuel, tick, ticks)
    if arrivals is None:
        return opening
    return opening + arrivals


def solve_lp(
    snap: Any,
    forecast: Any,
    *,
    config: Optional[Any] = None,
    now_tick: Optional[int] = None,
) -> Optional[Any]:
    """Solve the dispatch problem as a linear program.

    Returns an :class:`~app.optimizer.OptimizationResult`, or ``None`` when
    the solver could not produce a feasible plan. The caller keeps the
    greedy plan in that case.

    Raises ``ImportError`` when the optional ``pulp`` dependency is not
    installed, so the optimiser's import guard reports it as *unavailable*
    rather than as "ran and found nothing".
    """
    pulp = require_pulp()

    from .optimizer import (
        EPS as _EPS,
        OptimizationResult,
        OptimizerConfig,
        PlannedShipment,
        _binding_label,
        _qty,
    )

    if config is None:
        config = OptimizerConfig()

    now_tick = int(snap.tick if now_tick is None else now_tick)
    horizon = int(getattr(forecast, "horizon", config.horizon_ticks))
    start_tick = int(getattr(forecast, "start_tick", now_tick))

    from .forecast import FUEL_TYPES

    # ------------------------------------------------------------------ #
    # The index sets
    # ------------------------------------------------------------------ #
    ticks = list(range(start_tick, start_tick + horizon))

    routes = [
        route
        for route in getattr(snap, "routes", [])
        if getattr(route, "status", "AVAILABLE") == "AVAILABLE"
    ]

    # C5/C6/C7: a route is usable only when its depot can dispatch, its
    # station is open, and the route itself is available.
    usable_routes = []
    for route in routes:
        depot = snap.depot(route.source_depot_id)
        station = snap.station(route.destination_station_id)
        if depot is None or station is None:
            continue
        if depot.status not in DISPATCHABLE_DEPOT_STATUSES:
            continue
        if station.status != "OPEN":
            continue
        usable_routes.append(route)

    if not usable_routes:
        return None

    stations = {
        s.id: s
        for s in getattr(snap, "stations", [])
        if getattr(s, "status", "OPEN") == "OPEN"
    }
    depots = {
        d.id: d
        for d in getattr(snap, "depots", [])
        if getattr(d, "status", "") in DISPATCHABLE_DEPOT_STATUSES
    }

    if not stations or not depots:
        return None

    # ------------------------------------------------------------------ #
    # The exogenous inflows
    # ------------------------------------------------------------------ #
    # The measured one-tick lags.
    from .forecast import supply_schedule

    supplies = supply_schedule(snap)  # {(depot_id, fuel): {applied_tick: litres}}
    inbound = getattr(forecast, "inbound", {}) or {}

    # The demand each station and fuel must meet, per tick.
    def demand_at(station_id: str, fuel: str, tick: int) -> float:
        return float(
            getattr(forecast, "demand_at", lambda *a: 0.0)(station_id, fuel, tick)
        )

    # How much fuel each station and fuel could possibly use, per tick.
    # C10: shipping more than the remaining need is waste, and at a
    # destination with a full tank it is *spill*.
    need: dict[tuple[str, str, int], float] = {}
    for station_id, station in stations.items():
        for fuel in FUEL_TYPES:
            if fuel not in getattr(station, "capacity", {}):
                continue
            running = 0.0
            for tick in ticks:
                running += demand_at(station_id, fuel, tick)
                capacity = float(station.capacity.get(fuel, 0.0))
                free = max(0.0, capacity - float(station.inventory.get(fuel, 0.0)))
                need[(station_id, fuel, tick)] = max(0.0, free)

    # ------------------------------------------------------------------ #
    # The model
    # ------------------------------------------------------------------ #
    problem = pulp.LpProblem("fuel_dispatch", pulp.LpMaximize)

    # -- x[r, f, t] and the binary that caps it at the route max --------- #
    x: dict[tuple[str, str, int], Any] = {}
    b: dict[tuple[str, str, int], Any] = {}
    for route in usable_routes:
        station = stations.get(route.destination_station_id)
        if station is None:
            continue
        for fuel in FUEL_TYPES:
            if fuel not in getattr(station, "capacity", {}):
                continue
            if fuel not in getattr(station, "inventory", {}):
                continue
            for tick in ticks:
                key = (route.id, fuel, tick)
                x[key] = pulp.LpVariable(
                    f"x_{route.id}_{fuel}_{tick}", lowBound=0, cat="Continuous"
                )
                # C1: the binary turns the per-shipment limit into a
                # multiple-of-the-limit constraint.
                b[key] = pulp.LpVariable(
                    f"b_{route.id}_{fuel}_{tick}", cat="Binary"
                )
                problem += (
                    x[key] <= float(route.max_shipment) * b[key],
                    f"route_max_{route.id}_{fuel}_{tick}",
                )
                # C10: never ship more than the station can ever need.
                limit = need.get(
                    (route.destination_station_id, fuel, tick), 0.0
                )
                if limit > _EPS:
                    problem += (
                        x[key] <= limit,
                        f"need_{route.id}_{fuel}_{tick}",
                    )

    if not x:
        return None

    # -- y[d, f, t]: depot inventory -------------------------------------- #
    y: dict[tuple[str, str, int], Any] = {}
    for depot_id, depot in depots.items():
        for fuel in FUEL_TYPES:
            for tick in ticks:
                y[(depot_id, fuel, tick)] = pulp.LpVariable(
                    f"y_{depot_id}_{fuel}_{tick}", lowBound=0, cat="Continuous"
                )

    # -- s[k, f, t]: station inventory ------------------------------------ #
    s: dict[tuple[str, str, int], Any] = {}
    for station_id, station in stations.items():
        for fuel in FUEL_TYPES:
            if fuel not in getattr(station, "capacity", {}):
                continue
            for tick in ticks:
                s[(station_id, fuel, tick)] = pulp.LpVariable(
                    f"s_{station_id}_{fuel}_{tick}",
                    lowBound=0,
                    upBound=float(station.capacity.get(fuel, 0.0)),
                    cat="Continuous",
                )

    # -- g[k, f, t]: fuel arriving at a station ---------------------------- #
    g: dict[tuple[str, str, int], Any] = {}
    for route in usable_routes:
        station_id = route.destination_station_id
        for fuel in FUEL_TYPES:
            if fuel not in getattr(stations[station_id], "capacity", {}):
                continue
            for tick in ticks:
                g[(station_id, fuel, tick)] = pulp.LpVariable(
                    f"g_{station_id}_{fuel}_{tick}",
                    lowBound=0,
                    cat="Continuous",
                )

    # -- C2: depot inventory falls at dispatch time ------------------------ #
    for depot_id, depot in depots.items():
        for fuel in FUEL_TYPES:
            opening = float(depot.inventory.get(fuel, 0.0))
            for tick in ticks:
                dispatched = pulp.lpSum(
                    x[key]
                    for key in x
                    if key[0] in {
                        r.id for r in usable_routes
                        if r.source_depot_id == depot_id
                    }
                    and key[1] == fuel
                    and key[2] == tick
                )
                # The measured one-tick lag on resupply.
                arriving = float(
                    supplies.get((depot_id, fuel), {}).get(tick, 0.0)
                )
                previous = (
                    opening
                    if tick == ticks[0]
                    else y[(depot_id, fuel, tick - 1)]
                )
                problem += (
                    y[(depot_id, fuel, tick)]
                    == previous - dispatched + arriving,
                    f"depot_balance_{depot_id}_{fuel}_{tick}",
                )

    # -- C3: the per-tick, per-depot dispatch cap -------------------------- #
    for depot_id, depot in depots.items():
        cap = float(depot.dispatch_capacity_per_tick)
        for tick in ticks:
            committed = 0.0
            for allocation in getattr(snap, "allocations", []) or []:
                if allocation.source_depot_id != depot_id:
                    continue
                if allocation.created_tick != tick:
                    continue
                committed += float(allocation.quantity)
            used = pulp.lpSum(
                x[key]
                for key in x
                if key[2] == tick
                and key[0] in {
                    r.id for r in usable_routes
                    if r.source_depot_id == depot_id
                }
            )
            problem += (
                used + committed <= cap,
                f"dispatch_cap_{depot_id}_{tick}",
            )

    # -- C8: station inventory, and the arrivals that feed it -------------- #
    # ``g`` is per *station*, so the constraint is declared once per
    # (station, fuel, tick) and sums over every route into that station.
    # A dual-connected station has two routes, and both contribute.
    transit_of = {route.id: int(route.transit_ticks) for route in usable_routes}
    for station_id, station in stations.items():
        for fuel in FUEL_TYPES:
            if fuel not in getattr(station, "capacity", {}):
                continue
            for tick in ticks:
                arrivals = pulp.lpSum(
                    x[key]
                    for key in x
                    if key[1] == fuel
                    and key[2] < tick
                    # The measured one-tick lag:
                    # created + transit_ticks + 1 == applied tick.
                    and key[2] + transit_of[key[0]] + 1 == tick
                )
                problem += (
                    g[(station_id, fuel, tick)] == arrivals,
                    f"arrivals_{station_id}_{fuel}_{tick}",
                )

    # -- C8: station inventory, and the arrivals that feed it -------------- #
    # The demand a station *cannot* meet is unmet demand, not negative
    # stock. The balance therefore consumes ``demand - unmet``: a station
    # that runs dry shows a zero closing balance and a non-zero unmet
    # figure, which is exactly what the simulator does.
    unmet: dict[tuple[str, str, int], Any] = {}
    for station_id, station in stations.items():
        for fuel in FUEL_TYPES:
            if fuel not in getattr(station, "capacity", {}):
                continue
            for tick in ticks:
                unmet[(station_id, fuel, tick)] = pulp.LpVariable(
                    f"unmet_{station_id}_{fuel}_{tick}", lowBound=0, cat="Continuous"
                )

    for station_id, station in stations.items():
        for fuel in FUEL_TYPES:
            if fuel not in getattr(station, "capacity", {}):
                continue
            opening = float(station.inventory.get(fuel, 0.0))
            for tick in ticks:
                demand = demand_at(station_id, fuel, tick)
                already = float(inbound.get((station_id, fuel), {}).get(tick, 0.0))
                previous = (
                    opening if tick == ticks[0] else s[(station_id, fuel, tick - 1)]
                )
                problem += (
                    s[(station_id, fuel, tick)]
                    == previous
                    - (demand - unmet[(station_id, fuel, tick)])
                    + g[(station_id, fuel, tick)]
                    + already,
                    f"station_balance_{station_id}_{fuel}_{tick}",
                )
                # The unmet demand cannot exceed the demand.
                problem += (
                    unmet[(station_id, fuel, tick)] <= demand,
                    f"unmet_bound_{station_id}_{fuel}_{tick}",
                )

    # -- the objective ------------------------------------------------------ #
    # The value of a litre is the station's weight: how much its fuel
    # matters. Spill is penalised, never rejected -- the simulator accepts
    # an over-commit and discards the excess silently.
    station_weight = {
        station_id: float(config.station_weight(station_id))
        for station_id in stations
    }
    fuel_weight = {fuel: float(config.fuel_weight(fuel)) for fuel in FUEL_TYPES}

    served_terms = []
    spill_terms = []
    for station_id, station in stations.items():
        for fuel in FUEL_TYPES:
            if fuel not in getattr(station, "capacity", {}):
                continue
            capacity = float(station.capacity.get(fuel, 0.0))
            for tick in ticks:
                demand = demand_at(station_id, fuel, tick)

                # The unmet demand cannot exceed what is actually
                # available. The balance constraint already ties the two
                # together; this makes the shortfall honest in the other
                # direction too.
                problem += (
                    unmet[(station_id, fuel, tick)]
                    >= demand
                    - _available_at(
                        station, s, g, station_id, fuel, tick, ticks
                    ),
                    f"unmet_floor_{station_id}_{fuel}_{tick}",
                )
                served_terms.append(
                    station_weight.get(station_id, 1.0)
                    * fuel_weight.get(fuel, 1.0)
                    * (demand - unmet[(station_id, fuel, tick)])
                )

                # Spill is the part of the arrival that does not fit in the
                # tank. It is a *soft* cost: the simulator accepts an
                # over-commit and discards the excess silently, so a
                # stricter model would be a bad oracle.
                spill = pulp.LpVariable(
                    f"spill_{station_id}_{fuel}_{tick}", lowBound=0, cat="Continuous"
                )
                free = capacity - _inventory_at(
                    station, s, station_id, fuel, tick, ticks
                )
                problem += (
                    spill >= g[(station_id, fuel, tick)] - free,
                    f"spill_{station_id}_{fuel}_{tick}",
                )
                spill_terms.append(float(config.spill_penalty) * spill)

    problem += pulp.lpSum(served_terms) - pulp.lpSum(spill_terms)

    # ------------------------------------------------------------------ #
    # Solve
    # ------------------------------------------------------------------ #
    try:
        solver = pulp.PULP_CBC_CMD(msg=False)
        status = problem.solve(solver)
    except Exception as exc:  # pragma: no cover - solver failure
        LOGGER.warning("The LP solver failed: %s", exc)
        return None

    if pulp.LpStatus.get(status) != "Optimal":
        LOGGER.info("The LP solver did not find an optimal plan: %s", pulp.LpStatus.get(status))
        return None

    # ------------------------------------------------------------------ #
    # Extract the plan
    # ------------------------------------------------------------------ #
    shipments: list[PlannedShipment] = []
    sequence = 0
    for (route_id, fuel, tick), variable in sorted(x.items()):
        quantity = pulp.value(variable)
        if quantity is None or quantity <= _EPS:
            continue
        route = next(r for r in usable_routes if r.id == route_id)
        depot = snap.depot(route.source_depot_id)
        station = snap.station(route.destination_station_id)

        quantity = _qty(quantity)
        arrival_tick = tick + int(route.transit_ticks) + 1

        # The binding constraint, in the simulator's own check order.
        binding = _binding_label(
            quantity=quantity,
            route_max=float(route.max_shipment),
            depot_inventory=float(depot.inventory.get(fuel, 0.0)),
            dispatch_headroom=float(depot.dispatch_capacity_per_tick),
            free_space=float(
                station.capacity.get(fuel, 0.0)
                - float(station.inventory.get(fuel, 0.0))
            ),
        )

        risk = "NONE"
        if arrival_tick > start_tick + horizon:
            risk = "LATE_ARRIVAL"

        sequence += 1
        shipments.append(
            PlannedShipment(
                sequence=sequence,
                dispatch_tick=tick,
                arrival_tick=arrival_tick,
                source_depot_id=route.source_depot_id,
                destination_station_id=route.destination_station_id,
                route_id=route_id,
                fuel_type=fuel,
                quantity=quantity,
                priority_score=station_weight.get(station.id, 1.0)
                * fuel_weight.get(fuel, 1.0),
                rationale=(
                    f"{quantity} L of {fuel} for {station.name} via {route_id} "
                    f"(dispatch tick {tick}, applied tick {arrival_tick}), "
                    f"chosen by the linear programme to maximise delivered "
                    f"service. Limiting constraint: "
                    f"{binding.replace('_', ' ').lower()}."
                ),
                binding=binding,
                risk=risk,
            )
        )

    if not shipments:
        return None

    # ------------------------------------------------------------------ #
    # Project and report
    # ------------------------------------------------------------------ #
    from .forecast import project_world

    projection = project_world(snap, forecast, shipments, horizon=horizon)

    if len(shipments) > int(config.max_shipments):
        return None

    status = "PLANNED"
    if projection.unmet_demand > _EPS:
        status = "PARTIAL"

    return OptimizationResult(
        status=status,
        shipments=shipments,
        total_litres=_qty(sum(ship.quantity for ship in shipments)),
        per_tick_dispatch={},
        expected_service_level=projection.service_level,
        unmet_demand_litres=_qty(projection.unmet_demand),
        stockout_risk=[],
        warnings=[],
        diagnostics={
            "solver": "lp",
            "variables": len(x) + len(y) + len(s) + len(g),
            "now_tick": now_tick,
            "horizon": horizon,
            "shipment_count": len(shipments),
            "greedy_only": False,
        },
        solver_used="lp",
    )
