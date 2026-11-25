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
from .broker import EventBroker
from .config import Settings, get_settings
from .errors import register_error_handlers
from .journal import DispatchJournal
from .routers import allocations as allocations_router
from .routers import anomaly as anomaly_router
from .routers import events as events_router
from .routers import forecast as forecast_router
from .routers import health as health_router
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
    app.state.client = client

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

    broker = EventBroker(client, world, settings, observations=observations)
    app.state.broker = broker

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

    try:
        yield
    finally:
        LOGGER.info("Shutting down backend")
        await broker.stop()
        if journal is not None:
            journal.close()
        await client.aclose()


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

    app.include_router(health_router.router)
    app.include_router(state_router.router)
    app.include_router(events_router.router)
    app.include_router(allocations_router.router)
    app.include_router(optimizer_router.router)
    app.include_router(anomaly_router.router)
    app.include_router(forecast_router.router)

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
            ],
        }

    return app


app = create_app()
