"""Observability endpoints: the Prometheus exposition and the JSON mirror.

Two endpoints, two audiences:

* ``GET /metrics`` -- the Prometheus text exposition format, for a scraper.
  **Bare path, no ``/api`` prefix.** ``/api/metrics`` already exists and
  returns the simulator's business KPIs as JSON for the console; repurposing
  it would break the console's contract.
* ``GET /api/observability`` -- the same numbers as JSON, for the console's
  System tab.

The console reads JSON, not the exposition format. Parsing a text format in
the UI would put a number-parsing bug inside the guarantee that every number
on screen came from the backend -- the one rule the whole console is built
on.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from ..metrics import (
    CONTENT_TYPE,
    HTTP_DURATION,
    HTTP_REQUESTS,
    METRIC_FAMILIES,
    REGISTRY,
)

router = APIRouter(tags=["observability"])


@router.get("/metrics", response_class=PlainTextResponse)
async def prometheus_metrics() -> PlainTextResponse:
    """The Prometheus exposition of every metric the backend publishes.

    Served as ``text/plain; version=0.0.4`` -- the format's version is
    mandatory and Prometheus rejects a response without it.
    """
    return PlainTextResponse(
        content=REGISTRY.render(),
        media_type=CONTENT_TYPE,
    )


@router.get("/api/observability")
async def observability(request: Request) -> dict[str, Any]:
    """The observability numbers, as JSON.

    The console's System tab reads this. It is the JSON mirror of
    ``/metrics``, plus the two things a dashboard needs that a scraper does
    not: the *current* state of each component, and the error rate.
    """
    broker = getattr(request.app.state, "broker", None)
    breaker = getattr(request.app.state, "breaker", None)
    world = getattr(request.app.state, "world", None)

    # -- the request/error counters the brief's §16 table asks for --------- #
    total_requests = 0
    error_requests = 0
    for labels, value in HTTP_REQUESTS.values.items():
        label_map = dict(labels)
        total_requests += int(value)
        status = label_map.get("status", "")
        if status.startswith("5"):
            error_requests += int(value)

    error_rate = (error_requests / total_requests) if total_requests else 0.0
    p95 = HTTP_DURATION.percentile_across(0.95)

    # -- component health --------------------------------------------------- #
    components: dict[str, dict[str, Any]] = {
        "backend_api": {"state": "HEALTHY"},
        "redis": {"state": "UNKNOWN"},
        "fuel_simulator": {"state": "UNKNOWN"},
        "prediction_service": {"state": "UNKNOWN"},
        "decision_engine": {"state": "UNKNOWN"},
    }

    circuit: dict[str, Any] = {"state": "DISABLED"}
    if breaker is not None:
        circuit = breaker.status().to_dict()
    # An open breaker means the *data path* is broken even if the health
    # probe still answers -- which happens when the fault is injected
    # against the data endpoints and the simulator's liveness route is
    # unaffected. The circuit state is the data path's truth.
    circuit_open = circuit.get("state") in ("OPEN", "HALF_OPEN")

    upstream_error: str | None = None
    upstream_reachable = False
    # The probe deliberately bypasses the breaker. An open breaker means *we*
    # are not calling the upstream, not that the upstream is down -- so
    # probing through the gate would report "UNHEALTHY" for a simulator that
    # is answering fine. The circuit state is reported separately, and the
    # two together are the honest picture.
    raw_client = getattr(request.app.state, "simulator_client", None)
    probe_client = raw_client or getattr(request.app.state, "client", None)
    if probe_client is not None:
        try:
            await probe_client.health()
            upstream_reachable = True
            components["fuel_simulator"] = {"state": "HEALTHY"}
        except Exception as exc:  # noqa: BLE001 - health must never raise
            upstream_error = str(exc)
            components["fuel_simulator"] = {"state": "UNHEALTHY"}

    if circuit_open:
        components["fuel_simulator"] = {"state": "DEGRADED"}

    # Redis is optional: the backend runs without a cache. A missing Redis
    # degrades performance, not correctness, so it is DEGRADED rather than
    # UNHEALTHY.
    redis_ok = False
    if raw_client is not None and getattr(raw_client, "_redis", None) is not None:
        redis_ok = True
    components["redis"] = {"state": "HEALTHY" if redis_ok else "DEGRADED"}

    # The prediction service is the forecast. It is pure and offline, so it
    # is healthy whenever the backend is running -- but it is only *useful*
    # if the simulator is reachable, because it validates against the live
    # oracle.
    components["prediction_service"] = {
        "state": "HEALTHY" if upstream_reachable else "DEGRADED"
    }

    # The decision engine is the optimiser and the gate. It is pure too, but
    # a plan it produces must be written to a simulator that is up, and it
    # cannot be planned against a world that cannot be read.
    components["decision_engine"] = {
        "state": "DEGRADED"
        if circuit_open
        else ("HEALTHY" if upstream_reachable else "DEGRADED")
    }

    broker_status = broker.status.to_dict() if broker is not None else {}
    broker_mode = broker_status.get("mode", "unknown")

    snapshot_age = 0.0
    snapshot_tick = 0
    if world is not None:
        snapshot_age = round(world.current().age(), 3)
        snapshot_tick = world.current().tick

    # The dead-letter queue's size. A non-zero value means something is
    # arriving that the backend cannot use -- which is worth showing on a
    # dashboard rather than leaving in a log file.
    dlq_size = 0
    dlq = getattr(request.app.state, "dlq", None)
    if dlq is not None:
        try:
            dlq_size = dlq.count
        except Exception:  # pragma: no cover - defensive
            dlq_size = 0

    # -- the numbers -------------------------------------------------------- #
    metrics_json = REGISTRY.to_json()

    return {
        "status": "OK",
        "components": components,
        "latency": {
            "p95_seconds": round(p95, 6),
            "p50_seconds": round(HTTP_DURATION.percentile_across(0.50), 6),
            "avg_seconds": round(
                (
                    HTTP_DURATION.total_seconds_across()
                    / HTTP_DURATION.count_across()
                )
                if HTTP_DURATION.count_across()
                else 0.0,
                6,
            ),
        },
        "requests": {
            "total": total_requests,
            "errors": error_requests,
            "error_rate": round(error_rate, 6),
        },
        "circuit": circuit,
        "broker": {
            "mode": broker_mode,
            "events_received": broker_status.get("events_received", 0),
            "suspected_drops": broker_status.get("suspected_drops", 0),
            "probe_failures": broker_status.get("probe_failures", 0),
            "reconnects": broker_status.get("reconnects", 0),
        },
        "upstream": {
            "reachable": upstream_reachable,
            "error": upstream_error,
        },
        "snapshot": {
            "tick": snapshot_tick,
            "age_seconds": snapshot_age,
        },
        "dlq": {
            "size": dlq_size,
        },
        "metric_families": {
            layer: list(names) for layer, names in METRIC_FAMILIES.items()
        },
        "metrics": metrics_json,
    }
