"""Environment-driven settings for the operator backend.

Every knob is read from the environment so the same image runs identically in
docker-compose and on a developer laptop. Defaults match the compose file so
``uvicorn app.main:app`` works with no environment at all.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache

__all__ = ["Settings", "get_settings"]


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip()


def _env_bool(name: str, default: bool) -> bool:
    """Parse a boolean environment flag.

    Accepts the spellings people actually type: ``1``/``0``, ``true``/``false``,
    ``yes``/``no``, ``on``/``off`` -- case-insensitively.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    normalised = raw.strip().lower()
    if normalised in ("1", "true", "yes", "on"):
        return True
    if normalised in ("0", "false", "no", "off"):
        return False
    return default


@dataclass(frozen=True)
class Settings:
    """Resolved backend configuration."""

    # -- upstream ----------------------------------------------------------- #
    simulator_base_url: str = "http://simulator-api:8000"
    redis_url: str = "redis://redis:6379/0"
    backend_port: int = 8002
    backend_host: str = "0.0.0.0"

    # -- upstream client ---------------------------------------------------- #
    request_timeout: float = 3.0
    max_retries: int = 3

    # -- circuit breaker ---------------------------------------------------- #
    #: Consecutive *retryable* upstream failures before the breaker opens.
    #:
    #: Retryable means a timeout, an injected 503, or a transport error. A
    #: 404/409/422 is the simulator *answering* -- it understood the request
    #: and said no -- so it never counts towards this threshold. Counting it
    #: would take the whole backend down because one station's tank was full.
    breaker_failure_threshold: int = 5
    #: How long the breaker stays OPEN before it allows one probe through.
    #: Long enough that a brief outage does not flap the breaker, short
    #: enough that the console is not left showing stale data for long.
    breaker_cooldown_seconds: float = 15.0
    #: Set false to disable the breaker entirely (it still counts).
    breaker_enabled: bool = True

    # -- SSE broker --------------------------------------------------------- #
    #: Seconds without an SSE event while the world has advanced -> suspected drop.
    sse_stale_after: float = 45.0
    #: Seconds of poller-only operation before we try to reclaim SSE.
    sse_recover_after: float = 10.0
    #: How often the watchdog samples the tick counter.
    watchdog_interval: float = 5.0
    #: How often the poller reconciles world state (also the poll-only mode rate).
    poll_interval: float = 5.0
    #: Force a fresh SSE subscription after this long, bounding the blast radius
    #: of an undetected drop.
    sse_max_lifetime: float = 900.0
    #: How many events the broker retains for `/api/events/recent`.
    event_buffer: int = 200
    #: Per-dashboard-subscriber queue depth.
    subscriber_queue: int = 64
    #: How many world reads may be in flight against the simulator at once.
    #:
    #: The simulator's SQLAlchemy pool is 15 connections wide and every
    #: request it serves holds two of them, so eight concurrent reads demand
    #: 16 -- more than the pool has. Four is deliberate: it leaves headroom
    #: for the SSE subscription, which holds its connection for the life of
    #: the response, and for the watchdog's reachability probe. Overrunning
    #: the pool is a deadlock, not a slow-down.
    fetch_concurrency: int = 4

    # -- world snapshot ----------------------------------------------------- #
    #: Maximum age (seconds) of a cached snapshot before it is refetched.
    #:
    #: MUST exceed `poll_interval`. The snapshot can only ever be as fresh
    #: as the last poll, so a TTL below the poll interval marks a healthy
    #: world stale for most of every poll cycle -- which is exactly the
    #: false "Stale data" banner this pair was tuned to avoid.
    state_max_age: float = 15.0
    #: How often the snapshot refresher pulls fresh world state.
    state_refresh_interval: float = 1.0

    # -- dispatch journal --------------------------------------------------- #
    journal_path: str = "/app/data/journal.sqlite3"

    # -- dead-letter queue -------------------------------------------------- #
    #: Where the DLQ's SQLite file lives. It must be on a durable volume.
    dlq_path: str = "/app/data/dlq.sqlite3"
    #: The cap on the queue. Oldest entries are evicted first.
    dlq_max_entries: int = 1000

    # -- optimiser ---------------------------------------------------------- #
    #: The planning horizon, in ticks. 16 ticks == 4 hours of simulated time.
    optimizer_horizon_ticks: int = 16
    #: Upper bound on the shipments a single plan may contain.
    optimizer_max_shipments: int = 64
    #: Extra stock held at single-connected stations, as a fraction of need.
    optimizer_safety_stock_fraction: float = 0.0
    #: Which solver to use: ``greedy`` | ``lp`` | ``auto``.
    optimizer_solver: str = "auto"

    # -- forecast ----------------------------------------------------------- #
    forecast_horizon_ticks: int = 16
    #: How much of the live demand history to pull when validating a forecast.
    forecast_history_limit: int = 200
    #: The scenario's start time -- the epoch of tick 0.
    forecast_start_time: str = "2026-01-01T00:00:00+00:00"

    # -- scenario ----------------------------------------------------------- #
    #: The scenario configuration file. It carries the demand table, the
    #: regional multipliers, the clock origin and the seed.
    #:
    #: This is read **at import time** by ``app.scenario`` (and therefore by
    #: ``app.forecast``), not by ``get_settings()``, so the environment
    #: variable must be set before the process starts -- which is what
    #: docker-compose does. The path is relative to the project root on a
    #: laptop and absolute inside the image.
    scenario_path: str = "backend/scenarios/baseline.yaml"

    # -- anomaly detection -------------------------------------------------- #
    #: How many ticks of history the detectors compare against.
    anomaly_history_window: int = 32
    #: A z-score at or above this is a MEDIUM demand spike.
    anomaly_spike_z_threshold: float = 3.0
    #: A z-score at or above this is a HIGH demand spike.
    anomaly_spike_severe_z: float = 5.0
    #: An inventory drop of at least this many litres is anomalous.
    anomaly_inventory_drop_abs: float = 250.0
    #: ...and at least this fraction of the previous inventory.
    anomaly_inventory_drop_ratio: float = 0.20
    #: A depot with at most this much allowance left is a bottleneck.
    anomaly_bottleneck_threshold: float = 500.0
    #: How many ticks of proximity group anomalies into one incident.
    anomaly_incident_window_ticks: int = 8
    #: The rolling observation log's bound.
    anomaly_observation_log_max: int = 4096

    #: Extra fields kept for forward compatibility / debugging.
    extras: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            simulator_base_url=_env_str("SIMULATOR_BASE_URL",
                                        "http://simulator-api:8000"),
            redis_url=_env_str("REDIS_URL", "redis://redis:6379/0"),
            backend_port=_env_int("BACKEND_PORT", 8002),
            backend_host=_env_str("BACKEND_HOST", "0.0.0.0"),
            request_timeout=_env_float("REQUEST_TIMEOUT", 3.0),
            max_retries=_env_int("MAX_RETRIES", 3),
            breaker_failure_threshold=_env_int("BREAKER_FAILURE_THRESHOLD", 5),
            breaker_cooldown_seconds=_env_float("BREAKER_COOLDOWN_SECONDS", 15.0),
            breaker_enabled=_env_bool("BREAKER_ENABLED", True),
            sse_stale_after=_env_float("SSE_STALE_AFTER", 45.0),
            sse_recover_after=_env_float("SSE_RECOVER_AFTER", 10.0),
            watchdog_interval=_env_float("WATCHDOG_INTERVAL", 5.0),
            poll_interval=_env_float("POLL_INTERVAL", 5.0),
            sse_max_lifetime=_env_float("SSE_MAX_LIFETIME", 900.0),
            event_buffer=_env_int("EVENT_BUFFER", 200),
            subscriber_queue=_env_int("SUBSCRIBER_QUEUE", 64),
            fetch_concurrency=_env_int("FETCH_CONCURRENCY", 4),
            state_max_age=_env_float("STATE_MAX_AGE", 15.0),
            state_refresh_interval=_env_float("STATE_REFRESH_INTERVAL", 1.0),
            journal_path=_env_str("JOURNAL_PATH", "/app/data/journal.sqlite3"),
            dlq_path=_env_str("DLQ_PATH", "/app/data/dlq.sqlite3"),
            dlq_max_entries=_env_int("DLQ_MAX_ENTRIES", 1000),
            optimizer_horizon_ticks=_env_int("OPTIMIZER_HORIZON_TICKS", 16),
            optimizer_max_shipments=_env_int("OPTIMIZER_MAX_SHIPMENTS", 64),
            optimizer_safety_stock_fraction=_env_float(
                "OPTIMIZER_SAFETY_STOCK_FRACTION", 0.0
            ),
            optimizer_solver=_env_str("OPTIMIZER_SOLVER", "auto"),
            forecast_horizon_ticks=_env_int("FORECAST_HORIZON_TICKS", 16),
            forecast_history_limit=_env_int("FORECAST_HISTORY_LIMIT", 200),
            forecast_start_time=_env_str(
                "FORECAST_START_TIME", "2026-01-01T00:00:00+00:00"
            ),
            scenario_path=_env_str(
                "SCENARIO_PATH", "backend/scenarios/baseline.yaml"
            ),
            anomaly_history_window=_env_int("ANOMALY_HISTORY_WINDOW", 32),
            anomaly_spike_z_threshold=_env_float(
                "ANOMALY_SPIKE_Z_THRESHOLD", 3.0
            ),
            anomaly_spike_severe_z=_env_float("ANOMALY_SPIKE_SEVERE_Z", 5.0),
            anomaly_inventory_drop_abs=_env_float(
                "ANOMALY_INVENTORY_DROP_ABS", 250.0
            ),
            anomaly_inventory_drop_ratio=_env_float(
                "ANOMALY_INVENTORY_DROP_RATIO", 0.20
            ),
            anomaly_bottleneck_threshold=_env_float(
                "ANOMALY_BOTTLENECK_THRESHOLD", 500.0
            ),
            anomaly_incident_window_ticks=_env_int(
                "ANOMALY_INCIDENT_WINDOW_TICKS", 8
            ),
            anomaly_observation_log_max=_env_int(
                "ANOMALY_OBSERVATION_LOG_MAX", 4096
            ),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings singleton.

    Cached so that a single process resolves the environment exactly once --
    ``lru_cache`` is cleared by the test-suite fixture when it needs a
    different environment.
    """
    return Settings.from_env()
