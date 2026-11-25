# Project Status Update: Fuel Supply Intelligence & Resilience Platform
**Event:** BUP CSE FEST 2026 — Final Hackathon
**Prepared For:** Senior / Team Lead
**Status:** Operator Console (Phase 3) — Live-Verified, End-to-End Regression-Gated

---

## 1. Executive Summary

Day 4 delivered the **optimiser** — what is *optimal* to dispatch — together with the
intelligence layer (forecast, anomaly detection, explanation). Day 5 delivers the
**operator console**: the fifth service, the screen an operator actually uses, and the
proof that the whole stack works end to end against the live simulator.

The headline result is a **fifth service** in the compose stack:

```
browser ──▶ frontend (:8003) ──▶ backend (:8002) ──▶ simulator-api (:8000)
              │                       │
              └───── /api/events/stream (SSE) ◀──┘
```

The console is not a "dashboard" in the decorative sense. It is bound by one rule, which
is **executable rather than aspirational**:

> **Every number on screen is a number the backend sent, or a documented pure derivation
> of numbers the backend sent.**

`src/test/no-invented-numbers.test.tsx` scrapes every leaf element out of the rendered
DOM, extracts every number, and checks each one against an allow-list computed from the
*same* derivation functions the components use. A number can only appear on screen if the
backend sent it, or if a named function in `src/lib/derive.ts` produced it. The live
variant of that same test runs against the running stack.

The second headline result is a **production-grade incident**, found only by running the
complete stack under the console's load. The simulator's SQLAlchemy connection pool
wedged completely — every endpoint except `/v1/health` blocked for 30 seconds, while
Docker still reported the container as `healthy`. Two independent bugs on *our* side were
responsible, and both are now fixed with deterministic releases and regression tests. The
simulator itself was **not modified** — the 49-check harness depends on it being exactly as
delivered.

---

## 2. Milestones Completed Till Now

### A. The console (`frontend/`)

A **three-column operator console**, read left to right: **the network**, **the decision**,
**the intelligence**.

```
┌─ Telemetry bar ──────────────────────────────────────────────────────────┐
│ tick 42 · service 91.6% · disrupted 1 · closed 0 · mode SSE · 2s old     │
└──────────────────────────────────────────────────────────────────────────┘
┌─ Network ──────────────┐ ┌─ Decision ──────────────┐ ┌─ Intelligence ─────┐
│ Depots                 │ │ Recommended plan        │ │ Forecast            │
│  Gazipur Depot  ●      │ │ Ship 7,000 L DIESEL     │ │  10,302 L  82%      │
│ Stations               │ │  Gazipur → Mirpur       │ │  ▁▂▃▄▅ sparkline   │
│  Mirpur Fuel Station   │ │                         │ │ Demand by fuel      │
│  ×1.8 demand spike     │ │ total_litres  7,000     │ │  DIESEL  ████░      │
│ Routes                 │ │ service_level  0.92     │ │  PETROL  ███░░      │
│  gazipur → mirpur  ⚠   │ │                         │ │ Anomalies           │
└─────────────────────────┘ │ [Dispatch current-tick] │ │ Incidents           │
                           └─────────────────────────┘ └─────────────────────┘
```

Ten panels, every one fed by a named backend endpoint: `TelemetryBar`, `StaleBanner`,
`DepotPanel`, `StationPanel`, `RoutePanel`, `DecisionPanel`, `ForecastPanel`,
`DemandPanel`, `AnomalyPanel`, `IncidentPanel`.

### B. The three-layer split — the structural guarantee

The no-invented-numbers rule is only enforceable if there is exactly **one** place where
a number can be born. So the code is split into three layers:

| Layer | File | May it invent a number? |
| :--- | :--- | :--- |
| Transport | `src/api/client.ts` | **Never** — it only moves payloads |
| Derivation | `src/lib/derive.ts` | **Only** by pure function of backend numbers |
| Rendering | `src/lib/format.ts` | **Never** — it only formats |

`format.ts` renders; it never derives. `derive.ts` derives; it never formats. Keeping
them apart is what makes the test's allow-list finite and reviewable.

**Unknown is not zero.** A station whose capacity the backend did not report cannot be
called healthy:

```ts
fillRatio(5000, 0)  // => 0, not Infinity
serviceLevel(null)  // => 'warn', never 'ok'
```

An unknown reads as empty and is coloured as a warning, never as fine. Absence of data is
not evidence of health.

### C. A proxy, not CORS (`frontend/server.mjs`)

The browser talks to exactly one origin: the console's own server. That server proxies
`/api/*` to the backend as a **raw byte stream** — no buffering, no rewriting.

```js
const proxy = http.createProxyServer({ target: BACKEND_ORIGIN })
```

One decision removes an entire class of problem:

* **No CORS configuration** — the browser never makes a cross-origin request.
* **The backend's internal hostname never reaches the browser.** `backend:8002` is a
  compose-network name; exposing it would leak the topology.
* **SSE stays live.** A buffering proxy would batch the event stream and the console
  would look frozen while the backend was perfectly healthy. The proxy sets
  `Accept-Encoding: identity` and pipes the bytes straight through.

### D. SSE is advisory, never world state

The console treats `/api/events/stream` as a **nudge**. An event triggers a REST re-read;
the event payload is never rendered as truth. This mirrors the backend's own contract
exactly — REST is the truth, SSE is a nudge — and it is what makes the console correct
under a silent stream drop:

```ts
source.onmessage = () => { setStreamState('live'); refreshRef.current() }
source.onerror  = () => { setStreamState('polling'); source.close(); /* reconnect in 3s */ }
```

Polling continues underneath at `POLL_MS = 4000`, so a dropped stream degrades to a
slower but still-correct console — and the telemetry bar *says* it has done so rather
than pretending everything is fine.

### E. The dispatch button takes two clicks

```
[ Dispatch current-tick shipments ]   →  [ Confirm dispatch ]  [ Cancel ]
```

The first click **arms** the action and dispatches nothing. The second confirms. A fuel
dispatch is not undoable — the simulator records it, the journal records it, and the
tick's dispatch cap is consumed. So the console makes the operator say it twice.

The plan is also split into what is written now and what is advisory:
`shipments_now` (gated and written on confirmation) and `shipments_later` (future ticks,
rendered under an explicit *"not written now"* heading — a plan for a future tick is a
forecast, not an order).

### F. A two-stage image with no runtime dependencies

```
frontend/Dockerfile
├── stage 1: node:22-alpine  →  npm ci  →  tsc -b --force  →  vite build
└── stage 2: node:22-alpine  →  copy dist/ + server.mjs  →  node server.mjs
```

The runtime image contains no `node_modules` beyond Node itself. There is no package to
audit in production and no dependency to keep patched.

---

## 3. Key Engineering Findings — measured, not assumed

### Finding 1 — A connection pool is a one-way ratchet under a starved event loop

The simulator's pool is `pool_size=5` + `max_overflow=10` = **15 connections**, and
**one simulator request holds two** of them: the fault middleware opens a session, and
`Depends(get_db)` opens a second. Our broker's snapshot fetch fired **eight** concurrent
reads — demanding 16 connections from a 15-connection pool, or 17 with the SSE
subscription open.

That is not by itself a permanent wedge; SQLAlchemy would block and eventually recover.
The permanent wedge came from a second bug, and the two compound:

* The broker's SSE loop did `break` out of `async for` over an async generator. **Breaking
  out of an `async for` does not close the generator.** Its `finally` — where the HTTP
  response closes, and hence where the simulator's DB connection is released — is deferred
  to CPython's async-generator finaliser, which runs via the loop's finaliser hook.
* That finaliser only gets a slot when the loop is otherwise idle. Under load — which is
  exactly the condition under which the pool is already saturated — it **never** runs.

So: pool filled → requests block for 30 s → finalisers never get a slot → pool never
drains. The pool became a one-way ratchet. Docker kept reporting the container as
`healthy` because `/v1/health` was served while every other endpoint blocked.

**The lesson:** a resource must be released **deterministically**, not deferred to a
finaliser. `await subscription.aclose()` in a `finally` is not stylistic hygiene — it is
the difference between a pool that drains and one that does not.

### Finding 2 — The simulator's own `actual_arrival_tick` is one tick early

Carried forward from Day 4 because the console depends on it: the simulator stamps
`actual_arrival_tick = 2` while the fuel is only credited at **tick 3**. The console's
sparkline plots the backend's `projected_inventory` series, which already accounts for
the measured `+1` arrival lag — so the screen and the world agree. It is worth stating
explicitly because a one-tick error here would make the console's forecast panel
systematically wrong.

### Finding 3 — The console's own numbers are verifiable against the live stack

The live test (`src/test/live.test.tsx`) re-fetches all eight live payloads, renders the
whole `App`, and checks the rendered console against them — including the
no-invented-numbers guarantee, end to end, against the live simulator's world. The
fixtures in `src/test/fixtures.ts` are **copied verbatim from real backend responses**,
not hand-written approximations; a test that passes against a made-up payload proves
nothing about a payload the backend actually sends.

---

## 4. Real bugs found by running the thing

Two bugs, both on our side, both only visible when the complete stack ran under the
console's load:

1. **The snapshot fetch demanded more connections than the pool had.** `_fetch_snapshot`
   fired 8 concurrent reads against a 15-connection pool where each read costs 2. Fixed
   with `FETCH_CONCURRENCY` (default 4) bounding the **individual** reads — the first
   attempt put the semaphore around the whole fetch, which bounded nothing, and the
   regression test caught it (`max_in_flight` was 6 with a limit of 4).

   The subtle part: the semaphore must be entered around a **coroutine factory**, not a
   coroutine object. A coroutine object is created eagerly, and if the semaphore blocks,
   it is never awaited — Python emits `RuntimeWarning: coroutine ... was never awaited`
   and the request silently vanishes.

2. **The SSE subscription was closed lazily, not deterministically.** Fixed by closing the
   subscription explicitly in a `finally`, with a `_close_subscription` helper that
   swallows the specific `RuntimeError: coroutine ignored GeneratorExit` while still
   re-raising `CancelledError`.

A third, smaller one: the async generator is abandoned rather than closed, so its
`finally` never ran — **and** the semaphore was constructed at `__init__` time, before
the event loop existed (`RuntimeError: no running event loop`). Both are now handled.

### A note on the regression tests

The first draft of the "subscription is closed" tests did not actually distinguish the fix
from the bug. Running both variants against an **idle** event loop showed `max_open`,
`overlapped` and `max_delay` reading *identically* — the finaliser gets a slot when the
loop is idle, so the leak is invisible. Only under a **starved** loop does the difference
appear (89 overlapping subscriptions vs 0). The assertions were reworked onto the
`overlapped` invariant, and the *actual* trigger (fetch concurrency) got its own
deterministic test.

A test that cannot fail is worse than no test: it is a green light wired to nothing.

---

## 5. Verification results

| Suite | Result |
| :--- | :--- |
| `client-verifier` — 49 checks across 6 groups | **49/49 PASSED** (regression gate) |
| `backend` — full suite | **338 passed, 8 skipped** (up from 334 — 4 new broker tests) |
| `backend` — `test_broker.py` alone | **19 passed** |
| `frontend` — full suite | **116 passed, 3 skipped** (the 3 skips are the live tests without `LIVE_BACKEND`) |
| `frontend` — live suite against the running stack | **3/3 passed** |
| `docker compose ps` | **5 services, all healthy** |
| Simulator pool under a 120 s window | **stable at 5 connections, zero exhaustion events** |
| Live no-invented-numbers check | **passed** — no rendered number absent from the backend payloads |

The live console check is run with:

```bash
cd frontend
LIVE_BACKEND=http://localhost:8002 npm test
```

> **Always run the backend suite in-container.** The host Python (3.12) lacks
> `pytest-asyncio`, so `asyncio_mode = auto` is ignored and every `async def` test is
> **silently skipped** — the run reports green while skipping the broker and
> simulator-oracle tests entirely. Check the summary line's skipped count before trusting
> a local run.

> **Also note:** `docker compose build` does **not** recreate the running container. A
> rebuilt-but-not-recreated backend keeps serving the old code, and new tests appear
> "deselected". Always follow with
> `docker compose up -d --force-recreate <service>`.

---

## 6. Documentation

| Document | Contents |
| :--- | :--- |
| `docs/DASHBOARD.md` | The console end to end: the one rule and the three-layer split, proxy-not-CORS, SSE-is-advisory, every panel and its backend source, the two-click dispatch, the test suite, degradation behaviour, and what is deliberately absent |
| `docs/BACKEND.md` | Part I: API contract, validator rules, measured simulator semantics, broker design. Part II: the optimiser, forecast, anomalies, explanation, optional LLM. **§18: the connection-pool incident** — evidence, both causes, the fixes, the regression tests, and what was *not* changed |
| `README.md` | Runbook for all **five** services, design notes, troubleshooting (including simulator-pool exhaustion, console "Upstream unreachable", console live-test timeout) |
| `frontend/src/lib/derive.ts` | The pure derivations — the only place a number may be born |
| `frontend/src/test/no-invented-numbers.test.tsx` | The central invariant, executable |

---

## 7. Current Position Vs. the Phase Plan

| Target | Status |
| :--- | :--- |
| Backend service in the compose stack | ✅ Complete, 5 services healthy |
| SSE broker with drop detection + polling fallback | ✅ Complete, fault-injection rehearsal passed |
| `/api/plan` with inspectable reasoning | ✅ Complete |
| Pre-flight validator mirroring the simulator | ✅ Complete, proven by differential test |
| Idempotent dispatch + write-ahead journal | ✅ Complete |
| Optimisation engine (greedy + optional LP) | ✅ Complete — proven against the live simulator |
| Demand forecasting | ✅ Complete — exact, not approximate (60/60) |
| Anomaly detection | ✅ Complete — six detectors + bounded observation log |
| Anomaly-to-explanation layer | ✅ Complete — deterministic and auditable; LLM isolated and off |
| **Operator dashboard frontend** | ✅ **Complete — 5th service, live-verified, no-invented-numbers enforced** |
| **End-to-end verification + documentation** | ✅ **Complete — 49/49, 338 backend, 116 frontend** |
| Observability (Prometheus / Grafana) & load testing | ⬜ Not started |

---

## 8. Targets To Fulfil

### Priority 1 — Observability & Resilience

* **Circuit breaker** wrapping simulator calls; degraded mode served from cached state.
* **Prometheus metrics + Grafana dashboards** — request rate, p95 latency, error rate,
  fallback activations, broker mode, probe failures, **and simulator pool saturation**
  (the metric that would have made today's incident visible in seconds rather than hours).
* **Locust/k6 load test** on the decision endpoints; document p50/p95/p99 latency. The
  console's polling and SSE load should be included — it is the thing that exposed the
  pool bug.
* **DLQ** for malformed payloads; graceful worker crash recovery.

### Priority 2 — Submission Readiness

* Harness-compatible entry point with deterministic, machine-parseable JSON output.
* Scenario configuration via documented structured config (no hardcoded values).
* Demo narrative: **normal operations → crisis → injected fault → recovery**. The
  fault-injection rehearsal and the connection-pool incident are both strong raw
  material — the second one is a *real* incident story, found by running the thing.

---

## 9. Immediate Next Steps

1. Add the Prometheus metrics endpoint and the circuit breaker, then the load test.
2. Instrument simulator pool saturation explicitly — it is the missing early-warning
   signal for the failure mode we just fixed.
3. Record the demo narrative end to end against the live stack.

> **Note:** the 49-check harness, the validator oracle, the optimiser oracle and the
> console's no-invented-numbers test are reusable as a **regression suite** for every
> subsequent change — run all three after each commit to guarantee no integration
> contract has been broken.
