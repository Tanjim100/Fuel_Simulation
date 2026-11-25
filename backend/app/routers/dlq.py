"""The dead-letter queue's API.

Two endpoints:

* ``GET /api/dlq`` -- the quarantined payloads, newest first, with a count;
* ``POST /api/dlq/{id}/retry`` -- replay one entry through its original
  handler.

The retry endpoint is the point of the queue. A dead-letter queue that only
accumulates is a log with extra steps; the value is in being able to *replay*
the payload once whatever broke it has been fixed.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request

from ..errors import error_envelope

LOGGER = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["dlq"])


def _queue(request: Request):
    """The dead-letter queue, if one is configured."""
    queue = getattr(request.app.state, "dlq", None)
    if queue is None:
        raise HTTPException(
            status_code=503,
            detail="the dead-letter queue is not available",
        )
    return queue


@router.get("/dlq")
async def list_dead_letters(request: Request, limit: int = 100) -> dict[str, Any]:
    """The quarantined payloads, newest first."""
    queue = _queue(request)
    entries = queue.entries(limit=limit)
    return {
        "status": "OK",
        "count": len(entries),
        "total": queue.count,
        "max_entries": queue.max_entries,
        "entries": [entry.to_dict() for entry in entries],
    }


@router.get("/dlq/{entry_id}")
async def get_dead_letter(entry_id: str, request: Request) -> dict[str, Any]:
    """One quarantined payload."""
    queue = _queue(request)
    entry = queue.get(entry_id)
    if entry is None:
        raise HTTPException(
            status_code=404, detail=f"unknown dead-letter entry {entry_id!r}"
        )
    return {"status": "OK", "entry": entry.to_dict()}


@router.post("/dlq/{entry_id}/retry")
async def retry_dead_letter(entry_id: str, request: Request) -> dict[str, Any]:
    """Replay one entry through its original handler.

    A successful replay removes the entry -- it has been handled, so it is no
    longer a dead letter. A failed replay increments its attempt counter and
    leaves it in place, because it still has not been handled.

    The replay is a *re-delivery*, not a re-execution of the world: the
    payload is handed back to the handler that produced it. For an SSE frame
    that means the frame is re-parsed and re-fanned-out; the world is still
    re-fetched over REST, because REST is the source of truth.
    """
    queue = _queue(request)
    broker = getattr(request.app.state, "broker", None)

    entry = queue.get(entry_id)
    if entry is None:
        raise HTTPException(
            status_code=404, detail=f"unknown dead-letter entry {entry_id!r}"
        )

    if broker is None:
        raise HTTPException(
            status_code=503, detail="no broker is available to replay through"
        )

    source = entry.source or ""
    event_name = source.split(":", 1)[1] if ":" in source else "message"

    try:
        await broker.replay_dead_letter(entry)
    except Exception as exc:
        queue.resolve(entry_id, success=False)
        LOGGER.warning("Replay of %s failed: %s", entry_id, exc)
        return error_envelope(
            "DLQ_REPLAY_FAILED",
            f"the payload could not be replayed: {exc}",
            502,
            diagnostics={"entry": entry.to_dict()},
        )

    queue.resolve(entry_id, success=True)
    return {
        "status": "OK",
        "entry_id": entry_id,
        "replayed": True,
        "event": event_name,
        "remaining": queue.count,
    }
