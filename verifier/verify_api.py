"""
Small FastAPI sidecar for the ``client-verifier`` container.

It exposes:
  * ``GET /health``            -- liveness probe for the container healthcheck.
  * ``GET /``                  -- human-readable service banner.
  * ``POST /verify``           -- runs the full verification suite on demand
                                  and returns the machine-readable report.

The suite is also runnable directly from a shell:

    docker compose exec client-verifier python verify_simulator.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Optional

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from simulator_client import SimulatorClient
from verify_simulator import Report, SimulatorVerifier

LOGGER = logging.getLogger("verify_api")

app = FastAPI(
    title="BUP Fuel Supply Simulator Verifier",
    description="Health endpoint and on-demand end-to-end verification runner.",
    version="1.0.0",
)


@app.get("/health")
async def health() -> dict[str, Any]:
    """Liveness probe for this container."""
    return {"status": "ok", "service": "client-verifier"}


@app.get("/")
async def index() -> dict[str, Any]:
    return {
        "service": "client-verifier",
        "endpoints": {
            "GET /health": "liveness probe",
            "POST /verify": "run the end-to-end verification suite",
        },
        "simulator": os.environ.get(
            "SIMULATOR_BASE_URL", "http://simulator-api:8000"),
    }


@app.post("/verify")
async def verify(
    base_url: Optional[str] = Query(default=None),
    redis_url: Optional[str] = Query(default=None),
) -> JSONResponse:
    """Run the full verification suite and return the report as JSON."""
    client = SimulatorClient(
        base_url=base_url or os.environ.get(
            "SIMULATOR_BASE_URL", "http://simulator-api:8000"),
        redis_url=redis_url if redis_url is not None else os.environ.get(
            "REDIS_URL", "redis://redis:6379/0"),
    )
    report = Report()
    try:
        async with client:
            verifier = SimulatorVerifier(client, report)
            await verifier.run_all()
    except Exception as exc:  # noqa: BLE001 - report the failure, don't crash
        LOGGER.exception("Verification run failed")
        report.add("FATAL", "Verification suite completed without crashing",
                   False, f"{type(exc).__name__}: {exc}")
        report.duration = report.duration or 0.0
    finally:
        await client.aclose()

    payload = {
        "total": len(report.results),
        "passed": report.passed,
        "failed": report.failed,
        "duration_seconds": round(report.duration, 2),
        "summary": report.summary,
        "results": [
            {"group": r.group, "name": r.name, "passed": r.passed,
             "detail": r.detail}
            for r in report.results
        ],
    }
    return JSONResponse(payload, status_code=200 if report.failed == 0 else 500)
