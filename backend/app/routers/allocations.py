"""Plan and dispatch endpoints.

``POST /api/plan`` is the read-only half of the brain: it validates a request
against the current world, splits it into legal shipments and explains every
decision. It never writes.

``POST /api/allocations`` is the write half: it re-plans against a *fresh*
snapshot and only then executes. Every shipment is journalled before it is
sent, so a crash mid-dispatch replays as an idempotent ``201`` rather than a
duplicate shipment.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request

from ..circuit_breaker import CircuitOpen
from ..dispatch import GatedShipment, execute_shipments
from ..errors import PlanRejected
from ..metrics import DECISIONS
from ..models import (
    AllocationExecution,
    AllocationRequestBody,
    PlanRequest,
    PlanResponse,
    Shipment,
)
from ..optimizer import PlannedShipment
from ..validator import validate_and_plan

LOGGER = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["allocations"])


async def _plan_from_current_state(request: Request, plan_request: PlanRequest):
    """Validate a request against a freshly-refreshed world snapshot."""
    broker = request.app.state.broker
    world = request.app.state.world

    # Always plan against fresh state -- a stale plan is a wrong plan.
    await broker.refresh_snapshot()
    snapshot = world.current()
    return validate_and_plan(snapshot, plan_request)


def _planned_shipment(shipment: Shipment) -> PlannedShipment:
    """Adapt a validator shipment to the optimiser's shape.

    The shared execution path carries a :class:`PlannedShipment` so that the
    optimiser's dispatch ticks survive the gate. A manual dispatch has no
    multi-tick schedule, so its tick is the current one and the arrival is
    unknown.
    """
    return PlannedShipment(
        sequence=shipment.sequence,
        dispatch_tick=0,
        arrival_tick=0,
        source_depot_id=shipment.source_depot_id,
        destination_station_id=shipment.destination_station_id,
        route_id=shipment.route_id,
        fuel_type=shipment.fuel_type,
        quantity=shipment.quantity,
        priority_score=0.0,
        rationale=shipment.reasoning,
        binding="NONE",
        risk="NONE",
    )


@router.post("/plan", response_model=PlanResponse)
async def create_plan(request: Request, body: PlanRequest) -> PlanResponse:
    """Validate and split a dispatch request. **No write to the simulator.**

    Returns ``200`` with the plan even when the plan is a rejection -- the
    rejection *is* the answer, and it carries the reasoning.
    """
    plan = await _plan_from_current_state(request, body)
    # The decision frequency the PDF's section 15 intelligence layer asks
    # for: how many times the operator asked for a plan. A plan is a
    # *decision request* -- it is counted whether it was accepted or
    # rejected, because a run of rejections is itself a finding.
    DECISIONS.inc(kind="plan")
    return PlanResponse(plan=plan)


@router.post("/allocations")
async def create_allocations(
    request: Request, body: AllocationRequestBody
) -> dict[str, Any]:
    """Validate, split and execute a dispatch request."""
    client = request.app.state.client
    broker = request.app.state.broker
    journal = getattr(request.app.state, "journal", None)

    plan = await _plan_from_current_state(request, body)

    if plan.status == "REJECTED":
        raise PlanRejected(
            code=plan.reject_code or "REJECTED",
            message=plan.reject_message or "The request cannot be dispatched.",
            http_status=409,
            diagnostics=plan.diagnostics,
            explanation=(
                plan.explanation.model_dump() if plan.explanation else {}
            ),
        )

    created: list[dict[str, Any]] = []
    replayed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    total = 0.0

    # A dispatch that actually sent fuel. Counted here -- after the gate
    # has passed and the write is under way -- so the counter measures
    # *shipments*, not requests.
    DECISIONS.inc(kind="dispatch")

    # The write path is shared with the optimiser's execute endpoint -- one
    # implementation of the journal-then-write-then-record sequence.
    outcome = await execute_shipments(
        client,
        journal,
        [
            GatedShipment(shipment=shipment, planned=_planned_shipment(shipment))
            for shipment in plan.shipments
        ],
    )
    created = outcome.created
    replayed = outcome.replayed
    failed = outcome.failed
    total = outcome.total_litres

    # The world changed -- pull a fresh snapshot so the next plan sees it.
    # The shipments are already sent at this point, so a rejected refresh
    # must not fail the request: the write succeeded, and the *next* request
    # will pick up the new world when the breaker closes.
    try:
        await broker.refresh_snapshot()
    except CircuitOpen:
        pass

    execution = AllocationExecution(
        plan_status=plan.status,
        created=created,
        replayed=replayed,
        failed=failed,
        total_litres=total,
        plan=plan,
    )
    return {"status": "OK", "execution": execution.model_dump()}


@router.post("/allocations/{allocation_id}/cancel")
async def cancel_allocation(allocation_id: int, request: Request) -> dict[str, Any]:
    """Cancel a ``PENDING`` allocation.

    A departed shipment cannot be cancelled -- the simulator answers
    ``409 CANNOT_CANCEL``, which the global error handler maps through.
    """
    client = request.app.state.client
    broker = request.app.state.broker

    allocation = await client.cancel_allocation(allocation_id)
    DECISIONS.inc(kind="cancel")
    # The cancellation is already done; a rejected refresh must not fail
    # the request.
    try:
        await broker.refresh_snapshot()
    except CircuitOpen:
        pass
    return {"status": "OK", "allocation": allocation.model_dump()}


@router.get("/allocations/{allocation_id}/explain")
async def explain_allocation(allocation_id: int, request: Request) -> dict[str, Any]:
    """Reconstruct the decision record behind an allocation.

    The reasoning is recovered from the world state the shipment was made in --
    the same ceilings the validator would compute now. This makes the decision
    auditable after the fact.
    """
    world = request.app.state.world
    snapshot = world.current()
    allocation = snapshot.allocation(allocation_id)
    if allocation is None:
        raise PlanRejected(
            code="NOT_FOUND",
            message=f"unknown allocation {allocation_id}",
            http_status=404,
        )

    from ..validator import (
        dispatch_remaining,
        station_free_space,
        station_inbound,
    )

    route = snapshot.route(allocation.route_id)
    depot = snapshot.depot(allocation.source_depot_id)
    station = snapshot.station(allocation.destination_station_id)

    return {
        "status": "OK",
        "allocation": allocation.model_dump(),
        "decision_record": {
            "route": route.model_dump() if route else None,
            "depot": depot.model_dump() if depot else None,
            "station": station.model_dump() if station else None,
            "ceilings_at_dispatch_time": {
                "route_max_shipment": route.max_shipment if route else None,
                "depot_inventory": (
                    float(depot.inventory.get(allocation.fuel_type, 0.0))
                    if depot else None
                ),
                "dispatch_remaining_now": dispatch_remaining(
                    snapshot, allocation.source_depot_id
                ),
                "station_free_space_now": station_free_space(
                    snapshot, allocation.destination_station_id, allocation.fuel_type
                ),
                "station_inbound_now": station_inbound(
                    snapshot, allocation.destination_station_id, allocation.fuel_type
                ),
            },
            "created_tick": allocation.created_tick,
            "expected_arrival_tick": allocation.expected_arrival_tick,
            "note": (
                "Ceilings are recomputed against the current world. "
                "dispatch_remaining_now reflects the current tick, which may "
                "differ from the tick the shipment was created on."
            ),
        },
    }
