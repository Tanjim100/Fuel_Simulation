"""Deterministic explanations.

An operator asking "why did you ship 7 000 litres of diesel to Mirpur, and
not 5 000 to Tongi?" needs an answer. This module produces that answer
**without** a language model: every explanation is a function of the
optimiser's output, the gate's verdict and the forecast's numbers.

The central discipline is the ``numbers`` payload. Every figure quoted in
``headline`` or ``details`` is also present in ``numbers``, keyed. That
makes the prose *auditable* -- a test asserts that the two agree -- and it
is what makes the optional LLM layer in :mod:`app.llm` safe: the model may
only touch prose, and prose containing a number that is not in ``numbers``
is discarded.

Four kinds of explanation:

* **plan** -- why this dispatch plan, what it achieves, what it leaves out
* **incident** -- what is wrong, how bad it is, what to do about it
* **rejection** -- why a request was refused, and what would make it legal
* **stockout risk** -- how long a station has, and when it runs dry

Everything here is pure: no clock, no I/O, no network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

__all__ = [
    "Explanation",
    "explain_plan",
    "explain_incident",
    "explain_rejection",
    "explain_stockout_risk",
    "numbers_in",
]


#: Volumes are compared with a tolerance of a millilitre.
EPS = 1e-6

#: A number in prose: an optional sign, digits, optional decimals.
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _qty(value: float) -> float:
    """Normalise a volume to whole millilitres."""
    return round(float(value) * 1000.0) / 1000.0


def _pct(value: float) -> float:
    """A probability as a percentage, rounded to one decimal."""
    return round(float(value) * 100.0, 1)


# --------------------------------------------------------------------------- #
# The explanation
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Explanation:
    """One explanation.

    ``numbers`` is the audit trail: every quantity the prose mentions is
    here, keyed. ``verdict`` says what the operator should *do*, not merely
    what happened.
    """

    schema_version: str = "1.0"
    kind: str = "plan"  # plan | incident | rejection | stockout_risk
    title: str = ""
    headline: str = ""
    details: list[str] = field(default_factory=list)
    numbers: dict[str, float] = field(default_factory=dict)
    verdict: str = "INFORMATIONAL"  # RECOMMENDED | CAUTION | BLOCKED | INFORMATIONAL
    alternatives: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "title": self.title,
            "headline": self.headline,
            "details": list(self.details),
            "numbers": dict(self.numbers),
            "verdict": self.verdict,
            "alternatives": list(self.alternatives),
            "risks": list(self.risks),
            "next_actions": list(self.next_actions),
            "evidence": dict(self.evidence),
        }

    def prose(self) -> str:
        """The whole explanation as one block of text."""
        parts = [self.headline]
        parts.extend(self.details)
        if self.next_actions:
            parts.append("Next steps: " + "; ".join(self.next_actions))
        return " ".join(parts)


def numbers_in(text: str) -> list[float]:
    """Every number that appears in ``text``.

    Used to audit the prose against the ``numbers`` payload, and by the LLM
    layer to reject prose that invents a figure.
    """
    return [float(match) for match in _NUMBER_RE.findall(text)]


# --------------------------------------------------------------------------- #
# Plans
# --------------------------------------------------------------------------- #
def explain_plan(
    optimization: Any,
    *,
    gate: Optional[Any] = None,
    forecast: Optional[Any] = None,
    snapshot: Optional[Any] = None,
) -> Explanation:
    """Explain a dispatch plan: what it moves, what it achieves, what it misses.

    ``optimization`` is an :class:`~app.optimizer.OptimizationResult`.
    ``gate`` is an optional :class:`~app.dispatch.GateResult` -- when it is
    supplied, the explanation says whether the plan passed the validator.
    """
    shipments = list(getattr(optimization, "shipments", []) or [])
    total_litres = _qty(getattr(optimization, "total_litres", 0.0))
    service_level = float(getattr(optimization, "expected_service_level", 1.0))
    unmet = _qty(getattr(optimization, "unmet_demand_litres", 0.0))
    status = str(getattr(optimization, "status", "EMPTY"))
    solver_used = str(getattr(optimization, "solver_used", "unknown"))

    now_tick = (getattr(optimization, "diagnostics", {}) or {}).get("now_tick")
    now = [s for s in shipments if s.dispatch_tick == now_tick]
    later = [s for s in shipments if s.dispatch_tick != now_tick]

    numbers: dict[str, float] = {
        "shipment_count": float(len(shipments)),
        "total_litres": total_litres,
        "expected_service_level_pct": _pct(service_level),
        "unmet_demand_litres": unmet,
        "shipments_now": float(len(now)),
        "shipments_later": float(len(later)),
        "litres_now": _qty(sum(s.quantity for s in now)),
        "litres_later": _qty(sum(s.quantity for s in later)),
    }
    if now_tick is not None:
        numbers["now_tick"] = float(now_tick)

    details: list[str] = []
    risks: list[str] = []
    next_actions: list[str] = []
    alternatives: list[str] = []

    if shipments:
        stations = sorted({s.destination_station_id for s in shipments})
        fuels = sorted({s.fuel_type for s in shipments})
        details.append(
            f"The plan moves {total_litres:.0f} L across "
            f"{len(shipments)} shipment(s) to {len(stations)} station(s) "
            f"({', '.join(stations)}) covering {', '.join(fuels)}."
        )
    else:
        details.append(
            "The plan is empty: no shipment is needed within the horizon."
        )
        # An empty plan has moved nothing, whatever the optimiser's summary
        # field says. Quote the shipment list, not the summary.
        total_litres = 0.0
        numbers["total_litres"] = 0.0
        numbers["expected_service_level_pct"] = _pct(service_level)
        unmet = 0.0
        numbers["unmet_demand_litres"] = 0.0

    if now and later:
        details.append(
            f"{len(now)} shipment(s) can be dispatched now "
            f"({numbers['litres_now']:.0f} L); the remaining {len(later)} "
            f"({numbers['litres_later']:.0f} L) are advisory and will be "
            f"recomputed next cycle."
        )
    elif now:
        details.append(
            f"All {len(now)} shipment(s) ({numbers['litres_now']:.0f} L) "
            f"dispatch now."
        )

    details.append(
        f"Projected service level is {numbers['expected_service_level_pct']:.1f}% "
        f"of forecast demand over the horizon, leaving "
        f"{unmet:.0f} L unmet."
    )

    # -- the binding constraint ------------------------------------------- #
    binding = _dominant_binding(shipments)
    if binding:
        details.append(f"The binding constraint is {binding}.")
        numbers["binding_shipments"] = float(
            sum(1 for s in shipments if s.binding == binding)
        )

    # -- risks -------------------------------------------------------------- #
    spills = [s for s in shipments if s.risk == "SPILL_RISK"]
    if spills:
        numbers["spill_risk_shipments"] = float(len(spills))
        numbers["spill_risk_litres"] = _qty(sum(s.quantity for s in spills))
        risks.append(
            f"{len(spills)} shipment(s) may spill on arrival: the "
            f"destination's free space is smaller than the quantity "
            f"arriving. The simulator accepts this silently."
        )
        alternatives.append(
            "Reduce the quantity to the destination's free space, or wait "
            "for the station to draw down its stock first."
        )

    late = [s for s in shipments if s.risk == "LATE_ARRIVAL"]
    if late:
        numbers["late_arrival_shipments"] = float(len(late))
        risks.append(
            f"{len(late)} shipment(s) arrive after the tick they were "
            f"aimed at; they will not prevent a stockout in the meantime."
        )

    # -- the gate ----------------------------------------------------------- #
    if gate is not None:
        accepted = int(getattr(gate, "accepted_count", 0))
        rejected = int(getattr(gate, "rejected_count", 0))
        numbers["gate_accepted"] = float(accepted)
        numbers["gate_rejected"] = float(rejected)
        numbers["gate_total_litres"] = float(getattr(gate, "total_litres", 0.0))

        if rejected:
            details.append(
                f"The validator accepted {accepted} of {len(shipments)} "
                f"shipment(s) and rejected {rejected}."
            )
            risks.append(
                "Some shipments were rejected by the validator and are not "
                "in the executable plan."
            )
        else:
            details.append(
                f"Every one of the {accepted} shipment(s) passed the "
                f"validator, so the plan is executable as written."
            )

    # -- verdict ------------------------------------------------------------ #
    if not shipments:
        verdict = "INFORMATIONAL"
    elif gate is not None and getattr(gate, "rejected_count", 0):
        verdict = "CAUTION"
    elif service_level < 0.999:
        verdict = "CAUTION"
    else:
        verdict = "RECOMMENDED"

    if shipments:
        next_actions.append(
            "Execute the current-tick shipments, then re-plan once the "
            "world has moved."
        )
    if unmet > EPS:
        next_actions.append(
            f"Review the {unmet:.0f} L of unmet demand: no legal shipment "
            f"could cover it within the horizon."
        )
    if gate is not None and getattr(gate, "rejected_count", 0):
        next_actions.append(
            "Inspect the rejected shipments and dispatch them manually "
            "with corrected quantities."
        )

    return Explanation(
        kind="plan",
        title=f"Dispatch plan for tick {now_tick if now_tick is not None else '?'}",
        headline=(
            (
                f"{total_litres:.0f} L across {len(shipments)} shipment(s); "
                f"projected service level "
                f"{numbers['expected_service_level_pct']:.1f}%."
            )
            if shipments
            else (
                "No shipment is needed within the horizon; projected service "
                f"level {numbers['expected_service_level_pct']:.1f}%."
            )
        ),
        details=details,
        numbers=numbers,
        verdict=verdict,
        alternatives=alternatives,
        risks=risks,
        next_actions=next_actions,
        evidence={
            "status": status,
            "solver_used": solver_used,
            "shipments": [s.to_dict() for s in shipments],
            "warnings": list(getattr(optimization, "warnings", []) or []),
        },
    )


def _dominant_binding(shipments: Sequence[Any]) -> Optional[str]:
    """The constraint that shaped the most shipments.

    The binding constraint is *why* a shipment is the size it is. Naming
    the most common one is the single most useful sentence in the whole
    explanation.
    """
    counts: dict[str, int] = {}
    for shipment in shipments:
        binding = getattr(shipment, "binding", "") or ""
        if binding:
            counts[binding] = counts.get(binding, 0) + 1
    if not counts:
        return None
    # Sort by count, then lexicographically, so the answer is deterministic.
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]


# --------------------------------------------------------------------------- #
# Incidents
# --------------------------------------------------------------------------- #
def explain_incident(
    incident: Any,
    anomalies: Sequence[Any],
    *,
    snapshot: Optional[Any] = None,
) -> Explanation:
    """Explain an incident: what is wrong, how bad, what to do.

    ``incident`` is an :class:`~app.anomaly.Incident`; ``anomalies`` are the
    anomalies that make it up.
    """
    members = list(anomalies or [])
    severity = str(getattr(incident, "severity", "LOW"))
    entity_id = str(getattr(incident, "entity_id", ""))
    kinds = sorted({str(a.kind) for a in members})

    numbers: dict[str, float] = {
        "anomaly_count": float(len(members)),
        "start_tick": float(getattr(incident, "start_tick", 0) or 0),
        "end_tick": float(getattr(incident, "end_tick", 0) or 0),
    }

    details = [
        f"{len(members)} related anomaly(s) affected {entity_id} between tick "
        f"{getattr(incident, 'start_tick', '?')} and tick "
        f"{getattr(incident, 'end_tick', '?')}."
    ]

    risks: list[str] = []
    next_actions: list[str] = []
    alternatives: list[str] = []

    # -- the most severe member drives the recommendation ------------------ #
    worst = _worst_anomaly(members)
    if worst is not None:
        details.append(worst.detail)
        for key, value in worst.metrics.items():
            numbers[f"metric_{key}"] = float(value)
        if worst.recommended_action:
            next_actions.append(worst.recommended_action)

    # -- a demand spike has a specific remedy ----------------------------- #
    if any(a.kind == "DEMAND_SPIKE" for a in members):
        numbers["demand_spike_count"] = float(
            sum(1 for a in members if a.kind == "DEMAND_SPIKE")
        )
        alternatives.append(
            "Bring the dispatch schedule forward: the spike is transient, so "
            "the extra fuel is only needed for the duration."
        )
    if any(a.kind == "ROUTE_BOTTLENECK" for a in members):
        numbers["route_bottleneck_count"] = float(
            sum(1 for a in members if a.kind == "ROUTE_BOTTLENECK")
        )
        alternatives.append(
            "Reroute via the station's alternate connection, if it has one."
        )
    if any(a.kind == "DEPOT_BOTTLENECK" for a in members):
        numbers["depot_bottleneck_count"] = float(
            sum(1 for a in members if a.kind == "DEPOT_BOTTLENECK")
        )
        alternatives.append(
            "Draw from the other depot, or bring a scheduled resupply "
            "forward."
        )
    if any(a.kind == "INVENTORY_DROP" for a in members):
        numbers["inventory_drop_count"] = float(
            sum(1 for a in members if a.kind == "INVENTORY_DROP")
        )
        risks.append(
            "Fuel left the station without being served. The simulator "
            "accepts this silently, so the loss is only visible here."
        )
        next_actions.append(
            "Check for a manual removal or a booking against this station."
        )
    if any(a.kind == "STOCKOUT_RISK" for a in members):
        numbers["stockout_risk_count"] = float(
            sum(1 for a in members if a.kind == "STOCKOUT_RISK")
        )
        next_actions.append(
            "Dispatch against the projected shortfall before the stockout "
            "tick."
        )

    verdict = {
        "CRITICAL": "BLOCKED",
        "HIGH": "RECOMMENDED",
        "MEDIUM": "CAUTION",
        "LOW": "INFORMATIONAL",
    }.get(severity, "INFORMATIONAL")

    if not next_actions:
        next_actions.append(
            "Re-run the optimiser to see whether the situation has changed."
        )

    return Explanation(
        kind="incident",
        title=str(getattr(incident, "title", "Incident")),
        headline=(
            f"{severity} incident on {entity_id}: {len(members)} anomaly(s) "
            f"of kind(s) {', '.join(kinds) if kinds else 'none'}."
        ),
        details=details,
        numbers=numbers,
        verdict=verdict,
        alternatives=alternatives,
        risks=risks,
        next_actions=next_actions,
        evidence={
            "incident": incident.to_dict() if hasattr(incident, "to_dict") else {},
            "anomalies": [a.to_dict() for a in members if hasattr(a, "to_dict")],
        },
    )


def _worst_anomaly(anomalies: Sequence[Any]) -> Optional[Any]:
    """The most severe anomaly in the group."""
    if not anomalies:
        return None
    order = ("CRITICAL", "HIGH", "MEDIUM", "LOW")
    return sorted(
        anomalies,
        key=lambda a: (
            order.index(getattr(a, "severity", "LOW"))
            if getattr(a, "severity", "LOW") in order
            else len(order),
            -int(getattr(a, "tick", 0) or 0),
        ),
    )[0]


# --------------------------------------------------------------------------- #
# Rejections
# --------------------------------------------------------------------------- #
#: What each rejection code means, in the operator's language.
_REJECT_LANGUAGE: dict[str, str] = {
    "ROUTE_MAX_SHIPMENT": (
        "the route's per-shipment limit is smaller than the requested "
        "quantity"
    ),
    "DEPOT_INVENTORY": "the source depot does not hold enough fuel",
    "DISPATCH_CAPACITY_EXCEEDED": (
        "the source depot has already committed its dispatch allowance for "
        "this tick"
    ),
    "STATION_FREE_SPACE": "the destination has no free tank capacity",
    "ROUTE_DISRUPTED": "the route is not currently usable",
    "STATION_CLOSED": "the destination station is not open",
    "DEPOT_UNAVAILABLE": "the source depot cannot dispatch right now",
    "NO_ROUTE": "there is no route from any depot to the destination",
    "INVALID_QUANTITY": "the requested quantity is not positive",
    "UNKNOWN_FUEL": "the fuel type is not one the network carries",
}


def explain_rejection(
    plan: Any,
    *,
    snapshot: Optional[Any] = None,
) -> Explanation:
    """Explain why a request was refused, and what would make it legal.

    ``plan`` is a :class:`~app.models.PlanResult`.
    """
    code = str(getattr(plan, "reject_code", "") or "")
    message = str(getattr(plan, "reject_message", "") or "")
    request = getattr(plan, "request", None)
    quantity = float(getattr(request, "quantity", 0.0) or 0.0)
    fuel = str(getattr(request, "fuel_type", "") or "")
    depot = str(getattr(request, "source_depot_id", "") or "")
    station = str(getattr(request, "destination_station_id", "") or "")

    numbers: dict[str, float] = {"requested_litres": _qty(quantity)}

    binding = getattr(plan, "binding_constraint", None)
    if binding is not None:
        for key in ("limit", "available"):
            value = getattr(binding, key, None)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                numbers[f"ceiling_{key}"] = float(value)

    ceiling = _ceiling_summary(binding)

    details = [
        f"The request for {quantity:.0f} L of {fuel} from {depot} to "
        f"{station} was rejected."
    ]
    if message:
        details.append(message)

    language = _REJECT_LANGUAGE.get(code)
    if language:
        details.append(f"In plain terms: {language}.")

    risks: list[str] = []
    alternatives: list[str] = []
    next_actions: list[str] = []

    if ceiling:
        details.append(ceiling)
        alternatives.append(
            f"Reduce the quantity to {ceiling.split('at most ')[-1]} to make "
            f"the request legal."
        )
    else:
        alternatives.append(
            "Reduce the quantity, choose another route, or wait for the "
            "constraint to clear."
        )

    if code == "DISPATCH_CAPACITY_EXCEEDED":
        risks.append(
            "The depot's dispatch allowance is consumed for the current tick "
            "only; retrying next tick may succeed."
        )
        next_actions.append(
            "Wait for the next tick, or dispatch from the other depot."
        )
    elif code == "ROUTE_MAX_SHIPMENT":
        alternatives.append(
            "Split the request across two shipments on the same route -- the "
            "limit is per shipment, not per route."
        )
    elif code == "STATION_FREE_SPACE":
        risks.append(
            "The destination's tank is full. The simulator would silently "
            "spill the excess if the request were allowed through."
        )
        next_actions.append(
            "Wait for the station to draw down its stock, or ship less."
        )
    elif code == "ROUTE_DISRUPTED":
        alternatives.append(
            "Use the station's alternate route, if it has one."
        )
    elif code == "NO_ROUTE":
        risks.append(
            "This destination has no path to a depot at all, so no "
            "quantity would be accepted."
        )

    if not next_actions:
        next_actions.append(
            "Adjust the request and submit it again."
        )

    return Explanation(
        kind="rejection",
        title=f"Dispatch request rejected ({code or 'UNKNOWN'})",
        headline=(
            f"The request for {quantity:.0f} L of {fuel} was rejected: "
            f"{message or 'the request is not legal right now'}."
        ),
        details=details,
        numbers=numbers,
        verdict="BLOCKED",
        alternatives=alternatives,
        risks=risks,
        next_actions=next_actions,
        evidence={
            "reject_code": code,
            "reject_message": message,
            "binding_constraint": (
                binding.to_dict() if hasattr(binding, "to_dict") else None
            ),
            "warnings": [
                w.to_dict() if hasattr(w, "to_dict") else w
                for w in (getattr(plan, "warnings", []) or [])
            ],
        },
    )


def _ceiling_summary(binding: Optional[Any]) -> str:
    """One sentence naming the ceiling that stopped the request."""
    if binding is None:
        return ""
    limit = getattr(binding, "limit", None)
    if limit is None:
        return ""
    try:
        limit_value = float(limit)
    except (TypeError, ValueError):
        return ""
    kind = getattr(binding, "kind", None) or "the limit"
    available = getattr(binding, "available", None)
    detail = getattr(binding, "detail", "") or ""

    sentence = f"The ceiling is {kind}: at most {limit_value:.0f} L."
    if available is not None:
        try:
            sentence += f" Only {float(available):.0f} L of it was available."
        except (TypeError, ValueError):
            pass
    if detail:
        sentence += f" {detail}"
    return sentence


# --------------------------------------------------------------------------- #
# Stockout risk
# --------------------------------------------------------------------------- #
def explain_stockout_risk(
    risk: Any,
    *,
    forecast: Optional[Any] = None,
    snapshot: Optional[Any] = None,
) -> Explanation:
    """Explain how long a station has and when it runs dry.

    ``risk`` is a :class:`~app.forecast.StockoutRisk`.
    """
    station_id = str(getattr(risk, "station_id", ""))
    fuel = str(getattr(risk, "fuel_type", ""))
    inventory = float(getattr(risk, "current_inventory", 0.0))
    ticks_to_stockout = getattr(risk, "ticks_to_stockout", None)
    stockout_tick = getattr(risk, "stockout_tick", None)
    probability = float(getattr(risk, "stockout_probability", 0.0))
    unmet = float(getattr(risk, "expected_unmet_litres", 0.0))
    coverage = int(getattr(risk, "coverage_ticks", 0) or 0)
    confidence = str(getattr(risk, "confidence", ""))

    numbers: dict[str, float] = {
        "current_inventory_litres": _qty(inventory),
        "stockout_probability_pct": _pct(probability),
        "expected_unmet_litres": _qty(unmet),
        "coverage_ticks": float(coverage),
    }
    if ticks_to_stockout is not None:
        numbers["ticks_to_stockout"] = float(ticks_to_stockout)
    if stockout_tick is not None:
        numbers["stockout_tick"] = float(stockout_tick)

    details = [
        f"{station_id} holds {_qty(inventory):.0f} L of {fuel}, which covers "
        f"{coverage} tick(s) of forecast demand."
    ]
    if ticks_to_stockout is None:
        details.append(
            "No stockout is projected within the forecast horizon."
        )
    else:
        details.append(
            f"At the forecast rate it runs dry in {ticks_to_stockout} "
            f"tick(s), on tick {stockout_tick}."
        )

    details.append(
        f"The probability of running dry is "
        f"{numbers['stockout_probability_pct']:.1f}%, and the expected "
        f"unmet demand is {_qty(unmet):.0f} L."
    )

    risks: list[str] = []
    alternatives: list[str] = []
    next_actions: list[str] = []

    if probability >= 0.5:
        risks.append(
            "There is a better than even chance this station runs dry "
            "inside the horizon."
        )
        next_actions.append(
            "Dispatch now: the lead time of the fastest available route "
            "must be shorter than the time to stockout."
        )
    if unmet > EPS:
        alternatives.append(
            f"Dispatch at least {_qty(unmet):.0f} L to cover the projected "
            f"shortfall."
        )
    if confidence:
        details.append(f"Confidence in this projection: {confidence}.")

    verdict = (
        "RECOMMENDED"
        if probability >= 0.5
        # Below a tenth the risk is real but not worth interrupting an
        # operator for; the anomaly layer's own threshold is 0.5.
        else ("CAUTION" if probability >= 0.1 else "INFORMATIONAL")
    )

    if not next_actions:
        next_actions.append(
            "No action is needed yet; re-check on the next cycle."
        )

    return Explanation(
        kind="stockout_risk",
        title=f"Stockout risk at {station_id} ({fuel})",
        headline=(
            f"{station_id} has {_qty(inventory):.0f} L of {fuel} -- "
            f"{coverage} tick(s) of cover, "
            f"{numbers['stockout_probability_pct']:.1f}% chance of running "
            f"dry."
        ),
        details=details,
        numbers=numbers,
        verdict=verdict,
        alternatives=alternatives,
        risks=risks,
        next_actions=next_actions,
        evidence={"risk": risk.to_dict() if hasattr(risk, "to_dict") else {}},
    )


# --------------------------------------------------------------------------- #
# Auditing the prose
# --------------------------------------------------------------------------- #
def unaccounted_numbers(explanation: Explanation) -> list[float]:
    """Numbers in the prose that are not in the ``numbers`` payload.

    An empty list means the prose is fully auditable: every figure it
    quotes is declared. This is the property that lets the LLM layer be
    trusted with prose only.
    """
    declared: set[float] = set()
    for value in explanation.numbers.values():
        declared.add(round(float(value), 3))
        declared.add(round(float(value), 1))
        declared.add(round(float(value), 0))

    prose = [explanation.headline, *explanation.details]
    missing: list[float] = []
    for text in prose:
        for number in numbers_in(text):
            rounded = round(number, 3)
            if rounded in declared:
                continue
            if any(
                abs(number - d) <= 0.51 for d in declared
            ):
                continue
            missing.append(number)
    return missing
