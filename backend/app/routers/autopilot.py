"""The autopilot's HTTP surface.

Three routes, and the shape of each one is borrowed from the existing
``GET /api/optimize/solver`` and ``GET /api/optimize/llm`` endpoints: the API
never overclaims. It says what the mode is, what the autopilot last did, and
what it would do next -- and it says so in a way that a reader can act on.

* ``GET  /api/autopilot``       -- the current state.
* ``POST /api/autopilot/mode``  -- switch the mode.
* ``POST /api/autopilot/cycle`` -- run one cycle now, and report it.

The third route is diagnostic. The loop is already running, so this does not
"make the autopilot do something extra" -- it steps the policy by hand, which
is how a judge can watch the decision it *would* make without waiting for a
tick.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from ..autopilot import AutopilotMode
from ..errors import PlanRejected

router = APIRouter(prefix="/api/autopilot", tags=["autopilot"])


def _controller(request: Request) -> Any:
    """The autopilot controller, or ``None`` if it is not configured."""
    return getattr(request.app.state, "autopilot", None)


@router.get("")
@router.get("/")
async def autopilot_status(request: Request) -> dict[str, Any]:
    """The autopilot's current mode and what it last did."""
    controller = _controller(request)
    if controller is None:
        return {
            "status": "OK",
            "mode": "off",
            "enabled": False,
            "running": False,
            "cycle": 0,
            "note": "The autopilot is not configured on this backend.",
        }
    status = await controller.status()
    payload = status.to_dict()
    payload["status"] = "OK"
    payload["note"] = _note_for(status.mode)
    return payload


@router.post("/mode")
async def set_autopilot_mode(request: Request, body: dict[str, Any]) -> Any:
    """Switch the autopilot's mode.

    An unknown mode is a **400**, not a silent clamp: an operator who typed
    ``"lve"`` must be told, because a controller that quietly fell back to
    ``off`` would look identical to one that is working.
    """
    controller = _controller(request)
    if controller is None:
        raise PlanRejected(
            "AUTOPILOT_UNAVAILABLE",
            "The autopilot is not configured on this backend.",
            http_status=503,
        )

    raw = body.get("mode")
    try:
        mode = AutopilotMode.parse(str(raw))
    except ValueError as exc:
        raise PlanRejected(
            "INVALID_AUTOPILOT_MODE", str(exc), http_status=400
        ) from exc

    status = await controller.set_mode(mode)
    payload = status.to_dict()
    payload["status"] = "OK"
    payload["note"] = _note_for(status.mode)
    return payload


@router.post("/cycle")
async def run_autopilot_cycle(request: Request) -> Any:
    """Run one autopilot cycle and report what it decided.

    Diagnostic. The loop is already running, so this steps the policy by
    hand -- it does not schedule an extra dispatch.
    """
    controller = _controller(request)
    if controller is None:
        raise PlanRejected(
            "AUTOPILOT_UNAVAILABLE",
            "The autopilot is not configured on this backend.",
            http_status=503,
        )
    result = await controller.cycle_once()
    status = await controller.status()
    result["autopilot"] = status.to_dict()
    return result


def _note_for(mode: str) -> str:
    """The one-line explanation of what a mode does."""
    if mode == AutopilotMode.LIVE.value:
        return (
            "The autopilot re-plans against the live world every tick and "
            "dispatches what the gate accepts. You can still dispatch by hand."
        )
    return (
        "The autopilot is off. The console tells you when there is something "
        "to dispatch, and an operator dispatches it by hand."
    )


__all__ = ["router"]
