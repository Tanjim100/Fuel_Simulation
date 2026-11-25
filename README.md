# BUP Fuel Supply Simulator — Verification Harness, Operator Backend, Console & Observability

A complete, runnable local setup that connects to, interacts with, and verifies
**every API surface** of the BUP Fuel Supply Simulator
(`asifmahmoud414/bup-fuel-supply-simulator:1.0.0`), plus the **operator backend**
that forecasts, optimises and dispatches fuel shipments against it, and the
**operator console** that puts a human in front of that backend — with the
**observability, resilience and load-testing** layer that proves the platform
is healthy, degrades safely, and knows its own limits.

It consists of:

| Service | Image / Build | Purpose |
| :--- | :--- | :--- |
| `simulator-api` | `asifmahmoud414/bup-fuel-supply-simulator:1.0.0` | The simulator (the *world*). |
| `redis` | `redis:alpine` | Cache layer for read snapshots + state tracking. |
| `client-verifier` | `./verifier` (Python 3.11) | Defensive client + end-to-end verification suite. |
| `backend` | `./backend` (Python 3.11) | Operator backend: SSE broker + pre-flight validator + optimiser + forecast + anomaly detection + explanation layer + circuit breaker + metrics + DLQ. |
| `frontend` | `./frontend` (Node 22) | Operator console: a focused, tabbed dashboard over the backend's API. |

Plus a defensive async client (`simulator_client.py`), a 49-check
verification suite (`verify_simulator.py`), the backend service
(pre-flight validator, optimiser, forecast, anomaly detection, SSE broker,
dispatch API — see [`docs/BACKEND.md`](docs/BACKEND.md)), and the operator
console (see [`docs/DASHBOARD.md`](docs/DASHBOARD.md)).

To test the system under stress, inject domain events and API faults into the
simulator — see [`docs/TESTING.md`](docs/TESTING.md).

---

## 1. Quick start

From the project root (`BUET CSE Fest/`):

```bash
# 1. Build and start the whole stack
docker compose up -d --build

# 2. Confirm all five services are healthy
docker compose ps

# 3. Run the full verification suite inside the verifier container
docker compose exec -T client-verifier \
  python verify_simulator.py \
    --base-url http://simulator-api:8000 \
    --redis-url redis://redis:6379/0

# 4. Run the backend's own test suite
docker compose exec -T backend pytest tests/ -q

# 5. Run the console's test suite
#    (the console's runtime image is deliberately slim -- no toolchain, no
#     source -- so the suite runs in the Dockerfile's `test` build target)
docker build --target test -t buetcsefest-frontend-test -f frontend/Dockerfile .
docker run --rm buetcsefest-frontend-test

# 6. Open the console
open http://localhost:8003

# 7. Tear down
docker compose down
```

Expected output ends with:

```
  ALL 49 CHECKS PASSED
```

### Or run everything at once

`./scripts/verify-all.sh` runs all four suites in order and emits **one JSON
object** — the harness-compatible entry point. Exit code `0` only if everything
passed.

```bash
./scripts/verify-all.sh                      # human-readable
./scripts/verify-all.sh --json out.json      # machine-parseable
```

### The two operator-facing endpoints

```bash
# The Prometheus exposition (text/plain) — bare path, no /api prefix
curl -s http://localhost:8002/metrics

# The JSON mirror the console's System tab reads
curl -s http://localhost:8002/api/observability
```

`/api/metrics` is a *third* thing and stays JSON — it is the simulator's
business KPIs, the console's original contract. The three never drift:
`/metrics` is Prometheus text, `/api/observability` is the platform's own
metrics as JSON, `/api/metrics` is the world's business KPIs as JSON.

Full detail: [`docs/OBSERVABILITY.md`](docs/OBSERVABILITY.md).

---

## 1b. The operator backend

The backend owns the **single** upstream SSE subscription, maintains a fresh world
snapshot, and decides what is legal to dispatch **before** any write reaches the
simulator.

```bash
# Health
curl -s http://localhost:8002/api/health

# Broker state: mode, reachability, drop detection
curl -s http://localhost:8002/api/stream-health

# Plan a shipment (no write) — returns the plan *and* the reasoning
curl -s -X POST http://localhost:8002/api/plan \
  -H 'Content-Type: application/json' \
  -d '{"source_depot_id":"depot-patiya",
       "destination_station_id":"station-mirpur",
       "fuel_type":"DIESEL",
       "quantity":1000}'

# Dispatch it (re-plans against a fresh snapshot, then executes)
curl -s -X POST http://localhost:8002/api/allocations \
  -H 'Content-Type: application/json' \
  -d '{"source_depot_id":"depot-patiya",
       "destination_station_id":"station-mirpur",
       "fuel_type":"DIESEL",
       "quantity":1000}'

# Watch the dashboard event stream
curl -sN http://localhost:8002/api/events/stream
```

**The pre-flight validator** is the point of the service. It mirrors the simulator's
validation order exactly, splits oversized requests into legal shipments, and explains
every decision in operator-readable language. A randomised 40-request differential test
against the live simulator proves that anything the validator approves the simulator
accepts, and anything it rejects the simulator would have rejected with the same code.

See [`docs/BACKEND.md`](docs/BACKEND.md) for the full API contract, the validator rules,
and the measured simulator semantics they encode.

---

## 1c. The optimisation & intelligence layer

On top of the validator sits the **optimiser** (what is *optimal* to dispatch over a
multi-tick, multi-destination horizon, against *forecast* demand), a **demand forecast**,
**anomaly detection**, and a **deterministic explanation layer**.

```bash
# Compute the optimal plan — READ-ONLY, writes nothing
curl -s -X POST http://localhost:8002/api/optimize -H 'Content-Type: application/json' -d '{}'

# Compute the plan AND execute only the current-tick shipments
curl -s -X POST http://localhost:8002/api/optimize/execute -H 'Content-Type: application/json' -d '{}'

# Which solver is active, and why
curl -s http://localhost:8002/api/optimize/solver

# The forecast for the current horizon
curl -s http://localhost:8002/api/forecast

# The forecast's own oracle: compare it against live /v1/demand-history
curl -s http://localhost:8002/api/forecast/validate

# Current anomalies, and their deterministic explanations
curl -s http://localhost:8002/api/anomalies
curl -s http://localhost:8002/api/anomalies/summary
curl -s http://localhost:8002/api/incidents
```

Three properties are load-bearing:

1. **The optimiser never trusts its own arithmetic for legality.** Every candidate
   shipment is re-checked by `validate_and_plan` before it can reach the simulator. The
   optimiser may be *conservative*, never *illegal*.
2. **Only current-tick shipments are ever written.** The simulator stamps
   `created_tick = now`, so writing a future-tick shipment early would consume the current
   tick's dispatch cap. The plan is split into `shipments_now` (executable) and
   `shipments_later` (advisory).
3. **The language model is structurally incapable of being the decision authority.** The
   optional prose layer is disabled by default, may not import the optimiser, validator,
   state or journal (an AST test enforces this), receives only a finished immutable
   explanation, and may return prose only — prose quoting an undeclared figure is
   discarded.

The forecast is exact, not approximate: because the simulator's demand is deterministic
(SHA-256-seeded), future demand is computable offline, and `GET /api/forecast/validate`
reports `EXACT` against live `/v1/demand-history`.

Full detail: [`docs/BACKEND.md` Part II](docs/BACKEND.md).

---

## 1d. The operator console

The console is a fifth service on `:8003`. It renders the world the backend
publishes and lets an operator act on the backend's recommendation — with a
confirmation step, because a dispatch writes to the simulator.

The screen is **one column, one hero, one panel at a time**: a slim status bar,
a single action card carrying the plan and the dispatch button, and four tabs
(Network, Forecast, Problems, System) with everything else one click away
behind a disclosure. It has a **dark and a light theme**, toggled from the
status bar and remembered across reloads.

```bash
# The console (serves the built assets and proxies /api to the backend)
open http://localhost:8003

# The console's own test suite, in the Dockerfile's `test` build target
docker build --target test -t buetcsefest-frontend-test -f frontend/Dockerfile .
docker run --rm buetcsefest-frontend-test

# The live integration test — renders the whole console against the running backend
cd frontend && LIVE_BACKEND=http://localhost:8002 npm test
```

It has one rule, and the rule is executable:

> **Every number on screen is a number the backend sent, or a documented pure
> derivation of numbers the backend sent.**

`no-invented-numbers.test.tsx` scrapes every leaf element out of the rendered
DOM, extracts every number, and checks each against an allow-list computed
from the same shared derivation functions the components use. The live variant
does the same against the running stack.

Three other properties are worth stating:

1. **A proxy, not CORS.** The browser talks to one origin. The console's server
   pipes `/api/*` to the backend as a raw byte stream, so SSE stays live and the
   backend's internal hostname never reaches the browser.
2. **SSE is advisory.** An event triggers a REST re-read; the payload is never
   rendered as world state. A dropped stream degrades to polling and the
   status bar *says* so.
3. **Dispatch takes two clicks.** The first arms the action and dispatches
   nothing; the second confirms it.

Full detail: [`docs/DASHBOARD.md`](docs/DASHBOARD.md).

---

## 1e. Observability & resilience

The platform reports on its own health while it works, and refuses work it
cannot do safely.

### The metrics

`GET /metrics` — a hand-rolled Prometheus exposition, ~400 lines, **zero new
dependencies**. It covers the four layers PDF §15 names:

| Layer | The signal that matters |
| :--- | :--- |
| **Application** | request rate, latency histogram (p50/p95/p99), error rate, availability |
| **System** | in-flight simulator reads, the read limit, **pool saturation**, queue depth |
| **Intelligence** | forecast error, forecast verdicts, shortage alerts, decision frequency, **fallback activations** |
| **Resilience** | circuit state, circuit transitions and rejections, broker mode, suspected SSE drops, snapshot age, DLQ size |

The labels use the **route template**, never the raw path — otherwise
`/api/stations/station-mirpur` and `/api/stations/station-tongi` become two
time series and the metric's cardinality explodes.

### The health table

`GET /api/health` carries PDF §16's table verbatim — Backend API, Database,
Fuel Simulator, Prediction Service, Decision Engine — plus p95 latency, error
rate, the circuit state and the broker state. It **always returns HTTP 200**:
the container healthcheck must never kill the backend at the moment it should
be serving the cached world.

### The circuit breaker

A breaker wraps the simulator client (it never edits it — the client must stay
byte-identical to the verifier's reference copy). It converts a **13.75-second
retry stall into a 1-millisecond rejection**.

Only *retryable* failures trip it. A 409 from the simulator is a **business
answer**, not an outage — tripping on it would take the whole backend down for
one bad request.

**Reads degrade; writes refuse.**

| | Behaviour when the breaker is OPEN |
| :--- | :--- |
| `GET /api/state` | **200** with the last known good world and `degraded: true` |
| `POST /api/plan` | **503** `CIRCUIT_OPEN` — a plan against a stale world would be a *wrong* plan |

### Prove it, live

```bash
# Break the upstream
curl -s -X POST http://localhost:8000/admin/faults \
  -H 'Content-Type: application/json' \
  -d '{"type":"unavailable","duration_seconds":60}'

sleep 20
curl -s http://localhost:8002/api/health    # status: "degraded", circuit: OPEN
curl -s http://localhost:8002/api/state     # 200, degraded: true
curl -s http://localhost:8002/metrics | grep backend_circuit_state   # 2.0

# Restore it — the breaker closes on its own, nothing polls, nothing nudges
curl -s -X POST http://localhost:8000/admin/faults/clear
```

Full detail: [`docs/OBSERVABILITY.md`](docs/OBSERVABILITY.md),
[`docs/RESILIENCE.md`](docs/RESILIENCE.md).

### The monitoring stack — opt-in

Prometheus and Grafana live behind a **compose overlay**, not in the default
stack. `docker compose up -d` still yields exactly **five** healthy services.

```bash
docker compose -f docker-compose.yml -f observability/compose.monitoring.yml up -d
# Prometheus: http://localhost:9090    Grafana: http://localhost:3000
```

Nine alert rules ship with it — pool saturation, circuit open, high latency,
high error rate, suspected SSE drops, a non-empty DLQ and more. The Grafana
dashboard is provisioned automatically, with one row per §15 layer.

Full detail: [`observability/README.md`](observability/README.md).

---

## 2. Exact terminal commands

### 2.1 Start / stop the stack

```bash
# Start (build the verifier image on first run)
docker compose up -d --build

# Follow logs
docker compose logs -f

# Stop and remove containers
docker compose down

# Stop and also remove the named volume (discards cached snapshots + reports)
docker compose down -v
```

### 2.2 Confirm the services are up

```bash
# Container status (all should read "healthy")
docker compose ps

# Simulator liveness (bypasses fault injection)
curl -s http://localhost:8000/v1/health

# Redis
docker compose exec -T redis redis-cli ping

# Verifier sidecar liveness
curl -s http://localhost:8001/health

# Backend liveness
curl -s http://localhost:8002/api/health
```

### 2.3 Run the verification suite

```bash
# In-container (recommended: talks to simulator-api and redis over the compose network)
docker compose exec -T client-verifier \
  python verify_simulator.py \
    --base-url http://simulator-api:8000 \
    --redis-url redis://redis:6379/0 \
    --json-report /app/reports/report.json

# Over HTTP (returns the same report as JSON; 200 = all green, 500 = failures)
curl -s -X POST http://localhost:8001/verify

# On the host, against the published ports
cd verifier
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python verify_simulator.py --base-url http://localhost:8000 --redis-url redis://localhost:6379
```

Useful flags:

| Flag | Default | Meaning |
| :--- | :--- | :--- |
| `--base-url` | `http://simulator-api:8000` | Simulator base URL. |
| `--redis-url` | `redis://redis:6379/0` | Redis URL for the cache layer. |
| `--timeout` | `3.0` | Per-request HTTP timeout (seconds). |
| `--max-retries` | `3` | Retry attempts for `503`/timeouts. |
| `--json-report` | *(none)* | Write a machine-readable JSON report. |
| `-v`, `--verbose` | off | Debug logging. |

Exit code is `0` when every check passes, `1` otherwise — so it drops straight
into CI.

### 2.4 Useful simulator commands while developing

```bash
# The simulator's own HTML dashboard (auto-refreshes every 2s)
open http://localhost:8000/admin

# Swagger / ReDoc
open http://localhost:8000/docs
open http://localhost:8000/redoc

# Reset to the pristine baseline scenario
curl -s -X POST http://localhost:8000/admin/reset

# Advance exactly one tick (deterministic)
curl -s -X POST http://localhost:8000/admin/step

# Run / pause the background ticker
curl -s -X POST http://localhost:8000/admin/run
curl -s -X POST http://localhost:8000/admin/pause

# Inject a crisis event
curl -s -X POST http://localhost:8000/admin/events \
  -H 'Content-Type: application/json' \
  -d '{"type":"route_disruption","start_tick":0,"duration_ticks":10,
       "parameters":{"route_ids":["route-gazipur-mirpur"]}}'

# Inject / clear API faults
curl -s -X POST http://localhost:8000/admin/faults \
  -H 'Content-Type: application/json' \
  -d '{"type":"stale_data","duration_seconds":60,"parameters":{}}'
curl -s -X POST http://localhost:8000/admin/faults/clear

# Audit log
curl -s "http://localhost:8000/admin/audit?limit=20"
```

The simulator also has a **built-in web console** at
`http://localhost:8000/admin` — dropdowns for both injection surfaces, a live
audit pane, and one-click fault clearing.

### 2.5 Testing by injection

The commands above are the whole injection surface, but the *timing* is not
obvious and getting it wrong produces confusing results. `docs/TESTING.md`
documents the measured behaviour — including the `start_tick` off-by-one, which
six domain events and five API faults do and do not raise an anomaly for, and
why `stream_disconnect` does **not** tear down a live stream.

The short version:

```bash
# Break a route that has no alternative, and watch the system notice
curl -s -X POST http://localhost:8000/admin/events \
  -H 'Content-Type: application/json' \
  -d '{"type":"route_disruption","start_tick":0,"duration_ticks":10,
       "parameters":{"route_ids":["route-patiya-coxsbazar"]}}'

curl -s -X POST http://localhost:8000/admin/run
sleep 12

curl -s http://localhost:8002/api/anomalies    # → ROUTE_BOTTLENECK, CRITICAL
curl -s http://localhost:8002/api/metrics      # → kpis.disrupted_routes: 1
```

Two rules that will save you a debugging session:

- **An event with `start_tick: N` activates on the tick *after* N** if you
  inject it while the world already sits at tick N. Inject `N-1` for it to be
  live at tick N.
- **Reset the world before you test.** The baseline is well-stocked for its
  first ~60 ticks, so an empty plan early on is *correct*, not a bug.

### 2.6 The load test

Three named workloads, measured. **k6** is the primary tool; a **stdlib-only
Python harness** is the fallback, and both emit the **same JSON schema**, so
the report is reproducible on a machine that has neither.

```bash
./loadtest/run.sh --duration 60 --vus 20       # picks k6 if present
./loadtest/run.sh --duration 60 --vus 20 --python  # force the fallback
```

The report lands in `loadtest/results/<timestamp>.json`:

```json
{
  "schema_version": 1,
  "started_at": "...", "duration_seconds": 60, "vus": 20,
  "scenarios": [
    {"name": "W1-decision", "requests": 1377, "error_rate": 0.0,
     "latency_ms": {"avg": 209.0, "p50": 202.3, "p95": 249.3, "p99": 269.3},
     "throughput_rps": 45.5,
     "resource": {"cpu_pct": 0.47, "mem_mb": 51.5}}
  ],
  "backend_metrics_snapshot": {"pool_saturation_peak": 1.0, "circuit_state": "CLOSED"}
}
```

The `backend_metrics_snapshot` is the point: the report reads the backend's
own `/metrics` before and after the run, so the numbers tie request latency to
**simulator pool saturation**. That is the finding PDF §18 is asking for —
understanding the system's limits, not a benchmark number.

Measured results and their interpretation:
[`docs/LOAD_TEST.md`](docs/LOAD_TEST.md).

### 2.7 The one command

```bash
./scripts/verify-all.sh                      # human-readable
./scripts/verify-all.sh --json out.json      # machine-parseable
```

It runs, in order: the **49-check verifier suite**, the **backend pytest
suite**, the **frontend test suite**, the **load test**, and the
**observability self-check** (which asserts every required metric family is
present and that `/api/health` carries §16's fields). Output is one JSON
object with per-suite pass/fail, counts and durations. Exit code `0` only if
everything passed.

### 2.8 The demo

Two scripts run the whole 14-beat story, so the demo is repeatable and never
memorised. Each beat prints the exact command **and** the output a judge
should see, then runs it.

```bash
./scripts/demo-normal.sh                    # beats 1–8
./scripts/demo-crisis-fault-recovery.sh     # beats 9–14
./scripts/demo-normal.sh --narrate          # print the commands only
```

Full detail: [`docs/DEMO.md`](docs/DEMO.md).

---

## 3. What the suite verifies

Six sequential groups, 49 individual assertions.

### 1. Health & baseline topology
- `POST /admin/reset` returns a clean instance (`tick=0`, `status=PAUSED`,
  `scenario=baseline`, `seed=12345`).
- `GET /v1/health` returns `status: "ok"`.
- Exact world topology: **2 regions, 2 depots, 4 stations, 6 routes, 22
  scheduled supply arrivals**.
- Every baseline entity id matches the documented scenario, and every route
  connects a valid depot to a valid station.
- `GET /v1/demand-history?limit=100` returns a bounded list.

### 2. Deterministic tick & SSE
- `POST /admin/step` advances exactly one tick.
- `GET /v1/stream` delivers a `simulation.tick` event with `tick` + `sim_time`.
- The SSE event triggers an **immediate REST re-fetch** (SSE is advisory only).
- `: connected` / `: keepalive` comments are ignored, never parsed as events.
- `GET /v1/instance` confirms the tick increment; repeated steps are monotonic.

### 3. Allocation lifecycle & idempotency
- `POST /v1/allocations` → `201 Created`, `PENDING`.
- Replaying the same key + body is **idempotent** (same allocation id).
- Same key + different body → `409 IDEMPOTENCY_KEY_MISMATCH`.
- `PENDING → IN_TRANSIT → ARRIVED` across ticks; station inventory updates.
- Cancelling a departed allocation → `409 CANNOT_CANCEL`.
- Cancelling an unknown id → `404`.
- Cancelling a `PENDING` allocation succeeds (`CANCELLED`).
- A cancelled idempotency key **stays permanently occupied**.

### 4. Constraint & error validation
- Quantity above `route.max_shipment` → `409 ROUTE_CAPACITY_EXCEEDED`.
- Route connecting different endpoints → `409 ROUTE_MISMATCH`.
- Unknown route id → `404 NOT_FOUND`.
- Invalid `fuel_type` enum → `422`.
- `quantity <= 0` → `422`.

### 5. Crisis & fault resilience
- `POST /admin/events` injects a `route_disruption`; the route flips to
  `DISRUPTED` and allocations against it are rejected with
  `409 ROUTE_DISRUPTED`.
- The `stale_data` fault sets `X-Simulator-Stale: true` on `/v1/*` GETs and
  triggers an **immediate Redis cache invalidation**.
- `GET /v1/health` and all `/admin/*` paths **bypass fault injection**.
- The `unavailable` fault produces `503 FAULT_INJECTED`, which is retried with
  **exponential backoff** and then surfaced cleanly.
- The `stream_disconnect` fault makes `GET /v1/stream` return `503`; the
  listener backs off and reconnects.
- `POST /admin/faults/clear` restores full service.

### 6. Final audit & metrics summary
- `GET /v1/metrics` returns a coherent summary (`service_level` in `[0, 1]`).
- `GET /admin/audit?limit=20` returns the audit trail.
- The audit log records `allocation.created` and `simulation.tick`.
- The simulator is healthy and the instance is reachable at the end.

---

## 4. Design notes

### 4.1 The client is defensive by construction

`simulator_client.py` encodes the integration rules rather than assuming them:

- **REST is the source of truth; SSE is advisory.** `StreamListener` maps each
  event type to the resources it affects (`simulation.tick` → instance,
  metrics, depots, stations, allocations, supply-arrivals; …) and re-fetches
  them over REST the moment an event arrives. The event payload is never
  trusted as world state.
- **Stale detection.** Every `GET /v1/*` response is inspected for
  `X-Simulator-Stale: true` (inside `_request`, so it applies to *all* read
  paths, not just the cached ones). On a hit the whole `sim:*` Redis namespace
  is flushed and a warning is logged.
- **Backoff + jitter.** `503 FAULT_INJECTED` and transport timeouts are
  retried with exponential backoff and full jitter (capped at 4 s) under a
  hard 3 s per-request timeout. Retries stop once the attempt budget is spent
  and the error is surfaced as a typed exception.
- **Typed error taxonomy.** Domain `409`s raise `SimulatorConflict` carrying
  the exact code (`ROUTE_CAPACITY_EXCEEDED`, `IDEMPOTENCY_KEY_MISMATCH`, …);
  `404` raises `SimulatorNotFound`; `422` raises `SimulatorValidationError`;
  injected faults raise `SimulatorFaultInjected`.
- **Cache safety.** Only genuinely static resources (`/v1/regions`) are served
  from Redis. Everything that moves with the world is fetched fresh, and every
  mutation invalidates the namespace — otherwise a cached snapshot would
  silently contradict the simulator.

### 4.2 Two places where the guide and the task brief differ

Both were resolved **in favour of the live simulator**, which was measured
directly rather than assumed:

1. **Idempotent replay returns `201`, not `200`.** The guide states: *"Same
   key, same body → return existing allocation (HTTP 201, not 200)."* The
   client accepts `200` or `201` and detects a replay by tracking the highest
   allocation id seen, because the status code alone cannot distinguish a new
   allocation from a replay.

2. **Arrival takes `transit_ticks + 1` steps.** A shipment created on tick *N*
   departs on tick *N* and is scheduled to arrive on tick *N + transit_ticks*,
   but the arrival is **applied** when the counter advances past that tick.
   So `route-gazipur-mirpur` (`transit_ticks=2`) reaches `ARRIVED` three
   steps after creation, not two. The suite steps until `ARRIVED` (bounded to
   8 iterations) instead of hard-coding a count.

A third difference worth noting: the guide's fast-start snippet shows
`SIMULATION_SPEED: 15` / `TICK_MINUTES: 8`, which are swapped relative to the
documented defaults. `docker-compose.yml` uses the real semantics —
`SIMULATION_SPEED` (wall-clock ticks/second while running) and
`TICK_MINUTES=15` (simulated minutes per tick), matching the domain model's
"15-minute tick clock".

### 4.2a The tick clock: one tick every 10 real seconds

`SIMULATION_SPEED=0.1` gives **one tick every 10 real seconds**, and each tick
advances the simulated clock by 15 minutes. So the console shows a quarter-hour
of supply-chain activity roughly every ten seconds.

**10 seconds is the slowest this simulator can go.** Its runner sleeps

```python
await asyncio.sleep(max(0.05, 1.0 / max(0.1, settings.simulation_speed)))
```

The inner `max(0.1, ...)` floors the divisor at `0.1`, which floors the interval
at **10 seconds** no matter how small the speed is set. A 15-second tick is
therefore not expressible in this simulator version — 10s is the closest
reachable value. (At the previous `SIMULATION_SPEED=8` the world advanced a
full simulated day every 12 real seconds, which is far too fast to read.)

Two backend timings were retuned to match, because both were calibrated
against a fast-ticking world:

| Setting | Was | Now | Why |
| :--- | :--- | :--- | :--- |
| `SSE_STALE_AFTER` | `20` | `45` | Events arrive once per tick, so a 20s threshold left only 10s of slack over a 10s tick — it would fire on a healthy but slow world. 45s is ~4 ticks of slack. |
| `POLL_INTERVAL` | `2` | `5` | Polling twice a second read the same snapshot over and over. Half the tick interval is enough to keep a fresh snapshot without polling faster than the world changes. |
| `POLL_MS` (console) | `4000` | `5000` | Same reasoning on the console side. The SSE stream nudges it the instant a tick lands; the poll is the safety net for a dropped stream. |

### 4.3 The backend owns one SSE subscription, and detects drops by tick progress

The simulator's SSE endpoint has a **silent failure mode**: each subscriber gets its own
`asyncio.Queue(maxsize=200)`, and when that queue overflows the simulator drops the
subscriber from its set but **leaves the HTTP connection open** — permanently silent, no
events and no keepalives. The connection looks healthy; the simulator stays healthy.

A paused simulator is *also* silent, so "have I seen a frame recently?" cannot distinguish
the two. The backend therefore measures **tick progress**: the watchdog polls
`/v1/instance`, and if the tick advanced while no event arrived for 20 s, the subscription
is presumed dead and is replaced. A REST poller runs at all times, so a drop degrades
*freshness*, never correctness.

The backend also tracks **reachability separately from event delivery**. A short-lived SSE
probe reports whether the simulator will accept a subscription, so a healthy-but-paused
stream is not misreported as broken. Full details in [`docs/BACKEND.md`](docs/BACKEND.md).

### 4.4 The validator mirrors the simulator's validation *order*

The simulator applies its capacity limits in a **fixed order** and reports the *first* one
that fails — not the numerically smallest. A 7500 L request against a route limited to 7000 L
and a station with 6000 L free is rejected with `ROUTE_CAPACITY_EXCEEDED`, **not**
`DESTINATION_CAPACITY_EXCEEDED`.

The validator encodes this order explicitly, so the operator is told the true reason for a
rejection. This was caught by the randomised differential test against the live simulator,
not assumed from the guide.

### 4.5 The optimiser is gated by the validator, and the forecast is exact

The optimiser decides what is *optimal*; the validator decides what is *legal*. They are
kept strictly separate, and every candidate shipment the optimiser emits is re-checked by
`validate_and_plan` before it can reach the simulator. This preserves the property that
makes the validator a usable test oracle: it is never stricter than the simulator.

The forecast is **exact rather than approximate**. The simulator's demand is generated
from a SHA-256-seeded RNG, so the formula is reproducible offline:

```
demand(station, fuel, tick) = max(0, base · hour_factor · region_factor · multiplier · jitter)
```

Measured **60/60 exact match** against live `/v1/demand-history`, and exposed as an
endpoint — `GET /api/forecast/validate` — so a judge can check it themselves.

Two measured facts constrain the forecast, and both contradict the integration guide:

* **`demand_multiplier` is transient.** It reverts to `1.0` when a `demand_spike` event
  resolves. Caching it would make the forecast 80% wrong for exactly the window where it
  matters most, so it is re-read from `/v1/stations` every cycle.
* **Arrival is one tick later than the simulator's own field suggests.** Fuel is credited at
  `created_tick + transit_ticks + 1`, and supply arrivals at `planned_tick + 1`. The
  projection model uses the measured lag, not the advertised one.

### 4.6 The LP is written out, measured, and deliberately not installed

`backend/app/lp_solver.py` contains the full C1–C10 linear formulation — judges score
optimisation depth — and is import-guarded so the backend starts fine without it.

Measured on the baseline network:

| Solver | Service level | Shipments |
| :--- | ---: | ---: |
| greedy | 0.915702 | 3 |
| LP | 0.915702 | 39 |

Identical service level, **13× the shipments** — and every shipment costs an idempotency
key and a slice of the depot's per-tick dispatch cap. The rule was *enable the LP only if
it is measurably better*; it is not, so `pulp` stays out of `requirements.txt` and the image
keeps no native binary. `tests/test_lp_solver.py` pins this measurement, so if a future
change makes the LP win by more than 1%, the test fails and the question is re-asked.

### 4.7 Environment variables


`docker-compose.yml` reads these from the host environment, with the values
below as defaults:

| Variable | Default | Meaning |
| :--- | :--- | :--- |
| `SIMULATION_SPEED` | `0.1` | Wall-clock ticks per second while running. `0.1` = one tick every 10 seconds — the slowest this simulator version allows. |
| `TICK_MINUTES` | `15` | Simulated minutes advanced per tick. |
| `SIMULATOR_START_MODE` | `paused` | `paused` or `running`. |

Override them inline:

```bash
SIMULATION_SPEED=0.1 TICK_MINUTES=15 SIMULATOR_START_MODE=running docker compose up -d
```

> `SIMULATOR_START_MODE=paused` is the default so the test suites can drive
> ticks deterministically via `POST /admin/step`. To watch the console against
> a live world, start the stack as above and then run
> `curl -X POST http://localhost:8000/admin/run`.

The **backend** reads its own settings from the environment too:

| Variable | Default | Meaning |
| :--- | :--- | :--- |
| `BREAKER_FAILURE_THRESHOLD` | `5` | Consecutive *retryable* failures that open the circuit. |
| `BREAKER_COOLDOWN_SECONDS` | `15.0` | How long the breaker waits before probing the upstream. |
| `SCENARIO_PATH` | `backend/scenarios/baseline.yaml` | Which scenario configuration to load. |

A **terminal** failure — a 404, 409 or 422 from the simulator — never counts
toward the threshold. Those are the simulator *answering*, not the simulator
failing, and tripping on them would take the backend down for one bad request.

---

## 5. Files

```
.
├── docker-compose.yml            # simulator-api + redis + client-verifier + backend + frontend
├── README.md
├── docs/
│   ├── BACKEND.md                 # backend API contract, validator rules, measured semantics
│   │                              #   + Part II: optimiser, forecast, anomalies, explanation
│   │                              #   + Part III: observability, resilience, scenario config
│   ├── DASHBOARD.md               # the operator console: architecture, invariant, panels
│   ├── ARCHITECTURE.md            # the as-built diagram (PDF §20 item 6)
│   ├── OBSERVABILITY.md           # the metric catalogue, the health table, the §15 layers
│   ├── RESILIENCE.md              # the breaker's state machine, the degraded-mode contract
│   ├── LOAD_TEST.md               # the workloads, the measured results, their interpretation
│   ├── SCENARIO.md                # the scenario configuration schema
│   ├── DEMO.md                    # the 14-beat live demo script
│   ├── BRIEF.md
│   ├── SYSTEM_DESIGN.md
│   └── system_understanding_and_requirements.md
├── regular_updates/
│   ├── Day_1.md                   # harness design
│   ├── Day_2.md                   # 49-check verification suite
│   ├── Day_3.md                   # operator backend + validator
│   ├── Day_4.md                   # optimiser + forecast + anomalies + explanation
│   ├── Day_5.md                   # operator console + the connection-pool incident
│   └── Day_6.md                   # observability + resilience + load testing
├── scripts/
│   ├── verify-all.sh              # THE one command — four suites, one JSON verdict
│   ├── check_observability.py     # asserts every required metric family is present
│   ├── demo-normal.sh             # demo beats 1–8
│   └── demo-crisis-fault-recovery.sh  # demo beats 9–14
├── loadtest/
│   ├── run.sh                     # picks k6, falls back to the Python harness
│   ├── harness.py                 # stdlib-only fallback runner (same JSON schema)
│   ├── k6/                        # decision.js, dashboard.js, console-mix.js
│   └── results/                   # the committed reports
├── observability/                 # the OPT-IN monitoring overlay (not a default service)
│   ├── compose.monitoring.yml     # Prometheus + Grafana, one command away
│   ├── prometheus.yml             # the scrape config
│   ├── prometheus-alerts.yml      # nine alert rules
│   ├── grafana-dashboard.json     # one row per §15 layer
│   ├── grafana-provisioning/      # datasource + dashboard providers
│   └── README.md
├── verifier/
│   ├── Dockerfile                 # python:3.11-slim, deps first, healthcheck
│   ├── requirements.txt           # fastapi, httpx, pydantic, redis, pytest
│   ├── simulator_client.py        # defensive async client (typed models, SSE, admin)
│   ├── verify_simulator.py        # 49-check end-to-end verification suite
│   └── verify_api.py              # FastAPI sidecar: GET /health, POST /verify
└── backend/
    ├── Dockerfile                 # python:3.11-slim, deps first, healthcheck
    ├── requirements.txt           # fastapi, uvicorn, httpx, pydantic, redis, pytest
    ├── scenarios/
    │   └── baseline.yaml          # the scenario configuration — validated at boot
    ├── app/
    │   ├── main.py                # FastAPI app, lifespan, router wiring
    │   ├── broker.py              # SSE broker: fan-out, watchdog, probe, polling fallback
    │   ├── validator.py           # PRE-FLIGHT VALIDATOR (pure, no I/O)
    │   ├── forecast.py            # demand forecast + projection + stockout risk (pure)
    │   ├── optimizer.py           # multi-tick optimiser, greedy (pure)
    │   ├── lp_solver.py           # optional LP formulation, import-guarded
    │   ├── dispatch.py            # gate_plan + execute_shipments (shared write path)
    │   ├── anomaly.py             # anomaly detectors (pure)
    │   ├── explain.py             # deterministic explanation layer (pure)
    │   ├── llm.py                 # optional decorative prose layer (isolated, off by default)
    │   ├── state.py               # WorldSnapshot + TTL'd cache + ObservationLog
    │   ├── models.py              # request/response schemas
    │   ├── config.py              # env-driven settings
    │   ├── errors.py              # simulator exception -> HTTP mapping
    │   ├── keys.py                # deterministic idempotency keys (+ make_key_at)
    │   ├── journal.py             # write-ahead dispatch journal + dispatch_plans
    │   ├── circuit_breaker.py     # the breaker + the degraded-mode contract
    │   ├── metrics.py             # the Prometheus writer + the HTTP middleware
    │   ├── dlq.py                 # the dead-letter queue
    │   ├── scenario.py            # the scenario loader + the boot-time validator
    │   ├── simulator_client.py    # byte-identical copy of verifier/simulator_client.py
    │   └── routers/
    │       ├── health.py          # /api/health, /api/stream-health
    │       ├── state.py           # /api/state, /api/depots, /api/stations, ...
    │       ├── events.py          # /api/events/recent, /api/events/stream
    │       ├── allocations.py     # /api/plan, /api/allocations, .../cancel, .../explain
    │       ├── optimizer.py       # /api/optimize, .../execute, .../solver, .../llm
    │       ├── forecast.py        # /api/forecast, .../summary, .../{id}, .../validate
    │       ├── anomaly.py         # /api/anomalies, /api/incidents, .../explain
    │       └── observability.py   # /metrics, /api/observability, /api/dlq
    └── tests/
        ├── test_validator.py                      # pure unit tests, no simulator
        ├── test_validator_against_simulator.py   # oracle + randomised differential test
        ├── test_broker.py                         # fan-out, watchdog, reachability probe
        ├── test_api.py                            # HTTP surface
        ├── test_client_parity.py                  # drift guard
        ├── test_forecast.py                       # forecast unit tests + golden fixture oracle
        ├── test_optimizer.py                      # constraint + determinism properties
        ├── test_optimizer_vs_greedy.py            # never worse than the naive baseline
        ├── test_optimizer_against_simulator.py    # the live simulator as oracle
        ├── test_optimizer_api.py                  # optimiser HTTP surface
        ├── test_forecast_api.py                   # forecast HTTP surface + validate oracle
        ├── test_anomaly.py                        # detector properties
        ├── test_anomaly_api.py                    # anomaly HTTP surface
        ├── test_explain.py                        # determinism + "every number is declared"
        ├── test_llm_isolation.py                  # AST import-graph guard
        ├── test_lp_solver.py                      # import guard, fallback, LP-vs-greedy
        ├── test_circuit_breaker.py                # the state machine + degraded mode
        ├── test_metrics.py                        # the exposition format + the metric families
        ├── test_pool_saturation.py                # the semaphore bound is never exceeded
        ├── test_dlq.py                            # a bad frame is quarantined, not raised
        ├── test_scenario_config.py                # the YAML loads, validates, changes nothing
        ├── test_observability_api.py              # the §16 table on /api/health
        └── fixtures/
            └── demand_golden.json                 # 60-row live golden fixture
└── frontend/
    ├── Dockerfile                 # node:22-alpine, two-stage build, no runtime deps
    ├── server.mjs                 # /api proxy (raw byte stream) + static server
    ├── vite.config.ts             # dev proxy + test config
    ├── package.json
    ├── src/
    │   ├── App.tsx                # the focus + drill-down layout
    │   ├── api/
    │   │   ├── types.ts           # hand-written types mirroring the backend
    │   │   └── client.ts          # the only place that talks to the backend
    │   ├── lib/
    │   │   ├── format.ts          # rendering only — never derives
    │   │   ├── derive.ts          # the pure derivations — the only place a number is born
    │   │   ├── useTheme.ts        # the dark/light preference, persisted
    │   │   └── useBackend.ts      # the world hook: SSE (advisory) + polling fallback
    │   ├── components/
    │   │   ├── common.tsx         # Panel, Badge, Dot, Stat, Meter, Empty, ErrorBox
    │   │   ├── StatusBar.tsx      # the slim top bar + the theme toggle
    │   │   ├── ActionCard.tsx     # the hero: the plan, the why, the two-step dispatch
    │   │   ├── DetailTabs.tsx     # the tab strip — one panel at a time
    │   │   ├── Disclosure.tsx     # the reusable expander
    │   │   ├── TelemetryBar.tsx   # StaleBanner (the freshness contract)
    │   │   ├── NetworkPanel.tsx   # DepotPanel, StationPanel, RoutePanel
    │   │   ├── ForecastPanel.tsx  # the merged forecast tab
    │   │   ├── AnomalyPanel.tsx   # AnomalyPanel, IncidentPanel
    │   │   └── SystemPanel.tsx    # the §16 health table + the pool-saturation meter
    │   └── test/
    │       ├── derive.test.ts             # the pure derivations
    │       ├── format.test.ts             # the formatting
    │       ├── panels.test.tsx            # every panel, against real payloads
    │       ├── stream.test.ts             # SSE is advisory; drop degrades; reconnect works
    │       ├── no-invented-numbers.test.tsx  # THE invariant
    │       ├── theme.test.ts              # the toggle, the persistence, the fallbacks
    │       ├── live.test.tsx              # the same, against the running stack
    │       └── fixtures.ts                # real backend payloads, copied verbatim
```

---

## 6. Troubleshooting

| Symptom | Cause / fix |
| :--- | :--- |
| `client-verifier` keeps restarting | The image is stale. Run `docker compose up -d --build client-verifier`. |
| `error while creating mount source path` | A host bind-mount of `./verifier` was used. This setup copies the code into the image instead, so no bind-mount is required. |
| `platform (linux/amd64) does not match ... linux/arm64` | Harmless — the simulator image is amd64 and Docker emulates it. |
| Checks fail with stale tick values | The simulator was left running. `POST /admin/reset` before the suite (the suite does this itself). |
| Port 8000 already in use | Another simulator instance is running. `docker compose down` first. |
| Redis warnings in the log | The cache is optional; the client degrades to cache-less operation and still passes every check. |
| `backend` reports `mode: "polling"` while the simulator is healthy | The simulator is **paused**, so no events flow. Check `sse_reachable` in `/api/stream-health` — if it is `true`, SSE is fine and the world is simply quiet. |
| `backend` tests skip the async cases | `pytest-asyncio` is missing. Run the suite **inside the container**: `docker compose exec -T backend pytest tests/ -q`. |
| `simulator-api` is `healthy` but every request hangs | The simulator's connection pool is exhausted. `docker compose restart simulator-api` clears it; `docs/BACKEND.md` §18 explains the cause and the fix. |
| The console shows "Upstream unreachable" | The backend cannot reach the simulator. Check `curl localhost:8002/api/health` — its `upstream.reachable` field says why. |
| The console's live test times out | The backend is slow or the simulator is wedged. Check `docker compose ps` first. |
| `/api/health` says `degraded` and `circuit.state` is `OPEN` | The breaker tripped. It closes on its own after `BREAKER_COOLDOWN_SECONDS`. If the upstream is genuinely down, check `curl localhost:8000/v1/health`. |
| `POST /api/plan` returns `503 CIRCUIT_OPEN` | **Working as designed.** The write path refuses to plan against a stale world; reads still serve the last known good snapshot with `degraded: true`. See `docs/RESILIENCE.md`. |
| `curl /metrics` returns JSON, not Prometheus text | You asked for `/api/observability` or `/api/metrics`. `/metrics` is the bare path and returns `text/plain; version=0.0.4`. |
| Prometheus shows the backend target `down` | The scrape config expects the service name `backend` on port 8002. Start the overlay with the default compose file so the network name matches: `docker compose -f docker-compose.yml -f observability/compose.monitoring.yml up -d`. |
| `docker compose build` did not change the running container | `build` does not recreate. Always follow it with `--force-recreate`. |
| The load test reports 100% errors | The backend was not running, or the breaker was OPEN. Check `curl localhost:8002/api/health` first. |

---

## 7. The documentation set

| Document | What it answers |
| :--- | :--- |
| [`docs/BACKEND.md`](docs/BACKEND.md) | Every endpoint, the validator rules, the measured simulator semantics, the metrics, the breaker. |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | The as-built diagram — PDF §20 item 6. |
| [`docs/SYSTEM_DESIGN.md`](docs/SYSTEM_DESIGN.md) | The design as built, and an honest list of what is *not* built. |
| [`docs/DASHBOARD.md`](docs/DASHBOARD.md) | The console: its invariant, its architecture, its panels. |
| [`docs/OBSERVABILITY.md`](docs/OBSERVABILITY.md) | The metric catalogue, the §15 layers, the §16 health table. |
| [`docs/RESILIENCE.md`](docs/RESILIENCE.md) | The breaker's state machine, the degraded-mode contract, the measured recovery timeline. |
| [`docs/LOAD_TEST.md`](docs/LOAD_TEST.md) | The workloads, the measured results, and what they say about the system's limits. |
| [`docs/SCENARIO.md`](docs/SCENARIO.md) | The scenario configuration schema and how to make a new one. |
| [`docs/DEMO.md`](docs/DEMO.md) | The 14-beat live demo script. |
| [`docs/BRIEF.md`](docs/BRIEF.md) | What is implemented, what is not, and why — in plain words. |
| [`docs/TESTING.md`](docs/TESTING.md) | The measured behaviour of every injection surface. |
| [`observability/README.md`](observability/README.md) | Bringing up Prometheus + Grafana. |
