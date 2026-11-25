"""The dispatch gate and the shared execution path.

Two responsibilities live here:

1. :func:`gate_plan` -- run **every** candidate shipment through
   ``validate_and_plan`` before it can reach the simulator. The optimiser
   computes what is *optimal*; the validator decides what is *legal*. A
   shipment the validator rejects is dropped, never executed.

2. :func:`execute_shipments` -- the journal-then-write-then-record loop,
   extracted from ``routers/allocations.py`` so that both the manual
   dispatch endpoint and the optimiser's execute endpoint share **one**
   implementation of the write path. Two implementations of the write path
   would be two places for the idempotency discipline to drift.

The gate is not optional paranoia. The Phase 1 contract is that anything the
backend approves is accepted by the simulator -- no 409 the backend did not
predict. The optimiser's arithmetic is *not* trusted for that; the validator
re-derives every ceiling from the live snapshot.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from .keys import make_key_at
from .models import PlanRequest, Shipment
from .optimizer import PlannedShipment
from .simulator_client import AllocationRequest, SimulatorError
from .state import WorldSnapshot
from .validator import validate_and_plan

__all__ = [
    "GateRejection",
    "GatedShipment",
    "GateResult",
    "gate_plan",
    "ExecutionOutcome",
    "execute_shipments",
]

LOGGER = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class GateRejection:
    """A shipment the validator refused."""

    route_id: str
    source_depot_id: str
    destination_station_id: str
    fuel_type: str
    quantity: float
    code: str
    message: str
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id,
            "source_depot_id": self.source_depot_id,
            "destination_station_id": self.destination_station_id,
            "fuel_type": self.fuel_type,
            "quantity": self.quantity,
            "code": self.code,
            "message": self.message,
            "diagnostics": self.diagnostics,
        }


@dataclass(frozen=True)
class GatedShipment:
    """A shipment that passed the gate, with its validator-minted key."""

    shipment: Shipment
    planned: PlannedShipment

    def to_dict(self) -> dict[str, Any]:
        return {
            "idempotency_key": self.shipment.idempotency_key,
            "source_depot_id": self.shipment.source_depot_id,
            "destination_station_id": self.shipment.destination_station_id,
            "route_id": self.shipment.route_id,
            "fuel_type": self.shipment.fuel_type,
            "quantity": self.shipment.quantity,
            "dispatch_tick": self.planned.dispatch_tick,
            "arrival_tick": self.planned.arrival_tick,
            "reasoning": self.shipment.reasoning,
        }


@dataclass(frozen=True)
class GateResult:
    """The outcome of gating a plan."""

    accepted: list[GatedShipment] = field(default_factory=list)
    rejected: list[GateRejection] = field(default_factory=list)

    @property
    def accepted_count(self) -> int:
        return len(self.accepted)

    @property
    def rejected_count(self) -> int:
        return len(self.rejected)

    @property
    def total_litres(self) -> float:
        return round(sum(s.shipment.quantity for s in self.accepted), 3)

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted_count,
            "rejected": self.rejected_count,
            "total_litres": self.total_litres,
            "rejections": [r.to_dict() for r in self.rejected],
        }


def gate_plan(
    snap: WorldSnapshot,
    shipments: Sequence[PlannedShipment],
    *,
    key_factory: Optional[Callable[[WorldSnapshot, PlanRequest, int], str]] = None,
) -> GateResult:
    """Run every candidate shipment through ``validate_and_plan``.

    The route is always passed **explicitly** and alternate routes are
    **never** substituted: the optimiser chose that route deliberately, and
    silently rerouting would invalidate its arrival schedule.

    Splitting is disabled (``allow_split=False``) because the optimiser has
    already computed a legal quantity. Re-splitting here would produce
    chunks the optimiser never modelled and would change when the fuel
    arrives.

    A shipment that the validator rejects is **dropped**, not executed and
    not retried with different parameters.

    The idempotency key is minted for the shipment's **planned dispatch
    tick**, not the snapshot's current tick. The simulator stamps
    ``created_tick = now`` on every allocation, so a key minted for the
    current tick would collide with any other shipment of the same request
    planned for a different tick -- a collision the simulator reports as
    ``409 IDEMPOTENCY_KEY_MISMATCH``, a rejection we caused ourselves.
    """
    accepted: list[GatedShipment] = []
    rejected: list[GateRejection] = []

    for planned in shipments:
        request = PlanRequest(
            source_depot_id=planned.source_depot_id,
            destination_station_id=planned.destination_station_id,
            fuel_type=planned.fuel_type,
            quantity=planned.quantity,
            route_id=planned.route_id,
            allow_split=False,
            allow_alternate_route=False,
            reason=(
                f"Optimiser plan sequence {planned.sequence} "
                f"(dispatch tick {planned.dispatch_tick})"
            ),
        )

        plan = validate_and_plan(
            snap,
            request,
            allow_split=False,
            key_factory=key_factory or _key_for_tick(planned.dispatch_tick),
        )

        if plan.status == "REJECTED" or not plan.shipments:
            rejected.append(
                GateRejection(
                    route_id=planned.route_id,
                    source_depot_id=planned.source_depot_id,
                    destination_station_id=planned.destination_station_id,
                    fuel_type=planned.fuel_type,
                    quantity=planned.quantity,
                    code=plan.reject_code or "REJECTED",
                    message=plan.reject_message or "The shipment is not legal.",
                    diagnostics=plan.diagnostics,
                )
            )
            continue

        # The validator splits a legal request into exactly one shipment when
        # splitting is disabled, so there is exactly one key.
        shipment = plan.shipments[0]
        accepted.append(GatedShipment(shipment=shipment, planned=planned))

    return GateResult(accepted=accepted, rejected=rejected)


def _key_for_tick(tick: int) -> Callable[[WorldSnapshot, PlanRequest, int], str]:
    """A key factory that mints keys for a specific dispatch tick."""

    def factory(_snap: WorldSnapshot, req: PlanRequest, sequence: int) -> str:
        return make_key_at(tick, req, sequence)

    return factory


# --------------------------------------------------------------------------- #
# The shared execution path
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ExecutionOutcome:
    """What happened when a gated plan was written to the simulator."""

    created: list[dict[str, Any]] = field(default_factory=list)
    replayed: list[dict[str, Any]] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)
    total_litres: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.failed

    def to_dict(self) -> dict[str, Any]:
        return {
            "created": self.created,
            "replayed": self.replayed,
            "failed": self.failed,
            "total_litres": self.total_litres,
        }


async def execute_shipments(
    client: Any,
    journal: Any,
    gated: Sequence[GatedShipment],
) -> ExecutionOutcome:
    """Write gated shipments to the simulator, journalling each one.

    The order is the same for every shipment:

    1. record the **intent** in the journal (before the network call)
    2. create the allocation
    3. record the **result** in the journal

    A crash between 1 and 3 leaves a ``PENDING`` intent, which the startup
    replay path re-sends verbatim. Because the payload carries the
    idempotency key, the replay returns the simulator's idempotent ``201``
    rather than creating a second shipment.

    A shipment that fails does not stop the others: one rejected allocation
    must not strand the rest of the plan.
    """
    created: list[dict[str, Any]] = []
    replayed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    total = 0.0

    for gated_shipment in gated:
        shipment = gated_shipment.shipment

        allocation_request = AllocationRequest(
            idempotency_key=shipment.idempotency_key,
            source_depot_id=shipment.source_depot_id,
            destination_station_id=shipment.destination_station_id,
            route_id=shipment.route_id,
            fuel_type=shipment.fuel_type,
            quantity=shipment.quantity,
        )
        payload = allocation_request.to_payload()

        # Write-ahead: record the intent *before* touching the simulator.
        if journal is not None:
            await journal.record_intent(shipment.idempotency_key, payload)

        try:
            allocation, was_created = await client.create_allocation(
                allocation_request
            )
        except SimulatorError as exc:
            if journal is not None:
                await journal.record_result(
                    shipment.idempotency_key,
                    status="FAILED",
                    result={
                        "error": str(exc),
                        "code": getattr(exc, "code", None),
                    },
                )
            failed.append(
                {
                    "idempotency_key": shipment.idempotency_key,
                    "quantity": shipment.quantity,
                    "error": str(exc),
                    "code": getattr(exc, "code", None),
                }
            )
            LOGGER.warning(
                "Dispatch failed for %s: %s", shipment.idempotency_key, exc
            )
            continue

        if journal is not None:
            await journal.record_result(
                shipment.idempotency_key,
                status="CREATED" if was_created else "REPLAYED",
                result=allocation.model_dump(),
                allocation_id=allocation.id,
            )

        record = {
            **allocation.model_dump(),
            "idempotency_key": shipment.idempotency_key,
            "reasoning": shipment.reasoning,
        }
        if was_created:
            created.append(record)
            total += shipment.quantity
        else:
            replayed.append(record)

    return ExecutionOutcome(
        created=created,
        replayed=replayed,
        failed=failed,
        total_litres=round(total, 3),
    )
