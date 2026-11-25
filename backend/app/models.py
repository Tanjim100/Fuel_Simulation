"""Public API schemas for the operator backend.

The response models are intentionally *inspectable*: every plan carries the
numbers behind the decision, not just the outcome. That is what makes the
backend a decision-support tool rather than a black box.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "FuelType",
    "PlanRequest",
    "AllocationRequestBody",
    "CancelRequest",
    "PlanStatus",
    "Shipment",
    "BindingConstraint",
    "Warning",
    "RouteAlternative",
    "PlanExplanation",
    "PlanResult",
    "PlanResponse",
    "AllocationExecution",
    "ErrorBody",
    "ErrorResponse",
    "OptimizeRequest",
    "PlannedShipmentModel",
    "OptimizationResultModel",
    "OptimizeResponse",
    "OptimizeExecuteResponse",
]

FuelType = Literal["DIESEL", "PETROL", "OCTANE"]
PlanStatus = Literal["PLANNED", "PARTIAL", "REJECTED"]


class _Schema(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #
class PlanRequest(_Schema):
    """A dispatch request to validate and (optionally) split.

    ``route_id`` may be omitted, in which case the validator picks the best
    available route deterministically and explains why.
    """

    source_depot_id: str = Field(..., min_length=1)
    destination_station_id: str = Field(..., min_length=1)
    fuel_type: FuelType
    quantity: float = Field(..., description="Litres requested")
    route_id: Optional[str] = Field(
        default=None,
        description="Explicit route. When set it is never silently overridden.",
    )
    allow_split: bool = Field(
        default=True,
        description="Split oversized requests into multiple legal shipments.",
    )
    allow_alternate_route: bool = Field(
        default=False,
        description="Allow rerouting to another depot when the primary is closed "
                    "or its route is disrupted.",
    )
    reason: Optional[str] = Field(
        default=None, description="Free-text operator intent, echoed in the plan."
    )


class AllocationRequestBody(PlanRequest):
    """Same as :class:`PlanRequest` -- the execution endpoint re-plans first."""


class CancelRequest(_Schema):
    allocation_id: int = Field(..., ge=1)


# --------------------------------------------------------------------------- #
# Plan responses
# --------------------------------------------------------------------------- #
class BindingConstraint(_Schema):
    """One limit that shaped a shipment."""

    kind: str = Field(
        ...,
        description="ROUTE_MAX_SHIPMENT | DEPOT_INVENTORY | DISPATCH_CAPACITY "
                    "| STATION_FREE_SPACE | NONE",
    )
    limit: float = Field(..., description="The limiting value in litres.")
    available: float = Field(..., description="What was left of that limit.")
    detail: str = Field(..., description="Operator-readable description.")


class Shipment(_Schema):
    """One legal shipment within a plan."""

    sequence: int = Field(..., ge=1)
    idempotency_key: str
    source_depot_id: str
    destination_station_id: str
    route_id: str
    fuel_type: str
    quantity: float
    binding_constraints: list[BindingConstraint] = Field(default_factory=list)
    reasoning: str = Field(..., description="Why this quantity and not more.")
    deferred_litres: float = Field(
        default=0.0, description="Litres not shipped by this chunk, if any."
    )


class Warning(_Schema):
    """A soft warning -- the request is legal but carries risk."""

    code: str
    message: str
    spill_risk_litres: float = Field(default=0.0)
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class RouteAlternative(_Schema):
    route_id: str
    source_depot_id: str
    status: str
    transit_ticks: int
    max_shipment: float
    selected: bool = False
    rejected_because: Optional[str] = None


class PlanExplanation(_Schema):
    """The human-readable account of the decision."""

    human_summary: str
    route_choice: Optional[str] = None
    alternatives_considered: list[RouteAlternative] = Field(default_factory=list)
    is_only_route: bool = Field(
        default=False,
        description="True when the destination has exactly one route, so no "
                    "rerouting is possible.",
    )
    rerouting_possible: bool = True
    notes: list[str] = Field(default_factory=list)


class PlanResult(_Schema):
    """The outcome of validation -- the core value of the backend."""

    status: PlanStatus
    request: PlanRequest
    shipments: list[Shipment] = Field(default_factory=list)
    total_planned_litres: float = 0.0
    deferred_litres: float = 0.0
    reject_code: Optional[str] = None
    reject_message: Optional[str] = None
    #: The HTTP status this rejection should be reported with.
    #:
    #: A rejected plan is not always a conflict. An unknown station is a
    #: 404, a malformed request is a 422, and only a genuine business
    #: conflict -- "that route is over capacity" -- is a 409. The validator
    #: decides which; this carries the decision to the HTTP layer, so the
    #: status the client sees is the one the rule actually implies.
    http_status: int = 409
    binding_constraint: Optional[BindingConstraint] = None
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    warnings: list[Warning] = Field(default_factory=list)
    explanation: Optional[PlanExplanation] = None
    simulated_tick: int = 0


class PlanResponse(_Schema):
    status: Literal["OK"] = "OK"
    plan: PlanResult


# --------------------------------------------------------------------------- #
# Execution responses
# --------------------------------------------------------------------------- #
class AllocationExecution(_Schema):
    """The result of executing a plan against the simulator."""

    plan_status: PlanStatus
    created: list[dict[str, Any]] = Field(default_factory=list)
    replayed: list[dict[str, Any]] = Field(default_factory=list)
    failed: list[dict[str, Any]] = Field(default_factory=list)
    total_litres: float = 0.0
    plan: Optional[PlanResult] = None


# --------------------------------------------------------------------------- #
# Optimiser requests and responses
# --------------------------------------------------------------------------- #
class OptimizeRequest(_Schema):
    """A request to compute the optimal dispatch plan.

    Every field is optional: an empty body means "plan the default horizon
    for the whole world".
    """

    horizon_ticks: Optional[int] = Field(
        default=None,
        ge=1,
        le=96,
        description="Planning horizon in ticks. Defaults to the configured value.",
    )
    solver: Optional[Literal["greedy", "lp", "auto"]] = Field(
        default=None,
        description="Which solver to use. Defaults to the configured value.",
    )
    station_ids: Optional[list[str]] = Field(
        default=None,
        description="Restrict the plan to these stations. Null means all of them.",
    )
    fuel_types: Optional[list[FuelType]] = Field(
        default=None,
        description="Restrict the plan to these fuels. Null means all of them.",
    )
    safety_stock_fraction: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Extra stock for single-connected stations, as a fraction.",
    )
    reason: Optional[str] = Field(
        default=None, description="Free-text operator intent, echoed in the plan."
    )


class PlannedShipmentModel(_Schema):
    """One shipment the optimiser proposes."""

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
    risk: str = "NONE"


class OptimizationResultModel(_Schema):
    """The optimiser's answer, serialised for the API."""

    status: str = Field(..., description="PLANNED | PARTIAL | EMPTY")
    solver_used: str
    total_litres: float = 0.0
    expected_service_level: float = 0.0
    unmet_demand_litres: float = 0.0
    per_tick_dispatch: dict[str, dict[str, float]] = Field(default_factory=dict)
    shipments_now: list[PlannedShipmentModel] = Field(default_factory=list)
    shipments_later: list[PlannedShipmentModel] = Field(default_factory=list)
    shipments: list[PlannedShipmentModel] = Field(default_factory=list)
    stockout_risk: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class OptimizeResponse(_Schema):
    """The response of ``POST /api/optimize`` -- **read-only**, no write."""

    status: Literal["OK"] = "OK"
    plan_id: Optional[str] = Field(
        default=None,
        description="The recorded plan id, when the plan was journalled.",
    )
    tick: int = 0
    horizon: int = 0
    optimization: OptimizationResultModel
    gated: Optional[dict[str, Any]] = Field(
        default=None,
        description="The gate's verdict: how many shipments passed and why the "
                    "rest did not.",
    )
    explanation: Optional[dict[str, Any]] = Field(
        default=None,
        description="The deterministic explanation of the plan: what it moves, "
                    "what it achieves, and what it leaves out. Every figure "
                    "quoted in the prose is also in the 'numbers' payload.",
    )


class OptimizeExecuteResponse(_Schema):
    """The response of ``POST /api/optimize/execute``."""

    status: Literal["OK"] = "OK"
    plan_id: Optional[str] = None
    tick: int = 0
    horizon: int = 0
    optimization: OptimizationResultModel
    gated: dict[str, Any] = Field(default_factory=dict)
    explanation: Optional[dict[str, Any]] = Field(
        default=None,
        description="The deterministic explanation of the plan and its "
                    "execution.",
    )
    created: list[dict[str, Any]] = Field(default_factory=list)
    replayed: list[dict[str, Any]] = Field(default_factory=list)
    failed: list[dict[str, Any]] = Field(default_factory=list)
    total_litres: float = 0.0


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class ErrorBody(_Schema):
    code: str
    message: str
    http_status: int


class ErrorResponse(_Schema):
    """The single error envelope used by every endpoint."""

    status: Literal["ERROR"] = "ERROR"
    error: ErrorBody
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    explanation: dict[str, Any] = Field(default_factory=dict)
