"""
Defensive async client for the BUP Fuel Supply Simulator.

Design rules enforced here (from the official Integration Guide):

  * REST is the source of truth. SSE is advisory only -- every interesting SSE
    event triggers an immediate REST re-fetch of the affected resource.
  * Every ``/v1/*`` GET response must be inspected for ``X-Simulator-Stale: true``.
    When present, the local Redis cache is invalidated immediately and a warning
    is logged.
  * ``503 FAULT_INJECTED`` responses are retried with exponential backoff and
    jitter, under a hard 3s per-request timeout.
  * ``POST /v1/allocations`` is the only domain write. Idempotency keys are
    sent in the *body* (never as a header).
  * Admin endpoints (``/admin/*``) bypass fault injection entirely.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal, Optional

import httpx
from pydantic import BaseModel, ConfigDict, Field

try:  # Redis is optional so the client stays importable without a server.
    import redis.asyncio as aioredis
except ImportError:  # pragma: no cover
    aioredis = None  # type: ignore[assignment]

LOGGER = logging.getLogger("simulator_client")

DEFAULT_TIMEOUT = 3.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_BASE_BACKOFF = 0.25
DEFAULT_MAX_BACKOFF = 4.0

STALE_HEADER = "X-Simulator-Stale"
FAULT_INJECTED = "FAULT_INJECTED"


# --------------------------------------------------------------------------- #
# Domain models
# --------------------------------------------------------------------------- #
class _Base(BaseModel):
    # The simulator emits floats for some ints (e.g. 12000.0 for dispatch
    # capacity), and adds fields across versions. Be permissive on input.
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class HealthResponse(_Base):
    status: str
    database: str
    simulation: dict[str, Any] = Field(default_factory=dict)


class SimulationInstance(_Base):
    id: int
    scenario_id: str
    scenario_version: str
    seed: int
    sim_time: str
    tick: int
    tick_minutes: int
    status: str


class Region(_Base):
    id: str
    name: str
    demand_factor: float


class Depot(_Base):
    id: str
    name: str
    region_id: str
    status: str
    dispatch_capacity_per_tick: float
    capacity: dict[str, float]
    inventory: dict[str, float]


class Station(_Base):
    id: str
    name: str
    region_id: str
    status: str
    demand_profile: str
    demand_multiplier: float
    capacity: dict[str, float]
    inventory: dict[str, float]


class Route(_Base):
    id: str
    source_depot_id: str
    destination_station_id: str
    transit_ticks: int
    max_shipment: float
    status: str


class SupplyArrival(_Base):
    id: str
    depot_id: str
    fuel_type: str
    quantity: float
    planned_tick: int
    actual_tick: Optional[int] = None
    status: str


class DomainEvent(_Base):
    id: int
    type: str
    start_tick: int
    end_tick: int
    status: str
    parameters: dict[str, Any] = Field(default_factory=dict)


class Allocation(_Base):
    id: int
    idempotency_key: str
    source_depot_id: str
    destination_station_id: str
    route_id: str
    fuel_type: str
    quantity: float
    created_tick: int
    departure_tick: Optional[int] = None
    expected_arrival_tick: Optional[int] = None
    actual_arrival_tick: Optional[int] = None
    status: str
    failure_reason: Optional[str] = None


class DemandObservation(_Base):
    id: int
    station_id: str
    fuel_type: str
    tick: int
    sim_time: str
    demand_liters: float
    served_liters: float
    unmet_liters: float


class Metrics(_Base):
    served_demand_liters: float
    unmet_demand_liters: float
    service_level: float
    allocation_liters: float
    allocation_failures: int


class AuditEntry(_Base):
    id: int
    wall_time: str
    sim_time: str
    tick: int
    action: str
    entity_type: Optional[str] = None
    entity_id: Optional[str] = None
    result: Optional[str] = None
    metadata_json: dict[str, Any] = Field(default_factory=dict)


class Fault(_Base):
    id: int
    type: str
    start_wall_time: str
    end_wall_time: str
    active: bool
    parameters: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Error taxonomy
# --------------------------------------------------------------------------- #
class SimulatorError(Exception):
    """Base class for simulator-facing failures."""

    def __init__(self, message: str, *, status_code: Optional[int] = None,
                 code: Optional[str] = None, payload: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code
        self.payload = payload


class SimulatorFaultInjected(SimulatorError):
    """503 FAULT_INJECTED -- retryable."""


class SimulatorTimeout(SimulatorError):
    """Request exceeded the client-side timeout -- retryable."""


class SimulatorNotFound(SimulatorError):
    """404 on a referenced entity."""


class SimulatorValidationError(SimulatorError):
    """422 -- client-side payload validation failed."""


class SimulatorConflict(SimulatorError):
    """409 -- a domain rule rejected the write.

    ``code`` carries the specific domain code, e.g. ``ROUTE_CAPACITY_EXCEEDED``.
    """

    def __init__(self, code: str, message: str, *, status_code: int = 409,
                 payload: Any = None) -> None:
        super().__init__(message, status_code=status_code, code=code, payload=payload)


#: Every 409 domain code the simulator can return for allocations.
ALLOCATION_CONFLICT_CODES = frozenset({
    "IDEMPOTENCY_KEY_MISMATCH",
    "ROUTE_MISMATCH",
    "DEPOT_CLOSED",
    "STATION_CLOSED",
    "ROUTE_DISRUPTED",
    "ROUTE_CAPACITY_EXCEEDED",
    "INSUFFICIENT_INVENTORY",
    "DISPATCH_CAPACITY_EXCEEDED",
    "DESTINATION_CAPACITY_EXCEEDED",
    "CANNOT_CANCEL",
})


@dataclass
class AllocationRequest:
    """Body for ``POST /v1/allocations``.

    ``idempotency_key`` travels in the body -- the guide is explicit that it is
    *not* a header.
    """

    idempotency_key: str
    source_depot_id: str
    destination_station_id: str
    route_id: str
    fuel_type: Literal["DIESEL", "PETROL", "OCTANE"]
    quantity: float

    def to_payload(self) -> dict[str, Any]:
        return {
            "idempotency_key": self.idempotency_key,
            "source_depot_id": self.source_depot_id,
            "destination_station_id": self.destination_station_id,
            "route_id": self.route_id,
            "fuel_type": self.fuel_type,
            "quantity": self.quantity,
        }


@dataclass
class SSEEvent:
    """A parsed SSE frame. ``comment`` frames carry no event name."""

    event: Optional[str]
    data: str
    raw: str = ""

    @property
    def is_comment(self) -> bool:
        return self.event is None

    def json(self) -> Any:
        return json.loads(self.data)


@dataclass
class StreamStats:
    """Bookkeeping for the SSE listener, surfaced in verification reports."""

    connected: bool = False
    reconnects: int = 0
    events_received: int = 0
    comments_ignored: int = 0
    keepalives_ignored: int = 0
    dropped: bool = False
    last_error: Optional[str] = None
    seen_event_names: set[str] = field(default_factory=set)


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
class SimulatorClient:
    """Async, fault-tolerant client for the BUP Fuel Supply Simulator."""

    def __init__(
        self,
        base_url: str = "http://simulator-api:8000",
        *,
        redis_url: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = DEFAULT_BASE_BACKOFF,
        backoff_max: float = DEFAULT_MAX_BACKOFF,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max

        self._client = client or httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout),
        )
        self._owns_client = client is None

        self._redis = None
        self._redis_url = redis_url
        # Track which cache keys are stale so we can invalidate precisely.
        self.stale_invalidations: list[str] = []
        # Highest allocation id seen so far -- used to detect idempotent replays.
        self._max_seen_allocation_id: Optional[int] = None
        self._id_lock = asyncio.Lock()

    # -- lifecycle ---------------------------------------------------------- #
    async def __aenter__(self) -> "SimulatorClient":
        await self.connect_redis()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None

    async def connect_redis(self) -> None:
        if aioredis is None or not self._redis_url:
            return
        try:
            client = aioredis.from_url(self._redis_url, decode_responses=True)
            await client.ping()
            self._redis = client
            LOGGER.info("Redis cache connected: %s", self._redis_url)
        except Exception as exc:  # pragma: no cover - cache is best-effort
            LOGGER.warning("Redis unavailable (%s); running without cache", exc)
            self._redis = None

    # -- cache -------------------------------------------------------------- #
    async def cache_get(self, key: str) -> Optional[str]:
        if self._redis is None:
            return None
        try:
            return await self._redis.get(key)
        except Exception as exc:  # pragma: no cover
            LOGGER.warning("Redis GET %s failed: %s", key, exc)
            return None

    async def cache_set(self, key: str, value: str, ttl: int = 60) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.set(key, value, ex=ttl)
        except Exception as exc:  # pragma: no cover
            LOGGER.warning("Redis SET %s failed: %s", key, exc)

    async def invalidate_cache(self, key: Optional[str] = None) -> None:
        """Drop cached simulator state.

        Called whenever ``X-Simulator-Stale: true`` is observed, and after any
        mutation. A stale response means *any* cached snapshot may be outdated,
        so the whole ``sim:*`` namespace is flushed regardless of which key was
        read.
        """
        # Record the invalidation even when Redis is absent, so the behaviour
        # is observable (and testable) without a cache server.
        self.stale_invalidations.append(key or "*")
        if self._redis is None:
            LOGGER.warning(
                "Stale simulator data detected; cache invalidation recorded "
                "(no Redis connection)")
            return
        try:
            deleted = 0
            async for cached_key in self._redis.scan_iter(match="sim:*"):
                deleted += await self._redis.delete(cached_key)
            LOGGER.warning(
                "Stale simulator data detected; invalidated %s cached key(s)",
                deleted)
        except Exception as exc:  # pragma: no cover
            LOGGER.warning("Redis invalidation failed: %s", exc)

    async def _invalidate_after_mutation(self) -> None:
        """Drop cached snapshots after any state-changing call.

        The simulator's world state is mutable and advances on every tick, so
        a cached snapshot of ``/v1/instance``, ``/v1/routes`` etc. goes stale
        the moment anything changes. Caching is only safe if every mutation
        invalidates the whole namespace.
        """
        await self.invalidate_cache(None)

    # -- low-level request plumbing ---------------------------------------- #
    def _backoff_delay(self, attempt: int) -> float:
        """Exponential backoff with full jitter, capped at ``backoff_max``."""
        ceiling = min(self.backoff_max, self.backoff_base * (2 ** attempt))
        return random.uniform(0, ceiling)

    @staticmethod
    def _extract_error_code(payload: Any) -> Optional[str]:
        """Pull the domain/fault code out of an error body.

        Injected faults use ``{"error": {"code": ...}}``; domain errors use
        ``{"detail": {"code": ...}}``; FastAPI validation uses
        ``{"detail": [...]}``.
        """
        if not isinstance(payload, dict):
            return None
        if isinstance(payload.get("error"), dict):
            code = payload["error"].get("code")
            if code:
                return str(code)
        detail = payload.get("detail")
        if isinstance(detail, dict):
            code = detail.get("code")
            if code:
                return str(code)
        return None

    @staticmethod
    def _extract_error_message(payload: Any, fallback: str) -> str:
        if isinstance(payload, dict):
            for holder in (payload.get("error"), payload.get("detail")):
                if isinstance(holder, dict) and holder.get("message"):
                    return str(holder["message"])
            if isinstance(payload.get("detail"), list) and payload["detail"]:
                first = payload["detail"][0]
                if isinstance(first, dict) and first.get("msg"):
                    return str(first["msg"])
        return fallback

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict[str, Any]] = None,
        params: Optional[dict[str, Any]] = None,
        retry: bool = True,
    ) -> httpx.Response:
        """Perform a request with retry/backoff and fault-aware error mapping.

        Returns the raw response for non-2xx statuses so callers can inspect
        headers (e.g. the stale header) before deciding what to do.
        """
        last_exc: Optional[Exception] = None

        for attempt in range(self.max_retries + 1):
            try:
                response = await self._client.request(
                    method, path, json=json_body, params=params,
                    timeout=self.timeout,
                )
            except httpx.TimeoutException as exc:
                last_exc = SimulatorTimeout(
                    f"Timeout after {self.timeout}s calling {method} {path}",
                    payload=exc,
                )
                if not retry or attempt >= self.max_retries:
                    raise last_exc from exc
                delay = self._backoff_delay(attempt)
                LOGGER.warning(
                    "Timeout on %s %s; retrying in %.2fs (attempt %d/%d)",
                    method, path, delay, attempt + 1, self.max_retries)
                await asyncio.sleep(delay)
                continue
            except httpx.HTTPError as exc:
                last_exc = SimulatorError(
                    f"Transport error calling {method} {path}: {exc}", payload=exc)
                if not retry or attempt >= self.max_retries:
                    raise last_exc from exc
                delay = self._backoff_delay(attempt)
                LOGGER.warning(
                    "Transport error on %s %s; retrying in %.2fs",
                    method, path, delay)
                await asyncio.sleep(delay)
                continue

            if response.status_code == 503:
                try:
                    payload = response.json()
                except ValueError:
                    payload = None
                code = self._extract_error_code(payload)
                if code == FAULT_INJECTED or code is None:
                    last_exc = SimulatorFaultInjected(
                        self._extract_error_message(payload, "Simulator unavailable"),
                        status_code=503, code=code, payload=payload,
                    )
                    if not retry or attempt >= self.max_retries:
                        raise last_exc
                    delay = self._backoff_delay(attempt)
                    LOGGER.warning(
                        "503 FAULT_INJECTED on %s %s; retrying in %.2fs (attempt %d/%d)",
                        method, path, delay, attempt + 1, self.max_retries)
                    await asyncio.sleep(delay)
                    continue

            # Every /v1/* GET must be inspected for the stale-data signal.
            # This lives in _request (not just _get_json) so it applies to
            # every read path, including direct/raw requests.
            if method.upper() == "GET" and path.startswith("/v1/"):
                await self._check_stale(response, path, None)

            return response

        assert last_exc is not None  # pragma: no cover - loop always sets it
        raise last_exc

    async def _get_json(
        self,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        model: Optional[type[BaseModel]] = None,
        cache_key: Optional[str] = None,
        cache_ttl: int = 30,
        use_cache: bool = True,
    ) -> Any:
        """GET a ``/v1/*`` JSON resource with stale detection and caching.

        The stale check happens *before* the cache is consulted, so a stale
        upstream response always invalidates what we hold locally.
        """
        if use_cache and cache_key:
            cached = await self.cache_get(cache_key)
            if cached is not None:
                return self._decode(cached, model)

        response = await self._request("GET", path, params=params)

        if response.status_code >= 400:
            await self._raise_for_status(response, path)

        # The stale-header check already ran inside _request for every
        # GET /v1/* call.

        payload = response.json()
        if use_cache and cache_key:
            await self.cache_set(cache_key, json.dumps(payload), ttl=cache_ttl)
        return self._decode(payload, model)

    @staticmethod
    def _decode(payload: Any, model: Optional[type[BaseModel]]) -> Any:
        """Validate a payload into the requested Pydantic model.

        Cached values arrive from Redis as JSON *strings*, so a string payload
        is parsed before validation.
        """
        if isinstance(payload, (str, bytes)):
            try:
                payload = json.loads(payload)
            except (ValueError, TypeError) as exc:
                raise SimulatorError(
                    f"Could not decode cached payload: {exc}", payload=payload) from exc

        if model is None:
            return payload
        if isinstance(payload, list):
            return [model.model_validate(item) for item in payload]
        return model.model_validate(payload)

    async def _check_stale(
        self, response: httpx.Response, path: str, cache_key: Optional[str]
    ) -> None:
        """Inspect ``X-Simulator-Stale`` and invalidate the cache if set.

        A stale response means any cached snapshot may be out of date, so the
        whole ``sim:*`` namespace is flushed -- not just the key that happened
        to be read.
        """
        stale = response.headers.get(STALE_HEADER, "").strip().lower()
        if stale == "true":
            LOGGER.warning(
                "Stale simulator data on %s (X-Simulator-Stale: true); "
                "invalidating cache", path)
            await self.invalidate_cache(cache_key)

    async def _raise_for_status(self, response: httpx.Response, path: str) -> None:
        try:
            payload = response.json()
        except ValueError:
            payload = None

        code = self._extract_error_code(payload)
        message = self._extract_error_message(payload, f"HTTP {response.status_code}")

        if response.status_code == 404:
            raise SimulatorNotFound(
                f"{code or 'NOT_FOUND'}: {message}",
                status_code=404, code=code, payload=payload)
        if response.status_code == 409:
            raise SimulatorConflict(
                code or "CONFLICT", message,
                status_code=409, payload=payload)
        if response.status_code == 422:
            raise SimulatorValidationError(
                message, status_code=422, code=code, payload=payload)
        raise SimulatorError(
            f"HTTP {response.status_code} on {path}: {message}",
            status_code=response.status_code, code=code, payload=payload)

    # ------------------------------------------------------------------ #
    # Public read endpoints (REST is the source of truth)
    # ------------------------------------------------------------------ #
    async def health(self) -> HealthResponse:
        """Liveness probe. Bypasses fault injection -- never retried into a
        false-negative."""
        response = await self._request("GET", "/v1/health", retry=False)
        if response.status_code >= 400:
            await self._raise_for_status(response, "/v1/health")
        return HealthResponse.model_validate(response.json())

    # ------------------------------------------------------------------ #
    # Public read endpoints (REST is the source of truth)
    #
    # Caching policy: only genuinely static resources are served from Redis.
    # Everything that moves with the world -- the tick clock, inventories,
    # route status, allocations, metrics -- is fetched fresh, because a
    # cached snapshot of those would silently contradict the simulator.
    # ------------------------------------------------------------------ #
    async def instance(self) -> SimulationInstance:
        return await self._get_json(
            "/v1/instance", model=SimulationInstance)

    async def regions(self) -> list[Region]:
        # Regions and their demand factors are fixed for the scenario.
        return await self._get_json(
            "/v1/regions", model=Region, cache_key="sim:regions")

    async def depots(self) -> list[Depot]:
        return await self._get_json(
            "/v1/depots", model=Depot)

    async def depot(self, depot_id: str) -> Depot:
        return await self._get_json(
            f"/v1/depots/{depot_id}", model=Depot)

    async def stations(self) -> list[Station]:
        return await self._get_json(
            "/v1/stations", model=Station)

    async def station(self, station_id: str) -> Station:
        return await self._get_json(
            f"/v1/stations/{station_id}", model=Station)

    async def routes(self) -> list[Route]:
        return await self._get_json(
            "/v1/routes", model=Route)

    async def supply_arrivals(self) -> list[SupplyArrival]:
        return await self._get_json(
            "/v1/supply-arrivals", model=SupplyArrival)

    async def events(self) -> list[DomainEvent]:
        return await self._get_json(
            "/v1/events", model=DomainEvent)

    async def allocations(self) -> list[Allocation]:
        return await self._get_json(
            "/v1/allocations", model=Allocation)

    async def demand_history(
        self,
        *,
        station_id: Optional[str] = None,
        limit: int = 100,
    ) -> list[DemandObservation]:
        """Bounded time-series read.

        ``limit`` is always supplied: the table grows by 12 rows per tick
        without bound, so unbounded reads are never acceptable.
        """
        params: dict[str, Any] = {"limit": limit}
        if station_id:
            params["station_id"] = station_id
        return await self._get_json(
            "/v1/demand-history", params=params, model=DemandObservation)

    async def metrics(self) -> Metrics:
        return await self._get_json(
            "/v1/metrics", model=Metrics)

    # ------------------------------------------------------------------ #
    # Domain write: allocations
    # ------------------------------------------------------------------ #
    async def create_allocation(
        self,
        request: AllocationRequest,
    ) -> tuple[Allocation, bool]:
        """Create an allocation.

        Returns ``(allocation, created)`` where ``created`` is ``False`` for an
        idempotent replay of the same key + same body.

        The simulator answers ``201`` for both the first write and an exact
        replay (the guide is explicit: "Same key, same body -> return existing
        allocation (HTTP 201, not 200)"), so the status code alone cannot
        distinguish them. Replay is detected by asking the allocator whether
        the allocation id already existed before this call.
        """
        response = await self._request(
            "POST", "/v1/allocations", json_body=request.to_payload())

        if response.status_code in (200, 201):
            allocation = Allocation.model_validate(response.json())
            replayed = await self._allocation_existed_before(allocation.id)
            await self._invalidate_after_mutation()
            LOGGER.info(
                "Allocation %s for %s %s L -> %s (HTTP %d, replay=%s)",
                allocation.id, allocation.fuel_type, allocation.quantity,
                allocation.destination_station_id, response.status_code, replayed)
            return allocation, not replayed

        await self._raise_for_status(response, "/v1/allocations")
        raise AssertionError("unreachable")  # pragma: no cover

    async def _allocation_existed_before(self, allocation_id: int) -> bool:
        """Whether ``allocation_id`` was already present before this write.

        The allocator hands out monotonically increasing ids. If the highest
        id we have already observed is >= the id we just received, the write
        was a replay of an allocation we had already seen.
        """
        async with self._id_lock:
            if self._max_seen_allocation_id is not None:
                if allocation_id <= self._max_seen_allocation_id:
                    return True
            self._max_seen_allocation_id = max(
                self._max_seen_allocation_id or 0, allocation_id)
            return False

    async def cancel_allocation(self, allocation_id: int) -> Allocation:
        """Cancel a ``PENDING`` allocation.

        Raises ``SimulatorNotFound`` (404) for an unknown id and
        ``SimulatorConflict`` with code ``CANNOT_CANCEL`` (409) once the
        shipment has departed.
        """
        response = await self._request(
            "POST", f"/v1/allocations/{allocation_id}/cancel")
        if response.status_code == 200:
            allocation = Allocation.model_validate(response.json())
            await self._invalidate_after_mutation()
            return allocation
        await self._raise_for_status(
            response, f"/v1/allocations/{allocation_id}/cancel")
        raise AssertionError("unreachable")  # pragma: no cover

    # ------------------------------------------------------------------ #
    # Admin control (bypasses fault injection)
    # ------------------------------------------------------------------ #
    async def admin_reset(self) -> SimulationInstance:
        """Hard reset: wipe all rows and reload the baked scenario."""
        response = await self._request("POST", "/admin/reset")
        if response.status_code >= 400:
            await self._raise_for_status(response, "/admin/reset")
        await self._invalidate_after_mutation()
        return SimulationInstance.model_validate(response.json())

    async def admin_step(self) -> dict[str, Any]:
        """Advance exactly one tick, deterministically."""
        response = await self._request("POST", "/admin/step")
        if response.status_code >= 400:
            await self._raise_for_status(response, "/admin/step")
        # A tick changes the world: every cached snapshot is now suspect.
        await self._invalidate_after_mutation()
        return response.json()

    async def admin_run(self) -> dict[str, Any]:
        response = await self._request("POST", "/admin/run")
        if response.status_code >= 400:
            await self._raise_for_status(response, "/admin/run")
        await self._invalidate_after_mutation()
        return response.json()

    async def admin_pause(self) -> dict[str, Any]:
        response = await self._request("POST", "/admin/pause")
        if response.status_code >= 400:
            await self._raise_for_status(response, "/admin/pause")
        await self._invalidate_after_mutation()
        return response.json()

    async def admin_toggle(self) -> dict[str, Any]:
        response = await self._request("POST", "/admin/toggle")
        if response.status_code >= 400:
            await self._raise_for_status(response, "/admin/toggle")
        await self._invalidate_after_mutation()
        return response.json()

    async def admin_inject_event(
        self,
        *,
        type: str,
        start_tick: int,
        duration_ticks: int,
        parameters: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Inject a crisis event (the six documented event types)."""
        body: dict[str, Any] = {
            "type": type,
            "start_tick": start_tick,
            "duration_ticks": duration_ticks,
        }
        if parameters:
            body["parameters"] = parameters
        response = await self._request(
            "POST", "/admin/events", json_body=body)
        if response.status_code >= 400:
            await self._raise_for_status(response, "/admin/events")
        await self._invalidate_after_mutation()
        return response.json()

    async def admin_inject_fault(
        self,
        *,
        type: str,
        duration_seconds: int,
        parameters: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Inject an API fault (the five documented fault types)."""
        body: dict[str, Any] = {
            "type": type,
            "duration_seconds": duration_seconds,
        }
        if parameters:
            body["parameters"] = parameters
        response = await self._request(
            "POST", "/admin/faults", json_body=body)
        if response.status_code >= 400:
            await self._raise_for_status(response, "/admin/faults")
        await self._invalidate_after_mutation()
        return response.json()

    async def admin_clear_faults(self) -> dict[str, Any]:
        response = await self._request("POST", "/admin/faults/clear")
        if response.status_code >= 400:
            await self._raise_for_status(response, "/admin/faults/clear")
        await self._invalidate_after_mutation()
        return response.json()

    async def admin_faults(self) -> list[dict[str, Any]]:
        response = await self._request("GET", "/admin/faults")
        if response.status_code >= 400:
            await self._raise_for_status(response, "/admin/faults")
        return response.json()

    async def admin_events(self) -> list[dict[str, Any]]:
        response = await self._request("GET", "/admin/events")
        if response.status_code >= 400:
            await self._raise_for_status(response, "/admin/events")
        return response.json()

    async def admin_audit(self, limit: int = 200) -> list[AuditEntry]:
        response = await self._request("GET", "/admin/audit", params={"limit": limit})
        if response.status_code >= 400:
            await self._raise_for_status(response, "/admin/audit")
        return [AuditEntry.model_validate(item) for item in response.json()]

    # ------------------------------------------------------------------ #
    # SSE stream
    # ------------------------------------------------------------------ #
    @staticmethod
    def parse_sse_frame(frame: str) -> Optional[SSEEvent]:
        """Parse one raw SSE frame.

        Returns ``None`` for an empty frame. Comment frames (``: connected``,
        ``: keepalive``) come back with ``event=None`` so callers can ignore
        them explicitly rather than by accident.
        """
        if not frame.strip():
            return None

        event_name: Optional[str] = None
        data_lines: list[str] = []

        for line in frame.splitlines():
            if not line or line.startswith(":"):
                # Comment line -- ignored, but retained in the raw payload.
                continue
            if line.startswith("event:"):
                event_name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].strip())
            elif line.startswith("id:"):
                continue
            elif line.startswith("retry:"):
                continue

        if event_name is None and not data_lines:
            return SSEEvent(event=None, data="", raw=frame)

        return SSEEvent(event=event_name, data="\n".join(data_lines), raw=frame)

    async def stream(
        self,
        *,
        max_events: Optional[int] = None,
        reconnect: bool = True,
        max_reconnects: int = 10,
    ) -> AsyncIterator[SSEEvent]:
        """Yield SSE events from ``GET /v1/stream``.

        Handles the documented failure modes:

        * ``: connected`` / ``: keepalive`` comments are ignored.
        * A ``503`` (e.g. the ``stream_disconnect`` fault) triggers a reconnect
          with exponential backoff.
        * A dropped connection triggers a reconnect; callers are expected to
          perform a full REST state sync afterwards (there is no
          ``Last-Event-ID`` replay).
        """
        attempt = 0
        while True:
            try:
                async with self._client.stream(
                    "GET", "/v1/stream",
                    timeout=httpx.Timeout(self.timeout, read=None),
                ) as response:
                    if response.status_code == 503:
                        try:
                            payload = await response.aread()
                            decoded = json.loads(payload or b"{}")
                        except Exception:  # pragma: no cover
                            decoded = {}
                        raise SimulatorFaultInjected(
                            self._extract_error_message(
                                decoded, "Stream unavailable"),
                            status_code=503,
                            code=self._extract_error_code(decoded),
                            payload=decoded,
                        )
                    if response.status_code >= 400:
                        await response.aread()
                        raise SimulatorError(
                            f"HTTP {response.status_code} opening SSE stream",
                            status_code=response.status_code)

                    attempt = 0
                    frame = ""
                    async for line in response.aiter_lines():
                        if line == "":
                            parsed = self.parse_sse_frame(frame)
                            frame = ""
                            if parsed is None:
                                continue
                            if parsed.is_comment:
                                # ": connected" / ": keepalive" -- ignore.
                                continue
                            yield parsed
                            if max_events is not None:
                                max_events -= 1
                                if max_events <= 0:
                                    return
                        else:
                            frame += line + "\n"

            except (SimulatorFaultInjected, httpx.HTTPError) as exc:
                attempt += 1
                if not reconnect or attempt > max_reconnects:
                    raise
                delay = self._backoff_delay(attempt - 1)
                LOGGER.warning(
                    "SSE stream dropped (%s); reconnecting in %.2fs (attempt %d/%d)",
                    exc, delay, attempt, max_reconnects)
                await asyncio.sleep(delay)


# --------------------------------------------------------------------------- #
# Stream consumer with advisory-hint semantics
# --------------------------------------------------------------------------- #
class StreamListener:
    """Consumes the SSE feed and treats it strictly as an advisory hint.

    On every interesting event the listener triggers a REST re-fetch of the
    affected resource. That re-fetch *is* the state update -- the event
    payload itself is never trusted as world state.
    """

    #: Which resources each event type invalidates.
    RESOURCE_MAP: dict[str, tuple[str, ...]] = {
        "simulation.tick": ("instance", "metrics", "depots", "stations",
                            "allocations", "supply-arrivals"),
        "allocation.status_changed": ("allocations", "stations", "depots"),
        "inventory.updated": ("depots", "stations"),
        "simulator.notice": ("instance",),
    }

    def __init__(self, client: SimulatorClient, *, resync_on_event: bool = True) -> None:
        self.client = client
        self.resync_on_event = resync_on_event
        self.stats = StreamStats()
        self.events: list[SSEEvent] = []
        self.resync_count = 0
        self._resync_queue: asyncio.Queue[str] = asyncio.Queue()

    async def run(
        self,
        *,
        duration: Optional[float] = None,
        stop_event: Optional[asyncio.Event] = None,
        max_events: Optional[int] = None,
    ) -> StreamStats:
        """Consume the stream for ``duration`` seconds (or until stopped)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + duration if duration else None

        try:
            async for event in self.client.stream(max_events=max_events):
                self.stats.events_received += 1
                self.stats.seen_event_names.add(event.event or "")
                self.events.append(event)

                for resource in self.RESOURCE_MAP.get(event.event or "", ()):
                    await self._resync_queue.put(resource)

                if self.resync_on_event:
                    await self._resync(event)

                if stop_event is not None and stop_event.is_set():
                    break
                if deadline is not None and loop.time() >= deadline:
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.stats.last_error = str(exc)
            LOGGER.warning("SSE listener stopped: %s", exc)

        return self.stats

    async def _resync(self, event: SSEEvent) -> None:
        """Immediately re-fetch state affected by an SSE event.

        This is the core SSE rule: the event is a hint, REST is the truth.
        """
        self.resync_count += 1
        resources = self.RESOURCE_MAP.get(event.event or "", ())
        for resource in resources:
            try:
                if resource == "instance":
                    await self.client.instance()
                elif resource == "metrics":
                    await self.client.metrics()
                elif resource == "depots":
                    await self.client.depots()
                elif resource == "stations":
                    await self.client.stations()
                elif resource == "allocations":
                    await self.client.allocations()
                elif resource == "supply-arrivals":
                    await self.client.supply_arrivals()
            except SimulatorError as exc:
                LOGGER.warning(
                    "REST re-sync of %s after %s failed: %s",
                    resource, event.event, exc)


def build_default_client() -> SimulatorClient:
    """Client configured from environment variables (container-friendly)."""
    return SimulatorClient(
        base_url=os.environ.get("SIMULATOR_BASE_URL", "http://simulator-api:8000"),
        redis_url=os.environ.get("REDIS_URL", "redis://redis:6379/0"),
    )


__all__ = [
    "Allocation",
    "AllocationRequest",
    "AuditEntry",
    "DemandObservation",
    "Depot",
    "DomainEvent",
    "HealthResponse",
    "Metrics",
    "Region",
    "Route",
    "SSEEvent",
    "SimulatorClient",
    "SimulatorConflict",
    "SimulatorError",
    "SimulatorFaultInjected",
    "SimulatorNotFound",
    "SimulatorTimeout",
    "SimulatorValidationError",
    "Station",
    "StreamListener",
    "StreamStats",
    "SupplyArrival",
    "build_default_client",
]
