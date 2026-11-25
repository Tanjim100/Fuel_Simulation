"""Liveness and stream-health endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from ..simulator_client import SimulatorError

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    """Service liveness plus upstream reachability.

    Never raises: a degraded upstream is reported as ``status: "degraded"``
    with HTTP 200, so the container healthcheck stays meaningful.
    """
    client = request.app.state.client
    broker = request.app.state.broker

    upstream: dict[str, Any] = {"reachable": False, "status": None, "error": None}
    try:
        health_response = await client.health()
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

    payload: dict[str, Any] = {
        "status": "ok" if upstream["reachable"] else "degraded",
        "service": "buetcsefest-backend",
        "version": request.app.state.version,
        "upstream": upstream,
        "broker": {
            "mode": broker.status.mode,
            "events_received": broker.status.events_received,
        },
        "journal": journal_stats,
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
