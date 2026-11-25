"""Pre-flight dispatch validator.

This is the highest-value file in the backend. The simulator rejects invalid
shipments with **nine distinct 409 codes**; a backend that guesses will be
rejected constantly and burn operator trust. This module decides what is legal
*before* any write reaches the simulator.

Design rules
------------
1. **Pure functions, zero I/O.** The validator is a function of a
   :class:`~app.state.WorldSnapshot` and a request. That makes it unit-testable
   without a simulator and lets the Phase 2 optimisation engine call it as a
   final feasibility gate.

2. **Hard rules mirror the simulator exactly.** Anything this module approves
   is accepted by the simulator -- no 409 we did not predict. This is the
   *soundness* property, and it is why the validator must never be stricter
   than the oracle: a stricter validator is a bad test oracle.

3. **Soft rules report risk without rejecting.** The simulator's destination
   capacity check ignores in-flight fuel and silently spills the excess on
   arrival. Rejecting that would make us stricter than the oracle, so it is
   surfaced as a ``SPILL_RISK`` warning with the exact litres at risk.

Validation order (first failure wins) mirrors the simulator's own order:
``IDEMPOTENCY_KEY_MISMATCH`` -> ``NOT_FOUND`` -> ``ROUTE_MISMATCH`` ->
``DEPOT_CLOSED`` -> ``STATION_CLOSED`` -> ``ROUTE_DISRUPTED`` ->
``ROUTE_CAPACITY_EXCEEDED`` -> ``INSUFFICIENT_INVENTORY`` ->
``DISPATCH_CAPACITY_EXCEEDED`` -> ``DESTINATION_CAPACITY_EXCEEDED``.

All of the above was measured against the running simulator, not read from
documentation. See ``docs/BACKEND.md``.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from .models import (
    BindingConstraint,
    PlanExplanation,
    PlanRequest,
    PlanResult,
    RouteAlternative,
    Shipment,
    Warning,
)
from .state import WorldSnapshot

__all__ = [
    "VALID_FUEL_TYPES",
    "DISPATCHABLE_DEPOT_STATUSES",
    "dispatch_committed_this_tick",
    "dispatch_remaining",
    "station_free_space",
    "station_inbound",
    "select_route",
    "validate_and_plan",
]

#: The three fuel types the simulator recognises.
VALID_FUEL_TYPES: frozenset[str] = frozenset({"DIESEL", "PETROL", "OCTANE"})

#: A depot in ``CONSTRAINED`` state is *still dispatchable* -- measured, not
#: assumed. Only statuses outside this set raise ``DEPOT_CLOSED``.
DISPATCHABLE_DEPOT_STATUSES: frozenset[str] = frozenset({"OPEN", "CONSTRAINED"})

#: Statuses that count as "committed" against the current tick's dispatch cap.
_COMMITTED_STATUSES: frozenset[str] = frozenset({"PENDING", "IN_TRANSIT"})

#: Volumes are rounded to whole millilitres to keep float arithmetic clean.
_ML = 1000.0

#: The order in which the simulator applies its limits.
#:
#: **Measured, not assumed.** The simulator checks limits in a *fixed order*,
#: not in order of magnitude. A request of 7500 L against a route with
#: ``max_shipment=7000`` and a station with 6000 L of free space is rejected
#: with ``ROUTE_CAPACITY_EXCEEDED`` -- not ``DESTINATION_CAPACITY_EXCEEDED`` --
#: even though the station is the tighter limit.
#:
#: So the binding constraint is the *first* limit in this order that the
#: request exceeds, never the numerically smallest one.
_CONSTRAINT_ORDER: tuple[str, ...] = (
    "ROUTE_MAX_SHIPMENT",
    "DEPOT_INVENTORY",
    "DISPATCH_CAPACITY",
    "STATION_FREE_SPACE",
)


def _binding_constraint(ceilings: dict[str, Any], quantity: float) -> Optional[str]:
    """The limit the simulator would report for a request of ``quantity``.

    Returns the first limit in the simulator's validation order that the
    quantity exceeds, or ``None`` when the request fits within all of them.
    """
    for kind in _CONSTRAINT_ORDER:
        limit = float(ceilings[kind][0])
        if quantity > limit:
            return kind
    return None


def _tightest(ceilings: dict[str, Any]) -> str:
    """The numerically smallest limit -- used for *planning*, not for reporting.

    Planning genuinely wants the smallest ceiling (that is how much can be
    shipped right now). Reporting a rejection wants the simulator's order.
    The two are different questions and are deliberately separate functions.
    """
    return min(
        ceilings,
        key=lambda kind: (float(ceilings[kind][0]), _CONSTRAINT_ORDER.index(kind)),
    )


def _round(value: float) -> float:
    """Round to the nearest millilitre."""
    return round(value * _ML) / _ML


def _qty(value: float) -> float:
    """Normalise a volume for JSON output (integers stay integers)."""
    rounded = _round(value)
    if abs(rounded - round(rounded)) < 1e-6:
        return float(round(rounded))
    return rounded


# --------------------------------------------------------------------------- #
# Constraint helpers -- each one mirrors a specific simulator check
# --------------------------------------------------------------------------- #
def dispatch_committed_this_tick(snap: WorldSnapshot, depot_id: str) -> float:
    """Litres already committed for dispatch by ``depot_id`` at the CURRENT tick.

    Mirrors the simulator exactly: filters on ``created_tick == current_tick``
    (NOT ``departure_tick``) and status in ``(PENDING, IN_TRANSIT)``.

    Shipments dispatched on an earlier tick do **not** count against this
    tick's allowance -- the cap resets every tick. This was measured (6000 +
    6000 across two different routes from gazipur was accepted in one tick;
    a third shipment in the same tick was rejected) and confirmed against the
    simulator's source.
    """
    current_tick = snap.tick
    total = 0.0
    for allocation in snap.allocations:
        if allocation.source_depot_id != depot_id:
            continue
        if allocation.created_tick != current_tick:
            continue
        if allocation.status not in _COMMITTED_STATUSES:
            continue
        total += allocation.quantity
    return _qty(total)


def dispatch_remaining(snap: WorldSnapshot, depot_id: str) -> float:
    """Remaining dispatch allowance for ``depot_id`` at the current tick."""
    depot = snap.depot(depot_id)
    if depot is None:
        return 0.0
    committed = dispatch_committed_this_tick(snap, depot_id)
    return _qty(max(0.0, depot.dispatch_capacity_per_tick - committed))


def station_free_space(snap: WorldSnapshot, station_id: str, fuel: str) -> float:
    """Free space at ``station_id`` for ``fuel``.

    Mirror of the simulator's destination check: it compares live
    ``station.inventory`` against ``station.capacity`` only. In-flight fuel is
    deliberately **not** subtracted here, because the simulator does not
    subtract it either. Doing so would make the validator stricter than the
    oracle. The spill exposure is reported separately by
    :func:`station_inbound`.
    """
    station = snap.station(station_id)
    if station is None:
        return 0.0
    capacity = float(station.capacity.get(fuel, 0.0))
    inventory = float(station.inventory.get(fuel, 0.0))
    return _qty(max(0.0, capacity - inventory))


def station_inbound(snap: WorldSnapshot, station_id: str, fuel: str) -> float:
    """Litres already in flight *to* ``station_id`` for ``fuel``.

    Drives the ``SPILL_RISK`` warning: the simulator checks only live
    inventory at the destination, so fuel committed in flight can overflow the
    station's capacity and be **silently discarded on arrival** with no error
    and no audit flag.
    """
    total = 0.0
    for allocation in snap.allocations:
        if allocation.destination_station_id != station_id:
            continue
        if allocation.fuel_type != fuel:
            continue
        if allocation.status not in _COMMITTED_STATUSES:
            continue
        total += allocation.quantity
    return _qty(total)


# --------------------------------------------------------------------------- #
# Route selection
# --------------------------------------------------------------------------- #
def _route_rank(route: Any) -> tuple[Any, ...]:
    """Deterministic preference order for candidate routes.

    Prefer ``AVAILABLE`` routes, then the shortest transit time, then the
    largest single-shipment capacity, then the route id lexicographically. The
    final tie-break on id makes selection fully deterministic -- important
    because the same request must always produce the same plan (and therefore
    the same idempotency key).
    """
    available = 0 if route.status == "AVAILABLE" else 1
    return (available, route.transit_ticks, -route.max_shipment, route.id)


def select_route(
    snap: WorldSnapshot,
    *,
    source_depot_id: Optional[str],
    destination_station_id: str,
    explicit_route_id: Optional[str] = None,
    allow_alternate_route: bool = False,
) -> tuple[Optional[Any], list[RouteAlternative], bool]:
    """Pick the route to use.

    Returns ``(route, alternatives, is_only_route)``.

    * An explicit ``route_id`` is **never** silently overridden -- it is
      returned as-is (validation later reports why it is unusable).
    * Otherwise candidates are the routes serving the destination station.
      When ``allow_alternate_route`` is false, only routes from
      ``source_depot_id`` are considered.
    * ``is_only_route`` is true when the destination has exactly one route, so
      no rerouting is possible at all.
    """
    all_station_routes = snap.routes_for_station(destination_station_id)
    is_only_route = len(all_station_routes) == 1

    alternatives: list[RouteAlternative] = []

    if explicit_route_id is not None:
        route = snap.route(explicit_route_id)
        for candidate in all_station_routes:
            alternatives.append(
                RouteAlternative(
                    route_id=candidate.id,
                    source_depot_id=candidate.source_depot_id,
                    status=candidate.status,
                    transit_ticks=candidate.transit_ticks,
                    max_shipment=candidate.max_shipment,
                    selected=candidate.id == explicit_route_id,
                )
            )
        return route, alternatives, is_only_route

    candidates = list(all_station_routes)
    if not allow_alternate_route and source_depot_id is not None:
        candidates = [r for r in candidates if r.source_depot_id == source_depot_id]
    if not candidates:
        # Fall back to any route so the operator sees *why* it cannot be used
        # rather than an empty list.
        candidates = list(all_station_routes)

    ordered = sorted(candidates, key=_route_rank)
    chosen = ordered[0] if ordered else None

    for candidate in ordered:
        reason: Optional[str] = None
        if chosen is not None and candidate.id != chosen.id:
            if candidate.status != "AVAILABLE" and chosen.status == "AVAILABLE":
                reason = f"route status is {candidate.status}"
            elif candidate.transit_ticks > chosen.transit_ticks:
                reason = (
                    f"longer transit ({candidate.transit_ticks} ticks vs "
                    f"{chosen.transit_ticks})"
                )
            elif candidate.max_shipment < chosen.max_shipment:
                reason = (
                    f"smaller max shipment ({candidate.max_shipment} L vs "
                    f"{chosen.max_shipment} L)"
                )
            else:
                reason = "deterministic tie-break on route id"
        alternatives.append(
            RouteAlternative(
                route_id=candidate.id,
                source_depot_id=candidate.source_depot_id,
                status=candidate.status,
                transit_ticks=candidate.transit_ticks,
                max_shipment=candidate.max_shipment,
                selected=chosen is not None and candidate.id == chosen.id,
                rejected_because=reason,
            )
        )

    return chosen, alternatives, is_only_route


# --------------------------------------------------------------------------- #
# The validator
# --------------------------------------------------------------------------- #
def _reject(
    snap: WorldSnapshot,
    req: PlanRequest,
    code: str,
    message: str,
    *,
    http_status: int = 409,
    diagnostics: Optional[dict[str, Any]] = None,
    explanation: Optional[PlanExplanation] = None,
    binding_constraint: Optional[BindingConstraint] = None,
) -> PlanResult:
    return PlanResult(
        status="REJECTED",
        request=req,
        shipments=[],
        total_planned_litres=0.0,
        deferred_litres=0.0,
        reject_code=code,
        reject_message=message,
        http_status=http_status,
        binding_constraint=binding_constraint,
        diagnostics=diagnostics or {},
        explanation=explanation,
        simulated_tick=snap.tick,
    )


def validate_and_plan(
    snap: WorldSnapshot,
    req: PlanRequest,
    *,
    allow_split: bool = True,
    key_factory: Optional[Callable[["WorldSnapshot", PlanRequest, int], str]] = None,
) -> PlanResult:
    """Validate ``req`` against ``snap`` and produce a legal shipment plan.

    The function never raises for a business-rule violation -- it returns a
    ``REJECTED`` result carrying the code, the message and the raw numbers.
    That keeps the decision inspectable, which is the point.
    """
    if key_factory is None:
        from .keys import make_key  # local import avoids a circular dependency

        key_factory = make_key

    # -- 1. Request sanity (mirrors the simulator's 422) -------------------- #
    if req.quantity <= 0:
        return _reject(
            snap, req, "INVALID_QUANTITY",
            f"quantity must be greater than zero (got {req.quantity})",
            http_status=422,
            diagnostics={"quantity": req.quantity},
        )
    if req.fuel_type not in VALID_FUEL_TYPES:
        return _reject(
            snap, req, "INVALID_FUEL_TYPE",
            f"unknown fuel type {req.fuel_type!r}; expected one of "
            f"{sorted(VALID_FUEL_TYPES)}",
            http_status=422,
            diagnostics={"fuel_type": req.fuel_type},
        )

    # -- 2. Entity existence (mirrors 404 NOT_FOUND) ------------------------ #
    depot = snap.depot(req.source_depot_id)
    station = snap.station(req.destination_station_id)
    missing = []
    if depot is None:
        missing.append(f"depot {req.source_depot_id!r}")
    if station is None:
        missing.append(f"station {req.destination_station_id!r}")
    if missing:
        return _reject(
            snap, req, "NOT_FOUND",
            f"unknown {' and '.join(missing)}",
            http_status=404,
            diagnostics={
                "source_depot_id": req.source_depot_id,
                "destination_station_id": req.destination_station_id,
            },
        )

    # -- 3. Route resolution and endpoint match (mirrors ROUTE_MISMATCH) ---- #
    route, alternatives, is_only_route = select_route(
        snap,
        source_depot_id=req.source_depot_id,
        destination_station_id=req.destination_station_id,
        explicit_route_id=req.route_id,
        allow_alternate_route=req.allow_alternate_route,
    )

    if route is None:
        return _reject(
            snap, req, "NOT_FOUND",
            f"no route serves station {req.destination_station_id!r}",
            http_status=404,
            diagnostics={
                "destination_station_id": req.destination_station_id,
                "routes_considered": [a.route_id for a in alternatives],
            },
            explanation=PlanExplanation(
                human_summary=(
                    f"No route connects any depot to "
                    f"{req.destination_station_id}, so this request cannot be "
                    f"dispatched."
                ),
                alternatives_considered=alternatives,
                is_only_route=is_only_route,
                rerouting_possible=False,
            ),
        )

    if route.id != req.route_id:
        # We chose a route ourselves. If the caller named a depot, the chosen
        # route must originate from it.
        if route.source_depot_id != req.source_depot_id:
            return _reject(
                snap, req, "ROUTE_MISMATCH",
                f"route {route.id} runs from depot {route.source_depot_id} to "
                f"station {route.destination_station_id}, which does not match "
                f"the requested depot {req.source_depot_id} -> station "
                f"{req.destination_station_id}",
                diagnostics={
                    "requested_route_id": req.route_id,
                    "selected_route_id": route.id,
                    "route_source_depot_id": route.source_depot_id,
                    "route_destination_station_id": route.destination_station_id,
                },
                explanation=PlanExplanation(
                    human_summary=(
                        f"No route serves {req.destination_station_id} from depot "
                        f"{req.source_depot_id}."
                    ),
                    alternatives_considered=alternatives,
                    is_only_route=is_only_route,
                    rerouting_possible=not is_only_route,
                ),
            )

    # -- 4. Endpoint match ------------------------------------------------- #
    if (route.source_depot_id != req.source_depot_id
            or route.destination_station_id != req.destination_station_id):
        return _reject(
            snap, req, "ROUTE_MISMATCH",
            f"route {route.id} runs from depot {route.source_depot_id} to "
            f"station {route.destination_station_id}, which does not match the "
            f"requested {req.source_depot_id} -> {req.destination_station_id}",
            diagnostics={
                "route_id": route.id,
                "route_source_depot_id": route.source_depot_id,
                "route_destination_station_id": route.destination_station_id,
            },
            explanation=PlanExplanation(
                human_summary=(
                    f"Route {route.id} does not connect the requested depot and "
                    f"station."
                ),
                route_choice=route.id,
                alternatives_considered=alternatives,
                is_only_route=is_only_route,
                rerouting_possible=not is_only_route,
            ),
        )

    # -- 5. Status checks (mirrors DEPOT_CLOSED / STATION_CLOSED / ROUTE_DISRUPTED) #
    if depot.status not in DISPATCHABLE_DEPOT_STATUSES:
        return _reject(
            snap, req, "DEPOT_CLOSED",
            f"depot {depot.id} is {depot.status} and cannot dispatch",
            diagnostics={"depot_id": depot.id, "depot_status": depot.status},
            explanation=PlanExplanation(
                human_summary=(
                    f"Depot {depot.name} is {depot.status}; it cannot dispatch "
                    f"right now."
                ),
                route_choice=route.id,
                alternatives_considered=alternatives,
                is_only_route=is_only_route,
                rerouting_possible=not is_only_route,
            ),
        )

    if station.status != "OPEN":
        return _reject(
            snap, req, "STATION_CLOSED",
            f"station {station.id} is {station.status} and cannot receive fuel",
            diagnostics={"station_id": station.id, "station_status": station.status},
            explanation=PlanExplanation(
                human_summary=(
                    f"Station {station.name} is {station.status}; it cannot "
                    f"receive fuel right now."
                ),
                route_choice=route.id,
                alternatives_considered=alternatives,
                is_only_route=is_only_route,
                rerouting_possible=not is_only_route,
            ),
        )

    if route.status != "AVAILABLE":
        return _reject(
            snap, req, "ROUTE_DISRUPTED",
            f"route {route.id} is {route.status} and cannot carry a shipment",
            diagnostics={"route_id": route.id, "route_status": route.status},
            explanation=PlanExplanation(
                human_summary=(
                    f"Route {route.id} is {route.status}. "
                    + (
                        "This station has only one route, so no rerouting is "
                        "possible."
                        if is_only_route
                        else "Another route may serve this station."
                    )
                ),
                route_choice=route.id,
                alternatives_considered=alternatives,
                is_only_route=is_only_route,
                rerouting_possible=not is_only_route,
            ),
        )

    # -- 6. Ceilings -------------------------------------------------------- #
    depot_inventory = _qty(float(depot.inventory.get(req.fuel_type, 0.0)))
    route_max = _qty(float(route.max_shipment))
    dispatch_headroom = dispatch_remaining(snap, req.source_depot_id)
    free_space = station_free_space(snap, req.destination_station_id, req.fuel_type)

    ceilings: dict[str, tuple[float, str, str]] = {
        "ROUTE_MAX_SHIPMENT": (
            route_max,
            f"route {route.id} max shipment is {route_max} L",
            f"route {route.id} cannot carry more than {route_max} L per shipment",
        ),
        "DEPOT_INVENTORY": (
            depot_inventory,
            f"depot {depot.id} holds {depot_inventory} L of {req.fuel_type}",
            f"depot {depot.id} only holds {depot_inventory} L of {req.fuel_type}",
        ),
        "DISPATCH_CAPACITY": (
            dispatch_headroom,
            f"depot {depot.id} dispatch allowance left this tick is "
            f"{dispatch_headroom} L",
            f"depot {depot.id} can dispatch only {dispatch_headroom} L more this tick",
        ),
        "STATION_FREE_SPACE": (
            free_space,
            f"station {station.id} has {free_space} L of free space for "
            f"{req.fuel_type}",
            f"station {station.id} only has {free_space} L of free space for "
            f"{req.fuel_type}",
        ),
    }

    # -- 7. Split into legal shipments ------------------------------------- #
    # ``req.allow_split`` is the request's own field and is authoritative. The
    # function keyword is an override that can only *disable* splitting.
    effective_split = allow_split and req.allow_split

    # When splitting is disabled the whole request must fit inside a *single*
    # shipment's ceilings. Checking this up front (rather than after the
    # split) keeps the rejection honest: the operator asked for one shipment,
    # and one shipment is not possible.
    if not effective_split:
        # Report the limit the *simulator* would report: the first one in its
        # validation order that this request exceeds.
        single_ceiling = _binding_constraint(ceilings, req.quantity)
        if single_ceiling is not None:
            limit = ceilings[single_ceiling][0]
            reason = ceilings[single_ceiling][2]
            return _reject(
                snap, req, single_ceiling,
                f"{reason}; the request is {req.quantity} L and splitting is "
                f"disabled",
                diagnostics={
                    "route_id": route.id,
                    "depot_id": depot.id,
                    "station_id": station.id,
                    "fuel_type": req.fuel_type,
                    "requested_litres": req.quantity,
                    "single_shipment_ceiling": limit,
                    "route_max_shipment": route_max,
                    "depot_inventory": depot_inventory,
                    "dispatch_remaining": dispatch_headroom,
                    "station_free_space": free_space,
                },
                binding_constraint=BindingConstraint(
                    kind=single_ceiling,
                    limit=limit,
                    available=_qty(limit),
                    detail=reason,
                ),
                explanation=_build_explanation(
                    req=req,
                    route=route,
                    depot=depot,
                    station=station,
                    alternatives=alternatives,
                    is_only_route=is_only_route,
                    status="REJECTED",
                    binding_detail=reason,
                ),
            )

    remaining = _qty(req.quantity)
    shipped = 0.0
    shipments: list[Shipment] = []
    sequence = 0

    # Running totals are tracked per depot and per station (dicts, not a
    # scalar) so a Phase 2 multi-destination planner needs no rewrite.
    depot_used: dict[str, float] = {}
    station_used: dict[str, float] = {}

    while remaining > 1e-9:
        sequence += 1
        live_depot_inventory = _qty(
            float(depot.inventory.get(req.fuel_type, 0.0)) - depot_used.get(depot.id, 0.0)
        )
        live_dispatch = _qty(
            dispatch_headroom - depot_used.get(depot.id, 0.0)
        )
        live_free_space = _qty(
            free_space - station_used.get(station.id, 0.0)
        )

        live_ceilings: dict[str, float] = {
            "ROUTE_MAX_SHIPMENT": route_max,
            "DEPOT_INVENTORY": max(0.0, live_depot_inventory),
            "DISPATCH_CAPACITY": max(0.0, live_dispatch),
            "STATION_FREE_SPACE": max(0.0, live_free_space),
        }

        # The chunk size is governed by the *numerically tightest* ceiling --
        # that is genuinely how much can be shipped right now.
        tightest_kind = min(
            live_ceilings,
            key=lambda k: (live_ceilings[k], _CONSTRAINT_ORDER.index(k)),
        )
        ceiling = _qty(live_ceilings[tightest_kind])

        # The *label* follows the simulator's validation order. If a limit
        # that the simulator checks earlier is exactly filled by this chunk,
        # that is the code it would report if the request were submitted
        # unsplit. (A 7000 L chunk fills the route max exactly and also
        # exceeds the station's 6000 L of free space; the simulator checks
        # the route first and reports ROUTE_CAPACITY_EXCEEDED.)
        binding_kind = tightest_kind
        for kind in _CONSTRAINT_ORDER:
            if kind == tightest_kind:
                break
            if _qty(live_ceilings[kind]) >= ceiling > 0:
                binding_kind = kind
                break

        if ceiling <= 1e-9:
            # Nothing more can be shipped right now.
            break

        chunk = _qty(min(remaining, ceiling))
        if chunk <= 1e-9:
            break

        # A single chunk larger than the request means the whole request fits.
        binding: list[BindingConstraint] = []
        for kind, (limit, available_detail, reason) in ceilings.items():
            if kind == binding_kind:
                binding.append(
                    BindingConstraint(
                        kind=kind,
                        limit=limit,
                        available=_qty(live_ceilings[kind]),
                        detail=reason,
                    )
                )
        # The binding limit is the one that actually shaped the chunk.
        binding[0].detail = ceilings[binding_kind][2]

        deferred = _qty(max(0.0, remaining - chunk))

        reasoning = _build_reasoning(
            sequence=sequence,
            chunk=chunk,
            binding_kind=binding_kind,
            binding_detail=ceilings[binding_kind][2],
            binding_available=live_ceilings[binding_kind],
            route=route,
            depot=depot,
            station=station,
            remaining_before=remaining,
            will_defer=deferred > 1e-9,
        )

        key = key_factory(snap, req, sequence)

        shipments.append(
            Shipment(
                sequence=sequence,
                idempotency_key=key,
                source_depot_id=depot.id,
                destination_station_id=station.id,
                route_id=route.id,
                fuel_type=req.fuel_type,
                quantity=chunk,
                binding_constraints=binding,
                reasoning=reasoning,
                deferred_litres=deferred,
            )
        )

        shipped = _qty(shipped + chunk)
        remaining = _qty(remaining - chunk)
        depot_used[depot.id] = _qty(depot_used.get(depot.id, 0.0) + chunk)
        station_used[station.id] = _qty(station_used.get(station.id, 0.0) + chunk)

        if len(shipments) >= 64:
            # Hard stop: 64 chunks is far beyond any realistic dispatch and
            # protects the API from a pathological split loop.
            break

        if not effective_split:
            # Splitting is off -- one shipment only. The pre-check above
            # guaranteed the whole request fits, so nothing is deferred.
            break

    deferred_total = _qty(req.quantity - shipped)

    # -- 8. Outcome --------------------------------------------------------- #
    if not shipments:
        # Nothing could be shipped. Report the limit the simulator would
        # report -- the first one in its validation order that the request
        # exceeds -- not merely the numerically smallest.
        binding_kind = _binding_constraint(ceilings, req.quantity)
        if binding_kind is None:
            # Every ceiling is individually sufficient but the split loop
            # still produced nothing (should not happen, but never guess).
            binding_kind = _tightest(ceilings)
        limit, _available, reason = ceilings[binding_kind]
        binding = BindingConstraint(
            kind=binding_kind,
            limit=limit,
            available=_qty(limit),
            detail=reason,
        )
        return _reject(
            snap, req, binding_kind, reason,
            diagnostics={
                "route_id": route.id,
                "depot_id": depot.id,
                "station_id": station.id,
                "fuel_type": req.fuel_type,
                "requested_litres": req.quantity,
                "route_max_shipment": route_max,
                "depot_inventory": depot_inventory,
                "dispatch_remaining": dispatch_headroom,
                "station_free_space": free_space,
            },
            binding_constraint=binding,
            explanation=_build_explanation(
                req=req,
                route=route,
                depot=depot,
                station=station,
                alternatives=alternatives,
                is_only_route=is_only_route,
                status="REJECTED",
                binding_detail=reason,
            ),
        )

    status = "PARTIAL" if deferred_total > 1e-9 else "PLANNED"

    # -- 9. Soft warnings --------------------------------------------------- #
    warnings: list[Warning] = []
    inbound = station_inbound(snap, req.destination_station_id, req.fuel_type)
    projected = _qty(free_space - station_used.get(station.id, 0.0))
    if inbound > 1e-9 and projected - inbound < 0:
        spill = _qty(inbound - projected)
        warnings.append(
            Warning(
                code="SPILL_RISK",
                message=(
                    f"{inbound} L of {req.fuel_type} is already in flight to "
                    f"{station.id}, but only {max(0.0, projected)} L of space "
                    f"remains after this plan. The simulator checks only live "
                    f"inventory, so up to {spill} L would be silently discarded "
                    f"on arrival."
                ),
                spill_risk_litres=spill,
                diagnostics={
                    "station_id": station.id,
                    "fuel_type": req.fuel_type,
                    "in_flight_litres": inbound,
                    "projected_free_space_litres": max(0.0, projected),
                    "station_capacity": float(
                        station.capacity.get(req.fuel_type, 0.0)
                    ),
                    "station_inventory": float(
                        station.inventory.get(req.fuel_type, 0.0)
                    ),
                },
            )
        )

    if is_only_route:
        warnings.append(
            Warning(
                code="SINGLE_CONNECTED_STATION",
                message=(
                    f"{station.id} is served by exactly one route "
                    f"({route.id}); any disruption there leaves no alternative."
                ),
                diagnostics={"station_id": station.id, "route_id": route.id},
            )
        )

    explanation = _build_explanation(
        req=req,
        route=route,
        depot=depot,
        station=station,
        alternatives=alternatives,
        is_only_route=is_only_route,
        status=status,
        binding_detail=ceilings[_tightest(ceilings)][2],
        shipped=shipped,
        deferred=deferred_total,
        chunks=len(shipments),
    )

    return PlanResult(
        status=status,
        request=req,
        shipments=shipments,
        total_planned_litres=shipped,
        deferred_litres=deferred_total,
        diagnostics={
            "tick": snap.tick,
            "route_id": route.id,
            "depot_id": depot.id,
            "station_id": station.id,
            "fuel_type": req.fuel_type,
            "requested_litres": req.quantity,
            "route_max_shipment": route_max,
            "depot_inventory": depot_inventory,
            "dispatch_remaining": dispatch_headroom,
            "station_free_space": free_space,
            "station_inbound": inbound,
            "chunk_count": len(shipments),
        },
        warnings=warnings,
        explanation=explanation,
        simulated_tick=snap.tick,
    )


# --------------------------------------------------------------------------- #
# Explanation builders
# --------------------------------------------------------------------------- #
def _build_reasoning(
    *,
    sequence: int,
    chunk: float,
    binding_kind: str,
    binding_detail: str,
    binding_available: float,
    route: Any,
    depot: Any,
    station: Any,
    remaining_before: float,
    will_defer: bool,
) -> str:
    parts = [
        f"Chunk {sequence}: {_qty(chunk)} L of {remaining_before} L remaining, "
        f"limited by {binding_detail}."
    ]
    if will_defer:
        parts.append(
            f"The balance is deferred to a later tick because the "
            f"{binding_kind.replace('_', ' ').lower()} limit is reached."
        )
    parts.append(
        f"Route {route.id} takes {route.transit_ticks} ticks; depot "
        f"{depot.id} and station {station.id} both remain open."
    )
    return " ".join(parts)


def _build_explanation(
    *,
    req: PlanRequest,
    route: Any,
    depot: Any,
    station: Any,
    alternatives: list[RouteAlternative],
    is_only_route: bool,
    status: str,
    binding_detail: str,
    shipped: float = 0.0,
    deferred: float = 0.0,
    chunks: int = 0,
) -> PlanExplanation:
    if status == "REJECTED":
        human_summary = (
            f"Cannot dispatch {_qty(req.quantity)} L of {req.fuel_type} from "
            f"{depot.name} to {station.name}: {binding_detail}."
        )
        notes = [
            f"Depot {depot.name} status: {depot.status}",
            f"Station {station.name} status: {station.status}",
            f"Route {route.id} status: {route.status}",
        ]
    elif status == "PARTIAL":
        human_summary = (
            f"Dispatch {_qty(shipped)} L of {_qty(req.quantity)} L requested in "
            f"{chunks} shipment(s); {_qty(deferred)} L is deferred to a later "
            f"tick because {binding_detail}."
        )
        notes = [
            f"Route {route.id} takes {route.transit_ticks} ticks.",
            f"Depot {depot.name} status: {depot.status}.",
            f"Station {station.name} status: {station.status}.",
        ]
    else:
        human_summary = (
            f"Dispatch the full {_qty(shipped)} L of {req.fuel_type} from "
            f"{depot.name} to {station.name} in {chunks} shipment(s) over route "
            f"{route.id} ({route.transit_ticks} ticks)."
        )
        notes = [
            f"Route {route.id} takes {route.transit_ticks} ticks.",
            f"Depot {depot.name} status: {depot.status}.",
            f"Station {station.name} status: {station.status}.",
        ]

    if is_only_route:
        notes.append(
            "This station is served by exactly one route, so no rerouting is "
            "possible if it is disrupted."
        )
    elif req.allow_alternate_route:
        notes.append(
            "Alternate routes were considered for this station; the selected "
            "route is the best available."
        )
    else:
        notes.append(
            "Alternate routes were not substituted; pass allow_alternate_route "
            "to enable rerouting."
        )

    if req.reason:
        notes.append(f"Operator intent: {req.reason}")

    return PlanExplanation(
        human_summary=human_summary,
        route_choice=route.id,
        alternatives_considered=alternatives,
        is_only_route=is_only_route,
        rerouting_possible=not is_only_route,
        notes=notes,
    )
