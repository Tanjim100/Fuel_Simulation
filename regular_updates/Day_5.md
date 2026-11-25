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

**The screen was redesigned after the first build.** The first version was functionally
complete but genuinely hard to use — ten panels across three columns, all expanded, ten
statistics in the telemetry bar, and the operator's actual question (*what should I do,
and is it safe?*) answered in a corner of a wall of tables. The redesign makes it **one
column, one hero, one panel at a time**: a slim status bar, a single action card
carrying the plan and the dispatch button, and three tabs with everything else one click
away. It also gained a **light and a dark theme**. The layout changed; the arithmetic did
not — every guarantee below survived the redesign unchanged.

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

An **operator console** built as one column, one hero, one panel at a time: a slim status
bar, a single action card carrying the plan and the dispatch button, and three tabs —
**Network**, **Forecast**, **Problems** — with everything else one click away behind a
disclosure. Dark and light themes, toggled from the status bar and remembered across
reloads.

**What the redesign fixed.** The previous screen showed ten panels across three columns, all
expanded, with ten statistics in the telemetry bar and the actual decision buried in a
corner of it. The operator had to read too much to learn too little. The new screen shows
four facts, one decision, and drills down on demand — and the tabs carry counts, so a
problem is never hidden behind a tab nobody opened.

The single centred column also **reflows on a narrow screen**: below 720px the status
bar's brand drops to one line above the facts, and a row's meter drops to its own line
under the name and the figure rather than overflowing. That came free with the
one-column layout — there is only one layout, so there is no second one to keep in sync.

### G. The tick clock was retuned to a readable pace

The first build ran at `SIMULATION_SPEED=8` — **6.4 ticks per second**, so a whole
simulated day passed every 12 real seconds. Correct, but unreadable: the console's
figures changed faster than anyone could read them, and the backend was polling a
moving target 25 times a second.

The clock now runs at **one tick every 10 real seconds**, with each tick advancing the
simulated clock by 15 minutes — a quarter-hour of supply-chain activity every ten
seconds. 10 seconds is the slowest the simulator allows: its runner sleeps
`max(0.05, 1.0 / max(0.1, speed))`, and that inner `max(0.1, ...)` floors the interval
at 10s regardless of how small the speed is set. A 15-second tick is not expressible
in this simulator version.

Two backend timings were calibrated against the old fast world and had to move with it:
`SSE_STALE_AFTER` (20s → 45s, because events now arrive once per 10s tick and a 20s
threshold would have fired on a *healthy* world) and `POLL_INTERVAL` (2s → 5s, half a
tick, because polling twice a second only re-read the same snapshot). The console's
`POLL_MS` moved the same way (4s → 5s). Verified: `suspected_drops: 0`, `mode: sse`.

The redesign changed the **layout, not the arithmetic**. Every guarantee below survived it
unchanged — no invented numbers, SSE advisory, two-click dispatch. Two new named
derivations were added to `lib/derive.ts` (the emptiest tank at a station; demand totalled
by fuel) rather than computed inline, so the invariant test could see them.

```
┌─ Status bar ─────────────────────────────────────────────────────────────┐
│ ● NOMINAL   Service 91.6%   Disrupted 1   Closed 0   Dispatched 12  ☀ ⋯ │
└──────────────────────────────────────────────────────────────────────────┘
┌─ The action ─────────────────────────────────────────────────────────────┐
│ RECOMMENDED                                              [ Recompute ]   │
│ Ship 7000 L of DIESEL from Patiya Depot to Mirpur Fuel Station now.     │
│ patiya → mirpur · DIESEL                                                  │
│ Mirpur DIESEL runs dry in 2 ticks                                         │
│ Planned volume 7,000 L · Projected service 92.0% · Shippable now 1      │
│ [ Dispatch 7,000 L ]   [ Details ]                                       │
└──────────────────────────────────────────────────────────────────────────┘
┌─ Network 2 ──────────┐ ┌─ Forecast 12 ─────────┐ ┌─ Problems 1 ─────────┐
│ Depots  ● Gazipur    │ │ Forecast 10,302 L 82%│ │ Anomalies            │
│ Stations ● Mirpur 40% │ │ DIESEL ████░         │ │  route disrupted     │
│ Routes ⚠ gazipur→mirpur│ │ ▁▂▃▄▅ sparkline     │ │  ▸ Evidence          │
└──────────────────────┘ └───────────────────────┘ └──────────────────────┘
```

Every panel, every one fed by a named backend endpoint: `StatusBar`, `StaleBanner`,
`ActionCard`, `DepotPanel`, `StationPanel`, `RoutePanel`, `ForecastPanel`,
`AnomalyPanel`, `IncidentPanel`.

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

Polling continues underneath at `POLL_MS = 5000`, so a dropped stream degrades to a
slower but still-correct console — and the status bar *says* it has done so rather
than pretending everything is fine.

### E. The dispatch button takes two clicks

```
[ Dispatch 7,000 L ]   →   [ Confirm — dispatch 7,000 L ]   [ Cancel ]
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

The live test (`src/test/live.test.tsx`) renders the whole `App` against the running
backend and checks the rendered console against **the responses the console itself
received** — including the no-invented-numbers guarantee, end to end, against the live
simulator's world. The fixtures in `src/test/fixtures.ts` are **copied verbatim from real
backend responses**, not hand-written approximations; a test that passes against a made-up
payload proves nothing about a payload the backend actually sends.

**A subtlety the live test taught us.** The first version of this test snapshotted the
eight endpoints separately, *then* rendered. Against a live simulator that is a race: the
world ticks between the snapshot and the render, so every figure that moved in between
looked "invented" — the plan's `total_litres`, the tick number, a fill ratio. The console
was innocent; the *sampling* was wrong. The fix is to wrap `fetch` and record the bodies
the console actually fetched, then build the allow-list from exactly those. A figure the
console invented is not in them; a figure that merely moved between ticks still is.

The same class of bug appeared inside the console itself during the redesign. Merging the
forecast and demand panels brought a per-fuel demand **sum** into scope — and the sum
(2,041 L) was a number the backend had never sent. It was a legitimate derivation, but
it was computed inline in a component rather than named in `lib/derive.ts`, so the
invariant test caught it. The fix was the discipline the plan called for: add the
derivation to `derive.ts` **and** to the allow-list. The test was working, not failing.

---

## 4. Real bugs found by running the thing

Five bugs, all on our side, all only visible when the complete stack ran under the
console's load — or, in the case of the last three, when the console was rebuilt and
re-verified from scratch:

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

### Two more, found by the console redesign

The redesign was judged by the same rules as everything else, and it surfaced two more:

3. **The container build was broken, and the local build hid it.** `live.test.tsx` read
   `process.env.LIVE_BACKEND` with no Node types in scope. On a developer machine
   `tsc -b` skipped the check behind a stale `tsconfig.tsbuildinfo`, so the file passed
   locally — and the Docker build, which starts from a clean checkout, failed outright
   with `TS2591: Cannot find name 'process'`. The bug had been in the tree since the
   original commit. Fixed with a local `declare` for the one global the test needs, so
   the browser bundle never depends on Node's type definitions.

4. **The live invariant test was racing the simulator.** It snapshotted the endpoints
   separately and *then* rendered. Because the world ticks between the two, every figure
   that moved in between — the plan's `total_litres`, the tick number, a fill ratio — was
   flagged as "invented" even though the console had rendered it verbatim from a live
   backend response. Fixed by wrapping `fetch` and building the allow-list from the
   responses the console actually received.

A fifth, smaller one of the same family: the live test originally waited for the verdict
badge to read `RECOMMENDED`, `CAUTION` or `BLOCKED`. The backend also emits
`INFORMATIONAL` — and it does so whenever the plan is empty and no shipment is needed,
which is a perfectly normal state for a healthy network. The test failed on a correct
console. It now waits for the hero's **headline**, which the backend always sends once a
plan exists, rather than for a verdict string that depends on the world's state.

Neither is a race in the console. Both are the *same* mistake in a different place: a
check that samples the world at a different moment than the code under test, and so
reports a failure that is really a timing artefact. The lesson generalises — **a test
must observe the same thing the code observed**, or it is measuring the clock.

### A note on the regression tests

The first draft of the "subscription is closed" tests did not actually distinguish the fix
from the bug. Running both variants against an **idle** event loop showed `max_open`,
`overlapped` and `max_delay` reading *identically* — the finaliser gets a slot when the
loop is idle, so the leak is invisible. Only under a **starved** loop does the difference
appear (89 overlapping subscriptions vs 0). The assertions were reworked onto the
`overlapped` invariant, and the *actual* trigger (fetch concurrency) got its own
deterministic test.

A test that cannot fail is worse than no test: it is a green light wired to nothing.

### A sixth bug, found by injecting faults into a healthy system

The simulator exposes two injection surfaces: **domain events** (break a route,
dark a station, spike demand) and **API faults** (make the simulator slow,
unavailable, or stale). Documenting both — and measuring what each one
actually does rather than what its name suggests — turned up a bug that the
49-check verifier does not cover and that no amount of unit testing would have.

**`STATE_MAX_AGE` was 1 second against a `POLL_INTERVAL` of 5 seconds.**

The backend refreshes its world snapshot every 5 seconds. `STATE_MAX_AGE`
decides when the backend calls that snapshot *stale*, and the console renders
a red **"Stale data"** banner when it is. With a 1-second threshold, the
snapshot was flagged stale for four seconds out of every five — so the console
showed a red staleness warning on a **perfectly healthy system, 80% of the
time**.

Sampling `/api/stream-health` ten times in a row confirmed it:

```
banner=True  is_stale True  age 2.96     banner=False  is_stale False  age 0.83
banner=True  is_stale True  age 3.94     banner=True   is_stale True   age 1.78
banner=True  is_stale True  age 4.90     banner=True   is_stale True   age 2.74
```

Fixed to `STATE_MAX_AGE = 15` — three poll intervals of slack. The rule is
now written down in `docker-compose.yml` and `docs/BACKEND.md`:

> **`STATE_MAX_AGE` must be greater than `POLL_INTERVAL`.** A snapshot can only
> ever be as fresh as the poll that captured it.

This is the same class of mistake as the tick-interval retune in §2.G, and it
has the same shape: a threshold calibrated against one clock being applied to
another. A threshold near the tick period reports a dropped stream that never
happened; a threshold below the poll interval reports stale data that never
was.

There is a second reason it mattered. **A permanently visible warning is
indistinguishable from a real one.** After a week of "Stale data" on a healthy
stack, nobody looks up when it is true — so the freshness contract, which is
the console's credibility claim, had quietly become noise.

### What the injections actually do — four measured surprises

Writing the guide meant measuring each injection rather than trusting the
integration guide PDF, and four things did not match the obvious reading:

1. **`start_tick` is inclusive of the *next* tick's pass.** Events start
   before the tick counter increments, so an event injected while the world
   already sits at tick N does **not** activate at tick N — it waits for tick
   N+1. Inject `N-1` to have it live at tick N. Resolution has no such
   off-by-one: the event reverses exactly when the world reaches `end_tick`.

2. **Four of the six domain events raise no anomaly at all.** `station_outage`
   and `depot_constraint` are *capacity* facts, not anomalies — the backend
   enforces them at the gate (`optimizer.py` constraint C7, `validator.py`)
   rather than flagging them. And `shipment_delay` / `supply_shortfall` only
   surface as `SUPPLY_SHORTFALL` when a depot actually **runs dry** before its
   resupply lands — a much narrower condition than the name suggests. The
   honest assertions for these are behavioural, not anomaly-based.

3. **`stream_disconnect` does not tear down a live stream.** It rejects *new*
   connections with a 503 and leaves an established subscription running, so
   the backend's watchdog correctly did **not** fire (`suspected_drops` stayed
   at 0, because the tick advanced *and* events kept arriving). The fault is
   only visible at the next reconnect attempt.

4. **`latency` past 3 seconds is a timeout, not a slow response.** The
   backend's per-request timeout is 3.0s, so `delay_ms: 3000` exercises a
   different code path — and a different failure mode — than `delay_ms: 500`.

The guide is `docs/TESTING.md`. It documents all six domain events, all five
API faults, the timing semantics above, and a full end-to-end pass: break a
route → confirm the anomaly, the incident and the KPI → take the upstream
away → confirm the console degrades to polling and says so → let it recover on
its own → run the live suite against the *injected* world.

That last step is the strongest single check in the project: it renders the
console against a broken world and verifies every number on screen still came
from a backend response. **132 passed** against a world with a disrupted route
and a station in outage.

---

## 5. Verification results

| Suite | Result |
| :--- | :--- |
| `client-verifier` — 49 checks across 6 groups | **49/49 PASSED** (regression gate) |
| `backend` — full suite | **338 passed, 8 skipped** (up from 334 — 4 new broker tests) |
| `backend` — `test_broker.py` alone | **19 passed** |
| `frontend` — full suite | **132 passed, 3 skipped** (the 3 skips are the live tests without `LIVE_BACKEND`) |
| `frontend` — live suite against the running stack | **3/3 passed** |
| `frontend` — Docker build (`npm run build` in-container) | **passed** — the container's `tsc` step type-checks from a clean checkout |
| `docker compose ps` | **5 services, all healthy** |
| Simulator pool under a 120 s window | **stable at 5 connections, zero exhaustion events** |
| Live no-invented-numbers check | **passed** — no rendered number absent from the backend payloads |
| Live no-invented-numbers check **against an injected world** | **passed** — 132 passed with a route disrupted and a station in outage |
| Injection sweep — 6 domain events + 5 API faults | **all 11 measured** against the running container; every claim in `docs/TESTING.md` verified |
| Freshness contract — healthy system | **silent** — `is_stale` false, no banner, across 10 samples |
| Freshness contract — under `unavailable` fault | **loud** — `mode: polling`, `is_stale: true`, banner shown; recovers on its own |

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

> **The Docker build is the honest typecheck.** `npm run build` on a developer machine
> can be masked by a leftover `tsconfig.tsbuildinfo` — and it was: a `process.env`
> reference in the live test had been sitting in the tree since the original commit,
> invisible locally, and it broke the container build outright. The fix is a local
> `declare` for the one global the test needs, so the browser bundle never depends on
> Node's type definitions. `rm -f frontend/tsconfig.tsbuildinfo` before a local build
> if you want the same view the container gets.

---

## 6. Documentation

| Document | Contents |
| :--- | :--- |
| `docs/TESTING.md` | Testing by injection: the six domain events and five API faults, what each one *actually* does (measured, not assumed), the `start_tick` off-by-one, which events raise an anomaly and which do not, why `stream_disconnect` does not drop a live stream, the freshness-threshold rule, and a full end-to-end pass |
| `docs/DASHBOARD.md` | The console end to end: the one rule and the three-layer split, proxy-not-CORS, SSE-is-advisory, the redesigned screen and its three layers, the theme, every panel and its backend source, the two-click dispatch, the test suite, degradation behaviour, and what is deliberately absent |
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
| **Console redesign** | ✅ **Complete — one column, one hero, one panel at a time; light + dark theme** |
| **End-to-end verification + documentation** | ✅ **Complete — 49/49, 338 backend, 132 frontend** |
| Observability (Prometheus / Grafana) & load testing | ✅ **Complete — see `regular_updates/Day_6.md`** |

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
