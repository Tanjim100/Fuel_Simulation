"""Liveness and stream-health endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from ..metrics import HTTP_DURATION, HTTP_REQUESTS
from ..simulator_client import SimulatorError

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    """Service liveness plus upstream reachability.

    Never raises: a degraded upstream is reported as ``status: "degraded"``
    with HTTP 200, so the container healthcheck stays meaningful.

    The response carries the brief's §16 "Health and Status" table -- the
    component states, the p95 latency and the error rate -- alongside the
    keys this endpoint has always returned. Nothing is removed: the console
    and the container healthcheck both read the existing keys.
    """
    client = request.app.state.client
    broker = request.app.state.broker

    # The probe bypasses the breaker on purpose. An open breaker means *we*
    # are not calling the upstream, not that the upstream is down. Probing
    # through the gate would report the simulator as unreachable while it is
    # answering perfectly -- and the circuit state is already reported
    # separately, so the two together are the honest picture.
    probe = getattr(request.app.state, "simulator_client", None) or client

    upstream: dict[str, Any] = {"reachable": False, "status": None, "error": None}
    try:
        health_response = await probe.health()
        upstream["reachable"] = True
        upstream["status"] = health_response.status
    except SimulatorError as exc:
        upstream["error"] = str(exc)
    except Exception as exc:  # pragma: no cover - defensive
        upstream["error"] = str(exc)

    journal_stats: dict[str, Any] = {}
    journal = getattr(request.app.state, "journal", None)
    if journal is not None:
        try:
            journal_stats = await journal.stats()
        except Exception:  # pragma: no cover - journal is best-effort
            journal_stats = {}

    # -- the brief's §16 table ---------------------------------------------- #
    total_requests = 0
    error_requests = 0
    for labels, value in HTTP_REQUESTS.values.items():
        total_requests += int(value)
        if dict(labels).get("status", "").startswith("5"):
            error_requests += int(value)

    breaker = getattr(request.app.state, "breaker", None)
    circuit = breaker.status().to_dict() if breaker is not None else {
        "state": "DISABLED"
    }
    # The breaker's own rejections are 503s, which the error count would
    # otherwise attribute to the upstream. They are the breaker's doing, and
    # the circuit state says so.

    # An open breaker means the *data path* is broken, even if the health
    # probe still answers. That combination is real -- the simulator's
    # ``/health`` is a liveness check on the process, and a fault injected
    # against the data endpoints leaves it answering while every read the
    # backend cares about fails. Reporting "ok" then would be the worst
    # possible answer: the backend is up and the world is unreachable.
    circuit_open = circuit.get("state") in ("OPEN", "HALF_OPEN")

    components = {
        "backend_api": {"state": "HEALTHY"},
        "redis": {"state": "DEGRADED"},
        "fuel_simulator": {
            "state": "DEGRADED"
            if circuit_open
            else ("HEALTHY" if upstream["reachable"] else "UNHEALTHY")
        },
        "prediction_service": {
            "state": "HEALTHY" if upstream["reachable"] else "DEGRADED"
        },
        "decision_engine": {
            # The decision engine is the optimiser and the gate. It is pure,
            # so it is healthy as *code* -- but a plan it produces must be
            # written to a simulator that is up, and it cannot be planned
            # against a world that cannot be read.
            "state": "DEGRADED"
            if circuit_open
            else ("HEALTHY" if upstream["reachable"] else "DEGRADED")
        },
    }
    raw_client = getattr(request.app.state, "simulator_client", None)
    if raw_client is not None and getattr(raw_client, "_redis", None) is not None:
        components["redis"] = {"state": "HEALTHY"}

    # The overall status is the *worst* of the two signals: the process
    # being reachable and the data path being usable are different claims,
    # and the answer to "is the platform working?" is the conjunction.
    healthy = upstream["reachable"] and not circuit_open

    payload: dict[str, Any] = {
        "status": "ok" if healthy else "degraded",
        "service": "buetcsefest-backend",
        "version": request.app.state.version,
        "upstream": upstream,
        "broker": {
            "mode": broker.status.mode,
            "events_received": broker.status.events_received,
        },
        "journal": journal_stats,
        # -- the §16 additions ---------------------------------------------- #
        "components": components,
        "latency": {
            # The §16 table asks for a system-wide p95, so the samples are
            # pooled across every route rather than read from the unlabelled
            # key (which holds nothing -- the samples are labelled by
            # method and path).
            "p95_seconds": round(HTTP_DURATION.percentile_across(0.95), 6),
            "p50_seconds": round(HTTP_DURATION.percentile_across(0.50), 6),
        },
        "requests": {
            "total": total_requests,
            "errors": error_requests,
            "error_rate": (
                round(error_requests / total_requests, 6) if total_requests else 0.0
            ),
        },
        "circuit": circuit,
    }
    return payload


@router.get("/stream-health")
async def stream_health(request: Request) -> dict[str, Any]:
    """Detailed broker diagnostics: mode, drop detection, poll counters."""
    broker = request.app.state.broker
    state = request.app.state.world

    return {
        "status": "OK",
        "broker": broker.status.to_dict(),
        "snapshot": {
            "tick": state.current().tick,
            "age_seconds": round(state.current().age(), 3),
            "is_stale": state.is_stale(),
            "refresh_count": state.refresh_count,
            "last_error": state.last_error,
        },
    }
