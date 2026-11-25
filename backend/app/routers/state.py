"""Read-only world-state endpoints, served from the cached snapshot."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ..degraded import degraded_fields as _degraded_fields
from ..degraded import read_snapshot as _fresh_snapshot
from ..validator import (
    dispatch_committed_this_tick,
    dispatch_remaining,
    station_free_space,
    station_inbound,
)

router = APIRouter(prefix="/api", tags=["state"])


@router.get("/state")
async def get_state(request: Request) -> dict[str, Any]:
    """The full world snapshot in one call."""
    snapshot = await _fresh_snapshot(request)
    return {
        "status": "OK",
        "state": snapshot.to_dict(),
        "snapshot_age_seconds": round(snapshot.age(), 3),
        **_degraded_fields(request),
    }


@router.get("/depots")
async def list_depots(request: Request) -> dict[str, Any]:
    """Depots with their dispatch headroom at the current tick."""
    snapshot = await _fresh_snapshot(request)
    depots = []
    for depot in snapshot.depots:
        depots.append(
            {
                **depot.model_dump(),
                "dispatch_committed_this_tick": dispatch_committed_this_tick(
                    snapshot, depot.id
                ),
                "dispatch_remaining": dispatch_remaining(snapshot, depot.id),
            }
        )
    return {
        "status": "OK",
        "tick": snapshot.tick,
        "depots": depots,
        **_degraded_fields(request),
    }


@router.get("/depots/{depot_id}")
async def get_depot(depot_id: str, request: Request) -> dict[str, Any]:
    snapshot = await _fresh_snapshot(request)
    depot = snapshot.depot(depot_id)
    if depot is None:
        raise HTTPException(status_code=404, detail=f"unknown depot {depot_id!r}")
    return {
        "status": "OK",
        "tick": snapshot.tick,
        "depot": {
            **depot.model_dump(),
            "dispatch_committed_this_tick": dispatch_committed_this_tick(
                snapshot, depot.id
            ),
            "dispatch_remaining": dispatch_remaining(snapshot, depot.id),
        },
        **_degraded_fields(request),
    }


@router.get("/stations")
async def list_stations(request: Request) -> dict[str, Any]:
    """Stations with free space, inbound fuel and spill exposure."""
    snapshot = await _fresh_snapshot(request)
    stations = []
    for station in snapshot.stations:
        station_payload = station.model_dump()
        free_space = {}
        inbound = {}
        spill_exposure = {}
        for fuel in station.capacity:
            free_space[fuel] = station_free_space(snapshot, station.id, fuel)
            inbound[fuel] = station_inbound(snapshot, station.id, fuel)
            spill_exposure[fuel] = max(
                0.0, inbound[fuel] - free_space[fuel]
            )
        stations.append(
            {
                **station_payload,
                "free_space": free_space,
                "inbound_in_flight": inbound,
                "spill_exposure": spill_exposure,
                "route_count": len(snapshot.routes_for_station(station.id)),
                "is_single_connected": len(
                    snapshot.routes_for_station(station.id)
                ) == 1,
            }
        )
    return {
        "status": "OK",
        "tick": snapshot.tick,
        "stations": stations,
        **_degraded_fields(request),
    }


@router.get("/stations/{station_id}")
async def get_station(station_id: str, request: Request) -> dict[str, Any]:
    snapshot = await _fresh_snapshot(request)
    station = snapshot.station(station_id)
    if station is None:
        raise HTTPException(status_code=404, detail=f"unknown station {station_id!r}")
    free_space = {
        fuel: station_free_space(snapshot, station.id, fuel)
        for fuel in station.capacity
    }
    inbound = {
        fuel: station_inbound(snapshot, station.id, fuel)
        for fuel in station.capacity
    }
    routes = snapshot.routes_for_station(station.id)
    return {
        "status": "OK",
        "tick": snapshot.tick,
        "station": {
            **station.model_dump(),
            "free_space": free_space,
            "inbound_in_flight": inbound,
            "spill_exposure": {
                fuel: max(0.0, inbound[fuel] - free_space[fuel])
                for fuel in station.capacity
            },
            "routes": [r.model_dump() for r in routes],
            "route_count": len(routes),
            "is_single_connected": len(routes) == 1,
        },
        **_degraded_fields(request),
    }


@router.get("/routes")
async def list_routes(request: Request) -> dict[str, Any]:
    """Routes, flagged with whether the destination is single-connected."""
    snapshot = await _fresh_snapshot(request)
    routes = []
    for route in snapshot.routes:
        station_routes = snapshot.routes_for_station(route.destination_station_id)
        routes.append(
            {
                **route.model_dump(),
                "destination_route_count": len(station_routes),
                "is_single_connected": len(station_routes) == 1,
            }
        )
    return {
        "status": "OK",
        "tick": snapshot.tick,
        "routes": routes,
        **_degraded_fields(request),
    }


@router.get("/allocations")
async def list_allocations(request: Request) -> dict[str, Any]:
    """The allocation ledger."""
    snapshot = await _fresh_snapshot(request)
    return {
        "status": "OK",
        "tick": snapshot.tick,
        "allocations": [a.model_dump() for a in snapshot.allocations],
        **_degraded_fields(request),
    }


@router.get("/allocations/{allocation_id}")
async def get_allocation(allocation_id: int, request: Request) -> dict[str, Any]:
    snapshot = await _fresh_snapshot(request)
    allocation = snapshot.allocation(allocation_id)
    if allocation is None:
        raise HTTPException(
            status_code=404, detail=f"unknown allocation {allocation_id}"
        )
    return {
        "status": "OK",
        "tick": snapshot.tick,
        "allocation": allocation.model_dump(),
        **_degraded_fields(request),
    }


@router.get("/metrics")
async def get_metrics(request: Request) -> dict[str, Any]:
    """Simulator metrics plus derived operational KPIs."""
    snapshot = await _fresh_snapshot(request)
    metrics = snapshot.metrics
    payload: dict[str, Any] = {
        "status": "OK",
        "tick": snapshot.tick,
        "sim_time": snapshot.sim_time,
        "metrics": metrics.model_dump() if metrics else None,
    }
    if metrics is not None:
        payload["kpis"] = {
            "service_level_pct": round(metrics.service_level * 100, 2),
            "unmet_demand_litres": metrics.unmet_demand_liters,
            "allocation_failures": metrics.allocation_failures,
            "active_shipments": sum(
                1
                for a in snapshot.allocations
                if a.status in ("PENDING", "IN_TRANSIT")
            ),
            "disrupted_routes": sum(
                1 for r in snapshot.routes if r.status != "AVAILABLE"
            ),
            "closed_stations": sum(
                1 for s in snapshot.stations if s.status != "OPEN"
            ),
        }
    payload.update(_degraded_fields(request))
    return payload


@router.get("/supply-arrivals")
async def list_supply_arrivals(request: Request) -> dict[str, Any]:
    snapshot = await _fresh_snapshot(request)
    return {
        "status": "OK",
        "tick": snapshot.tick,
        "supply_arrivals": [s.model_dump() for s in snapshot.supply_arrivals],
        **_degraded_fields(request),
    }


@router.get("/events")
async def list_domain_events(request: Request) -> dict[str, Any]:
    """Domain events (crises, disruptions) currently registered."""
    snapshot = await _fresh_snapshot(request)
    return {
        "status": "OK",
        "tick": snapshot.tick,
        "events": [e.model_dump() for e in snapshot.events],
        **_degraded_fields(request),
    }
