# The autopilot

The autopilot dispatches fuel without an operator pressing the button.

---

## 1. What it is

The platform's manual dispatch path is safe but *reactive*. An operator has
to notice a card, read it, and click **Dispatch** inside the tick window.
That is a reasonable design for a system under supervision and a poor design
for one whose headline claim is *"fuel is allocated before demand bites"* —
because that claim depends on a human noticing.

The autopilot makes the platform *proactive*. Once it is on, every tick it
re-plans against the live world and dispatches what the gate accepts.

### What it is not

This matters more than what it is, so it comes first.

| It is **not** | Because |
| :--- | :--- |
| A new planner | It calls `optimize()` — the exact function the operator's button calls. A second planner would be a second set of arithmetic to be wrong. |
| A new gate | It calls `gate_plan()` — the same pre-flight validator. The autopilot's dispatch is gated by the same rules as a human's. |
| A new write path | It writes through `execute_shipments()` — the same journal-first function, with the same idempotency-key minting. |
| A scheduler | It runs on the backend's own tick loop, not on a cron. There is no second heartbeat to reason about. |
| A replacement for the manual button | The button works in both modes. A human must always be able to overrule the system. |

**The autopilot is *when* to call the planner, not *how* to plan.**

---

## 2. The two modes

| Mode | Plans? | Gates? | Writes? | Meaning |
| :--- | :--- | :--- | :--- | :--- |
| `off` | no | no | no | Today's behaviour. The console tells the operator when there is something to dispatch, and an operator dispatches it by hand. |
| `live` | yes | yes | yes | It dispatches, once per tick, and the console shows what it dispatched. |

**`off` is the shipped default.** `docker compose up -d` behaves exactly as
it did before this feature existed — byte-for-byte. That is what makes the
feature safe to exist: the 49-check verifier suite is never at risk unless
a human deliberately turns the autopilot on.

### There is no rehearsal mode

There was a third mode, `dry_run`, which planned and gated but wrote
nothing. It is gone, deliberately.

A mode that plans without writing needs its own status field, its own metric
outcome, its own console treatment, its own API branch and its own tests —
and it buys nothing that `live` cannot show, because `live`'s status already
reports the plan, the gate's verdict and the resulting allocations. The
console's manual mode shows the *plan* on the card; the autopilot's live mode
shows the *result*. Between them, every figure a rehearsal mode would have
displayed is already on screen.

Posting `{"mode": "dry_run"}` now returns **400 `INVALID_AUTOPILOT_MODE`**,
so an old console gets a clear error rather than a silent no-op.

---

## 3. The cycle

`cycle_once()` is the whole policy, in one function. Keeping it in one place
means the policy is testable without the loop, and there is exactly one
place where "should we dispatch?" is decided.

| # | Step | What it stops |
| :--- | :--- | :--- |
| 1 | **Mode gate** — `OFF` returns immediately. | The single on/off switch. |
| 2 | **Breaker gate** — `OPEN` **or** `HALF_OPEN` skips the cycle. | Writing against a known-bad upstream. `HALF_OPEN` means the breaker is still *probing* — the probe is about to tell us whether the upstream recovered at all, so writing now is exactly the wrong moment. |
| 3 | **Tick probe** — one cheap `client.instance()` read. | Planning and writing against a world the simulator cannot confirm exists. |
| 4 | **Tick-regression guard** — a tick already dispatched on is refused; a tick that went *backwards* (a world reset) clears the guard instead. | Dispatching twice on one tick. A rewind must **not** lock the autopilot out — see §5. |
| 5 | **Quarantine** — a tick whose dispatch was rejected is skipped for `autopilot_reset_quarantine_ticks` ticks. | Spamming a simulator that will reject the identical request every two seconds. |
| 6 | **One dispatch per tick** — the shared `dispatch_lock`, plus the last-dispatched-tick record. | Two concurrent cycles both dispatching on the same tick. |
| 7 | **Fresh snapshot** — `broker.refresh_snapshot()`. | Dispatching a plan computed against a stale world. A stale plan is a wrong write. |
| 8 | **Plan + gate** — `optimize()` → `gate_plan()`, filtered to the current tick. | Bypassing the pre-flight validator. The autopilot gets no privileges the operator does not. |
| 9 | **Anomaly escalation → budget tier.** | — (this step *raises* the budget, it never lowers it — see §4). |
| 10 | **Journal-first write** — the intent is journalled *before* the network call, then `execute_shipments()` writes. | A crash mid-write with no record of what was intended. |

### The order of 9 and 10

The journal write happens **before** the simulator call, not after. That is
`dispatch.py`'s existing discipline and the autopilot inherits it rather than
reimplementing it: the intent is durable before the write, so a crash halfway
through is recoverable by the startup replay in `main.py`.

---

## 4. Anomalies escalate the budget; they never gate the dispatch

This was a design decision, and it is the one that most affects what the
autopilot does in practice.

**A detected problem is a reason to move more fuel, not a reason to stop
moving it.** If the anomaly pipeline says a station is about to run dry,
then a budget that was calibrated for a calm world is the wrong budget. So
anomalies raise the ceiling:

| Tier | Condition | Max litres/tick | Max shipments/tick |
| :--- | :--- | :--- | :--- |
| **normal** | no anomalies | `autopilot_max_litres_per_tick` (12,000) | `autopilot_max_shipments_per_tick` (8) |
| **escalated** | any anomaly | `autopilot_max_litres_per_tick_escalated` (24,000) | `autopilot_max_shipments_per_tick` (8) |

Only the **pure** detectors are used: `detect_snapshot_anomalies()` is a
function of the snapshot. `AnomalyDetector.observe()` is *never* called by the
autopilot, because it mutates the detector's rolling history — an autopilot
running on its own cadence must not perturb the anomaly pipeline's state.

### Where the cap is applied

The cap is applied to the gate's **accepted** list, not to the planner's
input. The plan itself is unchanged — the optimiser still sees the whole
world — and only the *dispatched subset* is bounded.

Bounding the planner's input instead would produce a different plan for the
same world. That is precisely the kind of invisible divergence between the
autopilot's decisions and the operator's that makes a system hard to reason
about: the two would disagree about *why* a shipment was or was not sent.

---

## 5. The three double-dispatch defences

The autopilot writes to the same simulator that the 49-check verifier suite
asserts against. Two dispatches on one tick would fill a station's free space
twice, and the simulator's validation order is fixed
(`ROUTE_MAX_SHIPMENT → DEPOT_INVENTORY → DISPATCH_CAPACITY →
STATION_FREE_SPACE`) — so a rejection would flip from
`ROUTE_CAPACITY_EXCEEDED` to `DESTINATION_CAPACITY_EXCEEDED`, and a
currently-passing check would start failing.

Three independent mechanisms stand in the way. They are deliberately
redundant, because redundancy is what makes the property survive a
restart.

| Defence | Mechanism | Survives a backend restart? |
| :--- | :--- | :--- |
| **The tick guard** | The autopilot dispatches at most once per simulator tick. A tick it has already dispatched on is refused. | **No** — the tick memory is in-process. |
| **The idempotency key** | The gate mints a deterministic key per shipment: `alloc-t{tick}-{depot}-{station}-{fuel}-{seq}-{hash8}`. The simulator returns the *original* allocation for a repeated key, so a repeat is a **REPLAY**, not a second shipment. | **Yes.** |
| **The simulator's per-tick cap** | The simulator counts the allocations created on the current tick, per depot, and refuses to exceed its cap. The optimiser already subtracts that committed volume, so the autopilot's plan respects it too. | **Yes.** |

### There is deliberately no "already dispatched" table

A durable table of dispatched ticks would need its own schema, its own
reconciliation, and its own recovery story — to duplicate what the
idempotency key and the simulator's own cap already guarantee. It was left
out on purpose.

### A reset rewinds the tick, and the guard gets out of the way

The simulator's `/admin/reset` rewinds the tick counter to zero. The tick
guard remembers the last tick it dispatched on, so after a reset that
memory refers to a timeline that no longer exists.

The guard tells the two cases apart by the **direction** of the move:

| The world says | What it means | What the autopilot does |
| :--- | :--- | :--- |
| the **same** tick | the tick is spent — a repeat | refuses the tick (`tick_regressed`) |
| a **lower** tick | the world was reset — a rewind | clears the guard's memory and dispatches on the new timeline |
| a **higher** tick | a fresh tick | dispatches |

Without the rewind branch the guard becomes a **permanent lockout**: the
world would have to climb back past the old high-water mark before a single
shipment could move again. A reset is a routine operator action — the
49-check verifier suite performs one on every run — so a lockout would take
the autopilot offline for the rest of the session.

The rewind also clears the **quarantine**, which is tied to the same
timeline: a tick whose dispatch was rejected cannot reject the new
timeline's first tick.

### One lock, two callers

The autopilot and the operator's `POST /api/optimize/execute` share a single
`asyncio.Lock` at `app.state.dispatch_lock`. That makes *"the autopilot and a
manual click cannot interleave"* a **structural** property rather than a
convention.

---

## 6. The console: one surface, two modes

The manual dispatch and the autopilot were two separate panels. They are now
**one surface** — the Action Card — plus a mode switch above it.

### The mode switch

`AutopilotBar.tsx` is the switch: **Manual** / **Autopilot**. It carries the
mode, the last decision, the cycle count, and any quarantine or error. It
carries no dispatch figures — those live on the card, where the dispatch
happens.

### The card

The card knows the mode and speaks for both.

**Autopilot off.** The system is not dispatching, so the card tells the
operator there is work to do:

```
[ Dispatch needed — 7,000 L ]
```

Nothing is written until the operator clicks, confirms, and the gate accepts.
The confirm text is the ordinary one.

**Autopilot live.** The system dispatches on its own, and the card shows what
it dispatched:

```
[ Dispatch 4,200 L anyway ]

Last dispatch: tick 61 · 4,200 L · created 1
```

The button becomes an **override**, because a human must always be able to
overrule an automatic decision. The confirmation warns that the autopilot is
live and may also dispatch on the same tick.

### The two dispatch streams never blur

The card shows two dispatches, and they are labelled apart:

| Block | Whose dispatch |
| :--- | :--- |
| `Last dispatch — by you` (in *Details*) | the operator's last manual dispatch |
| `Last dispatch: tick 61 · 4,200 L · created 1` (headline) | the autopilot's last dispatch |

The headline line's figures come from the autopilot's status:
`last_cycle_tick`, `last_litres`, `last_created` and `last_shipments` — all
the backend's own. The `Details` block adds the autopilot's per-shipment
breakdown, again straight from the simulator's allocation records.

The autopilot's `last_shipments` is **cleared** by a cycle that dispatches
nothing, so the card shows the most recent *real* dispatch rather than a
stale one from several ticks ago.

### Every figure is the backend's

Nothing on the card is derived. The `no-invented-numbers` test renders the
card in both modes and asserts that every number it shows came from the
backend — including the autopilot's tick, litres and counts.

---

## 7. The HTTP surface

Three routes, shaped like the existing `GET /api/optimize/solver` and
`GET /api/optimize/llm` endpoints: the API never overclaims.

| Route | Method | Returns |
| :--- | :--- | :--- |
| `/api/autopilot` | `GET` | The current status. |
| `/api/autopilot/mode` | `POST` | The status after the switch. |
| `/api/autopilot/cycle` | `POST` | The result of one cycle. |

### An unknown mode is a 400, not a silent clamp

```json
POST /api/autopilot/mode
{"mode": "lve"}
```

```json
{
  "error": {
    "code": "INVALID_AUTOPILOT_MODE",
    "message": "Unknown autopilot mode 'lve'. Valid modes: off, live."
  }
}
```

This is load-bearing. A controller that quietly fell back to `off` would look
*identical* to one that is working — the operator would believe the autopilot
was dispatching when it was not, which is the worst possible failure for a
feature whose whole job is to act without supervision.

The error carries the domain code `INVALID_AUTOPILOT_MODE`, not the generic
`HTTP_400` — which is why the route raises `PlanRejected` rather than
Starlette's `HTTPException`.

### `POST /api/autopilot/cycle` is diagnostic

The loop is already running. This endpoint steps the policy **by hand**, so a
judge can watch the decision it *would* make without waiting for a tick. It
does not schedule an extra dispatch.

---

## 8. Configuration

| Setting | Default | Meaning |
| :--- | :--- | :--- |
| `AUTOPILOT_MODE` | `"off"` | The mode the backend boots into. |
| `AUTOPILOT_INTERVAL_SECONDS` | `2.0` | The cycle period. Sized against the 2-second tick. |
| `AUTOPILOT_MAX_LITRES_PER_TICK` | `12000.0` | The normal budget per tick. |
| `AUTOPILOT_MAX_LITRES_PER_TICK_ESCALATED` | `24000.0` | The budget when anomalies are present. |
| `AUTOPILOT_MAX_SHIPMENTS_PER_TICK` | `8` | The hard cap on allocations per cycle, both tiers. |
| `AUTOPILOT_RESET_QUARANTINE_TICKS` | `2` | How long a rejected tick is quarantined. |

Two invariants are pinned by `backend/tests/test_config_defaults.py`:

- **The code default must match the compose value.** The compose file is the
  shipped configuration; the code default is what you get running the backend
  directly. If they drift, one of the two paths is wrong, and nobody knows
  which.
- **`AUTOPILOT_INTERVAL_SECONDS` must be at least 1 second.** A cycle faster
  than the world's tick would re-plan the same tick repeatedly — burning
  upstream reads for a decision that cannot change, because the world has not
  moved.

`AUTOPILOT_MODE` is *not* a number, so it is pinned through a separate text
table in that test file (`float("off")` raises).

---

## 9. The metrics

Five new families, plus one new label value on an existing one.

| Metric | Type | Labels | Meaning |
| :--- | :--- | :--- | :--- |
| `backend_autopilot_enabled` | gauge | — | `1` when the mode is not `off`. |
| `backend_autopilot_mode` | gauge | — | `0`=off, `1`=live. |
| `backend_autopilot_cycles_total` | counter | `outcome` | Cycle outcomes. |
| `backend_autopilot_dispatches_total` | counter | `outcome` | Allocation outcomes. |
| `backend_autopilot_litres_total` | counter | — | Cumulative litres dispatched. |

### `backend_decisions_total` gains `kind="autopilot"`

The existing `kind="dispatch"` counts decisions *a person* made. The
autopilot's decisions are decisions the operator **did not** make.

Folding them together would make the human-decision counter dishonest, and
the brief's §15 asks for *decision frequency* — which is only meaningful if
the two are told apart. So the autopilot gets its own label value:

```
backend_decisions_total{kind="dispatch"}   # a person pressed the button
backend_decisions_total{kind="autopilot"}  # the system pressed it
```

`FALLBACK_ACTIVATIONS` is **not** touched — the autopilot is not a fallback.

### The cycle outcomes

`dispatched`, `no_demand`, `gated`, `breaker_open`, `quarantined`,
`tick_regressed`, `error`.

Every one of them is **pre-registered at zero**. A counter that has never
been touched renders as *absent*, and absent is indistinguishable from
uninstrumented — a dashboard would show a gap rather than "0".

---

## 10. What is deliberately unchanged

| Component | Why it was left alone |
| :--- | :--- |
| `backend/app/simulator_client.py` | It must stay byte-identical to `verifier/simulator_client.py` (SHA256 drift guard). The autopilot only *calls* it. |
| `backend/app/optimizer.py` | The planner is reused unchanged. |
| `backend/app/dispatch.py` | The gate and the write path are reused unchanged. |
| `backend/app/anomaly.py` | Only the *pure* detector is used; `observe()` is never called. |
| `backend/app/circuit_breaker.py` | Reused, not modified. |
| `frontend/src/lib/derive.ts` | The console renders backend numbers only, so the no-invented-numbers allow-list is unchanged. |

---

## 11. Where the code lives

| File | What it holds |
| :--- | :--- |
| `backend/app/autopilot.py` | `AutopilotMode`, `AutopilotStatus`, `AutopilotController` |
| `backend/app/routers/autopilot.py` | The three routes |
| `frontend/src/components/AutopilotBar.tsx` | The mode switch |
| `frontend/src/components/ActionCard.tsx` | The dispatch surface — both modes |
| `backend/tests/test_autopilot.py` | The policy |
| `backend/tests/test_autopilot_api.py` | The HTTP surface |
| `backend/tests/test_autopilot_no_double_dispatch.py` | The double-dispatch defences |
| `backend/tests/test_autopilot_metrics.py` | The metric families |

---

## 12. Seeing it work

```bash
# The default is off. This is the shipped behaviour.
curl -s http://localhost:8002/api/autopilot
# -> "mode": "off", "enabled": false

# The rehearsal mode is gone. Posting it is an error, not a silent no-op.
curl -s -X POST http://localhost:8002/api/autopilot/mode \
  -H 'Content-Type: application/json' -d '{"mode":"dry_run"}'
# -> 400, code INVALID_AUTOPILOT_MODE

# Live: the system dispatches on its own.
curl -s -X POST http://localhost:8002/api/autopilot/mode \
  -H 'Content-Type: application/json' -d '{"mode":"live"}'
sleep 8
curl -s http://localhost:8002/api/autopilot
# -> "last_decision": "dispatched", "last_created": > 0, "last_shipments": [...]

curl -s http://localhost:8002/metrics | grep backend_autopilot
curl -s http://localhost:8002/metrics | grep 'backend_decisions_total'

# The acceptance check: the verifier suite must still pass while the
# autopilot is live.
docker compose exec -T client-verifier python verify_simulator.py \
  --base-url http://simulator-api:8000 --redis-url redis://redis:6379/0
# -> ALL 49 CHECKS PASSED

# The verifier resets the simulator, so that run also exercised the rewind
# path. The autopilot must recover rather than lock itself out:
curl -s -X POST http://localhost:8000/admin/run
sleep 20
curl -s http://localhost:8002/api/autopilot
# -> "last_decision": "no_demand" or "gated" on the NEW timeline,
#    NOT "tick_regressed"

# Back to the shipped default.
curl -s -X POST http://localhost:8002/api/autopilot/mode \
  -H 'Content-Type: application/json' -d '{"mode":"off"}'
```

### In the browser

At `http://localhost:8003`:

1. The bar shows **Manual / Autopilot**, and Manual is selected on load.
2. The card's button reads `Dispatch needed — 7,000 L`.
3. Switch to **Autopilot**: within one tick the system dispatches, the card
   shows `Last dispatch: tick N · N L · created N`, and the button becomes
   `Dispatch … anyway`.
4. Clicking it anyway still works — the same gate, the same journal.
5. Every figure traces to the backend — the no-invented-numbers test proves
   it.

---

## 13. See also

- `docs/BACKEND.md` — the API contract, the gate, and the configuration table.
- `docs/OBSERVABILITY.md` — the metric catalogue.
- `docs/DASHBOARD.md` — the console.
- `docs/RESILIENCE.md` — the circuit breaker the autopilot gates on.
