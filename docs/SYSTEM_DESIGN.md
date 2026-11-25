# BUP CSE Fest 2026 — Fuel Supply Intelligence & Resilience Platform
## System Design (as built)

Companion to `BRIEF.md` and `ARCHITECTURE.md`.

> **This document was rewritten.** Its previous version described an
> architecture that was never built — a scenario builder at `/scenario`, a
> run monitor at `/run/:runId`, ten console pages, and a simulator built
> from scratch. None of that was built, because the **simulator is
> organizer-provided** (`simulator-api`, a fixed image with a specified
> interface). The design changed; this document now describes what runs.

> **Why a rewrite and not an edit.** A judge opening a design document that
> does not match the running product reads it as a broken submission. The
> original design has value only as history, and the history is recorded in
> `regular_updates/Day_1.md` … `Day_6.md` and in git.

---

# Table of Contents

1. [What the system is](#1-what-the-system-is)
2. [The five services](#2-the-five-services)
3. [The layers](#3-the-layers)
4. [The console](#4-the-console)
5. [Core logic and algorithms](#5-core-logic-and-algorithms)
6. [The data model](#6-the-data-model)
7. [The API contract](#7-the-api-contract)
8. [Resilience](#8-resilience)
9. [Observability](#9-observability)
10. [Determinism](#10-determinism)
11. [What is not built](#11-what-is-not-built)

---

# 1. What the system is

A **decision and resilience platform on top of an organizer-provided fuel
supply simulator.**

It does not simulate the world. It **reads** the world, **forecasts** what
will happen to it, **decides** what to move, **refuses** what cannot be
moved, and **reports** on its own health while doing it.

```
   the simulator owns the world
              │
              ▼
   the platform owns the decisions
```

That division is the single most important thing to understand about this
codebase. `simulator-api` is a fixed image with a specified HTTP contract;
`verifier/` is the 49-check suite that proves we integrate with it
correctly. Everything in `backend/` and `frontend/` is the part we built.

### The operator's question

The console answers exactly one question: **what should I do, and is it
safe?**

Everything else — the forecast, the anomalies, the network topology, the
metrics — is in service of that question. A panel that does not change a
decision is not on the first screen.

---

# 2. The five services

```yaml
simulator-api:   8000   the world        (organizer-provided image)
redis:           6379   the fan-out
client-verifier: 8001   the 49 checks
backend:         8002   the decisions
frontend:        8003   the console
```

`docker compose up -d` → **exactly five**, all healthy.

| Service | What it owns | What it may never do |
| :--- | :--- | :--- |
| `simulator-api` | Depots, stations, routes, demand, the tick clock. | — |
| `redis` | The broker's fan-out; the DLQ's mirror. | Hold world state. |
| `client-verifier` | The conformance suite against the simulator. | — |
| `backend` | The snapshot cache, the forecast, the anomaly detection, the optimiser, the gate, the journal, the metrics. | Invent a world figure. |
| `frontend` | The rendering. | Display a figure the backend did not send. |

**No host bind-mounts.** The project's path contains spaces and Docker
Desktop cannot mount it, so the code is baked into the images and only
*data* lives on named volumes. This is a constraint, not a preference.

---

# 3. The layers

PDF §20's chain: `simulator → data/backend → intelligence → decision →
application → monitoring`.

### Data

`WorldStateCache` holds the last known good world, refreshed on a timer and
on demand. Reads are bounded by a **4-wide semaphore** — the backend's own
demand on the simulator's 15-connection pool is capped, and the saturation
is measured (`backend_simulator_pool_saturation_ratio`).

`journal.py` records every dispatch intent **before** the write, so a crash
mid-dispatch replays as an idempotent `201` rather than a duplicate shipment.

### Intelligence

`forecast.py` produces the demand series and the stockout risk.
`anomaly.py` detects deviations and groups them into incidents.

The forecast is **pure and offline** — it reads the scenario configuration
and the cached world, and it is validated against the live simulator's own
demand by the oracle test (60 of 60 exact matches).

### Decision

`optimizer.py` plans the shipments. `validator.py` is the **gate**: it
re-validates every shipment against a fresh snapshot before anything is
written, and a rejection carries its reason.

`dispatch.py` is the shared journal-then-write-then-record path, used by
both the optimiser's execute endpoint and the manual allocation endpoint.

### Application

The console. One column, one hero, one drill-down at a time.

### Monitoring

`metrics.py` — the Prometheus exposition and its JSON mirror. `dlq.py` — the
dead-letter queue. `circuit_breaker.py` — the breaker.

---

# 4. The console

### The layout

```
┌──────────────────────────────────────────┐
│  StatusBar    four facts                 │
├──────────────────────────────────────────┤
│  StaleBanner  only when it is true       │
├──────────────────────────────────────────┤
│  ActionCard   the recommendation         │
│               + the one button           │
├──────────────────────────────────────────┤
│  DetailTabs   Network │ Forecast │       │
│               Problems │ System          │
└──────────────────────────────────────────┘
```

**The old console showed nine panels at once, all expanded.** Everything was
visible, which meant nothing was. The operator's question was answered in a
corner of a wall of tables.

### The three load-bearing properties

1. **Every number is a backend number** or a documented pure derivation of
   one. Enforced by `frontend/src/test/no-invented-numbers.test.tsx`, whose
   allow-list is *computed from the same derivation functions the panels
   use* — so a new derivation, or a panel computing one privately, both fail.
2. **The event stream is advisory.** It triggers a re-read; it is never
   treated as world state. This mirrors the backend's own contract: REST is
   the truth, SSE is a nudge.
3. **A dispatch requires an explicit second click.** The first click arms
   the action; the second confirms it.

### The panels

| Panel | What it answers |
| :--- | :--- |
| `StatusBar` | Service level, tick, stream state, disrupted routes. |
| `ActionCard` | What to do, why, and the button that does it. |
| `NetworkPanel` | The depots, the stations, the routes — one figure each. |
| `ForecastPanel` | What will happen, and how sure the forecast is. |
| `AnomalyPanel` | What is anomalous, and what to do about it. |
| `IncidentPanel` | The grouped history. |
| `SystemPanel` | Is the platform healthy, and why does it look that way? |

---

# 5. Core logic and algorithms

## 5.1 The forecast

Pure and offline. For each station and fuel:

1. Look up the station's **demand profile** (`urban_high`, `industrial`,
   `highway`, `regional`) from the scenario configuration.
2. Look up the station's **region factor** (`region-dhaka` 1.00,
   `region-chattogram` 1.08).
3. Scale the profile's nominal daily litres by the factor and the station's
   `demand_multiplier`.
4. Apply the hour-of-day factor and the profile's `noise`, seeded from the
   scenario's seed so the series is **reproducible**.
5. Project the inventory forward over the horizon against that demand.
6. Compute `ticks_to_stockout`, `stockout_probability`,
   `expected_unmet_litres` and `coverage_ticks`.

The result is validated against the live simulator's demand. If the config
and the simulator ever disagree, the oracle test fails — a forecast that is
wrong by thousands of litres and looks perfectly plausible is the failure
mode worth guarding against.

## 5.2 The gate

The gate is what makes the write path safe. It re-validates every shipment
against a **fresh** snapshot, before anything is written:

| Check | The ceiling it enforces |
| :--- | :--- |
| Route status | A `DISRUPTED` route cannot carry a shipment. |
| Route capacity | `max_shipment` per shipment. |
| Depot inventory | The depot cannot send what it does not have. |
| Depot dispatch cap | `dispatch_capacity_per_tick`, minus what is already committed. |
| Station free space | The station's tank cannot overflow. |
| Fuel type | The route must carry that fuel. |

**A rejection is a feature.** It carries `reject_code`, `reject_message`,
`diagnostics` and a full `explanation`. The operator sees *why* it cannot be
done, not just that it cannot.

## 5.3 The optimiser

A greedy solver: rank the stockout risks by severity, satisfy them in order,
respecting every ceiling the gate would enforce, and defer what cannot
move now to a later tick. The plan is split into `shipments_now` (writable
this tick) and `shipments_later` (advisory).

The console renders them separately, because **"this will be dispatched" and
"this is advice" are different claims**.

## 5.4 The broker

Two modes, chosen automatically:

- **SSE** — the simulator's event stream. Fast.
- **Polling** — the fallback.

The watchdog detects the world advancing without a matching event (a
*silent* SSE drop) and switches to polling. The console stays correct, just
slower, and the suspicion is counted and shown.

---

# 6. The data model

The entities are the simulator's. The backend does not define them — it
reads them.

| Entity | Key fields the platform uses |
| :--- | :--- |
| **Depot** | `id`, `region_id`, `status`, `dispatch_capacity_per_tick`, `capacity`, `inventory`, `dispatch_remaining` |
| **Station** | `id`, `region_id`, `status`, `demand_profile`, `demand_multiplier`, `capacity`, `inventory`, `free_space`, `spill_exposure` |
| **Route** | `id`, `source_depot_id`, `destination_station_id`, `transit_ticks`, `max_shipment`, `status` |
| **Allocation** | `id`, `route_id`, `fuel_type`, `quantity`, `created_tick`, `expected_arrival_tick` |

The platform's own records:

| Record | Where |
| :--- | :--- |
| **Plan** | The optimiser's output, with the explanation and the gate's verdict. |
| **Shipment** | `sequence`, `dispatch_tick`, `arrival_tick`, route, fuel, quantity, `priority_score`, `rationale`, `binding`, `risk`. |
| **Anomaly** | `kind`, `severity`, `entity_type`, `entity_id`, `tick`, `title`, `detail`, `metrics`, `baseline`, `recommended_action`. |
| **Incident** | A group of anomalies over a tick range. |
| **Explanation** | `schema_version`, `kind`, `title`, `headline`, `details`, `numbers`, `verdict`, `alternatives`, `risks`, `next_actions`, `evidence`. |
| **Journal entry** | The intent, journalled before the write. |
| **DLQ entry** | `id`, `reason`, `source`, `payload`, `received_at`, `attempts`. |

The `Explanation.numbers` map is the audit trail: every figure quoted in the
prose also appears there, so a reader can check the sentence against the
numbers. The console renders that map verbatim.

---

# 7. The API contract

The full endpoint list is in `docs/BACKEND.md`. The ones that matter most:

| Endpoint | Purpose |
| :--- | :--- |
| `GET /api/health` | The §16 health table, plus p95 latency and the error rate. Always 200. |
| `GET /api/state` | The world: depots, stations, routes, allocations. |
| `GET /api/metrics` | The simulator's business KPIs, as JSON. **The console's contract.** |
| `GET /api/forecast/summary` | The forecast, condensed for the console. |
| `GET /api/optimize` | The plan and its explanation. |
| `POST /api/optimize` | Recompute the plan. Read-only. |
| `POST /api/optimize/execute` | Dispatch. Writes. |
| `POST /api/plan` | Validate and split a request. **Never writes.** |
| `POST /api/allocations` | Validate, split and dispatch. |
| `GET /api/anomalies`, `/api/incidents` | The problems. |
| `GET /api/dlq`, `POST /api/dlq/{id}/retry` | The dead-letter queue. |
| `GET /metrics` | The Prometheus exposition. **Bare path.** |
| `GET /api/observability` | The JSON mirror of the exposition. |

**Two endpoints that must not drift:**

- `/metrics` returns Prometheus text. `/api/metrics` returns JSON. They are
  different audiences and they stay that way.
- `POST /api/plan` never writes. `POST /api/optimize/execute` does. The
  separation is what makes the console's "simulate, then dispatch" flow
  safe.

---

# 8. Resilience

Full detail in `docs/RESILIENCE.md`. In summary:

- **A circuit breaker wraps the simulator client.** It converts a
  13.75-second retry stall into a **1-millisecond** rejection.
- **Only retryable failures trip it.** A 409 from the simulator is a
  *business answer*, not an outage — tripping on it would take the backend
  down for one full tank.
- **Reads degrade; writes refuse.** A stale read with `degraded: true` beats
  a 502. A write computed against a stale world would be a *wrong* plan, so
  it is refused with `503 CIRCUIT_OPEN`.
- **The journal makes writes idempotent.** Intent first, then the write.
- **The DLQ quarantines malformed payloads** instead of raising into the
  event loop. One bad frame must not kill the world's event feed.

---

# 9. Observability

Full detail in `docs/OBSERVABILITY.md`. In summary:

- **`/metrics`** — a hand-rolled Prometheus exposition, ~400 lines, zero new
  dependencies.
- **`/api/observability`** — the JSON mirror, which the console's System tab
  reads. The console never parses a text format: doing so would put a
  number-parsing bug inside its central promise.
- **The §16 table** — the five components, p95 latency and the error rate.
- **The pool-saturation gauge** — the early-warning signal for the
  connection-pool wedge that once took this platform down unnoticed.
- **Nine alert rules** and a Grafana dashboard, behind an optional compose
  overlay.

---

# 10. Determinism

The simulator is seeded, and the scenario configuration carries the same
seed. The forecast derives its noise from it, so the demand series is
reproducible for a given scenario and tick.

The **oracle test** is the guard: `backend/tests/test_forecast.py` asserts
**60 of 60 exact matches** against the live simulator's demand, checked
against `tests/fixtures/demand_golden.json`.

That single test protects three separate properties at once:

1. The forecast is correct.
2. The scenario configuration has not silently diverged from the simulator.
3. Moving the demand table out of Python literals changed **no** numbers.

---

# 11. What is not built

Stated plainly, so nobody has to discover it by reading the code.

| Not built | Why |
| :--- | :--- |
| **A simulator** | It is organizer-provided, with a fixed interface. Building a second one was never in scope. |
| **Sustainability reporting** — waste, expired fuel, transport emissions, efficiency % | There is **no emissions model** anywhere in the stack. The platform surfaces the simulator's own figures (`service_level`, `unmet_demand_litres`, `allocation_failures`) and the forecast's projected spilled litres. An emissions model built on top of the simulator would be *our* model of *their* transport fleet with no ground truth behind it — a number that looked authoritative and was invented. See `docs/BRIEF.md` §4b. |
| **Long-term history / time-series persistence** | The console shows the current tick and the forecast horizon. The time series live in Prometheus, which is the right place for them. |
| **Multiple concurrent scenarios** | One scenario, one world, one decision at a time. |
| **A user-facing scenario editor** | The scenario is a config file (`backend/scenarios/baseline.yaml`), validated at boot. PDF §21 asks for the configuration, not an editor. |

---

# See also

- `docs/ARCHITECTURE.md` — the as-built diagram and the data flow.
- `docs/BACKEND.md` — every endpoint, in detail.
- `docs/DASHBOARD.md` — the console.
- `docs/OBSERVABILITY.md` — the metrics.
- `docs/RESILIENCE.md` — the breaker.
- `docs/SCENARIO.md` — the scenario configuration.
- `docs/TESTING.md` — the test strategy.
