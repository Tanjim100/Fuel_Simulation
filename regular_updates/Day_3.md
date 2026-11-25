# Project Status Update: Fuel Supply Intelligence & Resilience Platform
**Event:** BUP CSE FEST 2026 — Final Hackathon
**Prepared For:** Senior / Team Lead
**Status:** Operator Backend Built, Live-Verified & Regression-Gated — Ready for the Optimisation Engine

---

## 1. Executive Summary

Day 2 delivered a **verified integration harness** — 49 automated checks against the live
simulator, all passing. Day 3 converts that harness into a **product**.

The `backend` service now runs inside the same Docker stack as the simulator and the
verifier. It owns the single upstream SSE subscription, maintains a fresh world snapshot,
and — the point of the whole exercise — **decides what is legal to dispatch before any
write reaches the simulator**.

The headline deliverable is the **pre-flight validator**: a pure function that mirrors the
simulator's validation order exactly, splits oversized requests into legal shipments, and
explains every decision in operator-readable language. It is proven correct by a
**randomised 40-request differential test** against the live simulator: anything the
validator approves the simulator accepts, and anything it rejects the simulator would have
rejected with the same code.

---

## 2. Milestones Completed Till Now

### A. Four-service Docker infrastructure (live & healthy)

| Service | Image / Build | Role | Status |
| :--- | :--- | :--- | :--- |
| `simulator-api` | `asifmahmoud414/bup-fuel-supply-simulator:1.0.0` | The simulator (the *world*) | Healthy, `:8000` |
| `redis` | `redis:alpine` | Cache layer + state tracking | Healthy, `:6379` |
| `client-verifier` | `./verifier` (Python 3.11) | Defensive client + 49-check suite | Healthy, `:8001` |
| `backend` | `./backend` (Python 3.11) | **Operator backend** | Healthy, `:8002` |

The compose change was **purely additive**. The verifier's build context, Dockerfile,
dependencies and ports are untouched, so the 49/49 baseline could not break — and it did
not break (see §5).

* **No host bind-mounts anywhere.** Docker Desktop cannot mount this project's path
  because the directory name contains spaces. The dispatch journal and the verifier's
  reports live on **named volumes** instead.
* Every service has a Docker `HEALTHCHECK` and `restart: unless-stopped`.
* `depends_on: service_healthy` gating so nothing races the simulator's startup.

### B. The pre-flight validator (`backend/app/validator.py`)

A **pure function** — no I/O, no network, no clock — so it is unit-testable without a
simulator and readable by judges.

```python
validate_and_plan(snapshot, request, *, allow_split=True, key_factory=None) -> PlanResult
```

It mirrors the simulator's check order (first failure wins):

`INVALID_QUANTITY` → `NOT_FOUND` → `ROUTE_MISMATCH` → `DEPOT_CLOSED` → `STATION_CLOSED` →
`ROUTE_DISRUPTED` → `ROUTE_CAPACITY_EXCEEDED` → `INSUFFICIENT_INVENTORY` →
`DISPATCH_CAPACITY_EXCEEDED` → `DESTINATION_CAPACITY_EXCEEDED`

Oversized requests are **split greedily** against live ceilings, recomputed on every
iteration, with running totals tracked per depot and per station (dictionaries, not a
single scalar) so multi-destination planning needs no rewrite.

Every plan carries inspectable reasoning: per-shipment `reasoning`, `binding_constraints`,
`explanation.route_choice`, `alternatives_considered` with `rejected_because`, a
one-sentence `human_summary`, raw `diagnostics`, and `warnings[]` with exact
`spill_risk_litres`.

### C. The SSE broker (`backend/app/broker.py`)

The backend owns **one** upstream subscription; dashboards connect to the backend, never to
the simulator. The broker fans events out to subscribers with a bounded per-subscriber
queue, and a slow consumer loses its oldest events rather than blocking the broker.

### D. The dispatch API (`backend/app/routers/`)

`POST /api/plan` (validate + split, **no write**), `POST /api/allocations` (re-plan from a
fresh snapshot, then execute), `POST /api/allocations/{id}/cancel`,
`GET /api/allocations/{id}/explain`, plus the full state surface
(`/api/state`, `/api/depots`, `/api/stations`, `/api/routes`, `/api/metrics`,
`/api/events/stream`, …).

Idempotency keys are deterministic:
`alloc-t{tick}-{depot}-{station}-{fuel}-{seq}-{hash8}`. A **write-ahead SQLite journal**
records every dispatch intent *before* the simulator call, so a crash mid-dispatch replays
as an idempotent `201` rather than a duplicate shipment.

---

## 3. Key Engineering Findings — measured, not assumed

The critical result this session was caught **by the randomised differential test**, not by
reading the guide.

### Finding 1 — The simulator's validation order is FIXED, not by magnitude

The simulator applies its capacity limits in a **fixed order** and reports the *first* one
that fails — regardless of which limit is numerically smallest.

| Quantity | Route max 7000, station free 6000 | Simulator's answer |
| :--- | :--- | :--- |
| 6000 | boundary inclusive | **accepted** |
| 6001 | station limit (4th) | `DESTINATION_CAPACITY_EXCEEDED` |
| 6500 | station limit (4th) | `DESTINATION_CAPACITY_EXCEEDED` |
| 7001 | route limit (1st) | `ROUTE_CAPACITY_EXCEEDED` |
| **7500** | route limit (1st) | **`ROUTE_CAPACITY_EXCEEDED`** — *not* the numerically tighter station limit |
| 12000 | route limit (1st) | `ROUTE_CAPACITY_EXCEEDED` — *not* `DISPATCH_CAPACITY_EXCEEDED` |

A validator reporting the *smallest* limit would have said `DESTINATION_CAPACITY_EXCEEDED`
for the 7500 L case and been **wrong**. The validator now encodes the order explicitly:

```
ROUTE_MAX_SHIPMENT → DEPOT_INVENTORY → DISPATCH_CAPACITY → STATION_FREE_SPACE
```

This split the code into two functions answering two different questions:
`_tightest()` (how big can this chunk be? → the smallest ceiling) and `_binding_constraint()`
(which limit will the simulator name? → the first in its order).

### Finding 2 — Dispatch capacity filters on `created_tick`, and resets every tick

```
in_flight = Σ quantity WHERE source_depot_id == depot
                             AND created_tick    == current_tick
                             AND status IN ('PENDING','IN_TRANSIT')
```

It filters on `created_tick`, **not** `departure_tick`; on `created_tick == current_tick`,
**not** "all in-flight"; and it accumulates **across routes**. Measured live (gazipur, cap
12000): `5000 + 5000 + 2000` accepted, `+1` in the same tick → `409
DISPATCH_CAPACITY_EXCEEDED`; after one `admin_step` the cap resets.

### Finding 3 — Destination capacity ignores in-flight fuel, and the excess is silently spilled

The simulator checks **live** `station.inventory` only. 11001 L committed in-flight to a
station with 6000 L free was **accepted**; on arrival the delivery clamps and the remainder
is **silently discarded** — both shipments reported `ARRIVED`, no error, no audit flag.

> **Deliberate divergence.** The validator enforces *hard rules* that mirror the simulator
> exactly (guaranteeing no unpredicted `409`), and reports the spill exposure as a **soft
> `SPILL_RISK` warning** with the exact litre count. Making it a hard rule would make the
> validator **stricter than the oracle** — a bad property for a test oracle.

### Finding 4 — `CONSTRAINED` depots are still dispatchable

Only `OUTAGE` and `CLOSED` are not. Easy to get wrong; would have rejected legal shipments.

---

## 4. Three real bugs found by running the thing

Each was found only because the service was exercised against the live simulator, and each
is now covered by a regression test.

1. **The router passed a `dict` where the client expected a model.** `create_allocation`
   calls `request.to_payload()` itself; the router had already converted to a dict, so every
   dispatch died with `AttributeError` → `500`. The test fake had accepted a dict and hid
   it — the fake now rejects anything that is not an `AllocationRequest`.

2. **The dashboard SSE endpoint silently delivered nothing.** It used
   `asyncio.wait_for` on the broker's `__anext__()`, and a timeout **cancels** the pending
   await — which destroys the async generator and unregisters the subscriber. The connection
   looked healthy; the dashboard received nothing. The fix keeps the `__anext__` alive in a
   task and runs the disconnect probe concurrently. A new test asserts a connected dashboard
   actually receives a fanned-out event.

3. **A healthy-but-paused SSE stream was reported as broken.** The broker flipped `mode` to
   `sse` only when an *event* arrived, conflating "can the simulator accept a connection?"
   with "are events flowing?". A paused simulator accepts the connection but emits nothing,
   so the broker said `polling` — and the recovery from a `stream_disconnect` fault looked
   like a permanent failure. Fixed with a **reachability probe** (a short-lived SSE
   connection opened and closed immediately) tracked separately from event delivery.

---

## 5. Verification results

| Suite | Result |
| :--- | :--- |
| `client-verifier` — 49 checks across 6 groups | **49/49 PASSED** (regression gate, after all backend changes) |
| `backend` — 106 tests | **106 passed** |
| — of which the randomised differential test | 40 seeded random requests, live simulator as oracle |
| `docker compose ps` | 4 services, all **healthy** |
| Fault-injection rehearsal | inject `stream_disconnect` → `polling` in ≤ 12 s; clear → `sse` in ≤ 12 s |
| Dispatch soundness | plan → dispatch → simulator accepted; repeat → `replayed`, `total_litres: 0` |
| Dashboard SSE fan-out | `simulation.tick` frame delivered to a live subscriber |

**The measured fault-injection rehearsal:**

| Phase | `mode` | `sse_reachable` | `probe_failures` |
| :--- | :--- | :--- | :--- |
| Healthy | `sse` | `true` | 0 |
| Fault injected | `polling` | `false` | 2 |
| Fault cleared | `sse` | `true` | 0 |

---

## 6. Documentation

| Document | Contents |
| :--- | :--- |
| `docs/BACKEND.md` | Full API contract, the validator's rules and rationale, every measured simulator semantic, the broker design, configuration reference |
| `README.md` | Runbook for all four services, design notes, troubleshooting |
| `backend/app/validator.py` | The validator — heavily commented, each rule cites the measurement that established it |

---

## 7. Current Position vs. the Phase Plan

| Target | Status |
| :--- | :--- |
| Backend service in the compose stack | ✅ Complete, 4 services healthy |
| SSE broker with drop detection + polling fallback | ✅ Complete, fault-injection rehearsal passed |
| `/api/plan` with inspectable reasoning | ✅ Complete |
| Pre-flight validator mirroring the simulator | ✅ Complete, proven by differential test |
| Idempotent dispatch + write-ahead journal | ✅ Complete |
| Optimisation engine (LP / heuristic) | ⬜ Not started — **next** |
| Demand forecasting & anomaly detection | ⬜ Not started |
| Operator dashboard frontend | ⬜ Not started |
| Observability (Prometheus / Grafana) & load testing | ⬜ Not started |

---

## 8. Targets to Fulfil

### Priority 1 — Optimisation Engine (Next Session)

The validator decides what is **legal**. The optimiser must decide what is **optimal** —
and it will call `validate_and_plan` as its final feasibility gate, which is precisely why
the validator was kept a pure function.

* **LP formulation** over the network: maximise delivered service level subject to
  `route.max_shipment`, `depot.inventory`, `dispatch_capacity_per_tick`,
  `station.capacity` and route status. Fall back to a greedy heuristic when no solver is
  available.
* **Rerouting** — dual-connected stations (`Mirpur`, `Karnaphuli`) fail over to the
  secondary depot on `DISRUPTED` routes; single-connected stations (`Tongi`, `Cox's Bazar`)
  hold a safety buffer.
* **Multi-destination planning** — the validator's per-depot / per-station running totals
  were built for this; no rewrite needed.

### Priority 2 — Intelligence Layer

* **Demand forecasting** from `demand-history` (bounded `limit`), hour-of-day factors and
  `demand_multiplier`; stockout probability and time-to-stockout per station.
* **Anomaly detection** — demand spikes, abnormal inventory drops, depot/route bottlenecks.
* **Anomaly-to-explanation layer** — human-readable incident and decision summaries (LLM
  as a *supporting* role, never the decision authority).

### Priority 3 — Operator Dashboard

* Three-column operational UI: telemetry bar, network/topology panels, decision support
  with human-in-the-loop approval.
* Live inventory, route status matrix, crisis alerts, projected stockout timelines,
  service-level KPIs, stale-data warning banner.
* Inspectable recommendations — signals, constraints, expected risk reduction, alternatives.
* The backend's `/api/events/stream` fan-out already exists for this.

### Priority 4 — Observability, Resilience & Load Testing

* **Circuit breaker** wrapping simulator calls; degraded mode served from cached state.
* **Prometheus metrics + Grafana dashboards** — request rate, p95 latency, error rate,
  fallback activations, broker mode and probe failures.
* **Locust/k6 load test** on the decision endpoints; document p50/p95/p99 latency.
* **DLQ** for malformed payloads; graceful worker crash recovery.

### Priority 5 — Submission Readiness

* Harness-compatible entry point with deterministic, machine-parseable JSON output.
* Scenario configuration via documented structured config (no hardcoded values).
* Demo narrative: **normal operations → crisis → injected fault → recovery** — the
  fault-injection rehearsal in §5 is already the backbone of this.

---

## 9. Immediate Next Steps

1. Build the optimisation engine on top of `validate_and_plan`, keeping it a pure function
   so it can be unit-tested without a simulator.
2. Validate the optimiser with the **existing 49-check harness** plus a new differential
   test comparing optimiser output against greedy.
3. Extend the differential test to assert the optimiser's plan is never *worse* than the
   validator's greedy split.
4. Stand up the frontend dashboard against the backend API and begin the observability
   stack.

> **Note:** the verification harness built on Day 2 and the validator oracle built on Day 3
> are reusable as a **regression suite** for every subsequent change — run both after each
> commit to guarantee no integration contract has been broken.
