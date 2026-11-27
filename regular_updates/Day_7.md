# Project Status Update: Fuel Supply Intelligence & Resilience Platform

**Day 7 — Autopilot mode: automatic dispatch with a manual override.**

---

## 1. Executive Summary

Yesterday the platform could *recommend* a fuel dispatch and could *execute*
one — but only when a human pressed the button. `POST /api/optimize/execute`
was the sole write path into the simulator, and the console's ActionCard was
the only trigger.

That is a safe design. It is also a design where the platform's headline
claim — *"fuel is allocated before demand bites"* — depends on somebody
noticing a card and clicking inside the tick window.

Today that dependency is gone. Three things were built:

1. **An autopilot with two modes.** `off` / `live`. It re-plans against the
   live world every tick and dispatches what the gate accepts. It is **off by
   default**, so `docker compose up -d` behaves exactly as it did yesterday.
2. **One merged dispatch surface.** The manual dispatch and the autopilot were
   two separate panels; they are now one — the action card — plus a mode
   switch above it. With the autopilot **off** the card asks you to dispatch
   (`Dispatch needed — 7,000 L`); with it **live** the system dispatches on
   its own and the card shows what it did (`Last dispatch: tick 61 · 4,200 L
   · created 1`), while the button becomes an override (`Dispatch 7,000 L
   anyway`) — because a human must always be able to overrule the system.
3. **Five new metric families, and a new label on an existing one.**
   `backend_decisions_total` now distinguishes `kind="dispatch"` (a person
   decided) from `kind="autopilot"` (the system decided).

**The acceptance check passed.** With the autopilot **live**, the 49-check
verifier suite still passes — 49/49. That is the property that makes the
feature safe to leave running, and it is the check that mattered.

**A real bug was found by running it.** The tick guard refused any tick that
was not strictly greater than the last one dispatched on — the right rule for
a repeat, the wrong rule for a **rewind**. The verifier suite resets the
simulator on every run, so after one reset the autopilot was locked out
permanently: the world had to climb back past the old high-water mark before
a single shipment could move. The guard now tells a repeat from a rewind by
the *direction* of the move and clears itself on a rewind (Bug 33).

The default stack is **unchanged**: `docker compose up -d` still yields exactly
**five healthy services**, and the autopilot boots **off**.

---

## 2. What was built

### 2.1 The autopilot — `backend/app/autopilot.py`

**It is not a new planner and not a new gate.** It calls `optimize()`,
`gate_plan()` and `execute_shipments()` — the exact functions the operator's
button calls — and decides only *when* to call them.

| It is **not** | Because |
| :--- | :--- |
| A new planner | A second planner would be a second set of arithmetic to be wrong. |
| A new gate | The autopilot's dispatch is gated by the same pre-flight validator as a human's. |
| A new write path | It writes through the same journal-first `execute_shipments()`, with the same idempotency-key minting. |
| A scheduler | It runs on the backend's own tick loop, not on a cron. |
| A replacement for the manual button | The button works in both modes. |

`cycle_once()` is the whole policy, in one function — which means the policy
is testable without the loop, and there is exactly one place where *"should
we dispatch?"* is decided.

### 2.2 The ten steps of a cycle

| # | Step | What it stops |
| :--- | :--- | :--- |
| 1 | Mode gate — `OFF` returns immediately. | The single on/off switch. |
| 2 | Breaker gate — `OPEN` **or** `HALF_OPEN` skips. | Writing against a known-bad upstream. |
| 3 | Tick probe — one cheap `client.instance()` read. | Planning against a world the simulator cannot confirm exists. |
| 4 | Tick-regression guard — the tick must be *strictly greater* than the last one dispatched on. | A world reset replaying a tick. |
| 5 | Quarantine — a rejected tick is skipped for two ticks. | Spamming a simulator that will reject it identically. |
| 6 | One dispatch per tick — the shared `dispatch_lock`. | Two concurrent cycles both dispatching. |
| 7 | Fresh snapshot — `broker.refresh_snapshot()`. | Dispatching a stale plan. |
| 8 | Plan + gate — `optimize()` → `gate_plan()`, filtered to the current tick. | Bypassing the validator. |
| 9 | Anomaly escalation → the budget tier. | — (this step *raises* the budget) |
| 10 | Journal-first write — the intent is journalled **before** the network call. | A crash mid-write with no record of the intent. |

Step 2 deserves a note: `HALF_OPEN` is skipped as well as `OPEN`. A
half-open breaker is still *probing* — the probe is about to tell us whether
the upstream recovered at all, so writing during that window is exactly the
wrong moment.

### 2.3 Anomalies escalate the budget; they never gate the dispatch

This was a design decision, and it is the one that most affects what the
autopilot does.

**A detected problem is a reason to move *more* fuel, not a reason to stop
moving it.** If the anomaly pipeline says a station is about to run dry, a
budget calibrated for a calm world is the wrong budget.

| Tier | Condition | Max litres/tick | Max shipments/tick |
| :--- | :--- | :--- | :--- |
| normal | no anomalies | 12,000 | 8 |
| escalated | any anomaly | 24,000 | 8 |

**Where the cap is applied matters.** The cap is applied to the gate's
*accepted* list, not to the planner's input. The plan itself is unchanged —
the optimiser still sees the whole world — and only the *dispatched subset* is
bounded. Bounding the planner's input instead would produce a different plan
for the same world, which is exactly the kind of invisible divergence between
the autopilot's decisions and the operator's that makes a system hard to
reason about.

Only the **pure** detectors are used. `detect_snapshot_anomalies()` is a
function of the snapshot; `AnomalyDetector.observe()` is **never** called by
the autopilot, because it mutates the detector's rolling history. An autopilot
running on its own cadence must not perturb the anomaly pipeline's state.

### 2.4 The three double-dispatch defences

The autopilot writes to the same simulator the 49-check verifier suite asserts
against. Two dispatches on one tick would fill a station's free space twice,
and the simulator's validation order is fixed (`ROUTE_MAX_SHIPMENT →
DEPOT_INVENTORY → DISPATCH_CAPACITY → STATION_FREE_SPACE`) — so a rejection
would flip from `ROUTE_CAPACITY_EXCEEDED` to `DESTINATION_CAPACITY_EXCEEDED`,
and a currently-passing check would start failing.

Three independent mechanisms stand in the way. They are deliberately
redundant, because redundancy is what survives a restart.

| Defence | Mechanism | Survives a restart? |
| :--- | :--- | :--- |
| **The tick guard** | At most one dispatch per simulator tick; a tick already dispatched on is refused. | **No** — the tick memory is in-process. |
| **The idempotency key** | The gate mints `alloc-t{tick}-{depot}-{station}-{fuel}-{seq}-{hash8}`. The simulator returns the *original* allocation for a repeated key, so a repeat is a **REPLAY**, not a second shipment. | **Yes.** |
| **The simulator's per-tick cap** | The simulator counts the allocations created on the current tick, per depot, and refuses to exceed its cap. The optimiser already subtracts that committed volume. | **Yes.** |

**There is deliberately no durable "already dispatched" table.** It would need
its own schema, its own reconciliation, and its own recovery story — to
duplicate what the idempotency key and the simulator's own cap already
guarantee.

**A reset rewinds the tick, and the guard gets out of the way.** The
simulator's `/admin/reset` rewinds the tick counter to zero, so the guard's
memory refers to a timeline that no longer exists. The guard tells the two
cases apart by the **direction** of the move: the *same* tick is a repeat
and is refused; a *lower* tick is a rewind and clears the guard's memory
(including the quarantine) so the autopilot dispatches on the new timeline.

Without that branch the guard becomes a **permanent lockout** — the world
would have to climb back past the old high-water mark before a single
shipment could move again. A reset is routine: the 49-check verifier suite
performs one on every run, so without the fix the autopilot would go
offline for the rest of every verification session.

### 2.5 One lock, two callers

The autopilot and the operator's `POST /api/optimize/execute` share a single
`asyncio.Lock` at `app.state.dispatch_lock`. That makes *"the autopilot and a
manual click cannot interleave"* a **structural** property rather than a
convention.

### 2.6 The HTTP surface — `backend/app/routers/autopilot.py`

| Route | Method | Returns |
| :--- | :--- | :--- |
| `/api/autopilot` | `GET` | The current status. |
| `/api/autopilot/mode` | `POST` | The status after the switch. |
| `/api/autopilot/cycle` | `POST` | The result of one cycle. **Diagnostic.** |

**An unknown mode is a 400 with the domain code `INVALID_AUTOPILOT_MODE`**,
not a silent clamp:

```json
POST /api/autopilot/mode   {"mode": "lve"}
→ 400
  {"error": {"code": "INVALID_AUTOPILOT_MODE",
             "message": "Unknown autopilot mode 'lve'. Valid modes: off, live."}}
```

This is load-bearing. A controller that quietly fell back to `off` would look
*identical* to one that is working — the operator would believe the autopilot
was dispatching when it was not, which is the worst possible failure for a
feature whose whole job is to act without supervision.

`POST /api/autopilot/cycle` is **diagnostic**: the loop is already running, so
this steps the policy by hand. A judge can watch the decision it *would* make
without waiting for a tick.

### 2.7 The mode switch — `frontend/src/components/AutopilotBar.tsx`

The bar sits **directly above the ActionCard** — the card it acts on — and
carries a two-way segmented control (**Manual / Autopilot**), a state row
(mode, running, escalated, last decision as a badge, cycle count), the mode's
plain-language explanation, plus any quarantine window and any error.

The bar carries **no dispatch figures**. Those live on the card, where the
dispatch happens.

**Every number comes from the backend.** Nothing is derived in the console, so
`frontend/src/lib/derive.ts` and the no-invented-numbers allow-list are
**unchanged** by this feature. The test
`frontend/src/test/no-invented-numbers.test.tsx` failing would mean the test
is working — and it now renders the card in *both* modes, so the autopilot's
tick, litres and counts are held to the same contract.

### 2.8 The merged dispatch card — `frontend/src/components/ActionCard.tsx`

The manual dispatch and the autopilot were two separate panels. They are now
**one surface** — the action card — plus the mode switch above it.

**Autopilot off.** The system is not dispatching, so the card tells the
operator there is work to do:

```
[ Dispatch needed — 7,000 L ]
```

Nothing is written until the operator clicks, confirms, and the gate accepts.

**Autopilot live.** The system dispatches on its own, and the card shows what
it dispatched:

```
[ Dispatch 7,000 L anyway ]

Last dispatch: tick 61 · 4,200 L · created 1
```

The button becomes an **override**, because a human must always be able to
overrule the system. The confirmation warns that the autopilot is live and may
also dispatch on the same tick.

**The two dispatch streams never blur.** The card shows the operator's last
dispatch as `Last dispatch — by you` (inside *Details*) and the autopilot's as
the headline `Last dispatch: tick N · N L · created N`. Both are the
backend's own figures. The autopilot's `last_shipments` is cleared by a cycle
that dispatches nothing, so the card shows the most recent *real* dispatch
rather than a stale one.

The two-click confirm is preserved in both modes. A manual dispatch on a tick
the autopilot has already dispatched may be refused by the simulator's
per-tick cap — the warning says so up front rather than letting the operator
discover it as a confusing error.

The button's behaviour when the autopilot is **unavailable** is unchanged too:
dispatch is the one thing the operator came to do, so losing the autopilot's
state must not take the button with it.

### 2.9 The metrics

Five new families, and one new label value on an existing one.

| Metric | Type | Labels | Meaning |
| :--- | :--- | :--- | :--- |
| `backend_autopilot_enabled` | gauge | — | `1` when the mode is not `off`. |
| `backend_autopilot_mode` | gauge | — | `0`=off, `1`=live. |
| `backend_autopilot_cycles_total` | counter | `outcome` | Cycle outcomes. |
| `backend_autopilot_dispatches_total` | counter | `outcome` | `created` / `replayed` / `failed`. |
| `backend_autopilot_litres_total` | counter | — | Cumulative litres dispatched. |

**`backend_decisions_total` gains `kind="autopilot"`.** The existing
`kind="dispatch"` counts decisions *a person* made. The autopilot's decisions
are decisions the operator **did not** make. Folding them together would make
the human-decision counter dishonest — and the brief's §15 asks for *decision
frequency*, which is only meaningful if the two are told apart.

`FALLBACK_ACTIVATIONS` is **not** touched. The autopilot is not a fallback; it
is a mode an operator turns on, not a path the system degrades into.

Every cycle outcome is **pre-registered at zero**. A counter that has never
been touched renders as *absent*, and absent is indistinguishable from
uninstrumented — a dashboard would show a gap rather than a "0".

### 2.10 The configuration

| Variable | Default | Meaning |
| :--- | :--- | :--- |
| `AUTOPILOT_MODE` | `"off"` | The mode the backend boots into. |
| `AUTOPILOT_INTERVAL_SECONDS` | `2.0` | The cycle period. Sized against the 2-second tick. |
| `AUTOPILOT_MAX_LITRES_PER_TICK` | `12000.0` | The normal budget per tick. |
| `AUTOPILOT_MAX_LITRES_PER_TICK_ESCALATED` | `24000.0` | The budget when anomalies are present. |
| `AUTOPILOT_MAX_SHIPMENTS_PER_TICK` | `8` | The hard cap on allocations per cycle. |
| `AUTOPILOT_RESET_QUARANTINE_TICKS` | `2` | How long a rejected tick is quarantined. |

`AUTOPILOT_MODE: "off"` in `docker-compose.yml` is the safety guarantee.
`backend/tests/test_config_defaults.py` pins the code default against the
compose value — the *same* mechanism that caught the `SSE_STALE_AFTER` /
`POLL_INTERVAL` / `STATE_MAX_AGE` drift in Day 6. Because `float("off")`
raises, the mode is pinned through a separate **text** table in that file.

Two invariants are also pinned: the interval must be **at least 1 second** (a
faster cycle would re-plan the same tick repeatedly, burning upstream reads
for a decision that cannot change), and the mode must boot **off** (the
autopilot writes to the same simulator the verifier suite asserts against).

---

## 3. The bugs this phase found

### Bug 28 — The breaker gate never fired in the tests

The autopilot's breaker gate skipped a cycle when the circuit was `OPEN` or
`HALF_OPEN`. In the tests it never fired: `test_an_open_breaker_stops_the_cycle`
asserted `breaker_open` and got `dispatched`.

The cause was a **shape mismatch, not a logic bug**. The controller is given
the **breaker** as its client — the breaker wraps the raw client and the app
stores the breaker at `app.state.client`, so every consumer calls *through*
it. The helper was looking for the breaker on `client._breaker`, and the test
stub kept it on `client.breaker`. Neither name matched, so the helper found
no breaker and returned the safe default, `CLOSED`.

The production code was right and the stub was wrong. But the stub being wrong
was the *real* finding: **the two shapes had drifted, and only the test could
tell.** The stub now answers `status()` the way a breaker does — because in
the app, the client's `status()` *is* the breaker's `status()`.

```python
# The stand-in is breaker-shaped on purpose. In the app,
# ``app.state.client`` *is* the ``CircuitBreaker``.
def status(self):
    return self.breaker.status()
```

### Bug 29 — An unknown mode returned a 200 with an error body

`POST /api/autopilot/mode {"mode": "lve"}` returned **HTTP 200** with an
error envelope in the body.

Two problems. The status was wrong — an unknown mode is a client error, not
a success. And the shape was wrong: the route returned `error_envelope(...)`
as a normal response body rather than raising, so every client that checks
`response.ok` would have treated the failure as a success.

The fix raises `PlanRejected("INVALID_AUTOPILOT_MODE", ..., http_status=400)`
rather than Starlette's `HTTPException`. That choice is deliberate: the
Starlette handler emits the generic code `HTTP_400`, and the contract here is
the *domain* code, because a console that wants to say "that is not a mode"
needs to know **which** rule was broken, not that a request failed.

### Bug 30 — Two test fixtures did not exist where they were used

`test_autopilot_metrics.py` asked for a `client` fixture, and
`test_autopilot_no_double_dispatch.py` asked for a `world` fixture. Both were
defined in *sibling* modules — and **pytest does not export fixtures from
imported modules**. A fixture is visible only to the module that defines it.

The fix defines each one locally, with a comment saying why. The
`world` fixture is a one-liner over `conftest.make_snapshot(tick=70)`.

### Bug 31 — The metrics assertions assumed a private registry

The new metrics tests asserted that the counters read zero. But the registry
is a **process-wide singleton** by design — a metric is defined exactly once
and every component sees the same numbers — so the counters carry over
between tests in the same session. The first test to touch a counter owned
the value; every later test saw a polluted number.

The tests were wrong, not the registry. They now take a **baseline**, act, and
assert on the **change** — which is also the property that actually matters: an
off autopilot must not move the dispatch counter.

### Bug 32 — The tests counted allocations, not dispatches

The double-dispatch tests asserted `len(client.created) == 1` after a
dispatch. But one dispatch legitimately creates **several** allocations — the
plan had three shipments, for DIESEL and two other fuel types.

The assertions were measuring the wrong unit, which would have made them
**vacuous** in the dangerous direction: the right way to express "one dispatch
per tick" is *"the second cycle wrote nothing"*, not *"the first cycle wrote
exactly one thing"*. Every such assertion now compares **growth**:

```python
after_first = len(client.created)
second = await controller.cycle_once()
assert second["decision"] == "tick_regressed"
assert len(client.created) == after_first, "the second cycle wrote again"
```

### Bug 33 — The tick guard became a permanent lockout after a reset

The tick guard refused any tick that was not strictly greater than the last
one it dispatched on. That is the right rule for a **repeat** — the tick is
spent, do not write to it twice — but it is the wrong rule for a **rewind**,
and a rewind is exactly what the simulator's `/admin/reset` does: it rewinds
the tick counter to zero.

The consequence was severe. The autopilot's memory of the last dispatched
tick (216, say) referred to a timeline that no longer existed, so after a
reset the world had to climb back past 216 before a single shipment could
move. In practice:

```
{"last_cycle_tick": 32, "last_decision": "tick_regressed", "last_created": 0}
```

— the tick advanced, the decision never changed, and the autopilot was
offline for the rest of the session. **The 49-check verifier suite performs a
reset on every run**, so this would have taken the autopilot offline during
every verification session — and the failure looked like "the gate is
refusing everything", not like a lockout.

The fix tells the two cases apart by the **direction** of the move:

```python
def _is_rewind(self, now_tick: int) -> bool:
    if self._last_dispatched_tick is None:
        return False
    return now_tick < self._last_dispatched_tick
```

A strictly-lower tick is only possible after a reset, because the world's
tick counter only moves forward within a run. A rewind clears the guard's
memory **and** the quarantine — which is tied to the same timeline — and the
autopilot dispatches on the new one. The same-tick repeat still refuses, so
the defence against a double dispatch is untouched.

The test that encoded the old behaviour — `test_a_reset_tick_is_skipped`,
which asserted that a rewound tick is skipped — was **rewriting the bug as a
specification**. It is now `test_a_reset_tick_clears_the_guard_and_dispatches_again`,
and a second test covers the quarantine being cleared by a rewind. The stub
broker learned to follow the client's tick, because a real reset rewinds the
world everywhere at once, not just the instance read.

Verified live: reset the world at tick 210 while the autopilot was live and
dispatching, and the autopilot went straight back to `dispatched` on the new
timeline instead of locking out.

---

## 4. Measured results

### 4.1 The regression gates

| Gate | Result |
| :--- | :--- |
| Backend tests (in-container) | **515 passed**, 8 skipped, 0 failed |
| Frontend tests | **154 passed**, 3 skipped |
| Observability self-check | **51 assertions, all passed** (was 46; the five autopilot families were added) |
| Verifier suite, autopilot **off** | **49/49 passed** |
| Verifier suite, autopilot **LIVE** | **49/49 passed** |

### 4.2 The acceptance check, in full

```bash
# The shipped default.
curl -s http://localhost:8002/api/autopilot
# -> "mode": "off", "enabled": false

# There is no rehearsal mode — posting one is an error, not a silent no-op.
curl -s -X POST http://localhost:8002/api/autopilot/mode \
  -H 'Content-Type: application/json' -d '{"mode":"dry_run"}'
# -> 400, code INVALID_AUTOPILOT_MODE

# Live.
curl -s -X POST http://localhost:8002/api/autopilot/mode \
  -H 'Content-Type: application/json' -d '{"mode":"live"}'
sleep 8
curl -s http://localhost:8002/api/autopilot
# -> "last_decision": "dispatched", "last_created": > 0, "last_shipments": [...]

# THE KEY CHECK — the verifier must still pass while the autopilot is live.
docker compose exec -T client-verifier python verify_simulator.py \
  --base-url http://simulator-api:8000 --redis-url redis://redis:6379/0
# -> ALL 49 CHECKS PASSED

# The verifier resets the simulator, so this also exercises the rewind path:
# the autopilot must recover rather than lock itself out.
curl -s -X POST http://localhost:8000/admin/run
sleep 20
curl -s http://localhost:8002/api/autopilot
# -> "last_decision": "no_demand" or "gated" on the NEW timeline,
#    NOT "tick_regressed"

# Back to the shipped default.
curl -s -X POST http://localhost:8002/api/autopilot/mode \
  -H 'Content-Type: application/json' -d '{"mode":"off"}'
```

### 4.3 The new test files

| File | What it proves |
| :--- | :--- |
| `backend/tests/test_autopilot.py` | The policy: `OFF` never plans; `LIVE` writes; the breaker gate skips on `OPEN` **and** `HALF_OPEN`; the tick guard skips a repeated tick and clears itself on a rewound one; the quarantine skips a failed tick and is cleared by a rewind; the budget caps truncate; an empty gate result counts `gated`; the status carries what was dispatched and clears it when nothing was. |
| `backend/tests/test_autopilot_api.py` | The three routes; an unknown mode → 400 with the domain code; `dry_run` → 400; a live cycle writes; the root lists the endpoints. |
| `backend/tests/test_autopilot_no_double_dispatch.py` | Two sequential cycles on one tick dispatch once; two **concurrent** cycles dispatch once; the lock is structurally shared; a restart produces a REPLAY, not a second allocation. |
| `backend/tests/test_autopilot_metrics.py` | The five families; every outcome pre-registered at zero; the mode gauge is a number; `kind="autopilot"` is a distinct series; a live cycle moves the dispatch counters, and an off one does not. |
| `frontend/src/test/autopilot.test.tsx` | The bar renders two modes; switching calls the endpoint; the card asks you to dispatch when off, and shows `Dispatch … anyway` plus the autopilot's last dispatch when live; the confirmation warns; **the button still works with no autopilot state at all**. |

---

## 5. The documentation

| Document | What changed |
| :--- | :--- |
| `docs/AUTOPILOT.md` | **New.** The autopilot in full: the two modes, the ten-step cycle, the budget tiers, the three double-dispatch defences, the merged dispatch card, the HTTP surface, the configuration, the metrics, and the commands to see it work. |
| `docs/BACKEND.md` | **New Part IV** — the autopilot's API summary and configuration table. |
| `docs/DASHBOARD.md` | The autopilot bar in the screen layout; the manual override's label table; the new test file and counts. |
| `docs/OBSERVABILITY.md` | The five new families in the intelligence-layer catalogue, with the `kind="autopilot"` explanation and the note that `FALLBACK_ACTIVATIONS` is deliberately untouched. |
| `README.md` | §5.14 (the feature), §6.6 (the commands), the API-surface table, the configuration table, the requirements-traceability row, and the corrected counts (45 routes, 34 metric families, 27 required families, 154 frontend tests). |
| `scripts/check_observability.py` | The five new families added to the required-families checklist — **51 assertions, up from 46**. |

---

## 6. What was deliberately *not* changed

| Component | Why |
| :--- | :--- |
| `backend/app/simulator_client.py` | It must stay byte-identical to `verifier/simulator_client.py` (SHA256 drift guard). The autopilot only *calls* it. |
| `backend/app/optimizer.py` | The planner is reused unchanged. |
| `backend/app/dispatch.py` | The gate and the write path are reused unchanged. |
| `backend/app/anomaly.py` | Only the *pure* detector is used; `observe()` is never called. |
| `backend/app/circuit_breaker.py` | Reused, not modified. |
| `frontend/src/lib/derive.ts` | The console renders backend numbers only, so the no-invented-numbers allow-list is unchanged. |

---

## 7. Current position

| Requirement | Status |
| :--- | :--- |
| Automatic dispatch when demand exists | **Done** — `live` mode, one dispatch per tick, against a fresh snapshot. |
| Problems escalate the response | **Done** — anomalies raise the budget from 12,000 L/tick to 24,000 L/tick. |
| A manual dispatch button while the autopilot is off | **Done** — and it survives into `live`, labelled `Dispatch … anyway`. |
| The default stack is unchanged | **Done** — `AUTOPILOT_MODE: "off"`, pinned by a test. |
| The verifier still passes with the autopilot live | **Done** — 49/49. |
| The autopilot survives a simulator reset | **Done** — a rewind clears the tick guard instead of locking the autopilot out (Bug 33). |

**The headline:** the platform can now dispatch on its own, and the proof that
it is safe to leave running is that the 49-check conformance suite still
passes while it is doing so.

---

## 8. Immediate next steps

1. **A demo beat for the autopilot.** `docs/DEMO.md` has fourteen beats;
   the autopilot deserves one — `Manual` (the card asks you to dispatch) →
   `Autopilot` (watch it dispatch on its own, and the card show what it
   dispatched) → the manual button in autopilot mode, with its warning → the
   verifier suite passing throughout.
2. **A Grafana row.** The five autopilot families are exposed but not yet on
   the dashboard. A row showing the mode, the cycle rate, and the
   created/replayed/failed split would make the autopilot's behaviour legible
   without reading the logs.
3. **The load test, with the autopilot live.** The autopilot adds a plan-and-
   dispatch cycle every two seconds on top of the console's polling. The
   current measured numbers were taken with it off, which is the shipped
   configuration — but the interaction is worth measuring.

---

## 9. Continuous integration

Unchanged from Day 6: `scripts/verify-all.sh` is the single entry point, and
it runs the verifier suite, the backend tests, the frontend tests, the
observability self-check and the load test, emitting one JSON object. The CI
workflow runs the same command a human runs.

The autopilot added **38 new backend tests** and **15 new frontend tests** to
that harness, and **five new assertions** to the observability self-check.
