"""Shared pytest fixtures and synthetic world builders.

The synthetic snapshots here mirror the *measured* baseline scenario of the
simulator (2 depots, 4 stations, 6 routes), so the pure unit tests exercise
the same shapes the real world has.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Optional

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.models import PlanRequest  # noqa: E402
from app.simulator_client import (  # noqa: E402
    Allocation,
    Depot,
    Metrics,
    Route,
    SimulationInstance,
    Station,
    SupplyArrival,
)
from app.state import WorldSnapshot  # noqa: E402

FUELS = ("DIESEL", "PETROL", "OCTANE")


def make_depot(
    depot_id: str = "depot-gazipur",
    *,
    status: str = "OPEN",
    dispatch_capacity_per_tick: float = 12000.0,
    inventory: Optional[dict[str, float]] = None,
) -> Depot:
    inv = {"DIESEL": 50000.0, "PETROL": 40000.0, "OCTANE": 30000.0}
    if inventory is not None:
        inv = inventory
    return Depot(
        id=depot_id,
        name=depot_id.replace("depot-", "Depot ").title(),
        region_id="region-dhaka",
        status=status,
        dispatch_capacity_per_tick=dispatch_capacity_per_tick,
        capacity={"DIESEL": 100000.0, "PETROL": 100000.0, "OCTANE": 100000.0},
        inventory=inv,
    )


def make_station(
    station_id: str = "station-mirpur",
    *,
    status: str = "OPEN",
    capacity: Optional[dict[str, float]] = None,
    inventory: Optional[dict[str, float]] = None,
    demand_profile: str = "urban_high",
    demand_multiplier: float = 1.0,
    region_id: str = "region-dhaka",
) -> Station:
    cap = {fuel: 6000.0 for fuel in FUELS}
    inv = {fuel: 0.0 for fuel in FUELS}
    if capacity is not None:
        cap = capacity
    if inventory is not None:
        inv = inventory
    return Station(
        id=station_id,
        name=station_id.replace("station-", "Station ").title(),
        region_id=region_id,
        status=status,
        demand_profile=demand_profile,
        demand_multiplier=demand_multiplier,
        capacity=cap,
        inventory=inv,
    )


def make_route(
    route_id: str = "route-gazipur-mirpur",
    *,
    source_depot_id: str = "depot-gazipur",
    destination_station_id: str = "station-mirpur",
    transit_ticks: int = 2,
    max_shipment: float = 7000.0,
    status: str = "AVAILABLE",
) -> Route:
    return Route(
        id=route_id,
        source_depot_id=source_depot_id,
        destination_station_id=destination_station_id,
        transit_ticks=transit_ticks,
        max_shipment=max_shipment,
        status=status,
    )


def make_allocation(
    allocation_id: int = 1,
    *,
    depot_id: str = "depot-gazipur",
    station_id: str = "station-mirpur",
    route_id: str = "route-gazipur-mirpur",
    fuel_type: str = "DIESEL",
    quantity: float = 1000.0,
    created_tick: int = 5,
    status: str = "PENDING",
) -> Allocation:
    return Allocation(
        id=allocation_id,
        idempotency_key=f"alloc-{allocation_id}",
        source_depot_id=depot_id,
        destination_station_id=station_id,
        route_id=route_id,
        fuel_type=fuel_type,
        quantity=quantity,
        created_tick=created_tick,
        departure_tick=None,
        expected_arrival_tick=None,
        actual_arrival_tick=None,
        status=status,
        failure_reason=None,
    )


def make_snapshot(
    *,
    tick: int = 5,
    depots: Optional[list[Depot]] = None,
    stations: Optional[list[Station]] = None,
    routes: Optional[list[Route]] = None,
    allocations: Optional[list[Allocation]] = None,
    supply_arrivals: Optional[list[SupplyArrival]] = None,
    metrics: Optional[Metrics] = None,
) -> WorldSnapshot:
    """A snapshot mirroring the measured baseline topology."""
    return WorldSnapshot(
        tick=tick,
        sim_time="2026-01-01T08:00:00",
        instance=SimulationInstance(
            id=1,
            scenario_id="baseline",
            scenario_version="1.0.0",
            seed=12345,
            sim_time="2026-01-01T08:00:00",
            tick=tick,
            tick_minutes=15,
            status="PAUSED",
        ),
        depots=depots if depots is not None else [make_depot()],
        stations=stations if stations is not None else [make_station()],
        routes=routes if routes is not None else [make_route()],
        allocations=allocations if allocations is not None else [],
        supply_arrivals=supply_arrivals if supply_arrivals is not None else [],
        metrics=metrics
        or Metrics(
            served_demand_liters=0.0,
            unmet_demand_liters=0.0,
            service_level=1.0,
            allocation_liters=0.0,
            allocation_failures=0,
        ),
    )


def make_request(
    *,
    depot_id: str = "depot-gazipur",
    station_id: str = "station-mirpur",
    fuel_type: str = "DIESEL",
    quantity: float = 1000.0,
    route_id: Optional[str] = None,
    allow_split: bool = True,
    allow_alternate_route: bool = False,
) -> PlanRequest:
    return PlanRequest(
        source_depot_id=depot_id,
        destination_station_id=station_id,
        fuel_type=fuel_type,
        quantity=quantity,
        route_id=route_id,
        allow_split=allow_split,
        allow_alternate_route=allow_alternate_route,
    )


@pytest.fixture
def snapshot() -> WorldSnapshot:
    return make_snapshot()


@pytest.fixture
def request_factory():
    return make_request
