"""The optimisation API.

``POST /api/optimize`` is **read-only**: it computes the plan and says what
the gate would do with it, but writes nothing to the simulator.

``POST /api/optimize/execute`` computes the plan, runs every candidate
through the gate, and writes the **current-tick** shipments. Only those.
Future-tick shipments are advisory -- the simulator stamps ``created_tick =
now`` on every allocation, so writing one early would consume the current
tick's dispatch cap, and the world will have moved by the time that tick
arrives anyway.

The two endpoints share one code path. The only difference is whether the
write happens.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Request

from ..circuit_breaker import CircuitOpen
from ..dispatch import gate_plan
from ..explain import explain_plan
from ..forecast import FUEL_TYPES, build_forecast
from ..models import OptimizeExecuteResponse, OptimizeRequest, OptimizeResponse
from ..optimizer import OptimizationResult, OptimizerConfig, optimize
from ..state import WorldSnapshot

LOGGER = logging.getLogger(__name__)

router = APIRouter(prefix="/api/optimize", tags=["optimizer"])


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
async def _fresh_snapshot(request: Request) -> WorldSnapshot:
    """A snapshot refreshed against the live simulator.

    Always refresh: a stale plan is a wrong plan. The forecast in particular
    depends on the stations' *current* ``demand_multiplier``, which changes
    tick-to-tick while a ``demand_spike`` event is active.
    """
    broker = request.app.state.broker
    await broker.refresh_snapshot()
    return request.app.state.world.current()


def _config(request: Request, body: OptimizeRequest) -> OptimizerConfig:
    """Resolve the optimiser configuration from the settings and the request."""
    settings = request.app.state.settings
    return OptimizerConfig(
        horizon_ticks=body.horizon_ticks or settings.optimizer_horizon_ticks,
        safety_stock_fraction=(
            body.safety_stock_fraction
            if body.safety_stock_fraction is not None
            else settings.optimizer_safety_stock_fraction
        ),
        solver=body.solver or settings.optimizer_solver,
        max_shipments=settings.optimizer_max_shipments,
    )


def _filter_world(snap: WorldSnapshot, body: OptimizeRequest) -> WorldSnapshot:
    """Restrict the plan to the requested stations and fuels.

    A restricted world is a *copy* -- the live snapshot is never mutated.
    Routes whose destination is filtered out are dropped too, otherwise the
    optimiser would see a route it is not allowed to use.
    """
    station_ids = body.station_ids
    fuel_types = body.fuel_types

    if station_ids is None and fuel_types is None:
        return snap

    stations = list(snap.stations)
    if station_ids is not None:
        wanted = set(station_ids)
        stations = [s for s in stations if s.id in wanted]

    if fuel_types is not None:
        wanted_fuels = set(fuel_types)
        stations = [
            _restrict_fuels(station, wanted_fuels) for station in stations
        ]

    kept_station_ids = {s.id for s in stations}
    routes = [r for r in snap.routes if r.destination_station_id in kept_station_ids]

    return WorldSnapshot(
        tick=snap.tick,
        sim_time=snap.sim_time,
        instance=snap.instance,
        depots=list(snap.depots),
        stations=stations,
        routes=routes,
        allocations=list(snap.allocations),
        supply_arrivals=list(snap.supply_arrivals),
        events=list(snap.events),
        metrics=snap.metrics,
    )


def _restrict_fuels(station: Any, fuels: set[str]) -> Any:
    """A copy of ``station`` whose capacity and inventory hold only ``fuels``."""
    return station.model_copy(
        update={
            "capacity": {
                fuel: value
                for fuel, value in station.capacity.items()
                if fuel in fuels
            },
            "inventory": {
                fuel: value
                for fuel, value in station.inventory.items()
                if fuel in fuels
            },
        }
    )


def _plan_id(snapshot: WorldSnapshot, body: OptimizeRequest, result: OptimizationResult) -> str:
    """A deterministic plan id.

    Deterministic on purpose: the same world and the same request produce the
    same id, so a repeated ``/api/optimize`` call is visibly the *same* plan
    rather than a new one. The digest covers the shipments, so a different
    plan gets a different id.
    """
    fingerprint = json.dumps(
        {
            "tick": snapshot.tick,
            "horizon": result.diagnostics.get("horizon"),
            "shipments": [s.to_dict() for s in result.shipments],
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:12]
    return f"plan-t{snapshot.tick}-{digest}"


def _result_model(result: OptimizationResult) -> dict[str, Any]:
    """Serialise an :class:`OptimizationResult` for the response."""
    payload = result.to_dict()
    payload["per_tick_dispatch"] = {
        str(tick): dict(by_depot)
        for tick, by_depot in sorted(result.per_tick_dispatch.items())
    }
    return payload


async def _record_plan(
    request: Request,
    plan_id: str,
    snapshot: WorldSnapshot,
    result: OptimizationResult,
    gated_payload: dict[str, Any],
) -> Optional[str]:
    """Journal the plan, before anything is dispatched.

    A journal failure must not stop the plan from being executed -- the audit
    trail is valuable, but the dispatch is the operator's actual request.
    """
    journal = getattr(request.app.state, "journal", None)
    if journal is None:
        return None
    try:
        await journal.record_plan(
            plan_id,
            tick=snapshot.tick,
            horizon=int(result.diagnostics.get("horizon", 0) or 0),
            solver_used=result.solver_used,
            status=result.status,
            payload={
                "optimization": _result_model(result),
                "gated": gated_payload,
            },
        )
    except Exception as exc:  # pragma: no cover - journal is best-effort
        LOGGER.warning("Could not journal plan %s: %s", plan_id, exc)
        return None
    return plan_id


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@router.post("", response_model=OptimizeResponse)
@router.post("/", response_model=OptimizeResponse)
async def create_optimization(
    request: Request, body: OptimizeRequest | None = None
) -> OptimizeResponse:
    """Compute the optimal dispatch plan. **No write to the simulator.**

    The response separates ``shipments_now`` (executable right now) from
    ``shipments_later`` (advisory -- recomputed next cycle), and reports what
    the gate would do with each candidate shipment.
    """
    body = body or OptimizeRequest()
    snapshot = await _fresh_snapshot(request)
    world = _filter_world(snapshot, body)

    config = _config(request, body)
    forecast = build_forecast(world, horizon=config.horizon_ticks)
    result = optimize(world, forecast, config=config)

    # Gate the plan so the operator can see the safety property *before*
    # anything is written. Nothing is dispatched here.
    gated = gate_plan(world, result.shipments)
    gated_payload = gated.to_dict()

    plan_id = _plan_id(snapshot, body, result)
    recorded = await _record_plan(request, plan_id, snapshot, result, gated_payload)

    explanation = explain_plan(result, gate=gated, forecast=forecast, snapshot=world)

    return OptimizeResponse(
        plan_id=recorded or plan_id,
        tick=snapshot.tick,
        horizon=config.horizon_ticks,
        optimization=_result_model(result),
        gated=gated_payload,
        explanation=explanation.to_dict(),
    )


@router.post("/execute", response_model=OptimizeExecuteResponse)
async def execute_optimization(
    request: Request, body: OptimizeRequest | None = None
) -> OptimizeExecuteResponse:
    """Compute the plan, gate it, and execute the **current-tick** shipments.

    Every candidate shipment goes through ``validate_and_plan`` before it can
    reach the simulator. A shipment the validator refuses is **dropped**, not
    retried with different parameters -- the optimiser's arithmetic is not
    trusted for legality.
    """
    from ..dispatch import execute_shipments

    body = body or OptimizeRequest()
    snapshot = await _fresh_snapshot(request)
    world = _filter_world(snapshot, body)

    config = _config(request, body)
    forecast = build_forecast(world, horizon=config.horizon_ticks)
    result = optimize(world, forecast, config=config)

    # Gate *every* shipment, so the operator sees the full picture -- but only
    # the current-tick ones may be written.
    gated = gate_plan(world, result.shipments)
    now_tick = snapshot.tick
    executable = [g for g in gated.accepted if g.planned.dispatch_tick == now_tick]

    gated_payload = gated.to_dict()
    gated_payload["executable_now"] = len(executable)
    gated_payload["deferred_to_future_ticks"] = len(gated.accepted) - len(executable)

    plan_id = _plan_id(snapshot, body, result)
    # The plan is recorded *before* any shipment is dispatched, so the audit
    # trail shows what was intended even if the write fails halfway.
    recorded = await _record_plan(request, plan_id, snapshot, result, gated_payload)

    client = request.app.state.client
    journal = getattr(request.app.state, "journal", None)

    outcome = await execute_shipments(client, journal, executable)

    # The world changed -- pull a fresh snapshot so the next plan sees it.
    # The shipments are already sent at this point, so a rejected refresh
    # must not fail the request: the write succeeded, and the *next* request
    # will pick up the new world when the breaker closes.
    try:
        await request.app.state.broker.refresh_snapshot()
    except CircuitOpen:
        pass

    explanation = explain_plan(result, gate=gated, forecast=forecast, snapshot=world)

    return OptimizeExecuteResponse(
        plan_id=recorded or plan_id,
        tick=now_tick,
        horizon=config.horizon_ticks,
        optimization=_result_model(result),
        gated=gated_payload,
        explanation=explanation.to_dict(),
        created=outcome.created,
        replayed=outcome.replayed,
        failed=outcome.failed,
        total_litres=outcome.total_litres,
    )


@router.get("/solver")
async def solver_status(request: Request) -> dict[str, Any]:
    """Which solver is active, and why.

    The API never overclaims: if the LP path is unavailable, this says so.
    """
    settings = request.app.state.settings
    configured = settings.optimizer_solver

    lp_available = True
    try:
        from ..lp_solver import require_pulp

        require_pulp()
    except ImportError:
        lp_available = False

    if configured == "greedy":
        active = "greedy"
        note = "The greedy solver is pinned by configuration."
    elif configured == "lp":
        active = "lp" if lp_available else "greedy"
        note = (
            "The LP solver is pinned by configuration and is available."
            if lp_available
            else "The LP solver is pinned by configuration but its optional "
                 "dependency is not installed; the greedy solver was used."
        )
    else:
        active = "lp" if lp_available else "greedy"
        note = (
            "Auto mode selected the LP solver."
            if lp_available
            else "The LP solver's optional dependency is not installed, so the "
                 "greedy solver was used."
        )

    return {
        "status": "OK",
        "configured": configured,
        "active": active,
        "lp_available": lp_available,
        "note": note,
        "horizon_ticks": settings.optimizer_horizon_ticks,
        "max_shipments": settings.optimizer_max_shipments,
        "safety_stock_fraction": settings.optimizer_safety_stock_fraction,
    }


@router.get("/llm")
async def llm_status(request: Request) -> dict[str, Any]:
    """The state of the optional prose layer.

    The layer is **disabled by default** and is strictly decorative: it may
    reword an explanation, never alter a decision. This endpoint says
    plainly whether it is on.
    """
    from ..llm import ProseRewriter, llm_config_from_env

    config = llm_config_from_env()
    rewriter = ProseRewriter(config)

    return {
        "status": "OK",
        "enabled": config.enabled,
        "available": rewriter.available(),
        "role": "decorative_prose_only",
        "note": (
            "The prose layer is disabled. Every explanation is produced "
            "deterministically by the backend, with no language model in "
            "the loop."
            if not config.enabled
            else "The prose layer is enabled. It may reword an explanation; "
                 "it cannot alter a plan, and prose that quotes an "
                 "undeclared figure is discarded."
        ),
        "config": config.to_dict(),
    }


@router.get("/{plan_id}")
async def get_plan(plan_id: str, request: Request) -> dict[str, Any]:
    """A recorded plan, from the journal."""
    journal = getattr(request.app.state, "journal", None)
    if journal is None:
        return {
            "status": "ERROR",
            "error": {
                "code": "JOURNAL_UNAVAILABLE",
                "message": "No dispatch journal is configured.",
                "http_status": 404,
            },
        }

    plan = await journal.find_plan(plan_id)
    if plan is None:
        return {
            "status": "ERROR",
            "error": {
                "code": "NOT_FOUND",
                "message": f"unknown plan {plan_id!r}",
                "http_status": 404,
            },
        }

    return {"status": "OK", "plan": plan.to_dict()}
