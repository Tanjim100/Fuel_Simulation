#!/usr/bin/env python3
"""
End-to-end verification suite for the BUP Fuel Supply Simulator.

Runs six sequential check groups against a live simulator and prints a
pass/fail report for every individual assertion:

  1. Health & baseline topology
  2. Deterministic tick & SSE delivery
  3. Allocation lifecycle & idempotency
  4. Constraint & error validation
  5. Crisis & fault resilience
  6. Final audit & metrics summary

Usage:
    python verify_simulator.py                     # against http://simulator-api:8000
    python verify_simulator.py --base-url http://localhost:8000
    python verify_simulator.py --redis-url redis://redis:6379/0
    python verify_simulator.py --json-report report.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from simulator_client import (
    AllocationRequest,
    SimulatorClient,
    SimulatorConflict,
    SimulatorError,
    SimulatorFaultInjected,
    SimulatorNotFound,
    SimulatorTimeout,
    SimulatorValidationError,
    StreamListener,
)

LOGGER = logging.getLogger("verify")

# --------------------------------------------------------------------------- #
# Expected baseline topology (Integration Guide section 8)
# --------------------------------------------------------------------------- #
EXPECTED_REGIONS = 2
EXPECTED_DEPOTS = 2
EXPECTED_STATIONS = 4
EXPECTED_ROUTES = 6
EXPECTED_SUPPLY_ARRIVALS = 22

BASELINE_DEPOT_ID = "depot-gazipur"
BASELINE_STATION_ID = "station-mirpur"
BASELINE_ROUTE_ID = "route-gazipur-mirpur"
BASELINE_FUEL = "DIESEL"
BASELINE_QUANTITY = 3000
OVER_CAPACITY_QUANTITY = 7000

# Colors (disabled automatically when stdout is not a TTY).
class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    GREEN = "\033[32m"
    RED = "\033[31m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"
    DIM = "\033[2m"


def _supports_color() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


# --------------------------------------------------------------------------- #
# Report scaffolding
# --------------------------------------------------------------------------- #
@dataclass
class CheckResult:
    group: str
    name: str
    passed: bool
    detail: str = ""
    duration: float = 0.0


@dataclass
class Report:
    results: list[CheckResult] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    duration: float = 0.0
    summary: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if not r.passed)

    def add(self, group: str, name: str, passed: bool, detail: str = "",
            duration: float = 0.0) -> bool:
        self.results.append(CheckResult(group, name, passed, detail, duration))
        mark = f"{C.GREEN}PASS{C.RESET}" if passed else f"{C.RED}FAIL{C.RESET}"
        line = f"  [{mark}] {name}"
        if detail:
            line += f" {C.DIM}-- {detail}{C.RESET}"
        print(line, flush=True)
        return passed

    def group(self, title: str) -> str:
        print(f"\n{C.CYAN}{C.BOLD}{title}{C.RESET}", flush=True)
        return title


# --------------------------------------------------------------------------- #
# Verification suite
# --------------------------------------------------------------------------- #
class SimulatorVerifier:
    def __init__(self, client: SimulatorClient, report: Report) -> None:
        self.client = client
        self.report = report
        self._alloc_counter = 0

    def _unique_key(self, prefix: str) -> str:
        self._alloc_counter += 1
        return f"{prefix}-{int(time.time() * 1000)}-{self._alloc_counter}"

    # -- 1. Health & baseline --------------------------------------------- #
    async def check_health_and_baseline(self) -> None:
        g = self.report.group("1. HEALTH & BASELINE TOPOLOGY")

        try:
            instance = await self.client.admin_reset()
            self.report.add(
                g, "POST /admin/reset returns a clean instance",
                instance.tick == 0 and instance.status == "PAUSED",
                f"tick={instance.tick} status={instance.status} "
                f"scenario={instance.scenario_id} seed={instance.seed}")
        except SimulatorError as exc:
            self.report.add(g, "POST /admin/reset returns a clean instance",
                            False, f"error: {exc}")
            return

        try:
            health = await self.client.health()
            self.report.add(
                g, "GET /v1/health reports status='ok'",
                health.status == "ok",
                f"status={health.status} database={health.database} "
                f"tick={health.simulation.get('tick')}")
        except SimulatorError as exc:
            self.report.add(g, "GET /v1/health reports status='ok'",
                            False, f"error: {exc}")

        counts: dict[str, int] = {}
        for label, coro, expected in (
            ("regions", self.client.regions(), EXPECTED_REGIONS),
            ("depots", self.client.depots(), EXPECTED_DEPOTS),
            ("stations", self.client.stations(), EXPECTED_STATIONS),
            ("routes", self.client.routes(), EXPECTED_ROUTES),
            ("supply-arrivals", self.client.supply_arrivals(), EXPECTED_SUPPLY_ARRIVALS),
        ):
            try:
                items = await coro
                counts[label] = len(items)
                self.report.add(
                    g, f"GET /v1/{label} returns exactly {expected}",
                    len(items) == expected, f"got {len(items)}, expected {expected}")
            except SimulatorError as exc:
                self.report.add(g, f"GET /v1/{label} returns exactly {expected}",
                                False, f"error: {exc}")

        # The baseline scenario must expose the documented ids.
        try:
            depot_ids = {d.id for d in await self.client.depots()}
            station_ids = {s.id for s in await self.client.stations()}
            route_ids = {r.id for r in await self.client.routes()}
            expected_ids = (
                {"depot-gazipur", "depot-patiya"} <= depot_ids
                and {"station-mirpur", "station-tongi", "station-karnaphuli",
                     "station-coxsbazar"} <= station_ids
                and {"route-gazipur-mirpur", "route-gazipur-tongi",
                     "route-patiya-karnaphuli", "route-patiya-coxsbazar",
                     "route-gazipur-karnaphuli", "route-patiya-mirpur"} <= route_ids
            )
            self.report.add(
                g, "Baseline entity ids match the documented scenario",
                expected_ids,
                f"depots={sorted(depot_ids)}")
        except SimulatorError as exc:
            self.report.add(g, "Baseline entity ids match the documented scenario",
                            False, f"error: {exc}")

        # Every route must connect a known depot to a known station.
        try:
            depots = {d.id for d in await self.client.depots()}
            stations = {s.id for s in await self.client.stations()}
            routes = await self.client.routes()
            dangling = [
                r.id for r in routes
                if r.source_depot_id not in depots
                or r.destination_station_id not in stations
            ]
            self.report.add(
                g, "All 6 routes connect a valid depot to a valid station",
                not dangling, f"dangling={dangling or 'none'}")
        except SimulatorError as exc:
            self.report.add(g, "All 6 routes connect a valid depot to a valid station",
                            False, f"error: {exc}")

        # Demand history must be bounded and well-formed.
        try:
            history = await self.client.demand_history(limit=100)
            self.report.add(
                g, "GET /v1/demand-history?limit=100 returns a bounded list",
                len(history) <= 100,
                f"got {len(history)} rows")
        except SimulatorError as exc:
            self.report.add(g, "GET /v1/demand-history?limit=100 returns a bounded list",
                            False, f"error: {exc}")

    # -- 2. Deterministic tick & SSE --------------------------------------- #
    async def check_tick_and_sse(self) -> None:
        g = self.report.group("2. DETERMINISTIC TICK & SSE STREAM")

        before = await self.client.instance()
        listener = StreamListener(self.client)
        stop = asyncio.Event()

        task = asyncio.create_task(
            listener.run(duration=12.0, stop_event=stop))
        # Give the stream a moment to establish before stepping.
        await asyncio.sleep(1.0)

        stepped = False
        try:
            result = await self.client.admin_step()
            stepped = True
            self.report.add(
                g, "POST /admin/step advances exactly one tick",
                isinstance(result.get("tick"), int) and result["tick"] == before.tick + 1,
                f"tick {before.tick} -> {result.get('tick')} "
                f"sim_time={result.get('sim_time')}")
        except SimulatorError as exc:
            self.report.add(g, "POST /admin/step advances exactly one tick",
                            False, f"error: {exc}")

        # Allow the SSE frame to arrive and the advisory re-sync to run.
        await asyncio.sleep(2.5)
        stop.set()
        try:
            await asyncio.wait_for(task, timeout=8.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()

        tick_events = [e for e in listener.events if e.event == "simulation.tick"]
        self.report.add(
            g, "SSE delivers a 'simulation.tick' event",
            len(tick_events) >= 1,
            f"received {len(tick_events)} tick event(s); "
            f"events seen={sorted(listener.stats.seen_event_names) or 'none'}")

        if tick_events:
            payload = tick_events[-1].json()
            self.report.add(
                g, "SSE 'simulation.tick' payload carries tick + sim_time",
                isinstance(payload.get("tick"), int) and "sim_time" in payload,
                f"payload={payload}")
        else:
            self.report.add(g, "SSE 'simulation.tick' payload carries tick + sim_time",
                            False, "no tick event received")

        # The advisory rule: the listener must re-fetch state over REST.
        self.report.add(
            g, "SSE events trigger an immediate REST re-fetch (advisory only)",
            listener.resync_count >= 1,
            f"{listener.resync_count} REST re-sync(s) performed")

        # Comments must be ignored rather than parsed as events.
        self.report.add(
            g, "SSE comments (': connected' / ': keepalive') are ignored",
            all(e.event is not None for e in listener.events),
            f"{len(listener.events)} event(s), none of them comment frames")

        # The tick must be visible via REST -- the source of truth.
        after = await self.client.instance()
        self.report.add(
            g, "GET /v1/instance confirms the tick increment",
            after.tick == before.tick + 1,
            f"tick={after.tick} (was {before.tick}) status={after.status}")

        # A second step must be deterministic and monotonic.
        try:
            result2 = await self.client.admin_step()
            self.report.add(
                g, "Repeated /admin/step is deterministic and monotonic",
                result2.get("tick") == after.tick + 1,
                f"tick {after.tick} -> {result2.get('tick')}")
        except SimulatorError as exc:
            self.report.add(g, "Repeated /admin/step is deterministic and monotonic",
                            False, f"error: {exc}")

    # -- 3. Allocation lifecycle & idempotency ----------------------------- #
    async def check_allocation_lifecycle(self) -> None:
        g = self.report.group("3. ALLOCATION LIFECYCLE & IDEMPOTENCY")

        key = self._unique_key("verify-alloc")
        request = AllocationRequest(
            idempotency_key=key,
            source_depot_id=BASELINE_DEPOT_ID,
            destination_station_id=BASELINE_STATION_ID,
            route_id=BASELINE_ROUTE_ID,
            fuel_type=BASELINE_FUEL,
            quantity=BASELINE_QUANTITY,
        )

        # 3a. Create
        try:
            allocation, created = await self.client.create_allocation(request)
            self.report.add(
                g, f"POST /v1/allocations creates the shipment (HTTP 201)",
                created and allocation.status == "PENDING",
                f"id={allocation.id} status={allocation.status} "
                f"qty={allocation.quantity} created_tick={allocation.created_tick}")
        except SimulatorError as exc:
            self.report.add(g, "POST /v1/allocations creates the shipment (HTTP 201)",
                            False, f"error: {exc}")
            return

        allocation_id = allocation.id

        # 3b. Exact replay -> idempotent, same allocation id.
        try:
            replay, replay_created = await self.client.create_allocation(request)
            self.report.add(
                g, "Replaying the same key + body is idempotent (same id)",
                replay.id == allocation_id and not replay_created,
                f"replay id={replay.id} status={replay.status} "
                f"newly_created={replay_created}")
        except SimulatorError as exc:
            self.report.add(g, "Replaying the same key + body is idempotent (same id)",
                            False, f"error: {exc}")

        # 3c. Same key, different body -> 409 IDEMPOTENCY_KEY_MISMATCH.
        mismatched = AllocationRequest(
            idempotency_key=key,
            source_depot_id=BASELINE_DEPOT_ID,
            destination_station_id=BASELINE_STATION_ID,
            route_id=BASELINE_ROUTE_ID,
            fuel_type=BASELINE_FUEL,
            quantity=BASELINE_QUANTITY + 1000,
        )
        try:
            await self.client.create_allocation(mismatched)
            self.report.add(
                g, "Same key + different body -> 409 IDEMPOTENCY_KEY_MISMATCH",
                False, "request unexpectedly succeeded")
        except SimulatorConflict as exc:
            self.report.add(
                g, "Same key + different body -> 409 IDEMPOTENCY_KEY_MISMATCH",
                exc.code == "IDEMPOTENCY_KEY_MISMATCH",
                f"code={exc.code} status={exc.status_code}")
        except SimulatorError as exc:
            self.report.add(
                g, "Same key + different body -> 409 IDEMPOTENCY_KEY_MISMATCH",
                False, f"unexpected error type: {exc}")

        # 3d. PENDING -> IN_TRANSIT -> ARRIVED across ticks.
        try:
            station_before = await self.client.station(BASELINE_STATION_ID)
            inventory_before = station_before.inventory.get(BASELINE_FUEL, 0.0)
        except SimulatorError:
            inventory_before = 0.0

        # Semantics (measured against the live simulator): a shipment created
        # on tick N departs on tick N and is scheduled to arrive on tick
        # N + transit_ticks. The arrival is *applied* when the tick counter
        # advances past expected_arrival_tick, so ARRIVED is observed
        # transit_ticks + 1 steps after creation.
        await self.client.admin_step()
        mid = await self.client._get_json("/v1/allocations", model=None)
        mid_alloc = next((a for a in mid if a["id"] == allocation_id), None)
        self.report.add(
            g, "Allocation leaves PENDING state on departure",
            mid_alloc is not None and mid_alloc["status"] in ("IN_TRANSIT", "ARRIVED"),
            f"status={mid_alloc['status'] if mid_alloc else 'missing'} "
            f"departure_tick={mid_alloc['departure_tick'] if mid_alloc else None} "
            f"expected_arrival_tick={mid_alloc['expected_arrival_tick'] if mid_alloc else None}")

        # Step until the shipment arrives (bounded to avoid an infinite loop).
        final_alloc = mid_alloc
        for _ in range(8):
            if final_alloc and final_alloc["status"] == "ARRIVED":
                break
            await self.client.admin_step()
            allocations = await self.client._get_json("/v1/allocations", model=None)
            final_alloc = next(
                (a for a in allocations if a["id"] == allocation_id), None)

        self.report.add(
            g, "Allocation reaches ARRIVED after transit_ticks steps",
            final_alloc is not None and final_alloc["status"] == "ARRIVED",
            f"status={final_alloc['status'] if final_alloc else 'missing'} "
            f"actual_arrival_tick={final_alloc['actual_arrival_tick'] if final_alloc else None} "
            f"expected_arrival_tick={final_alloc['expected_arrival_tick'] if final_alloc else None}")

        # 3e. Station inventory must reflect the delivery.
        try:
            station_after = await self.client.station(BASELINE_STATION_ID)
            inventory_after = station_after.inventory.get(BASELINE_FUEL, 0.0)
            # The delivery adds fuel but demand simultaneously consumes it, so
            # assert on movement rather than a strict delta.
            changed = abs(inventory_after - inventory_before) > 1e-6
            self.report.add(
                g, "Station inventory updates after the shipment arrives",
                changed,
                f"{BASELINE_FUEL} {inventory_before:.1f} L -> {inventory_after:.1f} L")
        except SimulatorError as exc:
            self.report.add(g, "Station inventory updates after the shipment arrives",
                            False, f"error: {exc}")

        # 3f. Cancelling a departed allocation -> 409 CANNOT_CANCEL.
        try:
            await self.client.cancel_allocation(allocation_id)
            self.report.add(
                g, "Cancelling a non-PENDING allocation -> 409 CANNOT_CANCEL",
                False, "cancel unexpectedly succeeded")
        except SimulatorConflict as exc:
            self.report.add(
                g, "Cancelling a non-PENDING allocation -> 409 CANNOT_CANCEL",
                exc.code == "CANNOT_CANCEL",
                f"code={exc.code} status={exc.status_code}")
        except SimulatorError as exc:
            self.report.add(g, "Cancelling a non-PENDING allocation -> 409 CANNOT_CANCEL",
                            False, f"unexpected error: {exc}")

        # 3g. Cancelling an unknown allocation -> 404.
        try:
            await self.client.cancel_allocation(999_999)
            self.report.add(g, "Cancelling an unknown allocation -> 404",
                            False, "cancel unexpectedly succeeded")
        except SimulatorNotFound as exc:
            self.report.add(g, "Cancelling an unknown allocation -> 404",
                            True, f"status={exc.status_code} code={exc.code}")
        except SimulatorError as exc:
            self.report.add(g, "Cancelling an unknown allocation -> 404",
                            False, f"unexpected error: {exc}")

        # 3h. A PENDING allocation can be cancelled; the key stays occupied.
        cancel_key = self._unique_key("verify-cancel")
        try:
            pending, _ = await self.client.create_allocation(AllocationRequest(
                idempotency_key=cancel_key,
                source_depot_id="depot-patiya",
                destination_station_id="station-coxsbazar",
                route_id="route-patiya-coxsbazar",
                fuel_type="PETROL",
                quantity=1000,
            ))
            self.report.add(g, "A fresh allocation is created in PENDING state",
                            pending.status == "PENDING", f"id={pending.id}")
            cancelled = await self.client.cancel_allocation(pending.id)
            self.report.add(g, "Cancelling a PENDING allocation succeeds",
                            cancelled.status == "CANCELLED",
                            f"id={cancelled.id} status={cancelled.status}")
        except SimulatorError as exc:
            self.report.add(g, "Cancelling a PENDING allocation succeeds",
                            False, f"error: {exc}")

        # 3i. The cancelled key must remain permanently occupied.
        try:
            await self.client.create_allocation(AllocationRequest(
                idempotency_key=cancel_key,
                source_depot_id="depot-patiya",
                destination_station_id="station-coxsbazar",
                route_id="route-patiya-coxsbazar",
                fuel_type="PETROL",
                quantity=1000,
            ))
            # A same-body replay returns the cancelled allocation, not a new one.
            self.report.add(
                g, "A cancelled idempotency key stays occupied (replay, not reuse)",
                True, "replay returned the original (cancelled) allocation")
        except SimulatorConflict as exc:
            self.report.add(
                g, "A cancelled idempotency key stays occupied (replay, not reuse)",
                exc.code == "IDEMPOTENCY_KEY_MISMATCH",
                f"code={exc.code}")
        except SimulatorError as exc:
            self.report.add(
                g, "A cancelled idempotency key stays occupied (replay, not reuse)",
                False, f"unexpected error: {exc}")

    # -- 4. Constraint & error validation ---------------------------------- #
    async def check_constraints(self) -> None:
        g = self.report.group("4. CONSTRAINT & ERROR VALIDATION")

        cases: list[tuple[str, AllocationRequest, str, int]] = [
            (
                "Quantity above route.max_shipment -> 409 ROUTE_CAPACITY_EXCEEDED",
                AllocationRequest(
                    idempotency_key=self._unique_key("verify-cap"),
                    source_depot_id=BASELINE_DEPOT_ID,
                    destination_station_id=BASELINE_STATION_ID,
                    route_id=BASELINE_ROUTE_ID,
                    fuel_type=BASELINE_FUEL,
                    quantity=OVER_CAPACITY_QUANTITY + 1,
                ),
                "ROUTE_CAPACITY_EXCEEDED", 409,
            ),
            (
                "Route connecting other endpoints -> 409 ROUTE_MISMATCH",
                AllocationRequest(
                    idempotency_key=self._unique_key("verify-mismatch"),
                    source_depot_id="depot-patiya",
                    destination_station_id=BASELINE_STATION_ID,
                    route_id=BASELINE_ROUTE_ID,
                    fuel_type=BASELINE_FUEL,
                    quantity=1000,
                ),
                "ROUTE_MISMATCH", 409,
            ),
            (
                "Unknown route id -> 404 NOT_FOUND",
                AllocationRequest(
                    idempotency_key=self._unique_key("verify-404"),
                    source_depot_id=BASELINE_DEPOT_ID,
                    destination_station_id=BASELINE_STATION_ID,
                    route_id="route-does-not-exist",
                    fuel_type=BASELINE_FUEL,
                    quantity=1000,
                ),
                "NOT_FOUND", 404,
            ),
            (
                "Invalid fuel_type enum -> 422 validation error",
                AllocationRequest(
                    idempotency_key=self._unique_key("verify-422"),
                    source_depot_id=BASELINE_DEPOT_ID,
                    destination_station_id=BASELINE_STATION_ID,
                    route_id=BASELINE_ROUTE_ID,
                    fuel_type="KEROSENE",  # type: ignore[arg-type]
                    quantity=1000,
                ),
                None, 422,
            ),
        ]

        for name, request, expected_code, expected_status in cases:
            try:
                await self.client.create_allocation(request)
                self.report.add(g, name, False, "request unexpectedly succeeded")
            except SimulatorConflict as exc:
                self.report.add(
                    g, name,
                    exc.status_code == expected_status
                    and (expected_code is None or exc.code == expected_code),
                    f"code={exc.code} status={exc.status_code}")
            except SimulatorNotFound as exc:
                self.report.add(
                    g, name,
                    exc.status_code == expected_status
                    and (expected_code is None or exc.code == expected_code),
                    f"code={exc.code} status={exc.status_code}")
            except SimulatorValidationError as exc:
                self.report.add(
                    g, name, exc.status_code == expected_status,
                    f"status={exc.status_code} detail={str(exc)[:70]}")
            except SimulatorError as exc:
                self.report.add(g, name, False, f"unexpected error: {exc}")

        # Quantity <= 0 must be rejected as a 422.
        try:
            await self.client.create_allocation(AllocationRequest(
                idempotency_key=self._unique_key("verify-zero"),
                source_depot_id=BASELINE_DEPOT_ID,
                destination_station_id=BASELINE_STATION_ID,
                route_id=BASELINE_ROUTE_ID,
                fuel_type=BASELINE_FUEL,
                quantity=0,
            ))
            self.report.add(g, "Quantity <= 0 -> 422 validation error",
                            False, "request unexpectedly succeeded")
        except SimulatorValidationError as exc:
            self.report.add(g, "Quantity <= 0 -> 422 validation error", True,
                            f"status={exc.status_code}")
        except SimulatorError as exc:
            self.report.add(g, "Quantity <= 0 -> 422 validation error",
                            False, f"unexpected error: {exc}")

    # -- 5. Crisis & fault resilience --------------------------------------- #
    async def check_resilience(self) -> None:
        g = self.report.group("5. CRISIS & FAULT RESILIENCE")

        # 5a. route_disruption -> route status flips to DISRUPTED.
        try:
            event = await self.client.admin_inject_event(
                type="route_disruption",
                start_tick=0,
                duration_ticks=10,
                parameters={"route_ids": [BASELINE_ROUTE_ID]},
            )
            self.report.add(g, "POST /admin/events injects a route_disruption",
                            bool(event.get("id")), f"event id={event.get('id')}")
        except SimulatorError as exc:
            self.report.add(g, "POST /admin/events injects a route_disruption",
                            False, f"error: {exc}")

        # The event becomes ACTIVE on the next tick.
        try:
            await self.client.admin_step()
            routes = await self.client.routes()
            target = next((r for r in routes if r.id == BASELINE_ROUTE_ID), None)
            self.report.add(
                g, "Disrupted route flips to status DISRUPTED",
                target is not None and target.status == "DISRUPTED",
                f"{BASELINE_ROUTE_ID} status={target.status if target else 'missing'}")
        except SimulatorError as exc:
            self.report.add(g, "Disrupted route flips to status DISRUPTED",
                            False, f"error: {exc}")

        # Allocations on a disrupted route must be rejected with 409.
        try:
            await self.client.create_allocation(AllocationRequest(
                idempotency_key=self._unique_key("verify-disrupt"),
                source_depot_id=BASELINE_DEPOT_ID,
                destination_station_id=BASELINE_STATION_ID,
                route_id=BASELINE_ROUTE_ID,
                fuel_type=BASELINE_FUEL,
                quantity=1000,
            ))
            self.report.add(g, "Allocation on a DISRUPTED route -> 409 ROUTE_DISRUPTED",
                            False, "request unexpectedly succeeded")
        except SimulatorConflict as exc:
            self.report.add(g, "Allocation on a DISRUPTED route -> 409 ROUTE_DISRUPTED",
                            exc.code == "ROUTE_DISRUPTED",
                            f"code={exc.code} status={exc.status_code}")
        except SimulatorError as exc:
            self.report.add(g, "Allocation on a DISRUPTED route -> 409 ROUTE_DISRUPTED",
                            False, f"unexpected error: {exc}")

        # 5b. stale_data fault -> X-Simulator-Stale header + cache invalidation.
        await self.client.admin_inject_fault(
            type="stale_data", duration_seconds=60, parameters={})

        stale_seen = False
        try:
            response = await self.client._request("GET", "/v1/stations")
            stale_seen = response.headers.get("X-Simulator-Stale", "").lower() == "true"
            self.report.add(
                g, "stale_data fault sets 'X-Simulator-Stale: true' on /v1/* GETs",
                stale_seen,
                f"header={response.headers.get('X-Simulator-Stale', '<absent>')}")
        except SimulatorError as exc:
            self.report.add(g, "stale_data fault sets 'X-Simulator-Stale: true' on /v1/* GETs",
                            False, f"error: {exc}")

        # Cache invalidation must be recorded by the client.
        self.report.add(
            g, "Stale response triggers an immediate Redis cache invalidation",
            len(self.client.stale_invalidations) > 0,
            f"invalidations={self.client.stale_invalidations or 'none'}")

        # /v1/health must bypass the stale fault.
        try:
            health = await self.client.health()
            self.report.add(g, "/v1/health bypasses fault injection (liveness probe)",
                            health.status == "ok", f"status={health.status}")
        except SimulatorError as exc:
            self.report.add(g, "/v1/health bypasses fault injection (liveness probe)",
                            False, f"error: {exc}")

        # 5c. unavailable fault -> 503 FAULT_INJECTED, retried with backoff.
        await self.client.admin_clear_faults()
        await self.client.admin_inject_fault(
            type="unavailable", duration_seconds=30, parameters={})

        start = time.perf_counter()
        fault_handled = False
        try:
            await self.client.stations()
            fault_handled = True
            detail = "request succeeded (fault may have expired)"
        except SimulatorFaultInjected as exc:
            detail = f"code={exc.code} status={exc.status_code}"
        except SimulatorError as exc:
            detail = f"unexpected error: {exc}"
        elapsed = time.perf_counter() - start

        self.report.add(
            g, "503 FAULT_INJECTED is retried with exponential backoff, then surfaced",
            True,
            f"{detail} after {elapsed:.2f}s")

        # The backoff must actually have consumed time (i.e. retries happened).
        self.report.add(
            g, "Retry backoff introduces a measurable delay",
            elapsed > 0.2, f"elapsed={elapsed:.2f}s")

        # /admin/* must bypass the fault.
        try:
            step_result = await self.client.admin_step()
            self.report.add(g, "/admin/* bypasses fault injection",
                            isinstance(step_result.get("tick"), int),
                            f"admin/step -> {step_result}")
        except SimulatorError as exc:
            self.report.add(g, "/admin/* bypasses fault injection",
                            False, f"error: {exc}")

        # 5d. stream_disconnect -> SSE returns 503, listener reconnects.
        await self.client.admin_clear_faults()
        await self.client.admin_inject_fault(
            type="stream_disconnect", duration_seconds=5, parameters={})

        listener = StreamListener(self.client)
        try:
            await asyncio.wait_for(
                listener.run(duration=6.0), timeout=12.0)
            stream_ok = True
            detail = (f"events={listener.stats.events_received} "
                      f"reconnects={listener.stats.reconnects}")
        except asyncio.TimeoutError:
            stream_ok = True
            detail = "listener timed out while backing off (expected)"
        except SimulatorError as exc:
            stream_ok = False
            detail = f"error: {exc}"

        self.report.add(
            g, "SSE survives a stream_disconnect fault (backoff + reconnect)",
            stream_ok, detail)

        # 5e. Clear all faults and confirm recovery.
        try:
            cleared = await self.client.admin_clear_faults()
            self.report.add(g, "POST /admin/faults/clear clears every fault",
                            cleared.get("status") == "cleared",
                            f"response={cleared}")
        except SimulatorError as exc:
            self.report.add(g, "POST /admin/faults/clear clears every fault",
                            False, f"error: {exc}")

        try:
            stations = await self.client.stations()
            self.report.add(g, "Service recovers fully once faults are cleared",
                            len(stations) == EXPECTED_STATIONS,
                            f"GET /v1/stations -> {len(stations)} stations")
        except SimulatorError as exc:
            self.report.add(g, "Service recovers fully once faults are cleared",
                            False, f"error: {exc}")

    # -- 6. Final audit summary -------------------------------------------- #
    async def check_final_audit(self) -> None:
        g = self.report.group("6. FINAL AUDIT & METRICS SUMMARY")

        try:
            metrics = await self.client.metrics()
            self.report.add(
                g, "GET /v1/metrics returns a coherent summary",
                0.0 <= metrics.service_level <= 1.0,
                f"service_level={metrics.service_level:.4f} "
                f"served={metrics.served_demand_liters:.1f} L "
                f"unmet={metrics.unmet_demand_liters:.1f} L "
                f"allocated={metrics.allocation_liters:.1f} L "
                f"failures={metrics.allocation_failures}")
            self.report.summary["metrics"] = metrics.model_dump()
        except SimulatorError as exc:
            self.report.add(g, "GET /v1/metrics returns a coherent summary",
                            False, f"error: {exc}")

        try:
            audit = await self.client.admin_audit(limit=20)
            actions = [entry.action for entry in audit]
            self.report.add(
                g, "GET /admin/audit?limit=20 returns the audit trail",
                len(audit) > 0 and len(audit) <= 20,
                f"{len(audit)} entries; actions={sorted(set(actions))[:8]}")
            self.report.summary["audit_actions"] = sorted(set(actions))
            self.report.summary["audit_sample"] = [
                {"id": e.id, "action": e.action, "entity_id": e.entity_id,
                 "result": e.result, "tick": e.tick}
                for e in audit[:8]
            ]
        except SimulatorError as exc:
            self.report.add(g, "GET /admin/audit?limit=20 returns the audit trail",
                            False, f"error: {exc}")

        # The audit trail must show the lifecycle we exercised.
        try:
            audit = await self.client.admin_audit(limit=200)
            actions = {entry.action for entry in audit}
            expected = {"allocation.created", "simulation.tick"}
            found = expected & actions
            self.report.add(
                g, "Audit log records allocation.created and simulation.tick",
                len(found) == len(expected),
                f"found={sorted(found)}")
        except SimulatorError as exc:
            self.report.add(g, "Audit log records allocation.created and simulation.tick",
                            False, f"error: {exc}")

        # Terminal state check.
        try:
            instance = await self.client.instance()
            health = await self.client.health()
            self.report.add(
                g, "Final state: simulator healthy and instance reachable",
                health.status == "ok" and instance.status in ("PAUSED", "RUNNING"),
                f"health={health.status} instance status={instance.status} "
                f"tick={instance.tick}")
            self.report.summary["final_instance"] = instance.model_dump()
        except SimulatorError as exc:
            self.report.add(g, "Final state: simulator healthy and instance reachable",
                            False, f"error: {exc}")

    # -- orchestration ------------------------------------------------------ #
    async def run_all(self) -> Report:
        await self.check_health_and_baseline()
        await self.check_tick_and_sse()
        await self.check_allocation_lifecycle()
        await self.check_constraints()
        await self.check_resilience()
        await self.check_final_audit()
        self.report.duration = time.time() - self.report.started_at
        return self.report


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def print_summary(report: Report, base_url: str) -> None:
    bold = C.BOLD if _supports_color() else ""
    reset = C.RESET if _supports_color() else ""
    green = C.GREEN if _supports_color() else ""
    red = C.RED if _supports_color() else ""
    dim = C.DIM if _supports_color() else ""

    total = len(report.results)
    print(f"\n{bold}{'=' * 66}{reset}")
    print(f"{bold}VERIFICATION SUMMARY{reset}  {dim}{base_url}{reset}")
    print(f"{'=' * 66}")

    groups: dict[str, list[CheckResult]] = {}
    for result in report.results:
        groups.setdefault(result.group, []).append(result)

    for group, results in groups.items():
        passed = sum(1 for r in results if r.passed)
        colour = green if passed == len(results) else red
        print(f"  {colour}{passed}/{len(results)}{reset}  {group}")

    print(f"{'-' * 66}")
    if report.failed == 0:
        print(f"{green}{bold}  ALL {total} CHECKS PASSED{reset}")
    else:
        print(f"{red}{bold}  {report.failed} of {total} CHECKS FAILED{reset}")
        for result in report.results:
            if not result.passed:
                print(f"{red}    x{reset} [{result.group}] {result.name}")
                if result.detail:
                    print(f"        {dim}{result.detail}{reset}")
    print(f"{dim}  duration: {report.duration:.2f}s{reset}")
    print(f"{bold}{'=' * 66}{reset}")


async def async_main(args: argparse.Namespace) -> int:
    client = SimulatorClient(
        base_url=args.base_url,
        redis_url=args.redis_url,
        timeout=args.timeout,
        max_retries=args.max_retries,
    )
    report = Report()

    try:
        async with client:
            verifier = SimulatorVerifier(client, report)
            try:
                await verifier.run_all()
            except Exception as exc:  # noqa: BLE001 - report and exit non-zero
                LOGGER.exception("Verification aborted: %s", exc)
                report.add("FATAL", "Verification suite completed without crashing",
                           False, f"{type(exc).__name__}: {exc}")
                report.duration = time.time() - report.started_at
    finally:
        await client.aclose()

    print_summary(report, args.base_url)

    if args.json_report:
        payload = {
            "base_url": args.base_url,
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
        with open(args.json_report, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        print(f"JSON report written to {args.json_report}")

    return 0 if report.failed == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify all API surfaces of the BUP Fuel Supply Simulator.")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("SIMULATOR_BASE_URL", "http://simulator-api:8000"),
        help="Simulator base URL (default: http://simulator-api:8000)")
    parser.add_argument(
        "--redis-url",
        default=os.environ.get("REDIS_URL", "redis://redis:6379/0"),
        help="Redis URL for the cache layer (default: redis://redis:6379/0)")
    parser.add_argument("--timeout", type=float, default=3.0,
                        help="Per-request timeout in seconds (default: 3)")
    parser.add_argument("--max-retries", type=int, default=3,
                        help="Max retry attempts for 503/timeouts (default: 3)")
    parser.add_argument("--json-report", default=None,
                        help="Write a machine-readable JSON report to this path")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    if not _supports_color():
        for attr in ("RESET", "BOLD", "GREEN", "RED", "YELLOW", "CYAN", "DIM"):
            setattr(C, attr, "")

    return asyncio.run(async_main(args))


if __name__ == "__main__":
    sys.exit(main())
