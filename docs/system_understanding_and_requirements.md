# BUP CSE FEST 2026: Final Hackathon

## Fuel Supply Intelligence & Resilience Platform — System & Requirements Understanding

## 1. Executive Summary & Core Objective

The goal of this hackathon is to build, deploy, and operate an end-to-end **Fuel Supply Intelligence & Resilience Platform** on top of the organizer-provided **BUP Fuel Supply Simulator**.

Rather than being purely a Machine Learning competition, this challenge evaluates full-stack systems engineering across three primary pillars:

1. **Application Development (Operator Experience):** A usable web dashboard for real-time visibility and decision execution.

2. **AI & Decision Intelligence:** Forecasting demand, detecting anomalies/bottlenecks, optimizing constrained fuel allocations, and providing human-readable reasoning.

3. **DevOps, Observability & Resilience:** Containerized deployment, health monitoring, load handling, and graceful degradation during domain crises or software/API failures.

## 2. The Simulated World (Domain Model)

The simulator represents a deterministic, localized Bangladeshi fuel supply chain operating on a **15-minute tick clock** (configurable speed):

* **Fuel Types (3):** `DIESEL`, `PETROL`, `OCTANE`.

* **Regions (2):** Dhaka Division (`region-dhaka`, demand factor `1.00`) and Chattogram Division (`region-chattogram`, demand factor `1.08`).

* **Depots (2):**

  * `depot-gazipur` (Dhaka) — Dispatch capacity: `12,000 L/tick`.

  * `depot-patiya` (Chattogram) — Dispatch capacity: `11,000 L/tick`.

* **Fuel Stations (4):**

  * `station-mirpur` (`urban_high` profile) — Dual-connected (Gazipur & Patiya).

  * `station-tongi` (`industrial` profile) — Single-connected (Gazipur only).

  * `station-karnaphuli` (`highway` profile) — Dual-connected (Patiya & Gazipur).

  * `station-coxsbazar` (`regional` profile) — Single-connected (Patiya only).

* **Transport Routes (6):** Each route has a fixed `transit_ticks` (2 to 4 ticks) and `max_shipment` limit (5,000 L to 7,000 L).

## 3. Simulator Integration Rules & API Surfaces

The simulator acts as the **World**, while our platform acts as the **Brain**. We must never modify the simulator source code.

### Key Interaction Principles

* **REST is the Source of Truth:** Read world state via `GET /v1/instance`, `/v1/regions`, `/v1/depots`, `/v1/stations`, `/v1/routes`, `/v1/supply-arrivals`, `/v1/events`, `/v1/allocations`, `/v1/demand-history` (always pass `limit`), and `/v1/metrics`.

* **SSE is Advisory Only:** Listen to `GET /v1/stream` for real-time notifications (`simulation.tick`, `allocation.status_changed`, `inventory.updated`, `simulator.notice`). Always re-fetch state via REST after receiving an event or reconnecting (there is no `Last-Event-ID` replay, and queue size is capped at 200 events).

* **Single Domain Write:** Fuel replenishment decisions are executed exclusively via `POST /v1/allocations` using a unique `idempotency_key` in the request body. Only `PENDING` allocations can be cancelled via `POST /v1/allocations/{id}/cancel`.

* **Admin Endpoints (`/admin/*`):** Bypass all fault injections. Used during development/testing to step ticks (`POST /admin/step`), reset state (`POST /admin/reset`), and inject domain events (`POST /admin/events`) or API faults (`POST /admin/faults`).

## 4. Functional & Technical Requirements

### A. Operator-Facing Application (Dashboard)

* Must provide a functional UI (not just a notebook) displaying live depot/station inventories, route statuses (`AVAILABLE` vs `DISRUPTED`), incoming supply schedules, shortage alerts, projected stockout timelines, and system health.

* **Inspectable Decision Support:** Recommendations must explain *why* an action is suggested (signals, constraints, expected risk reduction, and alternatives) and support human-in-the-loop approval alongside simulation/what-if checks.

### B. Intelligence & Optimization Layer

* **Prediction:** Demand forecasting (accounting for hour-of-day factors and `demand_multiplier`) and stockout probability estimation.

* **Detection:** Identifying sudden demand spikes, abnormal inventory drops, and route/depot bottlenecks.

* **Decision Intelligence:** Constrained optimization (e.g., Linear Programming or priority heuristics) that respects `route.max_shipment`, `depot.dispatch_capacity_per_tick`, `depot.inventory`, and `station.capacity`—splitting large shipments when necessary and rerouting when primary paths are disrupted.

* **Generative AI (Supporting Role):** Used to generate human-readable incident explanations and decision summaries rather than acting as a standalone chatbot.

### C. DevOps, Observability & Load Testing

* **Reproducible Deployment:** Must run cleanly via a single command (`docker compose up`) with health checks and auto-restart policies.

* **Observability:** Expose metrics (request rate, p95 latency, error rate, CPU/memory, ML confidence, fallback activations) and structured audit logs using tools like Prometheus, Grafana, or OpenTelemetry.

* **Load Testing:** Benchmark critical backend/decision endpoints (e.g., via Locust or k6) and document throughput, p50/p95/p99 latency, and error rates under concurrency.

## 5. Post-Build Operational Problems & Engineering Solutions

Once the platform is built and deployed, it must survive both **Domain Crises** and **Injected Engineering Faults**. Below is the complete catalog of post-build problems and their architectural solutions:

### 5.1 Simulator & API Integration Faults

| Potential Problem | Root Cause / Trigger | Engineering Solution |
| :--- | :--- | :--- |
| **API Unavailable / Transient Errors** | `unavailable` or `error_rate` faults returning `503 FAULT_INJECTED` on `/v1/*` endpoints. | Implement **Exponential Backoff with Jitter** retries and a **Circuit Breaker**. When tripped, enter **Degraded Mode** using cached state from Redis/DB. |
| **High API Latency** | `latency` fault adding artificial delays (e.g., $\ge 500\text{ ms}$) to every request. | Enforce strict client-side HTTP timeouts (e.g., $2\text{ s}$) and asynchronous I/O so slow upstream calls never block the dashboard or worker threads. |
| **Stale Data Serving** | `stale_data` fault injecting `X-Simulator-Stale: true` in GET response headers. | Inspect headers on every `/v1/*` GET response; immediately **invalidate local Redis caches**, raise a UI warning banner, and pause automated high-risk dispatches until fresh data arrives. |
| **SSE Stream Disconnection / Overflow** | `stream_disconnect` fault (`503`) or subscriber falling $>200$ events behind queue limit. | Implement auto-reconnect with backoff on `/v1/stream`, fall back to **REST Polling** while disconnected, and perform a **full REST state re-fetch** upon reconnection. |
| **Malformed / Incomplete Payloads** | Corrupted or missing fields injected into incoming data streams. | Validate all external inputs strictly using **Pydantic schemas**. Route invalid payloads to a **Dead Letter Queue (DLQ)** / error log instead of crashing workers. |

### 5.2 Supply Chain & Domain Crises

| Potential Problem | Root Cause / Trigger | Algorithmic & Operational Solution |
| :--- | :--- | :--- |
| **Sudden Demand Spikes** | `demand_spike` multiplying regional or station demand (e.g., $1.8\times$). | Combine historical sliding-window averages with live `demand_multiplier` and hour-of-day factors to dynamically recalculate stockout timelines every tick. |
| **Route Disruptions** | `route_disruption` setting primary route status to `DISRUPTED` (causing in-flight failures). | Filter out non-`AVAILABLE` routes. For dual-connected stations (`Mirpur`, `Karnaphuli`), reroute via the secondary depot. For single-connected stations (`Tongi`, `Cox's Bazar`), maintain a $50\text{--}60\%$ safety inventory buffer and monitor `/v1/events` for `end_tick`. |
| **Depot Constraints & Supply Delays** | `depot_constraint`, `shipment_delay`, or `supply_shortfall` reducing available fuel. | Dynamically shift allocation weights to unconstrained depots. If total network supply is scarce, apply **Priority-Based Allocation** targeting stations with the shortest Time-to-Stockout. |
| **Station Outages** | `station_outage` setting station status to `OUTAGE`. | Suppress allocations to `OUTAGE` stations immediately to prevent `409 STATION_CLOSED` rejections and redirect fuel to active stations. |
| **Combined Crises** | Multiple simultaneous disruptions (e.g., demand spike + route disruption + shipment delay). | Use **Constrained Mathematical Optimization (Linear Programming)** to solve multi-variable capacity, inventory, and routing constraints simultaneously. |

### 5.3 Allocation & Business Logic Errors (`409` / `422`)

| Potential Problem | Root Cause / Trigger | Pre-Flight Validation Solution |
| :--- | :--- | :--- |
| **Capacity Overflows** | Exceeding `route.max_shipment`, `depot.dispatch_capacity_per_tick`, or `station.capacity`. | Enforce a client-side **Pre-Flight Validator** calculating: $Q = \min(\text{Need}, \text{MaxShipment}, \text{DepotInv}, \text{RemainingDispatchCap}, \text{StationFreeSpace})$. Automatically **split** large needs into multiple valid shipments. |
| **Duplicate or Mismatched Retries** | Worker crash mid-request leading to duplicate dispatches or `409 IDEMPOTENCY_KEY_MISMATCH`. | Persist deterministic `idempotency_key`s (e.g., `alloc-t{tick}-{station}-{fuel}-{seq}`) alongside the exact payload in the local DB before sending. Replay the exact payload on retry. |
| **Invalid Cancellations** | Attempting `POST /v1/allocations/{id}/cancel` after an allocation transitions from `PENDING` to `IN_TRANSIT`. | Check real-time allocation status via REST prior to cancellation; disable cancel actions in the UI once departure occurs. |

### 5.4 Application, AI & Infrastructure Failures

| Potential Problem | Root Cause / Trigger | System & DevOps Solution |
| :--- | :--- | :--- |
| **ML Prediction Service Crash / Drift** | Forecasting container crashes, times out, or suffers accuracy drop due to sudden regime shifts. | Wrap ML inference in a **Circuit Breaker** with a deterministic **Fallback Heuristic Policy** (e.g., replenish when inventory $< 30\%$). Use short sliding windows to adapt quickly to drift. |
| **High Traffic / Load Spikes** | Sudden burst of operator dashboard or API requests causing high latency or OOM crashes. | Serve dashboard reads from **Redis Cache** rather than hitting the simulator directly. Use async workers (FastAPI/Asyncio) and verify limits with **Locust/k6** load tests. |
| **Unbounded History Growth** | `/v1/demand-history` growing by 12 rows every tick indefinitely. | Always query with a bounded `limit` parameter (e.g., `limit=200`) and store rolling aggregates locally. |
| **Container / Worker Crashes** | Unhandled runtime exceptions terminating backend or worker processes. | Configure `restart: unless-stopped` and Docker health checks in `docker-compose.yml`, backed by persistent queues so interrupted tasks resume safely. |

## 6. Evaluation Breakdown & Success Criteria

| Criterion | Weight | Key Focus | 
| :--- | :--- | :--- | 
| **Working Product & UX** | 20% | End-to-end usability, operational workflow, completeness | 
| **Intelligence & Decision Quality** | 20% | Practical ML/optimization accuracy, explainability, constraint adherence | 
| **Architecture & Integration** | 15% | Clean backend design, proper SSE + REST synchronization, idempotency | 
| **DevOps & Engineering Quality** | 15% | Dockerization, CI/CD, input validation, code hygiene | 
| **Resilience & Incident Response** | 15% | Handling `503` faults, stale headers, model crashes, and combined crises | 
| **Observability & Performance** | 10% | Live health status, metrics dashboards, logs, and load-test evidence | 
| **Demo & Problem Understanding** | 5% | Clear narrative demonstrating normal ops $\rightarrow$ crisis $\rightarrow$ fault $\rightarrow$ recovery | 

**Core Takeaway:** Judges prioritize a reliable, observable, fault-tolerant operational system that turns practical intelligence into safe, inspectable decisions over an overly complex standalone ML model.
