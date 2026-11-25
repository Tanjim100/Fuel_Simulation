"""World snapshot + TTL cache.

The backend keeps an in-memory view of the simulator's world so that
validation and planning are instant (no I/O on the hot path) and so that every
operator sees a *consistent* picture of the world rather than a different
snapshot per request.

The snapshot is refreshed by the broker (on every SSE event) and by the poller
(when SSE is unavailable or suspected dead). REST remains the source of truth:
SSE events only ever *trigger* a refresh, their payloads are never trusted.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

from .simulator_client import (
    Allocation,
    Depot,
    DomainEvent,
    Metrics,
    Route,
    SimulationInstance,
    Station,
    SupplyArrival,
)

__all__ = [
    "WorldSnapshot",
    "WorldStateCache",
    "Observation",
    "ObservationLog",
    "build_observations",
]


@dataclass
class WorldSnapshot:
    """An immutable-by-convention view of the simulator's world at one tick."""

    tick: int = 0
    sim_time: str = ""
    instance: Optional[SimulationInstance] = None
    depots: list[Depot] = field(default_factory=list)
    stations: list[Station] = field(default_factory=list)
    routes: list[Route] = field(default_factory=list)
    allocations: list[Allocation] = field(default_factory=list)
    supply_arrivals: list[SupplyArrival] = field(default_factory=list)
    events: list[DomainEvent] = field(default_factory=list)
    metrics: Optional[Metrics] = None
    captured_at: float = field(default_factory=time.monotonic)

    # -- lookups ----------------------------------------------------------- #
    def depot(self, depot_id: str) -> Optional[Depot]:
        for depot in self.depots:
            if depot.id == depot_id:
                return depot
        return None

    def station(self, station_id: str) -> Optional[Station]:
        for station in self.stations:
            if station.id == station_id:
                return station
        return None

    def route(self, route_id: str) -> Optional[Route]:
        for route in self.routes:
            if route.id == route_id:
                return route
        return None

    def allocation(self, allocation_id: int) -> Optional[Allocation]:
        for allocation in self.allocations:
            if allocation.id == allocation_id:
                return allocation
        return None

    def routes_for_station(self, station_id: str) -> list[Route]:
        """Every route whose destination is ``station_id``."""
        return [r for r in self.routes if r.destination_station_id == station_id]

    def routes_from_depot(self, depot_id: str) -> list[Route]:
        return [r for r in self.routes if r.source_depot_id == depot_id]

    def age(self) -> float:
        """Seconds since this snapshot was captured."""
        return time.monotonic() - self.captured_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "tick": self.tick,
            "sim_time": self.sim_time,
            "instance": self.instance.model_dump() if self.instance else None,
            "depots": [d.model_dump() for d in self.depots],
            "stations": [s.model_dump() for s in self.stations],
            "routes": [r.model_dump() for r in self.routes],
            "allocations": [a.model_dump() for a in self.allocations],
            "supply_arrivals": [s.model_dump() for s in self.supply_arrivals],
            "events": [e.model_dump() for e in self.events],
            "metrics": self.metrics.model_dump() if self.metrics else None,
            "captured_at": self.captured_at,
        }


class WorldStateCache:
    """TTL'd holder for the current :class:`WorldSnapshot`.

    A single :class:`asyncio.Lock` guards the swap so readers always observe a
    fully-built snapshot, never a partially-updated one.
    """

    def __init__(self, *, max_age: float = 1.0) -> None:
        self.max_age = max_age
        self._snapshot: WorldSnapshot = WorldSnapshot()
        self._lock = asyncio.Lock()
        self._refresh_count = 0
        self._last_refresh_at: float = 0.0
        self._last_error: Optional[str] = None

    # -- reads -------------------------------------------------------------- #
    def current(self, *, max_age: Optional[float] = None) -> WorldSnapshot:
        """The current snapshot, without touching the lock.

        A snapshot older than ``max_age`` is still returned -- callers that need
        freshness check :meth:`is_stale` and trigger a refresh themselves.
        """
        return self._snapshot

    def is_stale(self, *, max_age: Optional[float] = None) -> bool:
        limit = self.max_age if max_age is None else max_age
        return self._snapshot.age() > limit

    @property
    def refresh_count(self) -> int:
        return self._refresh_count

    @property
    def last_refresh_at(self) -> float:
        return self._last_refresh_at

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    # -- writes ------------------------------------------------------------- #
    async def replace(self, snapshot: WorldSnapshot) -> WorldSnapshot:
        """Atomically swap in a freshly-captured snapshot."""
        async with self._lock:
            self._snapshot = snapshot
            self._refresh_count += 1
            self._last_refresh_at = time.monotonic()
            self._last_error = None
            return self._snapshot

    async def mark_error(self, error: str) -> None:
        async with self._lock:
            self._last_error = error


# --------------------------------------------------------------------------- #
# The observation log
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Observation:
    """One tick's worth of observation for a single (station, fuel) pair.

    This is what the anomaly detectors consume. It exists because the
    simulator's ``/v1/demand-history`` reports *demand* but not *inventory*,
    and the most interesting anomalies are inventory anomalies: fuel that left
    a station without being served is either a **silent spill** (the simulator
    clamps the delivery to the tank and discards the excess with no error and
    no audit flag), a manual removal, or a booking.

    The log is a *local* rolling window, not a simulator read.
    """

    tick: int
    station_id: str
    fuel_type: str
    demand_litres: float
    served_litres: float
    unmet_litres: float
    inventory_litres: float
    depot_inventory_litres: float
    demand_multiplier: float
    station_status: str = "OPEN"
    route_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "tick": self.tick,
            "station_id": self.station_id,
            "fuel_type": self.fuel_type,
            "demand_litres": self.demand_litres,
            "served_litres": self.served_litres,
            "unmet_litres": self.unmet_litres,
            "inventory_litres": self.inventory_litres,
            "depot_inventory_litres": self.depot_inventory_litres,
            "demand_multiplier": self.demand_multiplier,
            "station_status": self.station_status,
            "route_ids": list(self.route_ids),
        }


class ObservationLog:
    """A bounded, in-memory rolling window of :class:`Observation` records.

    Bounded at ``max_entries`` (4096 by default) for the same reason the
    broker's event buffer is bounded: an unbounded log in a long-running
    backend is a slow memory leak.

    The log is **not** part of :class:`WorldSnapshot`. The snapshot stays a
    faithful mirror of the simulator; derived state lives in its own
    singleton. That keeps the "REST is the truth" line intact.
    """

    def __init__(self, *, max_entries: int = 4096) -> None:
        self.max_entries = max_entries
        self._entries: deque[Observation] = deque(maxlen=max_entries)
        self._lock = asyncio.Lock()

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def entries(self) -> list[Observation]:
        """A copy of the log, oldest first."""
        return list(self._entries)

    async def append(self, observation: Observation) -> None:
        """Add one observation, dropping the oldest if the log is full."""
        async with self._lock:
            self._entries.append(observation)

    async def extend(self, observations: list[Observation]) -> None:
        """Add several observations at once.

        The *served* and *unmet* figures are filled in here, from the
        inventory change against the previous tick for the same
        (station, fuel) pair. The simulator does not report them directly,
        but they are recoverable:

            consumed  = previous_inventory - current_inventory + arrivals
            served    = min(consumed, demand)
            unmet     = demand - served

        ``arrivals`` is the fuel credited to the station this tick -- the
        measured one-tick lag means a shipment created on tick ``t - k``
        lands on tick ``t - k + transit_ticks + 1``.
        """
        enriched: list[Observation] = []

        for observation in observations:
            previous = await self.previous_for(
                observation.station_id,
                observation.fuel_type,
                before_tick=observation.tick,
            )
            if previous is None:
                # Nothing to compare against yet: served and unmet stay zero,
                # which is honest rather than a guess.
                enriched.append(observation)
                continue

            arrivals = getattr(observation, "arrivals_litres", 0.0) or 0.0
            consumed = (
                previous.inventory_litres - observation.inventory_litres + arrivals
            )
            consumed = max(0.0, consumed)
            served = min(consumed, observation.demand_litres)
            unmet = max(0.0, observation.demand_litres - served)

            enriched.append(
                Observation(
                    tick=observation.tick,
                    station_id=observation.station_id,
                    fuel_type=observation.fuel_type,
                    demand_litres=observation.demand_litres,
                    served_litres=served,
                    unmet_litres=unmet,
                    inventory_litres=observation.inventory_litres,
                    depot_inventory_litres=observation.depot_inventory_litres,
                    demand_multiplier=observation.demand_multiplier,
                    station_status=observation.station_status,
                    route_ids=observation.route_ids,
                )
            )

        async with self._lock:
            self._entries.extend(enriched)

    async def recent(
        self,
        *,
        station_id: Optional[str] = None,
        fuel_type: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[Observation]:
        """The most recent observations, oldest first."""
        entries = list(self._entries)
        if station_id is not None:
            entries = [e for e in entries if e.station_id == station_id]
        if fuel_type is not None:
            entries = [e for e in entries if e.fuel_type == fuel_type]
        if limit is not None and limit > 0:
            entries = entries[-limit:]
        return entries

    async def latest_for(
        self, station_id: str, fuel_type: str
    ) -> Optional[Observation]:
        """The most recent observation for one (station, fuel) pair."""
        for entry in reversed(self._entries):
            if entry.station_id == station_id and entry.fuel_type == fuel_type:
                return entry
        return None

    async def previous_for(
        self, station_id: str, fuel_type: str, *, before_tick: int
    ) -> Optional[Observation]:
        """The most recent observation for one pair *before* a given tick."""
        for entry in reversed(self._entries):
            if (
                entry.station_id == station_id
                and entry.fuel_type == fuel_type
                and entry.tick < before_tick
            ):
                return entry
        return None

    async def clear(self) -> None:
        async with self._lock:
            self._entries.clear()


# --------------------------------------------------------------------------- #
# Building observations from a snapshot
# --------------------------------------------------------------------------- #
def build_observations(snap: WorldSnapshot) -> list[Observation]:
    """Derive one :class:`Observation` per (station, fuel) from a snapshot.

    The demand figures come from the **forecast**, not from history: the
    simulator's demand is deterministic, so the demand of the current tick
    can be computed exactly (verified 60/60 against
    ``/v1/demand-history``). Using the forecast rather than a history read
    means the observation log works on a cold start, with no history to
    fetch.

    The *served* and *unmet* figures are derived from the inventory change
    between this tick and the last one the log holds, so the first tick of a
    cold start reports zero for both -- which is honest: there is nothing to
    compare against yet.
    """
    from .forecast import FUEL_TYPES, build_forecast

    observations: list[Observation] = []

    # The demand for the current tick. The forecast is built for a single
    # tick starting at the snapshot's tick.
    try:
        forecast = build_forecast(snap, horizon=1)
    except Exception:  # pragma: no cover - defensive
        forecast = None

    for station in snap.stations:
        routes = tuple(
            sorted(r.id for r in snap.routes_for_station(station.id))
        )
        for fuel in FUEL_TYPES:
            if fuel not in station.capacity:
                continue

            demand = 0.0
            if forecast is not None:
                demand = forecast.demand_at(station.id, fuel, snap.tick)

            inventory = float(station.inventory.get(fuel, 0.0))

            # The depot that serves this station, for context.
            depot_inventory = 0.0
            serving = [
                r for r in snap.routes_for_station(station.id)
                if r.source_depot_id
            ]
            if serving:
                depot = snap.depot(serving[0].source_depot_id)
                if depot is not None:
                    depot_inventory = float(depot.inventory.get(fuel, 0.0))

            observations.append(
                Observation(
                    tick=snap.tick,
                    station_id=station.id,
                    fuel_type=fuel,
                    demand_litres=demand,
                    served_litres=0.0,
                    unmet_litres=0.0,
                    inventory_litres=inventory,
                    depot_inventory_litres=depot_inventory,
                    demand_multiplier=float(station.demand_multiplier),
                    station_status=station.status,
                    route_ids=routes,
                )
            )

    # Attach the arrivals hint, which the log uses to work out how much fuel
    # was credited this tick. The hint is not part of the frozen
    # ``Observation`` record -- it is an input to the calculation, not a
    # fact about the world -- so it rides along as an attribute.
    arrivals = _arrivals_by_tick(snap)
    for observation in observations:
        landed = arrivals.get((observation.station_id, observation.fuel_type), 0.0)
        object.__setattr__(observation, "arrivals_litres", landed)

    return observations


def _arrivals_by_tick(snap: WorldSnapshot) -> dict[tuple[str, str], float]:
    """The fuel credited to each station this tick.

    A shipment created on tick ``t`` is credited at
    ``t + transit_ticks + 1`` -- the **measured** one-tick lag, not the
    simulator's own ``expected_arrival_tick`` field, which understates it by
    one.
    """
    totals: dict[tuple[str, str], float] = {}

    for allocation in getattr(snap, "allocations", []) or []:
        if allocation.status not in ("PENDING", "IN_TRANSIT", "ARRIVED"):
            continue
        route = snap.route(allocation.route_id)
        if route is None:
            continue
        applied = allocation.created_tick + route.transit_ticks + 1
        if applied != snap.tick:
            continue
        key = (allocation.destination_station_id, allocation.fuel_type)
        totals[key] = totals.get(key, 0.0) + float(allocation.quantity)

    return totals
