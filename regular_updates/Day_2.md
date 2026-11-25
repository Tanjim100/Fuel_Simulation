# Project Status Update: Fuel Supply Intelligence & Resilience Platform
**Event:** BUP CSE FEST 2026 — Final Hackathon
**Prepared For:** Senior / Team Lead
**Status:** Local Integration Harness Built & Fully Verified — Ready for Platform Implementation

---

## 1. Executive Summary

Day 1 closed with an architecture and risk-mitigation plan. Day 2 converts that plan into **running, verified code**. The simulator integration layer is now fully built, containerized, and proven end-to-end against the live simulator with **all 49 automated checks passing**.

The critical engineering win this session was **not trusting the documentation**. We extracted the official Integration Guide PDF, then independently probed the running container to measure its actual behaviour. This surfaced three documentation defects that would each have produced a broken client if we had coded from the PDF alone.

---

## 2. Milestones Completed Till Now

### A. Local Docker Infrastructure (Live & Healthy)

Three-service stack defined in `docker-compose.yml` and verified running:

| Service | Image / Build | Role | Status |
| :--- | :--- | :--- | :--- |
| `simulator-api` | `asifmahmoud414/bup-fuel-supply-simulator:1.0.0` | The simulator (the *world*) | Healthy, `:8000` |
| `redis` | `redis:alpine` | Cache layer + state tracking | Healthy, `:6379` |
| `client-verifier` | `./verifier` (Python 3.11) | Defensive client + e2e suite | Healthy, `:8001` |

* Simulator configured with `SIMULATION_SPEED=8` (wall-clock ticks/sec), `TICK_MINUTES=15` (simulated minutes/tick), `SIMULATOR_START_MODE=paused` for deterministic testing.
* Every service has a Docker `HEALTHCHECK` and `restart: unless-stopped`.
* `depends_on: service_healthy` gating so the verifier never races the simulator's startup.

### B. Defensive Simulator Client (`verifier/simulator_client.py`)

An async `httpx` client that encodes the integration rules as **structure**, not assumptions:

* **Typed Pydantic models** for every `/v1/*` resource — `HealthResponse`, `SimulationInstance`, `Region`, `Depot`, `Station`, `Route`, `SupplyArrival`, `DomainEvent`, `Allocation`, `DemandObservation`, `Metrics`, `AuditEntry`, `Fault`.
* **Stale-data detection on every GET.** `X-Simulator-Stale: true` is checked inside the low-level request layer, so it applies to *all* read paths, not just cached ones. On detection the entire `sim:*` Redis namespace is flushed and a warning is logged.
* **Exponential backoff with full jitter** (capped 4s) under a hard 3s per-request timeout, specifically for `503 FAULT_INJECTED` and transport timeouts.
* **Typed error taxonomy** — `SimulatorFaultInjected`, `SimulatorTimeout`, `SimulatorNotFound`, `SimulatorValidationError`, and `SimulatorConflict` (carrying the exact domain code such as `ROUTE_CAPACITY_EXCEEDED`, `IDEMPOTENCY_KEY_MISMATCH`, `CANNOT_CANCEL`).
* **SSE as advisory only.** `StreamListener` maps each event type to the resources it affects (`simulation.tick` → instance/metrics/depots/stations/allocations/supply-arrivals, etc.) and re-fetches them over REST the instant an event arrives. Event payloads are never trusted as world state. `: connected` and `: keepalive` comments are explicitly ignored. Drops and `503`s trigger backoff reconnect + full REST re-sync.
* **Full `/admin/*` control surface** — reset, step, run, pause, toggle, event injection, fault injection, fault clear, and audit retrieval.

### C. Automated End-to-End Verification Suite (`verifier/verify_simulator.py`)

**49 checks across 6 sequential groups**, each printing an individual pass/fail line:

1. **Health & baseline topology** — reset integrity, `status: "ok"`, and the exact world: 2 regions, 2 depots, 4 stations, 6 routes, 22 supply arrivals.
2. **Deterministic tick & SSE** — single-tick advance, `simulation.tick` delivery, payload shape, advisory REST re-fetch, comment handling, tick monotonicity.
3. **Allocation lifecycle & idempotency** — `201 Created`, idempotent replay, `409 IDEMPOTENCY_KEY_MISMATCH`, `PENDING → IN_TRANSIT → ARRIVED`, inventory update, `409 CANNOT_CANCEL`, `404` on unknown id, successful `PENDING` cancel, permanently-occupied key.
4. **Constraint & error validation** — `409 ROUTE_CAPACITY_EXCEEDED`, `409 ROUTE_MISMATCH`, `404 NOT_FOUND`, `422` on bad enum, `422` on `quantity <= 0`.
5. **Crisis & fault resilience** — `route_disruption` → `DISRUPTED` + `409` rejection, stale-header cache invalidation, health/admin fault bypass, `503` backoff behaviour, SSE disconnect recovery, fault clearing and full service recovery.
6. **Final audit & metrics** — metrics coherence, audit trail, required audit actions, terminal health.

Also delivered: a JSON report artifact, exit code `0`/`1` for CI integration, and a FastAPI sidecar (`verify_api.py`) exposing `GET /health` and `POST /verify`.

### D. Documentation (`README.md`)

Complete runbook: exact terminal commands for start/stop/verify, flag reference, per-group coverage description, design rationale, environment-variable reference, and a troubleshooting table covering every failure mode encountered during the build.

---

## 3. Key Engineering Findings — Documentation vs. Reality

We extracted the official Integration Guide PDF and then **independently measured the running container**. This surfaced three defects where the guide is wrong or incomplete:

| # | Documented Behaviour | Measured Reality | Impact |
| :--- | :--- | :--- | :--- |
| 1 | Idempotent replay returns `200` | Returns **`201`** for both new allocation and replay | Status code alone cannot identify a replay — we detect it by tracking the highest allocation id seen |
| 2 | Arrival after `transit_ticks` steps | Arrival applies **one tick later** (tick counter advances past `expected_arrival_tick` before the arrival is committed) | `route-gazipur-mirpur` (`transit_ticks=2`) reaches `ARRIVED` on the **3rd** step, not the 2nd |
| 3 | Compose snippet shows `SIMULATION_SPEED: 15` / `TICK_MINUTES: 8` | These are **swapped**; real defaults are `8` ticks/sec and `15` minutes/tick | The shipped compose file uses the corrected values |

Additionally measured: domain errors use `{"detail":{"code":...}}` while injected faults use `{"error":{"code":"FAULT_INJECTED"}}` (a distinction the client must handle); and `dispatch_capacity_per_tick` / `max_shipment` serialize as **floats** (`12000.0`), so models accept `float | int`.

**Two real bugs were caught only by running inside the full container stack** — a host-only run would have shipped both:
* Cached values arrive from Redis as JSON **strings**, which were being handed directly to Pydantic and crashing validation.
* Caching mutable resources served **stale tick values** (`/v1/instance` returning tick 0 when the real tick was 1). Fixed by caching only genuinely static resources, fetching everything world-mutable fresh, and invalidating the namespace on every mutation.

---

## 4. Current Position vs. Day 1 Plan

| Day 1 Target | Status |
| :--- | :--- |
| Environment setup (`simulator-api`, `redis`) | ✅ Complete and verified healthy |
| Defensive client & ingestor (SSE + REST sync + stale invalidation) | ✅ Complete and verified (49/49 checks) |
| Backend service with `/health` + API surface | ✅ Verification sidecar complete (`GET /health`, `POST /verify`) |
| Optimization & fallback engine | ⬜ Not started |
| Dashboard & load testing | ⬜ Not started |

---

## 5. Targets to Fulfil

### Priority 1 — Operational Backend (Next Session)

* **Backend service** on the simulator client: expose `/api/state`, `/api/allocations`, `/api/metrics`, `/api/health` with Redis-backed reads and SSE-driven refresh.
* **Pre-flight validator** computing `Q = min(Need, route.max_shipment, depot.inventory, remaining_dispatch_capacity, station_free_space)`, with automatic shipment splitting for oversized requests.
* **Optimization engine** — Linear Programming (or priority heuristic fallback) respecting `max_shipment`, `dispatch_capacity_per_tick`, `station.capacity`, and route status.
* **Rerouting logic** — dual-connected stations (`Mirpur`, `Karnaphuli`) failover to the secondary depot on `DISRUPTED` routes; single-connected stations (`Tongi`, `Cox's Bazar`) hold a 50–60% safety buffer.

### Priority 2 — Intelligence Layer

* **Demand forecasting** using `demand-history` (bounded `limit`), hour-of-day factors, and `demand_multiplier`; stockout probability and time-to-stockout per station.
* **Anomaly detection** — demand spikes, abnormal inventory drops, depot/route bottlenecks.
* **Anomaly-to-explanation layer** — generate human-readable incident and decision summaries (LLM as a *supporting* role, never the decision authority).

### Priority 3 — Operator Dashboard

* Three-column operational UI per the Day 1 wireframe: telemetry bar, network/topology panels, decision support with human-in-the-loop approval.
* Live inventory, route status matrix, crisis alerts, projected stockout timelines, service-level KPIs, stale-data warning banner.
* Inspectable recommendations with reasoning — signals, constraints, expected risk reduction, and alternatives.

### Priority 4 — Observability, Resilience & Load Testing

* **Circuit breaker** wrapping simulator calls; degraded mode served from cached state.
* **Prometheus metrics + Grafana dashboards** — request rate, p95 latency, error rate, CPU/memory, ML confidence, fallback activations.
* **Locust/k6 load test** on the decision endpoints; document throughput and p50/p95/p99 latency.
* **DLQ** for malformed payloads; graceful worker crash recovery.

### Priority 5 — Submission Readiness

* Harness-compatible entry point with deterministic, machine-parseable JSON output.
* Scenario configuration via documented structured config (no hardcoded values).
* Demo narrative: **normal operations → crisis → injected fault → recovery**.
* Unit tests for the engine and metrics module.

---

## 6. Immediate Next Steps

1. Extend the Docker stack with the `backend` service (FastAPI + the existing defensive client) and wire it to `redis`.
2. Implement the pre-flight validator and shipment splitter against the already-verified constraint set.
3. Build the LP/heuristic allocation solver and validate it with the existing `verify_simulator.py` harness — the 49 checks already exercise every constraint code it must respect.
4. Stand up the frontend dashboard against the backend API and begin the observability stack.

**Note:** The verification harness built on Day 2 is reusable as a **regression suite** for every subsequent backend change — run it after each commit to guarantee no integration contract has been broken.
