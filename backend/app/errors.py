"""Exception -> HTTP mapping and the single error envelope.

Every failure that reaches a client -- whether it came from the simulator, from
our own validator, or from an unhandled bug -- leaves the backend in the same
shape::

    {"status": "ERROR",
     "error": {"code": ..., "message": ..., "http_status": ...},
     "diagnostics": {...},
     "explanation": {...}}

Raw stack traces never reach a client.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .circuit_breaker import CircuitOpen
from .models import ErrorBody, ErrorResponse
from .simulator_client import (
    SimulatorConflict,
    SimulatorError,
    SimulatorFaultInjected,
    SimulatorNotFound,
    SimulatorTimeout,
    SimulatorValidationError,
)

__all__ = [
    "PlanRejected",
    "error_envelope",
    "register_error_handlers",
]

LOGGER = logging.getLogger(__name__)


class PlanRejected(Exception):
    """The validator refused a request. Carries the full decision record."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int = 409,
        diagnostics: Optional[dict[str, Any]] = None,
        explanation: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.diagnostics = diagnostics or {}
        self.explanation = explanation or {}

    def envelope(self) -> ErrorResponse:
        return ErrorResponse(
            error=ErrorBody(
                code=self.code,
                message=self.message,
                http_status=self.http_status,
            ),
            diagnostics=self.diagnostics,
            explanation=self.explanation,
        )


def error_envelope(
    code: str,
    message: str,
    http_status: int,
    *,
    diagnostics: Optional[dict[str, Any]] = None,
    explanation: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Build the canonical error body."""
    return ErrorResponse(
        error=ErrorBody(code=code, message=message, http_status=http_status),
        diagnostics=diagnostics or {},
        explanation=explanation or {},
    ).model_dump()


def _status_for_simulator_error(exc: SimulatorError) -> tuple[int, str, dict[str, Any]]:
    """Map a simulator exception onto ``(http_status, code, diagnostics)``."""
    diagnostics: dict[str, Any] = {}
    if exc.payload is not None:
        diagnostics["simulator_payload"] = exc.payload

    if isinstance(exc, SimulatorConflict):
        return 409, exc.code or "CONFLICT", diagnostics
    if isinstance(exc, SimulatorNotFound):
        return 404, exc.code or "NOT_FOUND", diagnostics
    if isinstance(exc, SimulatorValidationError):
        return 422, exc.code or "VALIDATION_ERROR", diagnostics
    if isinstance(exc, SimulatorFaultInjected):
        return 503, exc.code or "FAULT_INJECTED", diagnostics
    if isinstance(exc, CircuitOpen):
        # The breaker rejected without touching the wire. From the caller's
        # point of view this is the same situation as an injected fault --
        # the upstream is not answering -- so it gets the same 503.
        return 503, exc.code or "CIRCUIT_OPEN", diagnostics
    if isinstance(exc, SimulatorTimeout):
        return 504, exc.code or "SIMULATOR_TIMEOUT", diagnostics
    if isinstance(exc, SimulatorError):
        return 502, exc.code or "SIMULATOR_ERROR", diagnostics
    return 502, "SIMULATOR_ERROR", diagnostics


def register_error_handlers(app: FastAPI) -> None:
    """Install handlers so no raw traceback ever reaches a client."""

    @app.exception_handler(PlanRejected)
    async def _plan_rejected(_: Request, exc: PlanRejected) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status, content=exc.envelope().model_dump()
        )

    @app.exception_handler(SimulatorError)
    async def _simulator_error(_: Request, exc: SimulatorError) -> JSONResponse:
        http_status, code, diagnostics = _status_for_simulator_error(exc)
        headers = {}
        if http_status == 503:
            # An open breaker knows exactly how long the caller should wait;
            # anyone else gets a generic second.
            retry_after = getattr(exc, "retry_after", None)
            headers["Retry-After"] = (
                str(int(retry_after)) if isinstance(retry_after, (int, float))
                else "1"
            )
        LOGGER.warning("Simulator error surfaced: %s (%s)", code, exc)
        return JSONResponse(
            status_code=http_status,
            content=error_envelope(
                code, str(exc.message or exc), http_status, diagnostics=diagnostics
            ),
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _request_validation(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=error_envelope(
                "VALIDATION_ERROR",
                "The request body failed schema validation.",
                422,
                diagnostics={"errors": exc.errors()},
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception(
        _: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_envelope(
                f"HTTP_{exc.status_code}",
                str(exc.detail),
                exc.status_code,
            ),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        LOGGER.exception("Unhandled error in backend: %s", exc)
        return JSONResponse(
            status_code=500,
            content=error_envelope(
                "INTERNAL_ERROR",
                "The backend hit an unexpected error. The incident has been logged.",
                500,
            ),
        )
