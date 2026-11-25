"""Dashboard event fan-out.

The backend owns the *one* upstream SSE subscription. Dashboards subscribe to
the backend instead of the simulator, which means:

* the simulator only ever sees one subscriber, so the 200-event queue overflow
  that orphans subscriptions is far less likely;
* a dashboard that connects and disconnects cannot disturb the backend's own
  subscription;
* the backend can inject keepalives, so a quiet simulator does not look like a
  dead connection through a proxy.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

router = APIRouter(prefix="/api", tags=["events"])


@router.get("/events/recent")
async def recent_events(request: Request, limit: int = 200) -> dict[str, Any]:
    """The last events the broker retained (oldest first)."""
    broker = request.app.state.broker
    events = broker.recent_events(limit=limit)
    return {
        "status": "OK",
        "count": len(events),
        "events": [
            {
                "event": e.event,
                "data": e.data,
                "tick": e.tick,
                "received_at": e.received_at,
            }
            for e in events
        ],
    }


@router.get("/events/stream")
async def stream_events(request: Request) -> StreamingResponse:
    """SSE fan-out of every event the backend observes."""
    broker = request.app.state.broker

    async def generator():
        # The broker's ``subscribe()`` is an async generator. Two things make
        # this non-trivial:
        #
        # 1. The generator body does not run until the first ``__anext__()``,
        #    so the subscriber is not registered until then.
        # 2. Cancelling a pending ``__anext__()`` -- which is exactly what
        #    ``asyncio.wait_for`` does on timeout -- destroys the generator
        #    and unregisters the subscriber. So the wait must never cancel the
        #    ``__anext__``; it has to live in a task that is shielded.
        #
        # Disconnect detection therefore runs *concurrently* with the event
        # wait instead of gating it.
        events = broker.subscribe()
        pending: asyncio.Task | None = None
        probe: asyncio.Task | None = None
        try:
            while True:
                if pending is None or pending.done():
                    pending = asyncio.create_task(events.__anext__())
                if probe is None or probe.done():
                    probe = asyncio.create_task(request.is_disconnected())

                done, _ = await asyncio.wait(
                    {pending, probe},
                    return_when=asyncio.FIRST_COMPLETED,
                    timeout=1.0,
                )

                if pending in done:
                    try:
                        event = pending.result()
                    except StopAsyncIteration:
                        return
                    yield event.to_sse()
                    continue

                # Only the disconnect probe finished.
                if probe in done and probe.result():
                    return
        except asyncio.CancelledError:  # pragma: no cover - client disconnect
            return
        finally:
            for task in (pending, probe):
                if task is not None and not task.done():
                    task.cancel()
                    with contextlib.suppress(
                        asyncio.CancelledError, StopAsyncIteration
                    ):
                        await task
            await events.aclose()

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
