# The Platform — A Complete Guide

**BUP CSE Fest 2026 · Fuel Supply Intelligence & Resilience Platform**

This document explains the whole system in one place: **what it is**, **what
stack it is built on**, **how it is implemented**, **how each problem is
solved**, and **how to run and use it**.

It is written for a judge, a teammate, or a person six months from now who has
to pick the project up cold. Everything here is verifiable — every claim has a
command or a file reference behind it.

---

## Table of contents

1. [What this platform is](#1-what-this-platform-is)
2. [The problem, and why the naive answers fail](#2-the-problem-and-why-the-naive-answers-fail)
3. [The stack](#3-the-stack)
4. [How the system is implemented](#4-how-the-system-is-implemented)
5. [How each problem is solved](#5-how-each-problem-is-solved)
6. [How to use it](#6-how-to-use-it)
7. [How it is verified](#7-how-it-is-verified)
8. [Configuration](#8-configuration)
9. [The API surface](#9-the-api-surface)
10. [Where to read next](#10-where-to-read-next)

---

## 1. What this platform is

A fuel-distribution operator has a network: **2 depots**, **4 stations**,
**6 routes**, and three fuel grades (**DIESEL**, **PETROL**, **OCTANE**).
Demand shifts. Tanks run low. Routes break. The operator has to answer one
question continuously:

> *What should I ship, from where, to where, how much, and can I afford to do
> it?*

This platform answers that question, and — just as importantly — **tells the
operator when it cannot answer it**, and says why.

### The five services

| Service | Port | What it owns |
| :--- | :---: | :--- |
| `simulator-api` | 8000 | The world. Organizer-provided, read-mostly. Never modified. |
| `redis` | 6379 | Cache, pub/sub, the SSE fan-out. |
| `client-verifier` | 8001 | The 49-check conformance suite + a health sidecar. |
| `backend` | 8002 | The intelligence layer: snapshot, forecast, anomalies, optimiser, gate, journal, metrics. |
| `frontend` | 8003 | The operator console. |

Plus an **opt-in** overlay (`observability/compose.monitoring.yml`) adding
Prometheus (9090) and Grafana (3000). It is *not* part of the default stack,
on purpose — see [§3.4](#34-why-monitoring-is-opt-in).

### The one-line summary

> The backend turns a **live, unreliable world** into a **defensible, auditable
> recommendation**, and refuses to pretend when the world is unavailable.

---

## 2. The problem, and why the naive answers fail

A fuel platform looks easy from the outside. It is not. Six traps live here,
and each one has a specific engineering answer in this codebase.

### Trap 1 — The world is stochastic, so the forecast is an estimate

Demand is driven by a seeded pseudo-random generator inside the simulator. A
plausible dashboard shows a demand curve and calls it a prediction. That is
wrong in a way that matters: an operator cannot act on "roughly 8,000 litres".

**The answer:** the backend **re-implements the simulator's exact demand
formula**, including its SHA-256-seeded RNG, and `GET /api/forecast/validate`
compares the forecast against the simulator's live `demand-history` history.
The comparison is exact to `1e-3` litres. The forecast is not a prediction of
the future — it is a **reproduction** of it. `backend/app/forecast.py`.

### Trap 2 — The simulator rejects writes, and its rejection reasons are ordered

Ship too much on a route → the simulator refuses. Ship more than a depot has →
refused. Ship to a station with no free space → refused. But **which** reason
does it report when a request violates several at once?

**The answer:** the simulator's validation order is **fixed**, not
magnitude-ordered:

```
ROUTE_MAX_SHIPMENT → DEPOT_INVENTORY → DISPATCH_CAPACITY → STATION_FREE_SPACE
```

The binding constraint is the **first limit in that order the request
exceeds**, never the numerically smallest. The backend's pre-flight validator
mirrors this exactly, in `backend/app/validator.py:78-89`. It is proven by a
**differential oracle test** (`backend/tests/test_validator_against_simulator.py`,
643 lines) that fires requests at the live simulator and asserts the backend
predicts the same code every time. A separate `_tightest()` function exists
for *planning* — where "tightest" genuinely is the smallest — so the two
concepts never contaminate each other.

### Trap 3 — Arrival is one tick later than the docs say

A shipment created on tick N departs on tick N and is *scheduled* to arrive on
tick N + `transit_ticks`. The arrival is **applied** when the counter advances
past the expected tick, so `ARRIVED` is observed **`transit_ticks + 1` steps**
after creation.

**The answer:** every part of the system that reasons about arrival uses the
same `+1` semantics — forecast, state, optimiser, LP solver, and the verifier.
Documented at `verifier/verify_simulator.py:381-385`. Getting this wrong by
one tick would silently corrupt every stock-out risk calculation.

### Trap 4 — A replay looks exactly like a first write

The simulator returns **HTTP 201 for both**. Status code cannot distinguish
"created" from "replayed". A dispatch flow that retries on timeout would
ship twice.

**The answer:** idempotency is detected by **tracking the highest allocation
id already seen**, not by status. `backend/app/simulator_client.py:740`. The
verifier's check #2 ("Replaying the same key + body is idempotent") and check
#11 ("a cancelled idempotency key stays occupied") pin both halves.

### Trap 5 — An outage must not become a total outage

The obvious failure mode: the upstream goes down, every request retries three
times with backoff, and a single slow upstream turns into a 13.75-second stall
on *every* request, which exhausts the server's workers, which looks exactly
like "the backend is down".

**The answer:** a **circuit breaker** wrapping the upstream client. When it
trips, an open breaker rejects in **under 1 ms** instead of burning a
13.75-second retry budget. Reads then degrade to the last-known-good cached
world with an honest `degraded: true` flag; writes refuse with
`503 CIRCUIT_OPEN`. See [§5.6](#56-resilience-the-circuit-breaker) and
`docs/RESILIENCE.md`.

### Trap 6 — A metric that counts reads counts the reader, not the world

The anomaly detectors recompute against the live world on **every** request. If
the counter increments where the detector runs, then the "alert rate" on your
dashboard is a function of **how often the console refreshed**. Three polls
moved `backend_shortage_alerts_total{severity="HIGH"}` from 7 to 10 — the
world did not change, the viewers did.

**The answer:** count-once trackers. Each finding is counted **once, when it is
first detected**, and the tracker resets when the world advances to a new tick.
Five polls of an unchanged world move nothing. Pinned by
`backend/tests/test_metrics.py`.

### The seventh trap — the one that actually took the platform down

This project once wedged: the simulator held exactly **15 database handles**,
every request blocked for **30 seconds**, and it was found by reading logs
hours later. The failure mode had **no signal at all**.

**The answer:** the backend owns the semaphore that bounds its own demand on
the simulator's pool, so it can measure its own saturation exactly:

```
backend_simulator_pool_saturation_ratio = inflight_reads / fetch_concurrency_limit
```

Above **0.8**, the simulator's 15-wide pool is one notch from being the
bottleneck. A regression test (`backend/tests/test_pool_saturation.py`) asserts
the bound is never exceeded and the gauge never exceeds 1.0 — turning the
incident into an **executable invariant**. See
[§5.8](#58-the-pool-saturation-signal).

---

## 2.5 The requirements, traced to what was built

The first thing this project did — before writing a line of code — was read the
brief and write down the requirements as a **separate analysis**
(`docs/system_understanding_and_requirements.md`). That document is the
contract: the domain model, the simulator's integration rules, the functional
and technical requirements, the full catalogue of post-build operational
problems, and the evaluation breakdown.

It is kept in the repo because the implementation was built *against* it, and
a judge can check any claim by walking the same path. Every requirement has
an answer somewhere in this codebase:

| Requirement (from the analysis) | Where it is answered |
| :--- | :--- |
| **Dashboard** — live depot/station inventories, route status, supply schedule, shortage alerts, stockout timeline, system health | The console's four tabs — Network, Forecast, Problems, System — [§4.6](#46-the-console-one-column-three-tiers) |
| **Inspectable decisions** — every recommendation explains its why, with human-in-the-loop approval | The ActionCard + Details disclosure, two-click dispatch — [§4.3](#43-the-decision-path-the-gate) |
| **Prediction** — demand forecast with hour-of-day and `demand_multiplier`; stockout probability | The backend re-implements the simulator's demand formula *exactly* — [§2 Trap 1](#trap-1-the-world-is-stochastic-so-the-forecast-is-an-estimate) |
| **Detection** — demand spikes, inventory drops, route/depot bottlenecks | Seven anomaly kinds, each with a measured threshold, counted once per finding — [§5.3](#53-anomaly-detection-seven-kinds-counted-once) |
| **Decision intelligence** — constrained optimisation honouring route, depot, inventory and station limits; split large shipments; reroute when disrupted | A greedy solver that is always feasible, with an optional LP path that is adopted only when it scores better — [§5.4](#54-optimisation-greedy-by-default-lp-when-it-helps) |
| **Reproducible deployment** — one command, health checks, restart policies | `docker compose up -d` → 5 healthy services — [§6.1](#61-quick-start) |
| **Observability** — request rate, p95, error rate, CPU/memory, fallback activations, structured logs | 22 required metric families across the four PDF layers — [§5.12](#512-observability-three-layers-by-metrics-one-by-logs) |
| **Load testing** — throughput and p50/p95/p99 under concurrency | Three measured runs, tied to pool saturation — [§6.9](#69-load-testing) |
| **API faults** — `unavailable`, `latency`, `error_rate`, `stale_data`, `stream_disconnect` | Backoff + circuit breaker + degraded mode — [§5.6](#56-resilience-the-circuit-breaker) |
| **Domain crises** — demand spike, route disruption, depot constraint, shipment delay, supply shortfall, station outage | Anomaly detection, rerouting, priority allocation — [§6.11](#611-testing-by-injection-the-simulators-fault-switches) |
| **Allocation errors** — `409`/`422` on capacity overflow, duplicate retries, invalid cancellation | A pre-flight validator that mirrors the simulator's *ordered* rejection reasons, and idempotency keys persisted before the write — [§2 Trap 2](#trap-2-the-simulator-rejects-writes-and-its-rejection-reasons-are-ordered) |
| **Malformed payloads** — validate strictly; quarantine rather than crash | Pydantic schemas + a bounded dead-letter queue with replay — [§9](#9-the-api-surface) |
| **Bounded history** — never query `/v1/demand-history` without a limit | `limit` is mandatory in every call, and `STATE_MAX_AGE` bounds the snapshot |

### The catalogue is the point

The analysis does not stop at "build a dashboard". §5 of that document
enumerates **every** way the platform can fail once it is running — simulator
outages, latency, stale headers, SSE disconnects, malformed payloads, demand
spikes, route disruptions, depot constraints, station outages, capacity
overflows, duplicate dispatches, model crashes, traffic spikes, unbounded
history, container crashes — and proposes a solution for each.

That catalogue is the reason this platform has a circuit breaker, a dead-letter
queue, a validator with a differential oracle, a count-once metric tracker, and
a load-test report. **Each mechanism answers a named row.** The Resilience
section below is that catalogue, implemented.

---

## 3. The stack

### 3.1 Services and runtime

| Layer | Technology | Why this and not the obvious alternative |
| :--- | :--- | :--- |
| API | **Python 3.11 / FastAPI** | The simulator, the verifier and the backend are all Python — one language across the whole boundary, no polyglot glue. |
| ASGI | **Uvicorn** | Standard, no extra moving parts. |
| HTTP client | **httpx** | Async-native, which the broker and breaker need. |
| Validation | **Pydantic v2** | FastAPI's native model layer. |
| Cache / pub-sub | **Redis 7** | The SSE fan-out and the invalidation path both want it; `redis-py` is already a dependency. |
| Durable state | **SQLite** (stdlib `sqlite3`) | The dispatch journal and the DLQ. Both are small, local, and must survive a Redis flush. No database service added. |
| Frontend | **React 18 + TypeScript 5.7 + Vite 6** | Typed, fast to build, and the runtime image ships only `dist/` — no toolchain, no source. |
| Frontend tests | **Vitest + Testing Library** | Same runner as the build toolchain; no second test stack. |
| Monitoring | **Prometheus + Grafana** (opt-in) | The PDF names them; the stack does not *depend* on them. |

### 3.2 Dependencies — the complete list

**Backend** (`backend/requirements.txt`, 7 packages):

```
fastapi>=0.115,<1.0
uvicorn[standard]>=0.30,<1.0
httpx>=0.27,<0.29
pydantic>=2.7,<3.0
redis>=5.0,<6.0
pytest>=8.0,<9.0
pytest-asyncio>=0.23,<1.0
```

**Frontend** (`frontend/package.json`): 2 runtime dependencies (`react`,
`react-dom`) and 10 dev dependencies (TypeScript, Vite, Vitest, Testing
Library, jsdom).

### 3.3 What is deliberately *not* a dependency

This is the part that distinguishes a considered stack from a collected one.

| Not used | What was used instead | Why |
| :--- | :--- | :--- |
| `prometheus_client` | `backend/app/metrics.py` — a hand-rolled text writer, 785 lines | The PDF says the stack is the team's choice. A hand-rolled writer has **zero new dependencies**, keeps the backend image slim and offline-buildable, and is small enough to *read* — and reading it is how the team knows the exposition is correct. |
| `PyYAML` | `backend/app/scenario.py` — a 447-line YAML-subset parser | One config file, one nesting level, one scalar type. A full YAML parser is a supply-chain surface for a job that is 30 lines of dict access. |
| `pulp` | `backend/app/lp_solver.py` — a stdlib linear program solver (613 lines) | The LP path is **import-guarded and optional**; the greedy solver is the default and is what runs in the shipped image. PuLP would be a native build dependency for an optional path. |
| `pandas` / `numpy` | stdlib `array` and `math` | The LP solver needs none of it. |
| A state-management library (Redux/Zustand) | `frontend/src/lib/useBackend.ts` — one hook, one fetch loop | The console has one source of truth and four tabs. A store would be ceremony. |

### 3.4 Why monitoring is opt-in

Adding Prometheus and Grafana as *default* services would grow the stack from
5 to 7 and make `docker compose up -d` heavier and more failure-prone — which
directly contradicts the PDF's "reproducibly runnable" (§13) and "do not
introduce infrastructure complexity unless it improves your solution" (§7).

So monitoring lives in a **separate compose overlay**:

```bash
docker compose -f docker-compose.yml -f observability/compose.monitoring.yml up -d
```

The default stack is still exactly five healthy services. The *evidence* —
a working `/metrics` endpoint, a real 19-panel dashboard, nine alert rules —
is the default artefact, not the running infrastructure.

---

## 4. How the system is implemented

### 4.1 The architecture

```
   ┌──────────────────────┐
   │    simulator-api     │  :8000   the world (organizer-provided)
   │  tick · demand ·     │
   │  inventory · routes  │
   └──────────┬───────────┘
              │  HTTP (httpx, retried, breaker-wrapped)
   ┌──────────▼───────────┐        ┌──────────────────────────────┐
   │       backend        │ :8002  │  client-verifier    :8001    │
   │                      │        │  49 conformance checks        │
   │  ┌──────────────────┐ │        └──────────────────────────────┘
   │  │ snapshot cache   │ │
   │  │ SSE broker       │◄┼──────────── redis :6379
   │  │ forecast         │ │           (cache + pub/sub)
   │  │ anomaly detectors│ │
   │  │ optimiser + gate │ │
   │  │ journal + DLQ    │ │
   │  │ circuit breaker  │ │
   │  │ metrics          │ │
   │  └──────────────────┘ │
   └──────────┬───────────┘
              │  /api/*  (JSON)          ┌───────────────────────────┐
   ┌──────────▼───────────┐              │  frontend  :8003          │
   │   operator console   │              │  React + TypeScript       │
   │  status · action ·   │              │  one column, 4 tabs       │
   │  network · forecast  │              └───────────────────────────┘
   │  problems · system   │
   └──────────────────────┘
                    │
                    │ GET /metrics  (Prometheus text)
                    ▼
         ┌──────────────────────────────┐
         │ Prometheus :9090 → Grafana   │  (opt-in overlay)
         └──────────────────────────────┘
```

### 4.2 The read path: snapshot first, simulator second

The console polls every 5 seconds. The simulator is expensive and sometimes
slow. So the backend keeps a **cached world snapshot** and serves reads from
it.

```
console poll
    │
    ▼
GET /api/state ──► snapshot cache ──► 200 with the cached world
                         │
                         │ (if stale, the refresher is already running)
                         ▼
                  GET /api/stations, /api/depots, /api/routes
```

The refresher (`backend/app/broker.py`) runs on its own interval and is bounded
by a semaphore of `FETCH_CONCURRENCY = 4`. **This is why the console's read
paths never touch the simulator at all** — visible in the load test, where
`GET /api/stations` records a pool-saturation peak of **0.25** while
`POST /api/optimize` records **1.00**.

### 4.3 The decision path: the gate

A write path is where the danger lives, so it is deliberately narrow.

```
POST /api/plan ──► pre-flight validator ──► optimiser ──► explanation
                        │                      │            │
                        │ reject?             │            └─► why, in prose,
                        ▼                      │                with every number
                   409 / 404 / 422             ▼                declared
                                         POST /api/execute
                                                │
                                    ┌───────────┴───────────┐
                                    ▼                       ▼
                              journal (write-ahead)    simulator /v1/allocations
                                    │                       │
                                    └───────────┬───────────┘
                                                ▼
                                          two-click dispatch
```

**The journal is written before the simulator is called.** If the process dies
mid-dispatch, the intent survives in SQLite and the console shows the last
dispatch's state (`created` / `replayed` / `failed` / `dispatched`).

**Dispatch is two-click.** The console's button is disabled when there is
nothing to ship, and a click only *dispatches the plan* — a separate
`POST /api/execute` re-plans against a fresh snapshot and then executes. The
operator sees the plan before the world changes.

### 4.4 The gate's rejected-shipping reason

The optimiser produces a set of shipments. Some may be executable now; others
must wait for the next tick. `backend/app/routers/optimizer.py` splits them:

- `shipments_now` — executable this tick.
- `shipments_later` — deferred.
- `gated.rejected` — refused, each with a **reason** from the validator
  (`ROUTE_CAPACITY_EXCEEDED`, `DEPOT_INVENTORY_EXHAUSTED`, …).

The console renders `accepted ok / rejected no` — the operator sees what was
dropped and why, not just a count.

### 4.5 The explanation layer

Every decision comes with prose. That prose is **deterministic** and
**auditable**:

- `backend/app/explain.py` builds an `Explanation` carrying a `verdict`, a
  `headline`, `details`, `risks`, `next_actions`, and a `numbers` map that
  **declares every figure quoted in the prose**.
- `unaccounted_numbers()` (`explain.py:747`) scans the prose for any number
  that was **not** declared — and flags it.
- The console renders the `numbers` map **verbatim** via `NumberChips`, so a
  reader can check each sentence against its figures.

An **LLM prose layer** exists (`backend/app/llm.py`) but is **off by default**
and **AST-isolated** — it runs in a separate process with a timeout, and its
output passes `is_safe_prose`, so it cannot introduce a number the backend did
not send. `backend/tests/test_llm_isolation.py` (316 lines) proves the
isolation.

### 4.6 The console: one column, three tiers

The old console was three columns of nine expanded panels. The redesign is
deliberately narrow (`frontend/src/App.tsx:1-21`):

1. **Status** — four facts. Is anything wrong right now?
2. **The action** — what to do, why, and the one button that does it.
3. **The drill-down** — four tabs (Network, Forecast, Problems, System), one at
   a time.

Plus a **StaleBanner** that fires when the snapshot is stale, the SSE is
unreachable, or a resync is required — *"a console that shows numbers without
saying how old they are is a console that can lie."*

### 4.7 The console invariant: no invented numbers

**Every number the console renders is a backend number or a documented pure
derivation.** This is not a convention — it is an **executable test**
(`frontend/src/test/no-invented-numbers.test.tsx`, 294 lines):

1. `scrapeNumbers` scrapes number-like tokens **per leaf element** (not from
   concatenated `textContent` — concatenation glues `'4,392'` and `'0 drops'`
   into `'4,3920 drops'`, a number that was never displayed anywhere).
2. `backendNumbers()` walks every fixture and adds each value plus its
   roundings.
3. `derivedNumbers()` calls the **same shared derivation functions the panels
   use** — so a panel that derives a value the test doesn't know about
   *fails*, and a panel that computes a value *differently* from the shared
   function *also fails*.
4. The scraped set must be a **subset**.

The architecture behind it: `lib/derive.ts` holds **all** derivation (307
lines, 21 functions); `lib/format.ts` holds **all** formatting and computes no
business figure — *"a display function that derives a number is a display
function that can disagree with the backend."*

### 4.8 The observability surface

Three endpoints, three jobs, that **must not drift**:

| Endpoint | Format | Audience |
| :--- | :--- | :--- |
| `GET /metrics` | `text/plain; version=0.0.4` | A Prometheus scraper. **Bare path, no `/api` prefix.** |
| `GET /api/observability` | `application/json` | The console's System tab, the load-test harness. |
| `GET /api/metrics` | `application/json` | The simulator's **business KPIs**. The console's original contract. |

The console **never parses a Prometheus text exposition** — doing so would put
a number-parsing bug inside the guarantee that every figure on screen came
from the backend.

---

## 5. How each problem is solved

### 5.1 Forecasting — exact, not approximate

**Problem:** demand is stochastic; an operator cannot act on "roughly 8,000
litres".

**Solution:** re-implement the simulator's demand formula exactly, including
its SHA-256-seeded RNG, over a 16-tick horizon, across three fuel types.
`backend/app/forecast.py` (842 lines).

- **Verification:** `GET /api/forecast/validate` pulls the simulator's live
  `demand-history` and compares. Exactness is enforced to `1e-3` litres.
  A live run right now: **84 compared, 84 matched, 0 mismatched,
  largest error 0.0 L, verdict `EXACT`.** `backend/tests/test_forecast.py`
  (625 lines) checks the same thing offline against a 60-row golden fixture
  captured from the simulator.
- **Fallback:** if the oracle cannot run, the forecast is marked
  `UNAVAILABLE` and the `backend_forecast_fallback` counter increments — it
  never silently guesses.

### 5.2 The pre-flight validator — mirror the rules, not the guesses

**Problem:** the simulator rejects writes, and its rejection reasons are
**fixed-ordered**.

**Solution:** `backend/app/validator.py` (967 lines) mirrors the order:

```
ROUTE_MAX_SHIPMENT → DEPOT_INVENTORY → DISPATCH_CAPACITY → STATION_FREE_SPACE
```

and distinguishes **terminal** rejections (404 / 409 / 422 — the simulator
*answering*) from **retryable** failures (timeout / 503 / transport — the
simulator *failing*). Only the second group may trip the circuit breaker;
counting the first would take the whole backend down because one station's
tank was full.

**Verification:** a **differential oracle** — `test_validator_against_simulator.py`
(643 lines) fires real requests at the live simulator and asserts the backend
predicts the same code every time.

### 5.3 Anomaly detection — seven kinds, counted once

**Problem:** the operator needs to know what changed and how much it matters,
without being flooded.

**Solution:** `backend/app/anomaly.py` (968 lines) emits seven anomaly kinds
over a rolling 32-tick observation log (bounded at 4,096 entries):

| Kind | Trigger |
| :--- | :--- |
| `DEMAND_SPIKE` | Demand z-score above 3.0 (MEDIUM) / 5.0 (HIGH) |
| `DEMAND_NORMALISED` | The spike has passed — the recovery is itself news |
| `INVENTORY_DROP` | Absolute drop > 250 L **or** > 20% of previous |
| `DEPOT_BOTTLENECK` | Depot allowance remaining below 500 L |
| `ROUTE_BOTTLENECK` | Route capacity nearly exhausted |
| `SUPPLY_SHORTFALL` | Supply cannot meet demand |
| `STOCKOUT_RISK` | The forecast says a tank runs dry |

Anomalies are grouped into **incidents** over an 8-tick window, so a storm of
related anomalies reads as one event with one explanation.

**The count-once rule:** the three "detected" counters
(`backend_shortage_alerts_total`, `backend_anomalies_total`,
`backend_incidents_total`) increment **once per finding, when it is first
detected** — not once per read. A tracker remembers what it has counted and
resets when the world advances to a new tick.

**The kinds are derived, not hand-copied.** `ANOMALY_KINDS` is computed from
the constants in `anomaly.py`, because a hand-copied list is free to drift —
and it did: `STATION_OUTAGE` was registered but never emitted, while
`DEPOT_BOTTLENECK` and `STOCKOUT_RISK` were emitted but never registered.
Deriving the list removes the second copy; two regression tests now guard it.

### 5.4 Optimisation — greedy by default, LP when it helps

**Problem:** decide what to ship, from where, to where, how much.

**Solution:** `backend/app/optimizer.py` (1,142 lines) solves a constrained
allocation problem over a 16-tick horizon (4 hours), subject to constraints
C1–C10: route max shipment, depot inventory, dispatch capacity per tick,
station free space, route connectivity, single-connected-station safety stock,
and more.

- **Default solver: greedy.** Deterministic, fast, and correct on the shipped
  path.
- **Optional solver: LP.** `backend/app/lp_solver.py` (613 lines) implements a
  stdlib simplex-style solver — **no PuLP**. The path is import-guarded;
  `GET /api/solver` reports which solver is active and why.
- **Verification:** `test_optimizer_against_simulator.py` (472 lines) is a
  second differential oracle against the live simulator.

### 5.5 The explanation layer — deterministic, auditable, LLM-isolated

Covered in [§4.5](#45-the-explanation-layer). The key properties:

- Every number in the prose is **declared** in `Explanation.numbers`.
- `unaccounted_numbers()` flags any number that is not declared.
- The console renders the declared map verbatim.
- The optional LLM is off by default, isolated by AST, and cannot introduce a
  number.

### 5.6 Resilience — the circuit breaker

**Problem:** the upstream can time out, fail, or be injected-faulty. The
client retries 3× with backoff, so one logical call can block for **13.75 s**
(4 attempts × 3.0 s + 0.25 + 0.5 + 1.0 s of backoff).

**Solution:** `backend/app/circuit_breaker.py` (433 lines).

```
CLOSED    --(N consecutive retryable failures)-->  OPEN
OPEN      --(after cooldown, first call)--------->  HALF_OPEN
HALF_OPEN --(1 success)-------------------------->  CLOSED
HALF_OPEN --(1 failure)-------------------------->  OPEN
```

**The breaker wraps the client; it does not edit it.** This is structural, not
stylistic: `backend/app/simulator_client.py` must stay **byte-identical** to
`verifier/simulator_client.py`, enforced by a SHA-256 drift guard
(`backend/tests/test_client_parity.py`). Editing the client would mean editing
the reference first, re-running the 49 checks, then re-copying — and would
put a resilience mechanism inside a file whose whole value is that it is
unmodified.

**The measured effect:** an open breaker rejects in **under 1 ms** — asserted
by `test_an_open_breaker_rejects_in_under_a_millisecond`
(`backend/tests/test_circuit_breaker.py:190`) — instead of 13.75 s.

**Degraded mode** — the breaker changes what each endpoint means:

| Endpoint | Behaviour when the breaker is OPEN |
| :--- | :--- |
| `GET /api/state`, `/api/stations`, `/api/depots`, `/api/routes` | **200** with the cached snapshot plus `degraded: true` and `degraded_reason`. |
| `GET /api/health` | **200** with `status: "degraded"` and the circuit state. |
| `POST /api/plan`, `POST /api/execute` | **503** `CIRCUIT_OPEN` — writes must not silently plan against a stale world. |

**Two claims kept apart.** The simulator's `/health` is a liveness check on
the *process*; a fault injected against the *data* endpoints leaves it
answering while every read fails. Reporting "ok" then would be the worst
possible answer — so an open circuit degrades the status regardless of what
the probe says. The converse is handled too: the probe **bypasses** the
breaker, because an open breaker means *we* are not calling, not that the
upstream is down.

### 5.7 The stream — SSE with a watchdog and a polling fallback

**Problem:** SSE drops silently. A console that loses its stream and does not
notice shows a stale world as if it were live.

**Solution:** `backend/app/broker.py` (970 lines):

- Subscribes to the simulator's SSE stream and fans events out to dashboard
  subscribers through Redis pub/sub, with a **per-subscriber bounded queue**
  (64). A subscriber whose queue is full has its events **dropped and
  counted** (`backend_sse_events_dropped_total`), never silently discarded.
- A **watchdog** samples every 5 s. If no event arrives while the world has
  advanced for longer than `SSE_STALE_AFTER` (45 s), that is a **suspected
  drop** — counted, and the broker **falls back to polling**.
- After `SSE_RECOVER_AFTER` (10 s) of poll-only operation, the broker tries to
  reclaim SSE.
- `SSE_MAX_LIFETIME` (900 s) forces a fresh subscription, so a long-lived
  connection cannot rot.
- The broker's mode is a **number** (`0`=sse, `1`=polling, `2`=offline) so a
  Prometheus gauge can carry it — and `CLOSED`, the healthy circuit state, is
  deliberately `0`, so "2" is unambiguously the broken one.

### 5.8 The pool-saturation signal

**Problem:** the backend cannot read the simulator's internal connection pool.

**Solution:** it owns the semaphore that bounds its own demand, so it measures
its own saturation exactly:

```
backend_simulator_pool_saturation_ratio = inflight_reads / fetch_concurrency_limit
```

Above **0.8** is the early-warning line. Each simulator request holds **2**
connections, so 8 concurrent reads demand 16 > the simulator's 15-wide pool —
**overrunning the pool is a deadlock, not a slow-down.** Hence
`FETCH_CONCURRENCY = 4`.

`backend/tests/test_pool_saturation.py` drives concurrent refreshes against a
stubbed client and asserts the bound is never exceeded and the gauge never
exceeds 1.0.

### 5.9 The DLQ — quarantine, don't crash

**Problem:** one malformed SSE frame, or one allocation payload that does not
match the model, must not kill the world's event feed.

**Solution:** `backend/app/dlq.py` (323 lines) — a bounded, on-disk dead-letter
queue with a Redis mirror.

- Malformed payloads become DLQ entries (`id`, `reason`, `source`, `payload`,
  `received_at`, `attempts`) instead of raising into the event loop.
- **The subscription survives.** The bad frame is quarantined; the feed keeps
  running.
- Stored in Redis (bounded at 1,000, oldest evicted) **and** appended to
  SQLite at `/app/data/dlq.sqlite3`, so it survives a Redis flush.
- `GET /api/dlq` lists entries; `POST /api/dlq/{id}/retry` replays one through
  the original handler.

### 5.10 Idempotency and the journal

**Problem:** a dispatch that times out after the simulator accepted it would
ship twice.

**Solution:** the journal (`backend/app/journal.py`, 355 lines) is written
**before** the simulator call; the client detects a replay by allocation-id
tracking, not status code; and the console shows the last dispatch's state.

### 5.11 Scenario configuration — nothing hardcoded

**Problem:** the demand profiles, region factors and epoch were transcribed
from the simulator image's `baseline.yaml` and pasted into Python literals.
Correct today, but a copy of a file that cannot be swapped.

**Solution:** `backend/scenarios/baseline.yaml` + `backend/app/scenario.py`
(447 lines, with a custom YAML-subset parser). The file is **validated at
boot** — an unknown profile, a missing fuel or a region without a factor is an
error, not a runtime surprise. `forecast.py`'s `PROFILES`,
`REGION_DEMAND_FACTORS` and `FORECAST_START_TIME` become *values derived from
the config*, with the public names preserved so every import and test keeps
working.

**The guard:** the forecast oracle must still pass unchanged — the offline
golden fixture (60 of 60 exact rows) and the live oracle (currently 84 of 84).
If the YAML and the old literals ever disagree, the oracle fails — which is the
point. `backend/tests/test_scenario_config.py` (445 lines) asserts the loaded
config **equals** the previously hardcoded values.

### 5.12 Observability — three layers by metrics, one by logs

PDF §15 names four layers: **application**, **system**, **intelligence** and
**logs**. The honest coverage statement:

> **Three layers are met with metric families; the fourth — logs — is met with
> the backend's structured logging, because a log line is not a metric.**

29 metric families cover application (request rate, latency, error rate,
availability), system (pool saturation, in-flight reads, queue depth) and
intelligence (forecast error, shortage alerts, decision frequency, **fallback
activation** — named explicitly in §15). The Grafana dashboard has a **fourth
row, resilience**, which §15 does not list but which is where this platform's
most important operational signal lives.

Logs cover the fourth layer with **60 log statements** across fourteen modules,
at four levels (`warning`, `info`, `exception`, `debug`): integration failures
(`simulator_client.py`), decision events (`broker.py`, `dispatch.py`,
`journal.py`), recoveries (`circuit_breaker.py`), and quarantined payloads
(`dlq.py`).

**Two subtleties worth knowing:**

- **Route-template labels.** Metrics label `path` with
  `request.scope["route"].path`, never the raw path — otherwise
  `/api/stations/station-mirpur` and `/api/stations/station-tongi` become
  separate time series and the cardinality explodes. Unmatched paths fall back
  to `"unmatched"`; paths over 64 characters are capped.
- **Histogram percentiles.** A histogram's samples live under their *label
  set*. `percentile(0.95)` reads the empty key and returns **0.0 — always**.
  `percentile_across(0.95)` pools every label set. This was a **real bug**
  here: the health endpoint reported `p95_seconds: 0.0` while serving 12,681
  requests. It is now guarded by three regression tests.

Full catalogue: `docs/OBSERVABILITY.md`.

### 5.13 The health table (PDF §16)

`GET /api/health` carries the table a judge reads. Live right now:

```json
{
  "status": "ok",
  "components": {
    "backend_api":        {"state": "HEALTHY"},
    "redis":              {"state": "HEALTHY"},
    "fuel_simulator":     {"state": "HEALTHY"},
    "prediction_service": {"state": "HEALTHY"},
    "decision_engine":    {"state": "HEALTHY"}
  },
  "latency":  {"p95_seconds": 0.022541, "p50_seconds": 0.004429},
  "requests": {"total": 3382, "errors": 0, "error_rate": 0.0},
  "circuit":  {"state": "CLOSED", "consecutive_failures": 0, "opened_at": null},
  "upstream": {"reachable": true, "status": "ok", "error": null},
  "broker":   {"mode": "sse", "events_received": 194},
  "journal":  {"path": "/app/data/journal.sqlite3", "total": 30,
               "by_status": {"CREATED": 30}}
}
```

Component states come from a **known set** — `HEALTHY`, `DEGRADED`,
`UNHEALTHY`, `UNKNOWN`, `DISABLED` — so a dashboard's colour mapping cannot
break on an unexpected string. The pre-existing keys are **preserved** (the
console and the container healthcheck both read them); the new ones are added
alongside.

---

## 6. How to use it

### 6.1 Quick start

**Prerequisites:** Docker Desktop, and a path **without spaces** (see
[§6.7](#67-the-docker-desktop-constraint)).

```bash
# 1. Build and start the whole stack
docker compose up -d --build

# 2. Confirm all five services are healthy
docker compose ps
#   backend           Up (healthy)
#   client-verifier   Up (healthy)
#   frontend          Up (healthy)
#   redis             Up (healthy)
#   simulator-api     Up (healthy)

# 3. Open the console
open http://localhost:8003
```

### 6.2 Verify everything at once

```bash
./scripts/verify-all.sh --json /tmp/verify.json
```

Runs, in order: the **49-check verifier suite**, the **backend pytest suite**,
the **frontend test suite**, the **load test**, and the **observability
self-check** (46 assertions covering every §15 layer, the §16 table, the
content type, and the label sets). Emits one JSON object with per-suite
pass/fail, counts and durations. Exit code 0 only if everything passed.

### 6.3 Run the suites individually

```bash
# The 49-check conformance suite, against the live simulator
docker compose exec -T client-verifier python verify_simulator.py \
  --base-url http://simulator-api:8000 --redis-url redis://redis:6379/0

# The backend's own suite (must run in-container: the host lacks pytest-asyncio)
docker compose exec -T backend pytest tests/ -q

# The console's suite (the runtime image is deliberately slim — no toolchain,
# no source — so the suite runs in the Dockerfile's `test` build target)
docker build --target test -t buetcsefest-frontend-test -f frontend/Dockerfile .
docker run --rm buetcsefest-frontend-test
```

### 6.4 The console

`http://localhost:8003`. Read it top to bottom:

| Region | What it tells you |
| :--- | :--- |
| **Status bar** | Four facts: overall verdict, service level, disrupted routes, closed stations. Plus a Diagnostics disclosure. |
| **Telemetry bar** | Stream mode, staleness, snapshot age. Red if the world is stale or unreachable. |
| **ActionCard** | The one action: what to ship, where, how much, why, and the dispatch button. Expand "Why this plan" for the full reasoning and the evidence. |
| **Tab: Network** | Depots, stations, routes — with meters and badges. |
| **Tab: Forecast** | Exact demand over the horizon, per station and fuel, with sparklines. |
| **Tab: Problems** | Anomalies and incidents, each with its evidence and recommended action. |
| **Tab: System** | The PDF §16 health table, the stream's state, pool saturation, fallback activations. |

Theme toggle is in the status bar (light/dark, persisted, follows the OS by
default).

### 6.5 The API, by hand

```bash
# Health — the §16 table
curl -s http://localhost:8002/api/health | python3 -m json.tool

# The world
curl -s http://localhost:8002/api/state     | python3 -m json.tool
curl -s http://localhost:8002/api/stations  | python3 -m json.tool
curl -s http://localhost:8002/api/depots    | python3 -m json.tool
curl -s http://localhost:8002/api/routes    | python3 -m json.tool

# Plan a shipment — READ-ONLY, writes nothing, returns the plan AND the reasoning
curl -s -X POST http://localhost:8002/api/plan \
  -H 'Content-Type: application/json' \
  -d '{"station_id":"station-mirpur","fuel_type":"DIESEL","quantity":3000}' \
  | python3 -m json.tool

# Optimise — also read-only
curl -s -X POST http://localhost:8002/api/optimize | python3 -m json.tool

# The forecast, and its own oracle against live demand history
curl -s http://localhost:8002/api/forecast/summary | python3 -m json.tool
curl -s http://localhost:8002/api/forecast/validate | python3 -m json.tool

# Anomalies, with their explanations
curl -s http://localhost:8002/api/anomalies | python3 -m json.tool

# The Prometheus exposition (bare path, no /api prefix)
curl -s http://localhost:8002/metrics | head -40

# The JSON mirror the console's System tab reads
curl -s http://localhost:8002/api/observability | python3 -m json.tool

# The event stream
curl -N http://localhost:8002/api/events/stream
```

### 6.6 The demos

Two repeatable scripts, each printing the exact commands **and** the expected
observable output at every step:

```bash
./scripts/demo-normal.sh                    # beats 1–8: normal operations
./scripts/demo-crisis-fault-recovery.sh     # beats 9–14: crisis, fault, recovery
```

The second one injects a fault, shows the breaker trip, shows the console
degrading to the cached world, clears the fault, and shows the breaker close
**on its own**. It cleans up the fault on exit, even if interrupted.

### 6.7 The Docker Desktop constraint

**This project's path contains spaces, and Docker Desktop cannot bind-mount
it.** Every config file is therefore **baked into the image** rather than
mounted, and the build context is the **project root**:

- The base stack bakes its code into images.
- Prometheus's config is baked into `observability/Dockerfile.prometheus`.
- Grafana's provisioning is baked into `observability/Dockerfile.grafana`.

**Also note:** `docker compose build` does **not** recreate the running
container. Always follow a build with `--force-recreate`.

### 6.8 The monitoring stack (opt-in)

```bash
docker compose -f docker-compose.yml -f observability/compose.monitoring.yml up -d
```

| Service | URL |
| :--- | :--- |
| Prometheus | <http://localhost:9090> |
| Grafana | <http://localhost:3000> — anonymous admin, no login |

The Grafana dashboard is **"Fuel Supply Intelligence — Platform Observability"**,
in the `BUET CSE Fest` folder: four rows, nineteen panels. Prometheus carries
**nine alert rules** (pool saturation, circuit open, high latency, high error
rate, suspected stream drops, probe failures, non-empty DLQ, …).

### 6.9 Load testing

```bash
./loadtest/run.sh --duration 60 --vus 20
```

k6 if installed, otherwise the stdlib-only Python harness — **both emit the
identical JSON schema**. The report lands in `loadtest/results/<timestamp>.json`
and carries avg / p50 / p95 / p99, throughput, error rate, concurrency, the
backend container's CPU and memory, **and a `backend_metrics_snapshot` tying
request latency to pool saturation**. That snapshot is the point: the PDF asks
for "understanding the behavior and limits of the implemented system rather
than achieving an arbitrary benchmark".

### 6.10 The measured results

From `loadtest/results/20261126T003154Z.json` — 30 s, 10 VUs, three scenarios:

| Scenario | Requests | p50 | p95 | p99 | Throughput | Errors | Pool saturation (peak) |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| **W1** — `POST /api/optimize` | 1,166 | 198 ms | 262 ms | 294 ms | 38.5 rps | 0.0% | **1.00** |
| **W2** — `GET /api/stations` | 5,297 | 3 ms | 7 ms | 16 ms | 176.3 rps | 0.0% | 0.00 |
| **W3** — the console mix | 4,864 | 6 ms | 18 ms | 31 ms | 151.7 rps | 0.0% | 0.00 |

**The mechanism is visible in the table.** The decision path saturates the
pool and pays for it in latency; the read paths are served from the cached
snapshot and never touch the pool at all. That is the design working, and the
numbers say so:

- **W1's saturation peak of 1.00 is the semaphore working.** The bound was
  reached and never exceeded — `backend/tests/test_pool_saturation.py`
  asserts the gauge can never exceed 1.0.
- **W2 and W3's zero saturation is the snapshot cache working.** The console
  polls every 5 s and the simulator is barely touched.
- **Zero errors across all three scenarios**, with the circuit `CLOSED`
  throughout.

The pattern **reproduces**: an earlier run (`20261125T193715Z.json`, 12 VUs)
gave the same shape — W1 saturation 1.00 / mean 0.64, W2 and W3 at 0.00 —
with W1's p50 at 202 ms. The numbers move with the machine; the *shape* does
not. That is the point of the report.

### 6.11 Testing by injection — the simulator's fault switches

The simulator exposes an **admin API** that lets you break the world on
purpose. This is how the platform's resilience is actually *demonstrated*
rather than asserted, and `docs/TESTING.md` is the full guide (614 lines,
with every command and its expected output).

**The controls** — a pristine world, and the clock:

```bash
curl -sX POST http://localhost:8000/admin/reset        # tick 0, PAUSED, seed 12345
curl -sX POST http://localhost:8000/admin/step         # advance exactly one tick
curl -sX POST http://localhost:8000/admin/run          # start the background ticker
curl -sX POST http://localhost:8000/admin/pause         # stop it
curl -s  "http://localhost:8000/admin/audit?limit=20"  # every event, tick and allocation
```

**The domain events** — `POST /admin/events`:

```bash
curl -sX POST http://localhost:8000/admin/events \
  -H 'Content-Type: application/json' \
  -d '{"type":"route_disruption","route_id":"route-patiya-coxsbazar"}'
```

**The API faults** — `POST /admin/faults`:

```bash
curl -sX POST http://localhost:8000/admin/faults \
  -H 'Content-Type: application/json' \
  -d '{"type":"unavailable","duration_seconds":30}'

curl -sX POST http://localhost:8000/admin/faults/clear
```

**What each injection should look like, on all three surfaces.** This is the
table `docs/TESTING.md` §5 gives, and it is the one worth memorising — it
says what the simulator does, what the backend does, and what the *console*
does:

| Injection | Simulator | Backend | Console |
| :--- | :--- | :--- | :--- |
| `route_disruption` | route → `DISRUPTED` | `ROUTE_BOTTLENECK` anomaly + incident | StatusBar "Disrupted 1"; Problems tab |
| `station_outage` | station → `OUTAGE` | *no anomaly*; allocations → `409 STATION_CLOSED` | Network tab: the station's dot turns red |
| `depot_constraint` | depot → `CONSTRAINED` | *no anomaly*; depot still dispatchable | Network tab: the depot's dot turns amber |
| `demand_spike` | `demand_multiplier` × multiplier | `DEMAND_SPIKE` anomaly; forecast ×multiplier | Forecast tab |
| `shipment_delay` | supply `planned_tick` += delay, `DELAYED` | *no anomaly*; resupply arrives later | Forecast tab (indirectly) |
| `supply_shortfall` | supply `quantity` × factor | *no anomaly*; depot holds less | Forecast tab (indirectly) |
| `unavailable` | `503` on `/v1/*` | `mode: polling`, `is_stale: true` | "Upstream unreachable", mode "polling" |
| `latency` | every request delayed | timeouts above 3 s | panels slow, then error |
| `error_rate` | random `503`s | partial failures retried | intermittent, still correct |
| `stale_data` | `X-Simulator-Stale: true` | Redis cache flushed; snapshot re-fetched | no banner (the backend is honest) |
| `stream_disconnect` | new `/v1/stream` → `503` | reconnect blocked | no visible change until reconnect |

**Read the second column before the third.** The rows marked *no anomaly* are
the interesting ones: a station outage is *not* an anomaly — it is a
**constraint**, and the correct response is to refuse the allocation with
`409 STATION_CLOSED` rather than to raise a spurious alert. A console that
raised an alarm for every outage would be a console nobody trusts.

`docs/TESTING.md` §4 walks a full end-to-end pass — reset the world, break a
route that has no alternative, run the world, wait one tick, and look at what
the system *thinks* and what the console *shows*.

---

## 7. How it is verified

### 7.1 The three gates

| Gate | What it proves | Current |
| :--- | :--- | :---: |
| `verifier/verify_simulator.py` | The platform honours the simulator's contract — 49 checks across 6 groups | **49/49** |
| `backend/tests/` | 26 files, 10,119 lines, including **two differential oracles against the live simulator** | **470 passed, 8 skipped** |
| `frontend/src/test/` | 9 files, 2,127 lines, including the no-invented-numbers guarantee | **136 passed, 3 skipped** |

**The verifier's 49 checks, by group:**

| # | Group | Checks |
| :---: | :--- | ---: |
| 1 | Health & baseline topology | 10 |
| 2 | Deterministic tick & SSE stream | 7 |
| 3 | Allocation lifecycle & idempotency | 11 |
| 4 | Constraint & error validation | 5 |
| 5 | Crisis & fault resilience | 12 |
| 6 | Final audit & metrics summary | 4 |
| | **Total** | **49** |

### 7.2 The tests that are not ordinary

Four test files are worth knowing by name, because each one is a *differential
oracle* rather than a unit test:

| File | What it does |
| :--- | :--- |
| `test_validator_against_simulator.py` | Fires real requests at the live simulator and asserts the backend predicts the same rejection code every time. |
| `test_optimizer_against_simulator.py` | Runs the optimiser against the live simulator's own constraint model. |
| `test_client_parity.py` | SHA-256 guard: `backend/app/simulator_client.py` must be byte-identical to `verifier/simulator_client.py`. |
| `no-invented-numbers.test.tsx` | Every number on the console traces to the backend or to a shared derivation. |

### 7.3 The observability self-check

```bash
./scripts/check_observability.py
```

**46 assertions** covering every §15 layer, the §16 table, the content type,
and the label sets. It fails if a metric family disappears, if a required label
goes missing, if the content type drifts, or if a component's state leaves the
known set.

### 7.4 The bugs this found

Verification here is not ceremony. Twenty-one defects were found — nineteen by
three independent fresh-eyes audits, and two more by wiring up CI. Each was a
**green stack with something quietly wrong**. A sample:

| Bug | What was wrong |
| :--- | :--- |
| The health endpoint reported `p95_seconds: 0.0` while serving 12,681 requests | The histogram read the empty label key instead of pooling |
| `backend_shortage_alerts_total{severity="HIGH"}` moved with the viewer's polling rate | Counted per read instead of per detection |
| `STATION_OUTAGE` was registered but never emitted; two other kinds were emitted but never registered | A hand-copied list of anomaly kinds, free to drift |
| `backend_upstream_retries_total` rendered as a permanent zero | The client retries *below* the breaker's choke point, so the backend cannot see a retry — the metric was unmeasurable |
| `backend_incidents_total` rendered with no sample lines | Never registered |
| A rejected plan always answered 409 | An unknown station is 404 and a malformed request is 422 |
| The console's Dispatch button moved fuel while the dispatch counter stayed at zero | Two dispatch paths existed; only one of them counted |
| The verifier suite could not run on a clean machine | `docker compose exec` needs `client-verifier` running, and `ensure_stack` never started it |

Full list: `regular_updates/Day_6.md` §3 and §10.

### 7.5 Continuous integration

`.github/workflows/ci.yml` runs the whole verification story on every push to
`main` and on every pull request:

```yaml
- name: Run the verification suite
  run: ./scripts/verify-all.sh --json verify.json --skip-load-test   # PRs
  run: ./scripts/verify-all.sh --json verify.json --with-load-test \
         --load-duration 30 --load-vus 10                              # main
```

It runs **the same command a human runs**, rather than a second implementation
of the checks — a re-implementation is a second thing that can be wrong. The
script's exit code is the gate (0 only when every suite passed), and the JSON
report is uploaded as an artifact either way, so a red run still leaves the
evidence behind.

| Trigger | Suites | Wall clock (measured locally) |
| :--- | :--- | :---: |
| Pull request | verifier, backend, frontend, observability | 145 s |
| Push to `main` | + the load test | 253 s |

The load test is off on pull requests because it deliberately puts the backend
under real load — it is a signal about the mainline, not a gate on every
draft.

**A cold runner takes longer** — the `npm ci` and `pip install` layers have
to be built from scratch. The job's timeout is set to 45 minutes so a slow
network cannot turn a passing build into a failure.

**One thing worth knowing if a CI run is inexplicably slow:** the bottleneck
is almost always **disk, not code**. On the development machine, a run took
1,143 s instead of 253 s — and the backend's own logs showed a 912-second
silence, which proved the time was not spent in the backend at all. The
machine's disk was at 98% with 3 GB of Docker build cache. The workflow
therefore prunes stale images before building and reports the disk state
afterwards, so the next slow run is diagnosable rather than mysterious.

---

## 8. Configuration

**38 environment variables**, all with safe defaults, all set in
`docker-compose.yml`. `backend/app/config.py:185-245`.

### The ones that change behaviour most

| Env var | Default | What it does |
| :--- | :--- | :--- |
| `SIMULATOR_BASE_URL` | `http://simulator-api:8000` | The upstream. |
| `REQUEST_TIMEOUT` | `3.0` | Per-request timeout, seconds. |
| `MAX_RETRIES` | `3` | Retry attempts (→ 4 total tries). |
| `BREAKER_FAILURE_THRESHOLD` | `5` | Consecutive retryable failures before OPEN. |
| `BREAKER_COOLDOWN_SECONDS` | `15.0` | OPEN → HALF_OPEN probe delay. |
| `BREAKER_ENABLED` | `true` | Disable the breaker (it still counts). |
| `FETCH_CONCURRENCY` | `4` | Concurrent world reads. **Do not raise past 4** — each simulator request holds 2 connections against a 15-wide pool. |
| `STATE_MAX_AGE` | `15.0` | Snapshot TTL. **Must exceed `POLL_INTERVAL`** — this is exactly the bug that produced a red "Stale data" banner on a perfectly healthy system. |
| `POLL_INTERVAL` | `5.0` | The poller's reconcile rate. |
| `SSE_STALE_AFTER` | `45.0` | No event + world advanced → suspected drop. |
| `SSE_RECOVER_AFTER` | `10.0` | Poll-only seconds before reclaiming SSE. |
| `SSE_MAX_LIFETIME` | `900.0` | Forced fresh SSE subscription. |
| `SUBSCRIBER_QUEUE` | `64` | Per-dashboard queue depth. |
| `OPTIMIZER_HORIZON_TICKS` | `16` | Planning horizon (16 ticks = 4 hours). |
| `OPTIMIZER_SOLVER` | `auto` | `greedy` \| `lp` \| `auto`. |
| `FORECAST_HORIZON_TICKS` | `16` | Forecast horizon. |
| `SCENARIO_PATH` | `backend/scenarios/baseline.yaml` | The scenario file. **Read at import time** — must be set before the process starts. |
| `DLQ_MAX_ENTRIES` | `1000` | DLQ cap; oldest evicted. |
| `ANOMALY_HISTORY_WINDOW` | `32` | Ticks of history the detectors compare against. |

Booleans accept `1/0`, `true/false`, `yes/no`, `on/off`, case-insensitively.

**The scenario file** (`backend/scenarios/baseline.yaml`, 70 lines): scenario
name, seed, start time, tick minutes, four demand profiles, two region
factors. Resolution order: `SCENARIO_PATH` → the packaged file → built-in
defaults, so the backend still boots if the file is missing. Schema and how to
derive a new scenario: `docs/SCENARIO.md`.

---

## 9. The API surface

**42 routes** on the backend (`GET /openapi.json` lists them all), plus the
verifier's own.

### Health & observability

| Method | Path | Purpose |
| :--- | :--- | :--- |
| `GET` | `/` | The service root. |
| `GET` | `/api/health` | The PDF §16 health table. |
| `GET` | `/api/stream-health` | Broker mode, SSE reachability, drop detection. |
| `GET` | `/api/observability` | The JSON instrumentation mirror (the console's System tab). |
| `GET` | `/metrics` | The Prometheus exposition. **Bare path.** |
| `GET` | `/api/optimize/solver` | Which optimiser solver is active, and why. |
| `GET` | `/api/optimize/llm` | The LLM prose layer's status (off by default). |

### The world (read endpoints)

| Method | Path | Purpose |
| :--- | :--- | :--- |
| `GET` | `/api/state` | The whole world snapshot. |
| `GET` | `/api/stations`, `/api/stations/{station_id}` | Stations. |
| `GET` | `/api/depots`, `/api/depots/{depot_id}` | Depots. |
| `GET` | `/api/routes` | Routes. |
| `GET` | `/api/supply-arrivals` | Inbound supply. |
| `GET` | `/api/metrics` | The simulator's business KPIs (the console's original contract). |

### Intelligence

| Method | Path | Purpose |
| :--- | :--- | :--- |
| `GET` | `/api/forecast` | The forecast over the horizon. |
| `GET` | `/api/forecast/summary` | The forecast, summarised for the console. |
| `GET` | `/api/forecast/validate` | The forecast's oracle against live `demand-history`. |
| `GET` | `/api/forecast/{station_id}` | One station's forecast. |
| `GET` | `/api/anomalies`, `/api/anomalies/summary`, `/api/anomalies/{anomaly_id}` | Anomalies. |
| `GET` | `/api/anomalies/{anomaly_id}/explain` | An anomaly's explanation. |
| `GET` | `/api/incidents`, `/api/incidents/{incident_id}` | Grouped incidents. |

### Decisions

| Method | Path | Purpose |
| :--- | :--- | :--- |
| `POST` | `/api/plan` | Plan a shipment. **Read-only** — returns the plan *and* the reasoning. |
| `POST` | `/api/optimize` | Optimise. **Read-only** — writes nothing. |
| `POST` | `/api/optimize/execute` | Dispatch the current-tick shipments. Re-plans against a fresh snapshot first. |
| `GET` | `/api/optimize/{plan_id}` | A stored plan. |
| `GET` | `/api/allocations`, `/api/allocations/{allocation_id}` | Allocation history. |
| `GET` | `/api/allocations/{allocation_id}/explain` | An allocation's explanation. |
| `POST` | `/api/allocations` | Create an allocation. |
| `POST` | `/api/allocations/{allocation_id}/cancel` | Cancel a PENDING allocation. |

### Events & the DLQ

| Method | Path | Purpose |
| :--- | :--- | :--- |
| `GET` | `/api/events` | The event feed. |
| `GET` | `/api/events/recent` | The last N events (buffer 200). |
| `GET` | `/api/events/stream` | The SSE stream the console subscribes to. |
| `GET` | `/api/dlq`, `/api/dlq/{entry_id}` | The dead-letter queue. |
| `POST` | `/api/dlq/{entry_id}/retry` | Replay a dead-lettered payload. |

---

## 10. Where to read next

| Document | What it covers |
| :--- | :--- |
| `README.md` | Quick start, the operator commands, the observability walkthrough. |
| `docs/ARCHITECTURE.md` | The as-built diagram, sequence diagrams, the data-flow table. |
| `docs/BACKEND.md` | Every backend module, the event vocabulary, the configuration table. |
| `docs/DASHBOARD.md` | The console, panel by panel. |
| `docs/OBSERVABILITY.md` | The full metric catalogue, the alert rules, the pool-saturation signal. |
| `docs/RESILIENCE.md` | The breaker's state machine, the degraded-mode contract, the measured recovery timeline. |
| `docs/LOAD_TEST.md` | The workload definition, the report schema, the measured results. |
| `docs/SCENARIO.md` | The scenario schema and how to derive a new one. |
| `docs/DEMO.md` | The 14-beat demo narrative, with the exact commands for each beat. |
| `docs/TESTING.md` | Testing by injection — the admin controls, the six domain events, the five API faults, and what each surface should show for every injection. |
| `docs/SYSTEM_DESIGN.md` | The as-built design (rewritten to match the running product). |
| `docs/BRIEF.md` | What the brief asked for, what is implemented, and what is not — and why. |
| `docs/system_understanding_and_requirements.md` | The Day-1 requirements analysis: the domain model, the simulator's integration rules, the functional and technical requirements, the operational problems, and the evaluation breakdown. |
| `docs/PROMPT_JOURNAL.md` | The prompt sequence that built this system, step by step — and what each prompt actually bought. |
| `regular_updates/Day_1.md` … `Day_6.md` | The build log, including the bugs found and the measured results. |

---

## A closing note

The number to remember is not a benchmark. It is this:

> **A platform that shows a number without saying how old it is, or why it is
> the way it is, is not telling the operator anything.**

Everything above — the exact forecast, the named rejection reason, the honest
`degraded: true`, the count-once counters, the declared-numbers audit — follows
from that one conviction. An operator acting on a fuel allocation is making a
physical decision about the world. The software's job is not to look certain.
It is to be **right, or to say clearly that it does not know.**
