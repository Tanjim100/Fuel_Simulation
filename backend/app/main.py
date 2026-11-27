"""FastAPI application: lifespan, singletons and router wiring.

Two singletons matter here:

* **One** :class:`SimulatorClient` for the whole process. The client's
  idempotency-replay detection is per-instance state (it tracks the highest
  allocation id it has seen). A per-request client would mis-report a replay as
  a brand-new allocation.
* **One** :class:`EventBroker`. The backend owns exactly one upstream SSE
  subscription; dashboards subscribe to the backend.

The lifespan deliberately tolerates a failing upstream at startup. The
verification suite resets the simulator, which races the backend's boot; the
backend logs and retries rather than crash-looping.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .anomaly import AnomalyConfig, AnomalyDetector
from .autopilot import AutopilotController
from .broker import EventBroker
from .circuit_breaker import BreakerState, CircuitBreaker
from .config import Settings, get_settings
from .dlq import DeadLetterQueue
from .errors import register_error_handlers
from .journal import DispatchJournal
from .metrics import (
    CIRCUIT_REJECTIONS,
    CIRCUIT_STATE,
    CIRCUIT_TRANSITIONS,
    FALLBACK_ACTIVATIONS,
    HTTP_DURATION,
    HTTP_REQUESTS,
    UPSTREAM_DURATION,
    UPSTREAM_REQUESTS,
    install_http_middleware,
)
from .routers import allocations as allocations_router
from .routers import anomaly as anomaly_router
from .routers import autopilot as autopilot_router
from .routers import dlq as dlq_router
from .routers import events as events_router
from .routers import forecast as forecast_router
from .routers import health as health_router
from .routers import observability as observability_router
from .routers import optimizer as optimizer_router
from .routers import state as state_router
from .simulator_client import SimulatorClient
from .state import ObservationLog, WorldStateCache

LOGGER = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Boot the client, the broker and the journal; tear them down cleanly."""
    settings: Settings = app.state.settings
    LOGGER.info(
        "Starting backend %s (simulator=%s)", __version__, settings.simulator_base_url
    )

    client = SimulatorClient(
        base_url=settings.simulator_base_url,
        redis_url=settings.redis_url,
        timeout=settings.request_timeout,
        max_retries=settings.max_retries,
    )
    await client.connect_redis()

    # The breaker *wraps* the client rather than replacing it. The client
    # file must stay byte-identical to the verifier's reference copy (the
    # drift guard in tests/test_client_parity.py enforces it), and a policy
    # layer belongs outside transport anyway.
    #
    # ``app.state.client`` is the breaker, so every existing consumer is
    # protected without touching it. ``app.state.simulator_client`` is the
    # raw client, for the few callers that must bypass the breaker: the
    # admin endpoints, the health probe, and the breaker itself.
    breaker = CircuitBreaker(
        client,
        failure_threshold=settings.breaker_failure_threshold,
        cooldown_seconds=settings.breaker_cooldown_seconds,
        enabled=settings.breaker_enabled,
        on_state_change=_on_breaker_state_change,
        on_call=_on_breaker_call,
    )
    app.state.breaker = breaker
    app.state.simulator_client = client
    app.state.client = breaker

    world = WorldStateCache(max_age=settings.state_max_age)
    app.state.world = world

    # The rolling observation window the anomaly detectors read. It lives
    # outside the snapshot on purpose: the snapshot stays a faithful mirror
    # of the simulator, and derived state lives in its own singleton.
    observations = ObservationLog(max_entries=settings.anomaly_observation_log_max)
    app.state.observations = observations

    # The detector keeps the rolling window it compares against. It is
    # separate from the observation log so the log can be replayed or
    # inspected without perturbing the detector's state.
    app.state.detector = AnomalyDetector(
        AnomalyConfig(
            spike_z_threshold=settings.anomaly_spike_z_threshold,
            spike_z_severe=settings.anomaly_spike_severe_z,
            inventory_drop_abs=settings.anomaly_inventory_drop_abs,
            inventory_drop_ratio=settings.anomaly_inventory_drop_ratio,
            bottleneck_litres=settings.anomaly_bottleneck_threshold,
            incident_window_ticks=settings.anomaly_incident_window_ticks,
        )
    )

    broker = EventBroker(
        client, world, settings, observations=observations, breaker=breaker
    )
    app.state.broker = broker

    # The dead-letter queue. It has to exist *before* the broker starts, so
    # a malformed frame that arrives on the very first event has somewhere
    # to go. A payload that cannot be used is quarantined, not dropped and
    # not raised -- an event feed that dies on one bad frame is an event
    # feed that stops delivering events.
    dlq: DeadLetterQueue | None = None
    try:
        dlq = DeadLetterQueue(
            settings.dlq_path,
            max_entries=settings.dlq_max_entries,
            redis=getattr(client, "_redis", None),
        )
        dlq.connect()
        app.state.dlq = dlq
        broker.dlq = dlq
    except Exception as exc:  # pragma: no cover - the DLQ is best-effort
        LOGGER.warning("Dead-letter queue unavailable: %s", exc)
        app.state.dlq = None

    journal: DispatchJournal | None = None
    try:
        journal = DispatchJournal(settings.journal_path)
        journal.connect()
        app.state.journal = journal
    except Exception as exc:  # pragma: no cover - journal is best-effort
        LOGGER.warning("Dispatch journal unavailable: %s", exc)
        app.state.journal = None

    # Replay any dispatch intent that was recorded but never resolved. The
    # simulator's idempotency means a replay produces the original shipment,
    # not a duplicate.
    if journal is not None:
        try:
            pending = await journal.pending_intents()
            if pending:
                LOGGER.warning(
                    "Replaying %d unresolved dispatch intent(s) from a previous run",
                    len(pending),
                )
                for intent in pending:
                    try:
                        allocation, created = await client.create_allocation(
                            _allocation_request(intent.payload)
                        )
                        await journal.record_result(
                            intent.idempotency_key,
                            status="CREATED" if created else "REPLAYED",
                            result=allocation.model_dump(),
                            allocation_id=allocation.id,
                        )
                    except Exception as exc:
                        LOGGER.warning(
                            "Replay of %s failed: %s", intent.idempotency_key, exc
                        )
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.warning("Journal replay failed: %s", exc)

    # Start the broker *after* the replay so the first snapshot reflects it.
    await broker.start()
    await broker.refresh_snapshot()

    # One lock for every write to the simulator. The autopilot and the
    # operator's "Dispatch" button both take it, so an automatic dispatch
    # and a manual one can never interleave -- which is a structural
    # property, not a convention.
    app.state.dispatch_lock = asyncio.Lock()

    # The autopilot. It is created after the broker and the journal because
    # it needs both, and it starts *after* the first snapshot so its first
    # cycle sees a populated world.
    autopilot = AutopilotController(
        client=breaker,
        broker=broker,
        journal=journal,
        settings=settings,
        dispatch_lock=app.state.dispatch_lock,
    )
    app.state.autopilot = autopilot
    await autopilot.start()

    try:
        yield
    finally:
        LOGGER.info("Shutting down backend")
        await autopilot.stop()
        await broker.stop()
        if journal is not None:
            journal.close()
        if dlq is not None:
            # The DLQ's SQLite handle is opened lazily and would otherwise
            # leak on shutdown -- the same treatment the journal gets.
            dlq.close()
        await client.aclose()


def _on_breaker_state_change(state: BreakerState) -> None:
    """Record a circuit breaker transition in the metrics."""
    CIRCUIT_STATE.set(state.gauge_value)
    CIRCUIT_TRANSITIONS.inc(to_state=state.value)
    if state is BreakerState.OPEN:
        FALLBACK_ACTIVATIONS.inc(kind="breaker_open")
    LOGGER.warning("Circuit breaker is now %s", state.value)


def _on_breaker_call(outcome: str, duration_seconds: float) -> None:
    """Record one upstream call in the metrics.

    The breaker is the single choke point through which every simulator call
    passes, so instrumenting it instruments the upstream completely -- no
    per-call-site instrumentation, and no way to forget a call site.
    """
    UPSTREAM_REQUESTS.inc(outcome=outcome)
    UPSTREAM_DURATION.observe(duration_seconds)
    if outcome == "circuit_open":
        # The breaker rejected the call without touching the wire. This is
        # a distinct, important number: it is how much traffic the outage
        # absorbed, and it is the counter the Grafana panel queries.
        CIRCUIT_REJECTIONS.inc()


def _allocation_request(payload: dict[str, Any]):
    """Rebuild an :class:`AllocationRequest` from a journalled payload."""
    from .simulator_client import AllocationRequest

    return AllocationRequest(
        idempotency_key=payload["idempotency_key"],
        source_depot_id=payload["source_depot_id"],
        destination_station_id=payload["destination_station_id"],
        route_id=payload["route_id"],
        fuel_type=payload["fuel_type"],
        quantity=payload["quantity"],
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI application."""
    resolved = settings or get_settings()

    app = FastAPI(
        title="Fuel Supply Intelligence -- Operator Backend",
        version=__version__,
        description=(
            "Decision-support backend for the BUP Fuel Supply Simulator. "
            "Validates and splits dispatch requests before they reach the "
            "simulator, so an approved plan is never rejected with a 409."
        ),
        lifespan=lifespan,
    )

    app.state.settings = resolved
    app.state.version = __version__

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_error_handlers(app)

    # The Prometheus exposition lives at a **bare** ``/metrics`` -- no
    # ``/api`` prefix. ``/api/metrics`` already exists and returns the
    # simulator's business KPIs as JSON for the console; repurposing it
    # would break the console's contract. Two endpoints, two purposes.
    app.include_router(observability_router.router)

    app.include_router(health_router.router)
    app.include_router(state_router.router)
    app.include_router(events_router.router)
    app.include_router(allocations_router.router)
    app.include_router(optimizer_router.router)
    app.include_router(anomaly_router.router)
    app.include_router(forecast_router.router)
    app.include_router(dlq_router.router)
    app.include_router(autopilot_router.router)

    @app.get("/", tags=["meta"])
    async def root() -> dict[str, Any]:
        return {
            "service": "buetcsefest-backend",
            "version": __version__,
            "docs": "/docs",
            "endpoints": [
                "GET  /api/health",
                "GET  /api/stream-health",
                "GET  /api/state",
                "GET  /api/depots, /api/depots/{id}",
                "GET  /api/stations, /api/stations/{id}",
                "GET  /api/routes",
                "GET  /api/allocations, /api/allocations/{id}",
                "GET  /api/allocations/{id}/explain",
                "GET  /api/metrics",
                "GET  /api/events, /api/events/recent",
                "GET  /api/events/stream",
                "POST /api/plan",
                "POST /api/allocations",
                "POST /api/allocations/{id}/cancel",
                "POST /api/optimize",
                "POST /api/optimize/execute",
                "GET  /api/optimize/solver",
                "GET  /api/optimize/llm",
                "GET  /api/optimize/{plan_id}",
                "GET  /api/anomalies, /api/anomalies/{id}",
                "GET  /api/anomalies/{id}/explain",
                "GET  /api/anomalies/summary",
                "GET  /api/incidents, /api/incidents/{id}",
                "GET  /api/forecast, /api/forecast/{station_id}",
                "GET  /api/forecast/summary",
                "GET  /api/forecast/validate",
                "GET  /api/observability",
                "GET  /api/dlq, /api/dlq/{id}/retry",
                "GET  /api/autopilot",
                "POST /api/autopilot/mode, /api/autopilot/cycle",
                "GET  /metrics  (Prometheus exposition)",
            ],
        }

    # Installed last, so it is the outermost middleware: it observes the
    # total request time, including whatever the error handlers do.
    install_http_middleware(app)

    return app


app = create_app()
