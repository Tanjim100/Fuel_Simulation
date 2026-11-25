"""Anomaly and incident endpoints.

The detectors run against the live world on every request, so an operator
sees the current state rather than a stale summary. The rolling observation
log -- fed by the broker's refresh path -- is what makes the *historical*
detectors (demand spikes, inventory drops) possible at all.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Request

from ..anomaly import (
    SEVERITY_ORDER,
    Anomaly,
    detect_snapshot_anomalies,
    group_incidents,
)
from ..explain import explain_incident, explain_stockout_risk
from ..forecast import build_forecast
from ..state import WorldSnapshot

router = APIRouter(prefix="/api", tags=["anomaly"])


async def _fresh_snapshot(request: Request) -> WorldSnapshot:
    broker = request.app.state.broker
    await broker.refresh_snapshot()
    return request.app.state.world.current()


async def _all_anomalies(request: Request) -> list[Anomaly]:
    """Every anomaly currently detectable, from history and from the snapshot.

    Two sources:

    * the **observation-driven** detectors (demand spike, inventory drop),
      which need the rolling window the broker maintains
    * the **snapshot-driven** detectors (depot bottleneck, route bottleneck,
      supply shortfall, projected stockout), which read the world directly
    """
    snapshot = await _fresh_snapshot(request)
    settings = request.app.state.settings
    detector = getattr(request.app.state, "detector", None)
    observations = getattr(request.app.state, "observations", None)

    anomalies: list[Anomaly] = []

    # -- the observation-driven detectors ---------------------------------- #
    if detector is not None and observations is not None:
        entries = await observations.recent(limit=settings.anomaly_history_window)
        # Replay the window through the detector so the history-driven
        # detectors see the same sequence they would have seen live.
        for observation in entries:
            found = detector.observe(observation)
            # Only report the anomalies of the *latest* tick: older ones have
            # already been reported and are visible in the incidents view.
            if observation.tick == snapshot.tick:
                anomalies.extend(found)

    # -- the snapshot-driven detectors -------------------------------------- #
    forecast = build_forecast(snapshot, horizon=settings.forecast_horizon_ticks)
    anomalies.extend(
        detect_snapshot_anomalies(snapshot, forecast=forecast)
    )

    # Deduplicate by id -- the two sources can report the same entity.
    unique: dict[str, Anomaly] = {}
    for anomaly in anomalies:
        unique.setdefault(anomaly.id, anomaly)

    return sorted(unique.values(), key=lambda a: a.sort_key)


def _filter(
    anomalies: list[Anomaly],
    *,
    kind: Optional[str],
    severity: Optional[str],
    since_tick: Optional[int],
) -> list[Anomaly]:
    out = anomalies
    if kind:
        out = [a for a in out if a.kind == kind]
    if severity:
        out = [a for a in out if a.severity == severity]
    if since_tick is not None:
        out = [a for a in out if a.tick >= since_tick]
    return out


@router.get("/anomalies")
async def list_anomalies(
    request: Request,
    kind: Optional[str] = None,
    severity: Optional[str] = None,
    since_tick: Optional[int] = None,
) -> dict[str, Any]:
    """The anomalies currently detectable."""
    anomalies = await _all_anomalies(request)
    filtered = _filter(anomalies, kind=kind, severity=severity, since_tick=since_tick)
    snapshot = request.app.state.world.current()

    return {
        "status": "OK",
        "tick": snapshot.tick,
        "count": len(filtered),
        "anomalies": [a.to_dict() for a in filtered],
    }


@router.get("/anomalies/summary")
async def anomaly_summary(request: Request) -> dict[str, Any]:
    """Counts by kind and by severity."""
    anomalies = await _all_anomalies(request)
    snapshot = request.app.state.world.current()

    by_kind: dict[str, int] = {}
    by_severity: dict[str, int] = {severity: 0 for severity in SEVERITY_ORDER}
    for anomaly in anomalies:
        by_kind[anomaly.kind] = by_kind.get(anomaly.kind, 0) + 1
        by_severity[anomaly.severity] = by_severity.get(anomaly.severity, 0) + 1

    return {
        "status": "OK",
        "tick": snapshot.tick,
        "total": len(anomalies),
        "by_kind": by_kind,
        "by_severity": by_severity,
        "severity_order": list(SEVERITY_ORDER),
    }


@router.get("/anomalies/{anomaly_id}")
async def get_anomaly(anomaly_id: str, request: Request) -> dict[str, Any]:
    """One anomaly, with its evidence."""
    anomalies = await _all_anomalies(request)
    for anomaly in anomalies:
        if anomaly.id == anomaly_id:
            return {"status": "OK", "anomaly": anomaly.to_dict()}

    return {
        "status": "ERROR",
        "error": {
            "code": "NOT_FOUND",
            "message": f"unknown anomaly {anomaly_id!r}",
            "http_status": 404,
        },
    }


@router.get("/incidents")
async def list_incidents(request: Request) -> dict[str, Any]:
    """The anomalies grouped into incidents.

    An incident is the operator-facing unit: three anomalies on the same
    station within eight ticks are one story, not three.
    """
    anomalies = await _all_anomalies(request)
    settings = request.app.state.settings
    incidents = group_incidents(
        anomalies, window_ticks=settings.anomaly_incident_window_ticks
    )
    snapshot = request.app.state.world.current()

    return {
        "status": "OK",
        "tick": snapshot.tick,
        "count": len(incidents),
        "incidents": [i.to_dict() for i in incidents],
    }


@router.get("/incidents/{incident_id}")
async def get_incident(incident_id: str, request: Request) -> dict[str, Any]:
    """One incident, with the anomalies that make it up."""
    anomalies = await _all_anomalies(request)
    settings = request.app.state.settings
    snapshot = request.app.state.world.current()
    incidents = group_incidents(
        anomalies, window_ticks=settings.anomaly_incident_window_ticks
    )

    for incident in incidents:
        if incident.id == incident_id:
            members = [a for a in anomalies if a.id in set(incident.anomaly_ids)]
            explanation = explain_incident(incident, members, snapshot=snapshot)
            return {
                "status": "OK",
                "incident": incident.to_dict(),
                "anomalies": [a.to_dict() for a in members],
                "explanation": explanation.to_dict(),
            }

    return {
        "status": "ERROR",
        "error": {
            "code": "NOT_FOUND",
            "message": f"unknown incident {incident_id!r}",
            "http_status": 404,
        },
    }


@router.get("/anomalies/{anomaly_id}/explain")
async def explain_anomaly(anomaly_id: str, request: Request) -> dict[str, Any]:
    """One anomaly, with a deterministic explanation of it."""
    anomalies = await _all_anomalies(request)
    for anomaly in anomalies:
        if anomaly.id == anomaly_id:
            # A single anomaly is explained as a one-member incident: the
            # explanation layer's unit of work is the group, and a group of
            # one is the degenerate case.
            from ..anomaly import Incident

            incident = Incident(
                id=f"incident-{anomaly.id}",
                title=anomaly.title,
                severity=anomaly.severity,
                entity_type=anomaly.entity_type,
                entity_id=anomaly.entity_id,
                start_tick=anomaly.tick,
                end_tick=anomaly.tick,
                anomaly_ids=[anomaly.id],
                kinds=[anomaly.kind],
                summary=anomaly.detail,
            )
            explanation = explain_incident(incident, [anomaly])
            return {
                "status": "OK",
                "anomaly": anomaly.to_dict(),
                "explanation": explanation.to_dict(),
            }

    return {
        "status": "ERROR",
        "error": {
            "code": "NOT_FOUND",
            "message": f"unknown anomaly {anomaly_id!r}",
            "http_status": 404,
        },
    }
