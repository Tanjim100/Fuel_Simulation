"""Tests for the deterministic explanation layer.

The central property is **auditability**: every number the prose quotes must
also appear in the explanation's ``numbers`` payload. That is what makes
the prose safe to hand to an LLM, and it is what makes the explanation
worth reading -- a figure in the text can always be traced to the
computation that produced it.
"""

from __future__ import annotations

import pytest

from app.anomaly import Anomaly, Incident, anomaly_id
from app.explain import (
    Explanation,
    explain_incident,
    explain_plan,
    explain_rejection,
    explain_stockout_risk,
    numbers_in,
    unaccounted_numbers,
)
from app.forecast import StockoutRisk
from app.models import BindingConstraint, PlanRequest, PlanResult
from app.optimizer import OptimizationResult, PlannedShipment


def _shipment(
    sequence: int = 1,
    *,
    dispatch_tick: int = 10,
    arrival_tick: int = 13,
    quantity: float = 5000.0,
    binding: str = "ROUTE_MAX_SHIPMENT",
    risk: str = "NONE",
) -> PlannedShipment:
    return PlannedShipment(
        sequence=sequence,
        dispatch_tick=dispatch_tick,
        arrival_tick=arrival_tick,
        source_depot_id="depot-gazipur",
        destination_station_id="station-mirpur",
        route_id="route-gazipur-mirpur",
        fuel_type="DIESEL",
        quantity=quantity,
        priority_score=1.0,
        rationale="test",
        binding=binding,
        risk=risk,
    )


def _optimization(
    shipments: list[PlannedShipment],
    *,
    now_tick: int = 10,
    service_level: float = 0.95,
    unmet: float = 100.0,
    total: float = 5000.0,
) -> OptimizationResult:
    return OptimizationResult(
        status="PLANNED",
        shipments=shipments,
        total_litres=total,
        per_tick_dispatch={},
        expected_service_level=service_level,
        unmet_demand_litres=unmet,
        stockout_risk=[],
        warnings=[],
        diagnostics={"now_tick": now_tick, "horizon": 16},
        solver_used="greedy",
    )


# --------------------------------------------------------------------------- #
# Plans
# --------------------------------------------------------------------------- #
def test_a_plan_explanation_summarises_the_plan():
    explanation = explain_plan(_optimization([_shipment()]))
    assert explanation.kind == "plan"
    assert explanation.verdict in ("RECOMMENDED", "CAUTION", "INFORMATIONAL")
    assert explanation.numbers["shipment_count"] == 1.0
    assert explanation.numbers["total_litres"] == 5000.0
    assert explanation.numbers["shipments_now"] == 1.0
    assert explanation.numbers["shipments_later"] == 0.0


def test_a_plan_explanation_separates_now_from_later():
    optimization = _optimization(
        [_shipment(1, dispatch_tick=10), _shipment(2, dispatch_tick=12)]
    )
    explanation = explain_plan(optimization)
    assert explanation.numbers["shipments_now"] == 1.0
    assert explanation.numbers["shipments_later"] == 1.0
    assert explanation.numbers["litres_now"] == 5000.0
    assert explanation.numbers["litres_later"] == 5000.0


def test_an_empty_plan_is_informational_not_an_error():
    explanation = explain_plan(_optimization([]))
    assert explanation.verdict == "INFORMATIONAL"
    assert explanation.numbers["shipment_count"] == 0.0
    # An empty plan has moved nothing, whatever the optimiser's summary
    # field says -- the headline must not quote a volume.
    assert explanation.numbers["total_litres"] == 0.0
    assert "no shipment" in explanation.headline.lower()


def test_a_plan_names_its_binding_constraint():
    explanation = explain_plan(
        _optimization(
            [
                _shipment(1, binding="ROUTE_MAX_SHIPMENT"),
                _shipment(2, binding="ROUTE_MAX_SHIPMENT"),
                _shipment(3, binding="DEPOT_INVENTORY"),
            ]
        )
    )
    assert "ROUTE_MAX_SHIPMENT" in " ".join(explanation.details)
    assert explanation.numbers["binding_shipments"] == 2.0


def test_a_spill_risk_is_a_risk_not_a_rejection():
    explanation = explain_plan(
        _optimization([_shipment(1, risk="SPILL_RISK", quantity=7000.0)], total=7000.0)
    )
    assert explanation.verdict != "BLOCKED"
    assert explanation.numbers["spill_risk_shipments"] == 1.0
    assert explanation.numbers["spill_risk_litres"] == 7000.0
    assert explanation.risks
    assert explanation.alternatives


def test_a_plan_with_unmet_demand_asks_for_a_review():
    explanation = explain_plan(_optimization([_shipment()], unmet=250.0))
    assert explanation.verdict == "CAUTION"
    assert any("250" in action or "unmet" in action for action in explanation.next_actions)


# --------------------------------------------------------------------------- #
# Incidents
# --------------------------------------------------------------------------- #
def _anomaly(
    kind: str = "DEMAND_SPIKE",
    *,
    severity: str = "HIGH",
    tick: int = 40,
    entity_id: str = "station-mirpur:DIESEL",
) -> Anomaly:
    return Anomaly(
        id=anomaly_id(kind, entity_id, tick),
        kind=kind,
        severity=severity,
        entity_type="STATION_FUEL",
        entity_id=entity_id,
        tick=tick,
        detected_tick=tick,
        title=f"{kind} at {entity_id}",
        detail="Demand is running at 1.80x normal.",
        metrics={"demand_multiplier": 1.8, "observed_demand_litres": 116.4},
        baseline={"mean_demand_litres": 90.0},
        recommended_action="Dispatch extra fuel to this station.",
    )


def _incident(anomalies: list[Anomaly]) -> Incident:
    return Incident(
        id="inc-1",
        title="Demand spike affecting station-mirpur",
        severity="HIGH",
        entity_type="STATION",
        entity_id="station-mirpur",
        start_tick=40,
        end_tick=41,
        anomaly_ids=[a.id for a in anomalies],
        kinds=sorted({a.kind for a in anomalies}),
        summary="1 anomaly on station-mirpur.",
    )


def test_an_incident_explanation_names_the_anomalies():
    anomaly = _anomaly()
    explanation = explain_incident(_incident([anomaly]), [anomaly])
    assert explanation.kind == "incident"
    assert explanation.numbers["anomaly_count"] == 1.0
    assert "DEMAND_SPIKE" in explanation.headline
    assert explanation.next_actions


def test_a_critical_incident_is_blocked():
    anomaly = _anomaly(severity="CRITICAL")
    incident = _incident([anomaly])
    incident = Incident(
        id=incident.id,
        title=incident.title,
        severity="CRITICAL",
        entity_type=incident.entity_type,
        entity_id=incident.entity_id,
        start_tick=incident.start_tick,
        end_tick=incident.end_tick,
        anomaly_ids=incident.anomaly_ids,
        kinds=incident.kinds,
        summary=incident.summary,
    )
    explanation = explain_incident(incident, [anomaly])
    assert explanation.verdict == "BLOCKED"


def test_an_inventory_drop_is_flagged_as_a_silent_loss():
    anomaly = _anomaly("INVENTORY_DROP", severity="MEDIUM")
    explanation = explain_incident(_incident([anomaly]), [anomaly])
    assert explanation.numbers["inventory_drop_count"] == 1.0
    assert explanation.risks
    assert any("silently" in risk for risk in explanation.risks)


def test_the_most_severe_anomaly_drives_the_recommendation():
    mild = _anomaly("ROUTE_BOTTLENECK", severity="LOW")
    severe = _anomaly("DEPOT_BOTTLENECK", severity="CRITICAL")
    explanation = explain_incident(_incident([mild, severe]), [mild, severe])
    assert severe.recommended_action in explanation.next_actions


def test_a_route_bottleneck_suggests_rerouting():
    anomaly = _anomaly("ROUTE_BOTTLENECK")
    explanation = explain_incident(_incident([anomaly]), [anomaly])
    assert any("reroute" in alt.lower() for alt in explanation.alternatives)


# --------------------------------------------------------------------------- #
# Rejections
# --------------------------------------------------------------------------- #
def _rejection(code: str, *, quantity: float = 9000.0) -> PlanResult:
    return PlanResult(
        status="REJECTED",
        request=PlanRequest(
            source_depot_id="depot-gazipur",
            destination_station_id="station-mirpur",
            fuel_type="DIESEL",
            quantity=quantity,
        ),
        reject_code=code,
        reject_message=f"rejected: {code}",
        binding_constraint=BindingConstraint(
            kind=code,
            limit=7000.0,
            available=7000.0,
            detail="The route carries at most 7000 L per shipment.",
        ),
    )


def test_a_rejection_explanation_is_blocked():
    explanation = explain_rejection(_rejection("ROUTE_MAX_SHIPMENT"))
    assert explanation.kind == "rejection"
    assert explanation.verdict == "BLOCKED"
    assert explanation.numbers["requested_litres"] == 9000.0


def test_a_rejection_names_the_ceiling():
    explanation = explain_rejection(_rejection("ROUTE_MAX_SHIPMENT"))
    joined = " ".join(explanation.details)
    assert "7000" in joined
    assert explanation.numbers["ceiling_limit"] == 7000.0


def test_a_route_max_rejection_suggests_splitting():
    explanation = explain_rejection(_rejection("ROUTE_MAX_SHIPMENT"))
    assert any("split" in alt.lower() for alt in explanation.alternatives)


def test_a_dispatch_capacity_rejection_says_to_wait():
    explanation = explain_rejection(_rejection("DISPATCH_CAPACITY_EXCEEDED"))
    assert explanation.risks
    assert any(
        "next tick" in action.lower() for action in explanation.next_actions
    )


def test_a_station_free_space_rejection_warns_about_spill():
    explanation = explain_rejection(_rejection("STATION_FREE_SPACE"))
    assert any("spill" in risk.lower() for risk in explanation.risks)


def test_an_unknown_rejection_code_still_explains_itself():
    explanation = explain_rejection(_rejection("SOMETHING_NEW"))
    assert explanation.verdict == "BLOCKED"
    assert explanation.details


# --------------------------------------------------------------------------- #
# Stockout risk
# --------------------------------------------------------------------------- #
def _risk(
    *,
    ticks_to_stockout: int | None = 4,
    probability: float = 0.72,
    unmet: float = 350.0,
) -> StockoutRisk:
    return StockoutRisk(
        station_id="station-mirpur",
        fuel_type="DIESEL",
        current_inventory=1200.0,
        projected_inventory=[1200.0, 900.0, 600.0, 300.0, 0.0],
        demand_series=[300.0, 300.0, 300.0, 300.0, 300.0],
        ticks_to_stockout=ticks_to_stockout,
        stockout_tick=44 if ticks_to_stockout is not None else None,
        stockout_probability=probability,
        expected_unmet_litres=unmet,
        coverage_ticks=4,
        confidence="MEDIUM",
    )


def test_a_stockout_risk_explanation_states_the_timeline():
    explanation = explain_stockout_risk(_risk())
    assert explanation.kind == "stockout_risk"
    assert explanation.numbers["ticks_to_stockout"] == 4.0
    assert explanation.numbers["stockout_tick"] == 44.0
    assert explanation.numbers["current_inventory_litres"] == 1200.0


def test_a_high_probability_stockout_recommends_dispatching():
    explanation = explain_stockout_risk(_risk(probability=0.72))
    assert explanation.verdict == "RECOMMENDED"
    assert any("dispatch" in action.lower() for action in explanation.next_actions)


def test_a_low_probability_stockout_is_informational():
    explanation = explain_stockout_risk(_risk(probability=0.05, unmet=0.0))
    assert explanation.verdict == "INFORMATIONAL"


def test_no_projected_stockout_says_so():
    explanation = explain_stockout_risk(
        _risk(ticks_to_stockout=None, probability=0.0, unmet=0.0)
    )
    assert "No stockout is projected" in " ".join(explanation.details)


# --------------------------------------------------------------------------- #
# Auditability
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "explanation",
    [
        explain_plan(_optimization([_shipment()])),
        explain_plan(_optimization([])),
        explain_plan(_optimization([_shipment(1, risk="SPILL_RISK")])),
        explain_incident(_incident([_anomaly()]), [_anomaly()]),
        explain_incident(_incident([_anomaly("INVENTORY_DROP")]), [_anomaly("INVENTORY_DROP")]),
        explain_rejection(_rejection("ROUTE_MAX_SHIPMENT")),
        explain_rejection(_rejection("DISPATCH_CAPACITY_EXCEEDED")),
        explain_stockout_risk(_risk()),
    ],
)
def test_every_number_in_the_prose_is_declared(explanation: Explanation):
    """The auditability property.

    Every figure the prose quotes must be present in ``numbers``. A figure
    that is not declared is a figure nobody can trace back to a
    computation.
    """
    missing = unaccounted_numbers(explanation)
    assert missing == [], (
        f"undeclared numbers in the prose: {missing}\n"
        f"numbers: {explanation.numbers}\n"
        f"prose: {explanation.prose()}"
    )


def test_an_explanation_is_deterministic():
    first = explain_plan(_optimization([_shipment()]))
    second = explain_plan(_optimization([_shipment()]))
    assert first.to_dict() == second.to_dict()


def test_an_explanation_serialises():
    payload = explain_plan(_optimization([_shipment()])).to_dict()
    assert payload["schema_version"] == "1.0"
    assert payload["kind"] == "plan"
    assert "numbers" in payload
    assert "evidence" in payload


def test_numbers_in_finds_the_figures():
    assert numbers_in("ship 5000 L by tick 44") == [5000.0, 44.0]
    assert numbers_in("no figures here") == []


def test_unaccounted_numbers_finds_an_invented_figure():
    explanation = explain_plan(_optimization([_shipment()]))
    assert unaccounted_numbers(explanation) == []

    tampered = Explanation(
        kind=explanation.kind,
        title=explanation.title,
        headline="We shipped 999999 L.",
        details=list(explanation.details),
        numbers=dict(explanation.numbers),
        verdict=explanation.verdict,
    )
    assert 999999.0 in unaccounted_numbers(tampered)
