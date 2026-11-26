# Backend Service — API Contract, Validator Rules & Measured Simulator Semantics

**Service:** `buetcsefest-backend` · **Port:** `8002` · **Container:** `buetcsefest-backend`

This document is the contract for the Phase 1 operator backend. Every claim about the
simulator's behaviour below was **measured against the running container**, not inferred from
the Integration Guide PDF. Where the guide and the simulator disagree, the simulator wins and
the difference is called out explicitly.

---

## 1. What this service is

The simulator is the *world*: read-mostly, exposing `GET /v1/*`, a single write endpoint
`POST /v1/allocations`, an advisory SSE stream, and fault-bypassing `/admin/*` controls.

The backend is the *brain*. It owns the one upstream SSE subscription, maintains a fresh
world snapshot, and — critically — **decides what is legal to dispatch before any write
reaches the simulator**.

```
                       ┌──────────────────────┐
   dashboards  ◀──SSE──┤  backend (this svc)  │──REST──▶  simulator-api :8000
   operators   ──HTTP▶ │                      │──SSE──▶   (one subscription)
                       └──────────┬───────────┘
                                  │
                            redis :6379
                       (stale invalidation, cache)
```

Three properties follow from that shape:

1. **REST is the source of truth; SSE is advisory.** An SSE payload never becomes world
   state. Every event triggers a REST re-fetch.
2. **One upstream subscription.** Dashboards connect to the backend, never to the simulator.
   The simulator's per-subscriber queue overflow (the silent-drop failure mode) is therefore
   far less likely to be reached.
3. **A drop degrades freshness, never correctness.** The REST poller runs at all times.

---

## 2. The pre-flight validator

`backend/app/validator.py` is the highest-value file in this service. It is a **pure
function** — no I/O, no network, no clock — so it is unit-testable without a simulator and
readable by judges.

```python
validate_and_plan(snapshot, request, *, allow_split=True, key_factory=None) -> PlanResult
```

The validator answers two different questions and must not conflate them:

| Question | Function | Rule |
| :--- | :--- | :--- |
| *How big can this chunk legally be?* | `_tightest()` | the **numerically smallest** ceiling |
| *Which limit will the simulator name in its error?* | `_binding_constraint()` | the **first** ceiling in the simulator's fixed order |

Conflating them produces a validator that rejects for the *wrong reason* — the operator is
told "capacity exceeded" when the actual blocker was the route limit. Both functions exist
because both questions are asked in practice.

### 2.1 Validation order

`validate_and_plan` mirrors the simulator's own check order. The first failure wins.

| # | Check | Reject code | HTTP |
| :--- | :--- | :--- | :--- |
| 1 | `quantity <= 0` | `INVALID_QUANTITY` | 422 |
| 1 | Unknown fuel type | `INVALID_FUEL_TYPE` | 422 |
| 2 | Unknown depot / station / route | `NOT_FOUND` | 404 |
| 3 | Route endpoints ≠ request endpoints | `ROUTE_MISMATCH` | 409 |
| 4 | Depot status ∉ `{OPEN, CONSTRAINED}` | `DEPOT_CLOSED` | 409 |
| 5 | Station status ≠ `OPEN` | `STATION_CLOSED` | 409 |
| 6 | Route status ≠ `AVAILABLE` | `ROUTE_DISRUPTED` | 409 |
| 7 | `quantity > route.max_shipment` | `ROUTE_CAPACITY_EXCEEDED` | 409 |
| 8 | `depot.inventory[fuel] < quantity` | `INSUFFICIENT_INVENTORY` | 409 |
| 9 | Dispatch allowance exhausted this tick | `DISPATCH_CAPACITY_EXCEEDED` | 409 |
| 10 | `station.inventory[fuel] + quantity > station.capacity[fuel]` | `DESTINATION_CAPACITY_EXCEEDED` | 409 |

> **`CONSTRAINED` depots are still dispatchable.** Only `OUTAGE` and `CLOSED` are not. This
> is easy to get wrong and would make the validator reject legal shipments.

> **The status column is enforced, not merely documented.** The validator
> decides which status each rejection implies and the router reports that
> decision — so an unknown depot is a **404**, a negative quantity is a
> **422**, and only a genuine business conflict is a **409**. A client can
> therefore tell "that depot does not exist" apart from "that route is over
> capacity" without parsing the message.

### 2.2 The simulator's ceiling order is fixed, not by magnitude

**This is the single most important measured fact about the validator**, and it was caught
by the randomised differential test rather than assumed.

The simulator applies its capacity limits in a **fixed order**, and reports the *first* one
that fails — regardless of which limit is numerically smallest.

```
_CONSTRAINT_ORDER = (
    "ROUTE_MAX_SHIPMENT",   # 1
    "DEPOT_INVENTORY",      # 2
    "DISPATCH_CAPACITY",    # 3
    "STATION_FREE_SPACE",   # 4
)
```

Measured live, with `route.max_shipment = 7000` and station free space `= 6000`:

| Quantity | Simulator's answer | Note |
| :--- | :--- | :--- |
| 6000 | **accepted** | boundary is inclusive (`>`, not `>=`) |
| 6001 | `DESTINATION_CAPACITY_EXCEEDED` | station limit (4th) |
| 6500 | `DESTINATION_CAPACITY_EXCEEDED` | station limit, *not* the smaller-sounding route limit |
| 7001 | `ROUTE_CAPACITY_EXCEEDED` | route limit (1st) |
| 7500 | `ROUTE_CAPACITY_EXCEEDED` | route limit — **not** the numerically tighter station limit |
| 12000 | `ROUTE_CAPACITY_EXCEEDED` | route limit — **not** `DISPATCH_CAPACITY_EXCEEDED` |

A validator that reported the *smallest* limit would have said `DESTINATION_CAPACITY_EXCEEDED`
for the 7500 L case and been **wrong**. The differential test caught exactly this.

### 2.3 Dispatch capacity filters on `created_tick`

The per-tick dispatch allowance is consumed by shipments **created** on the current tick:

```
in_flight(depot, tick) = Σ quantity
                           WHERE source_depot_id == depot
                             AND created_tick    == current_tick
                             AND status IN ('PENDING', 'IN_TRANSIT')
remaining = depot.dispatch_capacity_per_tick − in_flight
```

Three consequences that are easy to get wrong:

* It filters on `created_tick`, **not** `departure_tick`.
* It filters on `created_tick == current_tick`, **not** "all in-flight shipments". A shipment
  dispatched on an earlier tick does **not** count against today's allowance — **the cap
  resets every tick.**
* It accumulates **across routes**. Two shipments of 6000 L on *different* routes from the
  same depot consume the same 12000 L allowance.

Measured live (gazipur, cap = 12000):

```
5000 + 5000 + 2000 = 12000  → accepted
+ 1 more in the same tick    → 409 DISPATCH_CAPACITY_EXCEEDED
after one admin_step         → the cap resets; a 7000 L request now fails on
                               DESTINATION_CAPACITY_EXCEEDED instead
```

### 2.4 Destination capacity ignores in-flight fuel — and the excess is silently spilled

The simulator checks **live** `station.inventory` only. It does not subtract fuel that is
already in flight to that station.

Measured: 11001 L committed in-flight to a station with 6000 L of free space was **accepted**.
On arrival the delivery clamps to the free space and the remainder is **silently discarded** —
both shipments reported `ARRIVED`, with no error and no audit flag.

> **Deliberate divergence.** The validator enforces **two layers**:
>
> * *Hard rules* mirror the simulator exactly. Anything the validator approves is accepted —
>   the operator never sees an unpredicted `409`.
> * *Soft rules* add an in-flight-aware `SPILL_RISK` warning carrying the exact
>   `spill_risk_litres` and an operator-facing explanation.
>
> Making in-flight destination capacity a hard rule would make the validator **stricter than
> the oracle** — a bad property for a test oracle. So it is reported, not rejected.

### 2.5 Splitting

Oversized requests are split greedily against **live** ceilings, recomputed on every
iteration, with running totals tracked **per depot and per station** (dictionaries, not a
single scalar) so multi-destination planning needs no rewrite.

| Outcome | Meaning |
| :--- | :--- |
| `PLANNED` | everything shipped |
| `PARTIAL` | some shipped, the remainder deferred to the next tick |
| `REJECTED` | nothing shipped; the binding constraint is identified |

`allow_split=false` (either on the request body or as a validator argument) disables splitting
entirely, which turns the result into a single-chunk evaluation against the full request.

### 2.6 Route selection

When `route_id` is omitted, routes serving the station are ranked by:

1. `AVAILABLE` before `DISRUPTED`
2. lowest `transit_ticks`
3. highest `max_shipment`
4. route id, lexicographically (determinism)

An explicit `route_id` is **never** silently overridden. When every candidate is `DISRUPTED`
the request is rejected with `ROUTE_DISRUPTED` and the alternatives are named in the
explanation.

`is_only_route` marks single-connected stations — `station-tongi` and `station-coxsbazar`.
Their explanation states plainly that no rerouting is possible. Dual-connected stations
(`station-mirpur`, `station-karnaphuli`) can reroute via the other depot when
`allow_alternate_route` is set, and that substitution is recorded explicitly.

### 2.7 Inspectable decision support

Every plan carries the reasoning, not just the verdict:

```jsonc
{
  "shipments": [{
    "sequence": 1,
    "quantity": 1000.0,
    "idempotency_key": "alloc-t2-depot-patiya-station-mirpur-diesel-1-43132e25",
    "binding_constraints": [
      {"kind": "ROUTE_MAX_SHIPMENT", "limit": 5000.0, "available": 5000.0,
       "detail": "route route-patiya-mirpur cannot carry more than 5000.0 L per shipment"}
    ],
    "reasoning": "Chunk 1: 1000.0 L of 1000.0 L remaining, limited by route route-patiya-mirpur cannot carry more than 5000.0 L per shipment. Route route-patiya-mirpur takes 4 ticks; depot depot-patiya and station station-mirpur both remain open."
  }],
  "diagnostics": {
    "tick": 2, "route_id": "route-patiya-mirpur",
    "depot_inventory": 55000.0, "dispatch_remaining": 11000.0,
    "station_free_space": 6118.493, "station_inbound": 0.0, "chunk_count": 1
  },
  "warnings": [],
  "explanation": {
    "human_summary": "Dispatch the full 1000.0 L of DIESEL from Patiya Depot to Mirpur Fuel Station in 1 shipment(s) over route route-patiya-mirpur (4 ticks).",
    "route_choice": "route-patiya-mirpur",
    "alternatives_considered": [],
    "is_only_route": false,
    "rerouting_possible": true,
    "notes": ["Alternate routes were not substituted; pass allow_alternate_route to enable rerouting."]
  }
}
```

`warnings[]` carries `SPILL_RISK` (with `spill_risk_litres`) and `SINGLE_CONNECTED_STATION`.

---

## 3. API surface

| Method | Path | Purpose |
| :--- | :--- | :--- |
| GET | `/` | Service banner and the endpoint index |
| GET | `/api/health` | Liveness + upstream reachability + journal stats |
| GET | `/api/stream-health` | Broker mode, drop detection, probe state, poll counts |
| GET | `/api/state` | The whole world snapshot |
| GET | `/api/depots`, `/api/depots/{id}` | Depots + computed dispatch headroom this tick |
| GET | `/api/stations`, `/api/stations/{id}` | Stations + free space, in-flight, spill risk |
| GET | `/api/routes` | Routes + single-connected flag |
| GET | `/api/allocations`, `/api/allocations/{id}` | Allocation ledger |
| GET | `/api/metrics` | Simulator metrics + derived KPIs |
| GET | `/api/supply-arrivals` | Supply arrival schedule |
| GET | `/api/events` | Raw simulator domain events |
| GET | `/api/events/recent` | Last ≤ 200 broker-retained events |
| GET | `/api/events/stream` | **SSE fan-out to dashboards** |
| POST | `/api/plan` | **Validate + split. No write.** |
| POST | `/api/allocations` | Re-plan from a fresh snapshot, then execute |
| POST | `/api/allocations/{id}/cancel` | Cancel a `PENDING` allocation |
| GET | `/api/allocations/{id}/explain` | Reconstruct the decision record |

### 3.1 `POST /api/plan`

Body:

```json
{
  "source_depot_id": "depot-patiya",
  "destination_station_id": "station-mirpur",
  "fuel_type": "DIESEL",
  "quantity": 1000,
  "route_id": null,
  "allow_split": true,
  "allow_alternate_route": false,
  "reason": null
}
```

Returns `200` with the plan — **including when the plan is a rejection**. The rejection *is*
the answer, and it carries the reasoning. Unknown fields and bad enums are `422`
(`VALIDATION_ERROR`); a negative quantity is a `200` with `reject_code: "INVALID_QUANTITY"`.

### 3.2 `POST /api/allocations`

Re-plans against a **freshly refreshed** snapshot, then executes. Returns:

```json
{"status": "OK",
 "execution": {"plan_status": "PLANNED",
               "created": [...], "replayed": [...], "failed": [...],
               "total_litres": 1000.0, "plan": {...}}}
```

A replayed shipment is reported under `replayed` (not `created`) and contributes `0.0` to
`total_litres`. A rejected plan returns `409` with the reject code.

### 3.3 Idempotency keys

```
alloc-t{tick}-{depot}-{station}-{fuel}-{seq}-{hash8}
```

The `hash8` covers the canonical JSON of the six identity fields (including `route_id` and
`quantity`), so the same logical plan reproduces the same key on every retry, while a
genuinely different request cannot collide and trip `IDEMPOTENCY_KEY_MISMATCH` ourselves.
The key embeds the tick because a cancelled key stays permanently occupied.

### 3.4 Write-ahead journal

`backend/app/journal.py` is a SQLite journal on a named volume. Every dispatch intent
`(key, payload)` is recorded **before** the simulator call. On startup, any intent with no
recorded result is replayed verbatim — the simulator's idempotency turns that into a `201`
for the original shipment, never a duplicate.

### 3.5 Error envelope

Every error — ours or the simulator's — uses one shape:

```json
{"status": "ERROR",
 "error": {"code": "NOT_FOUND", "message": "...", "http_status": 409},
 "diagnostics": {},
 "explanation": {}}
```

| Simulator exception | Our HTTP | Our `error.code` |
| :--- | :--- | :--- |
| `SimulatorConflict` | 409 | the domain code **verbatim** |
| `SimulatorNotFound` | 404 | `NOT_FOUND` |
| `SimulatorValidationError` | 422 | `VALIDATION_ERROR` |
| `SimulatorFaultInjected` | 503 | `FAULT_INJECTED` (+ `Retry-After: 1`) |
| `SimulatorTimeout` | 504 | `SIMULATOR_TIMEOUT` |
| `SimulatorError` (other) | 502 | `SIMULATOR_ERROR` |
| our `PlanRejected` | 409 / 422 | the reject code |

Global handlers ensure no raw stack trace ever reaches a client.

---

## 4. The SSE broker

`backend/app/broker.py` owns the single upstream subscription.

### 4.1 The silent drop

The simulator's SSE endpoint has a **silent failure mode**. Each subscriber gets its own
`asyncio.Queue(maxsize=200)`. When that queue overflows, the simulator removes the subscriber
from its subscriber set but **leaves the HTTP connection open**, with the generator blocked
awaiting a queue that will never be filled again.

Live-verified consequence: after 240+ events queued while the client did not read, the client
read **nothing at all** — no domain events and **no keepalives**. The connection looks healthy
to an HTTP client; the simulator itself stays healthy. It is a permanently silent
subscription.

### 4.2 Why frame-silence cannot detect it

A **paused** simulator also emits nothing for minutes. Any liveness check of the form "have I
seen a frame recently?" would false-positive constantly. So the broker measures **tick
progress**, not frame arrival:

```
watchdog (every 5s):
    tick_now = GET /v1/instance -> tick
    if tick_now != last_tick_seen:          # the world MOVED
        if now - last_sse_event_at > 20s:   # ...but we heard nothing
            suspected_drop = True
            -> cancel the orphaned SSE task
            -> open a NEW subscription (re-registers us)
            -> the poller covers the gap
    last_tick_seen = tick_now
```

The old subscription can never recover on its own — its queue is orphaned forever — so
reconnecting is the only cure. A forced reconnect every `SSE_MAX_LIFETIME` (900 s) bounds the
blast radius of an undetected drop.

### 4.3 Reachability is tracked separately from event delivery

`mode` reports what the broker knows about the *connection*, not about event flow:

| Field | Meaning |
| :--- | :--- |
| `mode` | `sse` / `polling` / `starting` / `stopped` |
| `sse_reachable` | the simulator accepted an SSE connection on the last probe |
| `sse_connected_since` | when the current subscription was established |
| `probe_failures` | consecutive failed reachability probes |
| `suspected_drops` | silent drops detected by the tick-progress watchdog |
| `resync_required` | a subscriber missed events and should re-fetch state |

> **Why the probe exists.** An earlier version flipped `mode` to `sse` only when an *event*
> arrived. That conflated "can the simulator accept a connection?" with "are events flowing?".
> A paused simulator accepts the connection but emits nothing, so the broker reported
> `polling` while SSE was perfectly healthy — and the recovery from a `stream_disconnect`
> fault looked like a permanent failure. The reachability probe opens a short-lived SSE
> connection and closes it immediately, which is independent of event delivery and therefore
> stays truthful while the simulator is paused.

### 4.4 The measured fault-injection rehearsal

```
POST /admin/faults {"type":"stream_disconnect","duration_seconds":120}
```

| Phase | `mode` | `sse_reachable` | `probe_failures` |
| :--- | :--- | :--- | :--- |
| Healthy | `sse` | `true` | 0 |
| Fault injected (≤ 12 s) | `polling` | `false` | 2 |
| Fault cleared (≤ 12 s) | `sse` | `true` | 0 |

Note that the simulator's `stream_disconnect` fault rejects **new** stream connections with a
`503`; it does not tear down an already-established stream. The backend's existing
subscription therefore survives the injection, and the degradation is observed on the next
reconnect attempt.

---

## 5. Measured simulator semantics (the full table)

| Fact | Value |
| :--- | :--- |
| Boundary checks | `quantity == limit` **passes** for route max, depot inventory, dispatch cap and station free space (all `>`, not `>=`) |
| Depot inventory | decrements at **dispatch** time, not departure — so `depot.inventory` is always net of commitments |
| `X-Simulator-Stale` header | present on `/v1/*` GETs, **absent on `/v1/health`** |
| SSE event names | `simulation.tick`, `allocation.status_changed`, `inventory.updated`, `simulator.notice` |
| SSE `id:` field | **none** → no `Last-Event-ID` replay |
| Per-subscriber queues | each subscriber gets its own `asyncio.Queue(maxsize=200)`; consumers cannot evict each other |
| Idempotent replay | returns **`201`**, same as a fresh create — the status code alone cannot identify a replay |
| Arrival timing | applies **one tick later** than `expected_arrival_tick` |
| Error shapes | domain errors `{"detail":{"code":...}}`; injected faults `{"error":{"code":"FAULT_INJECTED"}}` |
| Numeric types | `dispatch_capacity_per_tick` / `max_shipment` serialize as **floats** (`12000.0`) |

---

## 6. Configuration

All settings are environment-driven (`backend/app/config.py`).

**The defaults in this table are the code's defaults, and they are the same
numbers `docker-compose.yml` sets.** That equivalence is not a coincidence —
it is pinned by `backend/tests/test_config_defaults.py`, which reads the
compose file and fails if the two drift apart. It matters because the
defaults are what you get when running the backend *outside* Docker
(`uvicorn app.main:app`), and a default that disagrees with the deployed
value is a bug waiting for the person who runs it that way.

The one that bit: `STATE_MAX_AGE` (the snapshot TTL) was `1.0` in code while
the compose file set `15.0`, and `POLL_INTERVAL` was `2.0` against a compose
`5.0`. A TTL below the poll interval marks a healthy world stale for most of
every poll cycle — the false "Stale data" banner the pair was tuned to
avoid. Under Docker the compose values won, so the deployed platform was
never affected; under a bare `uvicorn` the bug was structurally guaranteed.

| Variable | Default | Meaning |
| :--- | :--- | :--- |
| `SIMULATOR_BASE_URL` | `http://simulator-api:8000` | Upstream base URL |
| `REDIS_URL` | `redis://redis:6379/0` | Cache / stale invalidation |
| `BACKEND_PORT` | `8002` | Listen port |
| `SSE_STALE_AFTER` | `45` | Seconds of silence while the world advances before a drop is suspected. Must exceed the simulator's 10s tick interval with slack — events arrive once per tick. |
| `SSE_RECOVER_AFTER` | `10` | Seconds of poller-only operation before reclaiming SSE |
| `POLL_INTERVAL` | `5` | REST reconciliation interval (seconds). Half the 10s tick interval — polling faster just re-reads the same snapshot. |
| `WATCHDOG_INTERVAL` | `5` | Tick-progress watchdog interval (seconds) |
| `SSE_MAX_LIFETIME` | `900` | Force a fresh subscription after this long (seconds) |
| `STATE_MAX_AGE` | `15` | Snapshot TTL (seconds). **Must exceed `POLL_INTERVAL`** — a snapshot can only ever be as fresh as the poll that captured it, so a threshold below the poll interval marks a healthy world stale for most of every poll cycle, and the console renders a red "Stale data" banner on a system that is working fine. 15s is three poll intervals of slack. |
| `EVENT_BUFFER` | `200` | Retained events for `/api/events/recent` |
| `SUBSCRIBER_QUEUE` | `64` | Per-subscriber queue depth |
| `JOURNAL_PATH` | `/app/data/journal.sqlite3` | Write-ahead journal location |
| `BREAKER_FAILURE_THRESHOLD` | `5` | Consecutive **retryable** failures before the circuit breaker opens. A 409/404/422 is a business answer and never counts — see `docs/RESILIENCE.md`. |
| `BREAKER_COOLDOWN_SECONDS` | `15.0` | How long the breaker stays OPEN before it allows a single probe. |
| `SCENARIO_PATH` | `backend/scenarios/baseline.yaml` | The scenario configuration file. In the container: `/app/scenarios/baseline.yaml`. Validated at boot; a broken file stops the backend rather than producing wrong forecasts. See `docs/SCENARIO.md`. |

---

## 7. Verification

The simulator is the **oracle**. Two properties are proven:

1. **Soundness** — anything the validator approves, the simulator accepts (no `409`).
2. **Completeness** — anything the validator rejects, the simulator would have rejected with
   the same code.

| File | Purpose |
| :--- | :--- |
| `tests/test_validator.py` | Pure unit tests — no simulator required |
| `tests/test_validator_against_simulator.py` | Soundness, completeness, boundary sweep, **40-request randomised differential test** |
| `tests/test_broker.py` | Fan-out, buffer bounds, slow subscribers, watchdog, reachability probe |
| `tests/test_api.py` | HTTP surface, error envelope, dashboard fan-out |
| `tests/test_client_parity.py` | Drift guard — `backend/app/simulator_client.py` must stay byte-identical to `verifier/simulator_client.py` |

### Commands

```bash
docker compose up -d --build
docker compose exec -T client-verifier python verify_simulator.py   # expect 49/49
docker compose exec -T backend pytest tests/ -q                     # expect all green
```

### The drift guard

`verifier/simulator_client.py` is the reference implementation that the 49-check suite
exercises. The backend uses a **copy** of it, so the two can silently drift. The parity test
compares their SHA-256 hashes and fails the build on drift. The image bakes the reference to
`/app/reference/simulator_client.py` so the guard works inside the container without a host
bind-mount.

**If the client genuinely needs a fix:** land it in `verifier/simulator_client.py` first,
re-run the 49-check suite, then re-copy it into `backend/app/`.

---

## 8. Operational notes

* **No host bind-mounts.** Docker Desktop cannot mount this project's path (it contains
  spaces). The journal and the verifier's reports live on **named volumes**.
* **The compose change is purely additive.** The `verifier/` service's build context,
  Dockerfile, dependencies and ports are untouched, so the 49/49 baseline cannot break.
* **One `SimulatorClient` per process.** `create_allocation`'s replay detection is
  per-instance state, so a per-request client would mis-report replays as new allocations.
* **The lifespan tolerates a failing upstream at startup** — it logs and lets the watchdog
  retry rather than crashing the container.

---

# Part II — Phase 2: Optimisation, Forecasting, Anomaly Detection & Explanation

Phase 1 built the **validator**: a pure function that decides what is *legal* to dispatch.
It is proven correct — a randomised differential test against the live simulator shows that
anything the validator approves the simulator accepts, and anything it rejects would have been
rejected with the same code.

Phase 2 builds the **optimiser**: what is *optimal* to dispatch, over a multi-tick,
multi-destination horizon, against *forecast* demand rather than current stock. Then the
intelligence layer on top: demand forecasting, anomaly detection, and a human-readable
explanation layer.

> **The division of labour.** The optimiser's job is **quality**. The validator's job is
> **soundness**. The optimiser may be **conservative**, never **illegal** — every shipment it
> emits is re-checked by `validate_and_plan` before it can reach the dispatch path. A
> validator stricter than the oracle is a bad oracle; so is an optimiser that trusts its own
> arithmetic for legality.

Phase 2 is **additive**: no new Docker service, no change to `verifier/`, and **no new
dependency by default**.

---

## 9. The optimiser (`backend/app/optimizer.py`)

A **pure function** — no I/O, no clock, no network:

```python
optimize(snap, forecast, *, config=None, horizon=None, now_tick=None) -> OptimizationResult
```

### 9.1 The variables

`x[r, f, t]` = litres shipped on route `r`, fuel `f`, dispatch tick `t`. A route fixes its own
depot and station, so no separate depot/station index is needed.

### 9.2 The objective — delivered service level, not shipment count

```
maximise  Σ x[r,f,t] · w(r,f,s(r))  +  Σ served[s,f] · w_station(s)
           −  Σ x[r,f,t] · spill_penalty · c_spill(r,f,t)
```

where `w(r,f,s) = fuel_criticality[f] · station_criticality[s] · transit_weight(r)` and
`w_station(s)` carries a risk bonus from the projected stockout probability. The objective
maximises **litres actually served**, so the solver has no incentive to fragment a plan into
many small shipments.

### 9.3 The constraints — each one a measured simulator rule

| # | Constraint | Measured rule it encodes |
| :--- | :--- | :--- |
| C1 | `x[r,f,t] ≤ route.max_shipment · b[r,f,t]` | route max is a **per-shipment** limit — 14000 L on a 7000-max route needs **two** shipments |
| C2 | `y[d,f,t] = y[d,f,t−1] − Σ x[r,f,t] + supply[d,f,t]` | depot inventory decrements at **dispatch** time |
| C3 | `Σ_r x[r,f,t] ≤ depot.dispatch_capacity_per_tick` | filters on `created_tick == current_tick`; resets every tick; accumulates **across routes** |
| C4 | `s[s,f,t] ≤ station.capacity[f]` | station tank capacity |
| C5 | `x = 0` if `route.status ≠ AVAILABLE` | `ROUTE_DISRUPTED` |
| C6 | `x = 0` if depot status ∉ `{OPEN, CONSTRAINED}` | **`CONSTRAINED` is dispatchable** |
| C7 | `x = 0` if station status ≠ `OPEN` | `STATION_CLOSED` |
| C8 | `s[s,f,t] = s[s,f,t−1] − demand[s,f,t] + arrivals[s,f,t]`, `s ≥ 0` | demand consumes stock; `served = min(demand, s)` |
| C9 | `y[d,f,t] ≥ 0` | depot inventory non-negative |
| C10 | `Σ_r x[r,f,t] ≤ need[s,f,t]` | don't fill a tank nobody needs |

Supply enters C2 as an exogenous inflow with the **measured** `+1` lag:
`supply[d,f,t] = Σ arrivals a where a.depot_id == d, a.fuel_type == f, a.planned_tick == t−1`.

### 9.4 The greedy algorithm (the default path)

```
for each station (by criticality desc, then id asc):
  for each fuel (by fuel_criticality desc, then name asc):
    cum_need = forecast.cumulative_demand(s, f, T0, T0+H)
    have     = station_inv[s][f][T0]
    want     = max(0, cum_need − have) · (1 + safety_stock_fraction)   # single-connected only
    for r in sorted(candidates, key=_route_rank):                      # REUSE validator._route_rank
      arrival_offset = r.transit_ticks + 1                             # MEASURED
      dispatch_tick  = max(T0, need_by − arrival_offset)
      q = min(want, r.max_shipment, depot_inv, dispatch_remaining, station_free_space)
      emit shipment; want −= q
```

Every iteration is over a **sorted** collection with an explicit tie-break and the RNG is never
touched, so two calls with the same inputs produce **byte-identical** output. The plan id is a
SHA-256 digest over the shipments, so a repeated `POST /api/optimize` is visibly the *same*
plan, not a new one.

**Reuse, not duplication.** The greedy path imports `validator._route_rank`,
`validator.DISPATCHABLE_DEPOT_STATUSES`, `validator.dispatch_committed_this_tick`,
`validator.dispatch_remaining`, `validator.station_free_space` and `validator.station_inbound`.
The optimiser therefore inherits the validator's route preference and dispatch accounting
rather than re-deriving them — which is why the two can never disagree.

### 9.5 The LP path (`backend/app/lp_solver.py`) — optional and import-guarded

```python
try:
    import pulp
except ImportError:
    pulp = None
```

The full C1–C10 formulation is written out — judges score optimisation depth — and returns the
**same `OptimizationResult` type** as the greedy path, so the two are directly comparable. When
`pulp` is absent, `solve_lp` raises `ImportError` rather than returning `None`: raising lets the
optimiser report the solver as *unavailable*, which is the truth, whereas `None` would be
indistinguishable from "ran and found no feasible plan".

The optimiser **adopts** an LP plan only when it is at least as good as greedy — the greedy plan
is always feasible, so a worse LP result is never taken. `solver_used` always reports which
solver actually produced the plan, and `GET /api/optimize/solver` reports whether the optional
dependency is present. **The API never overclaims.**

### 9.6 The measurement that keeps `pulp` out of the image

On the baseline network, measured live:

| Solver | Service level | Shipments | Gate result |
| :--- | ---: | ---: | :--- |
| greedy | 0.915702 | 3 | 3 accepted / 0 rejected |
| LP | 0.915702 | 39 | 39 accepted / 0 rejected |

**Identical service level, 13× the shipments.** Each shipment costs an idempotency key and a
slice of the depot's per-tick dispatch cap. The LP's global optimality buys nothing here and
its operational cost is real.

So the plan's rule — *enable the LP only if it is measurably better* — resolves to **skip**. The
LP formulation ships as an import-guarded, fully-written, tested module; **`pulp` does not enter
`requirements.txt`**. The image keeps no native binary, and the offline-reproducible build
stays intact. `backend/tests/test_lp_solver.py` pins this measurement so a future change that
makes the LP *win* by more than 1% will fail the test and force the question to be re-asked.

### 9.7 Only current-tick shipments are ever written

The simulator stamps `created_tick = now` on every allocation. Creating an allocation early
would consume the current tick's dispatch cap, and the world will have moved by the time that
tick arrives anyway. So the plan is split:

| List | Meaning |
| :--- | :--- |
| `shipments_now` | `dispatch_tick == now_tick` — executable right now |
| `shipments_later` | advisory — recomputed next cycle, never written early |

`POST /api/optimize` is **read-only**: it computes the plan, gates every candidate, and says
what the gate *would* do — writing nothing at all.

### 9.8 The gate (`backend/app/dispatch.py`)

```python
gate_plan(snap, shipments) -> GateResult
execute_shipments(client, journal, gated) -> ExecutionOutcome
```

`gate_plan` runs **every** candidate shipment through `validate_and_plan` with `route_id` set
explicitly (never silently rerouted) and `allow_split=False` — the optimiser has already
computed a legal quantity, and splitting here would produce chunks the optimiser never modelled
and would change the arrival schedule. A shipment the validator rejects is **dropped**, never
retried with different parameters.

`execute_shipments` is the journal→write→record loop **extracted** from
`backend/app/routers/allocations.py` and now shared by both routers, so there is exactly one
implementation of the write-ahead sequence.

A journal failure never stops a dispatch — the audit trail is valuable, but the dispatch is the
operator's actual request.

### 9.9 Multi-tick idempotency keys

Phase 2 adds a *sibling* to the Phase 1 key function, not a change to it:

```python
def make_key_at(tick: int, req: PlanRequest, sequence: int, *, route_id=None) -> str:
    """The Phase 1 key format, but with an EXPLICIT tick (the tick the shipment is CREATED at)."""

def make_key(snap, req, sequence) -> str:   # unchanged behaviour
    return make_key_at(snap.tick, req, sequence)
```

One format, one namespace. The tick in the key is the **dispatch** tick, so a shipment planned
for tick N+1 gets `alloc-t{N+1}-…`. The Phase 1 key tests pass **unchanged**.

### 9.10 The `dispatch_plans` table

A new journal table records the plan **once, before any shipment is dispatched**:

```
dispatch_plans(plan_id, created_at, tick, horizon, solver_used, status, payload_json)
```

This gives an audit trail of what was *intended*, distinct from the per-shipment
`dispatch_intents` which records what actually happened. Additive — it does not touch
`dispatch_intents`.

---

## 10. The forecast (`backend/app/forecast.py`)

Also pure. It answers "how much fuel will these stations want, tick by tick, over the horizon?"
**without calling the simulator** — because the simulator's demand is deterministic.

### 10.1 The demand formula is exactly reproducible offline

```
rng(seed, tick, entity, purpose) = random.Random(
    int.from_bytes(sha256(f"{seed}:{tick}:{entity}:{purpose}".encode()).digest()[:8], "big"))

_hour_factor(hour, profile):
   industrial : 1.55 if 6<=hour<18 else 0.45
   highway    : 1.35 if (6<=hour<10 or 16<=hour<21) else 0.75
   urban_high : 1.45 if (7<=hour<10 or 16<=hour<21) else 0.70
   regional   : 1.25 if 7<=hour<21 else 0.65

demand(station, fuel, tick) = max(0, base · factor · jitter)
   base   = daily_liters[fuel] / 96.0
   factor = _hour_factor(sim_time.hour, profile) · region.demand_factor · station.demand_multiplier
   jitter = 1 + rng(seed, tick, station_id, f"demand:{fuel}").uniform(−noise, +noise)
```

**Measured: 60/60 exact match** against live `/v1/demand-history` (ticks 0–4 × 4 stations ×
3 fuels, tolerance 1e-3). This is the load-bearing result of Phase 2: because demand is
deterministic, the planning horizon is limited only by CPU.

### 10.2 The forecast is tick-by-tick, never a scaled constant

The Phase 1 brief claimed realised daily demand is ~89% (urban_high) / 100% (industrial) /
~87.5% (highway) / ~93% (regional) of `daily_liters`. **Measured over a full 96-tick day
(1152 rows), those figures are wrong:**

| Station | Profile | Region | Realised/day | `daily_liters` | ratio |
| :--- | :--- | :--- | ---: | ---: | ---: |
| mirpur DIESEL | urban_high | dhaka | 8022.3 | 8500 | **0.944** |
| mirpur PETROL | urban_high | dhaka | 9933.5 | 10500 | **0.946** |
| mirpur OCTANE | urban_high | dhaka | 5320.3 | 5600 | **0.950** |
| tongi DIESEL | industrial | dhaka | 14002.9 | 14000 | **1.001** |
| tongi PETROL | industrial | dhaka | 4503.3 | 4500 | **1.001** |
| karnaphuli DIESEL | highway | chattogram | 11110.3 | 10500 | 1.058 (0.980 vs regional) |
| coxsbazar DIESEL | regional | chattogram | 7750.9 | 7200 | 1.077 (0.997 vs regional) |

So the forecast **must** evaluate the formula tick by tick. Scaling `daily_liters` by a fitted
constant would be wrong by up to 11% for `karnaphuli`. A unit test pins these ratios.

### 10.3 `demand_multiplier` is transient, not persistent

Measured with a `demand_spike` event (`start_tick=1`, `duration_ticks=3`, `multiplier=1.8`):

```
tick=0  mult=1.0    tick=1  mult=1.0    tick=2  mult=1.8
tick=3  mult=1.8    tick=4  mult=1.8    tick=5  mult=1.0  <-- REVERTS on resolve
```

The operational rule is therefore stronger than "cache it": **`demand_multiplier` changes
tick-to-tick and must be re-read from `/v1/stations` every cycle, never cached as a constant.**
A cached multiplier would make the forecast 80% wrong for the entire duration of a spike —
exactly the window where it matters most. The router's `_fresh_snapshot()` always refreshes for
this reason.

### 10.4 The arrival lag — the simulator's own field is misleading

Measured: a shipment created at tick 0 on a 2-transit-tick route:

```
tick=1  IN_TRANSIT  expected_arrival_tick=2  actual_arrival_tick=None
tick=2  IN_TRANSIT  expected_arrival_tick=2  actual_arrival_tick=None
tick=3  ARRIVED     expected_arrival_tick=2  actual_arrival_tick=2  <-- FUEL CREDITED
```

The simulator stamps `actual_arrival_tick=2`, but the fuel becomes available at **tick 3**. The
same `+1` lag applies to **supply arrivals**: `supply-001` has `planned_tick=12` /
`actual_tick=12`, but the depot's inventory went 60000 → 78000 at **tick 13**.

So the projection model uses:

```
applied_tick = created_tick + transit_ticks + 1      (shipments)
applied_tick = planned_tick + 1                      (supplies)
```

Anything else over-forecasts depot inventory and under-forecasts stockouts. Demand is consumed
*after* arrivals in the same tick, because the simulator credits arrivals before generating
demand.

### 10.5 Stockout risk is analytic, not sampled

The jitter is uniform on `[−noise, +noise]`, so `std = mean · noise / √3`. Cumulative demand
over the window is compared to available stock through a standard normal CDF:

```python
def _phi(z): return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))

p(stockout) = 1 − _phi((cumulative_demand − available) / std_cumulative)
```

`math.erf` is stdlib — **no scipy needed**. Keeping the module pure and deterministic is worth
more than a sampled estimate.

### 10.6 Spill is counted, never a rejection

`delivered = min(arriving, free_space)`, `spilled = arriving − delivered`. Spill is penalised in
the objective and reported as `SPILL_RISK`, because the simulator accepts over-commits and
silently spills — a stricter optimiser would be a bad oracle.

---

## 11. Anomaly detection (`backend/app/anomaly.py`)

Pure, deterministic, and stateful only over plain dataclasses — no clock, no I/O.

| Detector | Trigger | What it catches |
| :--- | :--- | :--- |
| **D1** demand spike | `z = (observed − forecast) / std ≥ 3.0` (MEDIUM) / `≥ 5.0` (HIGH); or `observed ≥ 2× median`; or `demand_multiplier > 1.05` (HIGH) | the **event-driven** path is the reliable one — the multiplier is directly observable, no inference needed. A `DEMAND_NORMALISED` note is emitted when the multiplier reverts. |
| **D2** inventory drop | `excess = (prev_inv − cur_inv) − demand ≥ 250 L` **and** `≥ 20%` of prev inventory | fuel left the station *without being served* — a **silent spill**, a manual removal, or a booking. Cross-checked against `station_inbound` to name the cause. This detector makes the simulator's silent-spill behaviour **visible**. |
| **D3** depot bottleneck | `dispatch_remaining ≤ 500 L` or `used/cap ≥ 0.95` | escalates when the depot is also `CONSTRAINED` |
| **D4** route bottleneck | `route.status ≠ AVAILABLE` → CRITICAL; or `in_flight/max_shipment ≥ 0.9` with demand exceeding projected supply | the **single-connected** case (`tongi`, `coxsbazar`) is CRITICAL — no rerouting path exists |
| **D5** supply shortfall | projected depot inventory at the next arrival < demand over the gap | uses the `+1` supply lag |
| **D6** projected stockout | forwards `StockoutRisk` when `p ≥ 0.5` and `ticks_to_stockout ≤ horizon` | not statistical — a projection, not an inference |

### 11.1 The `ObservationLog`

`/v1/demand-history` does not carry station inventory, so the observation history is a **local
bounded log** (`backend/app/state.py`), appended in the broker's existing
`refresh_snapshot` path — one hook call, no change to the broker's own logic. Bounded at 4096
entries.

`WorldSnapshot` itself gains **no fields**. It stays a faithful mirror of the simulator; derived
state lives in the new singletons. This preserves the "REST is the truth" line.

Anomaly ids are deterministic (`sha1(kind, entity, tick)[:12]`), so the same history always
produces the same ids.

---

## 12. The explanation layer (`backend/app/explain.py`)

Deterministic, pure, and **auditable by construction**.

```python
@dataclass(frozen=True)
class Explanation:
    schema_version: str = "1.0"
    kind: str = "plan"          # plan | incident | rejection | stockout_risk
    title: str = ""
    headline: str = ""
    details: list[str] = field(default_factory=list)
    numbers: dict[str, float] = field(default_factory=dict)
    verdict: str = "INFORMATIONAL"   # RECOMMENDED | CAUTION | BLOCKED | INFORMATIONAL
    alternatives: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
```

### 12.1 The central discipline

**Every figure quoted in `headline` or `details` also appears in `numbers`.**

That single rule makes the prose auditable (a test asserts it) and is what makes the optional
LLM layer safe: the LLM may only touch prose, and a post-hoc check rejects any prose that
quotes a number absent from `numbers`.

```python
def numbers_in(text: str) -> list[float]: ...
def unaccounted_numbers(explanation: Explanation) -> list[float]: ...
```

### 12.2 The verdict

| Verdict | Meaning |
| :--- | :--- |
| `RECOMMENDED` | execute it; the risk is acceptable and the benefit is quantified |
| `CAUTION` | executable, but something specific is worth knowing first |
| `BLOCKED` | the plan is not executable as it stands |
| `INFORMATIONAL` | no action implied (e.g. a stockout risk below 10%) |

### 12.3 The empty-plan branch

An empty plan says so plainly: `"No shipment is needed within the horizon; projected service
level X%."` with `total_litres` and `unmet` zeroed. A plan that reports `5000 L across 0
shipment(s)` is a bug, and the test asserts the headline says *no shipment*.

---

## 13. The optional LLM layer (`backend/app/llm.py`)

**Disabled by default** (`LLM_ENABLED=false`). The system is fully functional with no language
model anywhere in the loop.

The layer is **structurally incapable of being the decision authority**:

1. **An AST import-graph test forbids it.** `llm.py` may not import `optimizer`, `validator`,
   `state`, `journal`, `dispatch`, `forecast`, `anomaly`, `simulator_client`, `broker` or
   `config`. The test parses the module's AST and fails the build if any of those appear.
2. **It receives only a finished immutable `Explanation`** — never a plan, never a snapshot.
3. **It may return prose only** — a string. It cannot return a shipment.
4. **Unsafe prose is discarded.** `is_safe_prose()` rejects any output containing a number with a
   decimal point or magnitude ≥ 1000 that is not declared in `numbers`.

`GET /api/optimize/llm` reports the layer's state plainly:

```json
{"status": "OK", "enabled": false, "available": false,
 "role": "decorative_prose_only",
 "note": "The prose layer is disabled. Every explanation is produced deterministically
          by the backend, with no language model in the loop."}
```

`LLMConfig.to_dict()` reports `has_api_key` as a boolean — the key itself is never echoed.

**No new dependency.** The optional provider is called over plain HTTP with `httpx`, already in
`backend/requirements.txt`.

---

## 14. Phase 2 API surface

| Method | Path | Purpose |
| :--- | :--- | :--- |
| POST | `/api/optimize` | **Read-only.** Compute the plan. No write. |
| POST | `/api/optimize/execute` | Compute, gate, and execute the **current-tick** shipments |
| GET | `/api/optimize/solver` | Which solver is active, and why |
| GET | `/api/optimize/llm` | The state of the optional prose layer |
| GET | `/api/optimize/{plan_id}` | A recorded plan (from `dispatch_plans`) |
| GET | `/api/forecast` | Forecast for the current tick + horizon |
| GET | `/api/forecast/summary` | Cross-station roll-up |
| GET | `/api/forecast/{station_id}` | One station: per-fuel series, risk, coverage, explanations |
| GET | `/api/forecast/validate` | Compare the forecast against live `/v1/demand-history` — **the oracle, exposed for judges** |
| GET | `/api/anomalies` | Current anomalies (`?kind=`, `?severity=`, `?since_tick=`) |
| GET | `/api/anomalies/summary` | Counts by kind and severity |
| GET | `/api/anomalies/{id}` | One anomaly with its evidence |
| GET | `/api/anomalies/{id}/explain` | Its deterministic explanation |
| GET | `/api/incidents` | Grouped incidents |
| GET | `/api/incidents/{id}` | One incident + its deterministic explanation |

### 14.1 `POST /api/optimize`

Body:

```json
{"horizon_ticks": 16, "solver": "auto", "station_ids": null,
 "fuel_types": null, "safety_stock_fraction": 0.0, "reason": "..."}
```

The response separates `shipments_now` from `shipments_later`, reports
`gated: {accepted, rejected, rejections, total_litres}` — making the safety property *visible*
to the operator — and carries a deterministic `explanation`.

A restricted request (`station_ids` / `fuel_types`) plans against a **copy** of the snapshot;
the live snapshot is never mutated, and routes whose destination is filtered out are dropped
too, so the optimiser never sees a route it is not allowed to use.

### 14.2 `POST /api/optimize/execute`

Steps: refresh snapshot → build the forecast from **fresh** `/v1/stations` + `/v1/regions` +
`/v1/instance` → `optimize()` → `gate_plan()` (**every** candidate through `validate_and_plan`)
→ record the plan → journal intent → `create_allocation` → journal result → refresh snapshot →
return `created` / `replayed` / `failed` / `gated` / `plan`.

### 14.3 `GET /api/forecast/validate` — the oracle, exposed

Compares the offline forecast against live `/v1/demand-history`. The live result:

```json
{"verdict": "EXACT", "matched": 12, "mismatched": 0, "largest_error_litres": 0.0}
```

The forecast is anchored at the history's **earliest** tick — otherwise `demand_at` returns
`0.0` outside the horizon and the comparison reports a false mismatch.

---

## 15. Phase 2 configuration

All use the existing `_env_*` helpers and the `Settings.from_env()` pattern.

| Variable | Default | Meaning |
| :--- | :--- | :--- |
| `OPTIMIZER_HORIZON_TICKS` | `16` | Planning horizon |
| `OPTIMIZER_MAX_SHIPMENTS` | `64` | Cap on shipments per plan |
| `OPTIMIZER_SAFETY_STOCK_FRACTION` | `0.0` | Extra buffer for single-connected stations |
| `OPTIMIZER_SOLVER` | `auto` | `greedy` \| `lp` \| `auto` |
| `FORECAST_HORIZON_TICKS` | `16` | Forecast horizon |
| `FORECAST_HISTORY_LIMIT` | `200` | Bounded `demand-history` read |
| `FORECAST_START_TIME` | `2026-01-01T00:00:00+00:00` | The simulator's epoch |
| `ANOMALY_HISTORY_WINDOW` | `32` | Rolling observation window |
| `ANOMALY_SPIKE_Z_THRESHOLD` | `3.0` | D1 MEDIUM threshold |
| `ANOMALY_SPIKE_SEVERE_Z` | `5.0` | D1 HIGH threshold |
| `ANOMALY_INVENTORY_DROP_ABS` | `250.0` | D2 absolute drop (litres) |
| `ANOMALY_INVENTORY_DROP_RATIO` | `0.20` | D2 relative drop |
| `ANOMALY_BOTTLENECK_THRESHOLD` | `500.0` | D3 remaining headroom (litres) |
| `ANOMALY_INCIDENT_WINDOW_TICKS` | `8` | Incident grouping window |
| `ANOMALY_OBSERVATION_LOG_MAX` | `4096` | Observation log bound |
| `LLM_ENABLED` | `false` | The optional prose layer |
| `LLM_PROVIDER` / `LLM_MODEL` / `LLM_BASE_URL` | — | Provider details |
| `LLM_API_KEY` | — | Never echoed back |
| `LLM_TIMEOUT` | `4.0` | Seconds |

**`backend/requirements.txt` is unchanged.** `pulp` is optional and, per §9.6, is not installed.

### 15.1 The broker's settings

| Variable | Default | Meaning |
| :--- | :--- | :--- |
| `POLL_INTERVAL` | `5.0` | Seconds between poll-only reconciliation cycles |
| `WATCHDOG_INTERVAL` | `5.0` | How often the watchdog samples the tick counter |
| `SSE_MAX_LIFETIME` | `900.0` | Seconds before an SSE subscription is force-recycled |
| `SSE_STALE_AFTER` | `45.0` | Silence after which the stream is suspected dead |
| `FETCH_CONCURRENCY` | `4` | **World reads in flight against the simulator at once** |

`FETCH_CONCURRENCY` is the setting that keeps the simulator alive. See §18.

---

## 16. Phase 2 verification

| File | Purpose |
| :--- | :--- |
| `tests/test_forecast.py` | Pure unit tests + the golden fixture oracle |
| `tests/test_optimizer.py` | Constraint, determinism and rerouting properties |
| `tests/test_optimizer.py` | The "never worse than the naive baseline" matrix (see also `tests/test_lp_solver.py`) |
| `tests/test_optimizer_against_simulator.py` | **The oracle** — soundness against the live simulator, randomised differential test |
| `tests/test_optimizer_api.py` | The HTTP surface |
| `tests/test_anomaly.py` | Detector properties, determinism, silent-spill attribution |
| `tests/test_anomaly_api.py` | The HTTP surface |
| `tests/test_explain.py` | Determinism + the "every number is declared" auditability property |
| `tests/test_llm_isolation.py` | The AST import-graph guard + unsafe-prose rejection |
| `tests/test_lp_solver.py` | The import guard, the fallback, and the LP-vs-greedy measurement |
| `tests/test_forecast_api.py` | The HTTP surface, including the validate oracle |

### Commands

```bash
docker compose up -d --build backend
docker compose exec -T backend pytest tests/ -q 2>&1 | tail -3
docker compose exec -T client-verifier python verify_simulator.py \
  --base-url http://simulator-api:8000 --redis-url redis://redis:6379/0
```

> **Always run the backend suite in-container.** The host Python (3.12) lacks `pytest-asyncio`,
> so `asyncio_mode = auto` is ignored and every `async def` test is **silently skipped** — the
> run reports green while skipping the broker and simulator-oracle tests entirely. Check the
> summary line's skipped count before trusting a local run.

### The live oracle

`tests/test_optimizer_against_simulator.py` mirrors
`tests/test_validator_against_simulator.py` in structure. The critical property is **soundness**:

> Every gated shipment for the current tick is **accepted** by the live simulator — no `409`.

The simulator is reset before every comparison, so each iteration is judged against a world the
optimiser actually saw. The suite also asserts that a disrupted route is never used, that a
dual-connected station **is** rerouted, that the dispatch cap and depot inventory are never
oversubscribed, that multi-tick keys are unique and replayable, and that the key embeds the
dispatch tick.

### The drift guards

| Guard | Enforces |
| :--- | :--- |
| `tests/test_client_parity.py` | `backend/app/simulator_client.py` stays byte-identical to `verifier/simulator_client.py` |
| `tests/test_llm_isolation.py` | `llm.py` never imports the decision machinery |
| `tests/test_lp_solver.py` | the LP still does not beat greedy by more than 1% |

---

## 17. Phase 2 operational notes

* **The optimiser is read-only by default.** `POST /api/optimize` never writes. Writing
  requires `POST /api/optimize/execute`, which is explicit.
* **A stale plan is a wrong plan.** The routers always refresh the snapshot before planning,
  because the forecast depends on the *current* `demand_multiplier`.
* **Journal failure never blocks a dispatch**, and dispatch failure never corrupts the plan
  record — the plan is journalled *before* any shipment is written.
* **A second execute at the same tick creates nothing new.** The simulator stamps
  `created_tick = now`, so the dispatch cap is already consumed; the plan is recomputed and is
  legitimately smaller or empty rather than replaying.
* **The verifier is untouched.** Phase 2 is additive — `verifier/` was not modified, so the
  49/49 gate could not break, and it did not.

---

## 18. Incident: the simulator's connection pool wedged

**Date:** 2026-11-25. **Severity:** the whole stack went read-only. **Cause:** two
independent bugs on our side, both of which exhausted the simulator's 15-connection
pool. **Status:** fixed and covered by regression tests.

### 18.1 What it looked like

The simulator container reported `healthy` — its Docker healthcheck kept passing —
while every real request hung:

```
INFO:     172.19.0.5:59732 - "GET /v1/depots HTTP/1.1" 200 OK
INFO:     172.19.0.5:59734 - "GET /v1/instance HTTP/1.1" 500 Internal Server Error
```

The backend degraded exactly as designed:

```json
{"status": "degraded",
 "upstream": {"reachable": false, "error": "Timeout after 3.0s calling GET /v1/health"},
 "broker": {"mode": "polling"}}
```

The console showed its "Upstream unreachable" banner and kept polling. **That
fallback worked** — but it was masking a total outage behind a healthy-looking
healthcheck.

### 18.2 The evidence

The simulator's own logs named the cause outright:

```
sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow 10 reached,
connection timed out, timeout 30.00
```

Its pool is `pool_size=5` + `max_overflow=10` = **15 connections**. And the
process was holding exactly that:

```console
$ ls -l /proc/7/fd | grep -c fuel_simulator
15
```

Fifteen open handles on the database file, for a pool of fifteen. The pool was
100% exhausted, and every subsequent request blocked for 30 seconds and then
failed.

### 18.3 Why one request holds *two* connections

The simulator opens a session in its fault-injection middleware, and FastAPI opens
another for the request's own `Depends(get_db)`:

```python
# simulator/app/main.py -- fault middleware, runs on every request
db = SessionLocal()
try:
    response = await apply_request_faults(db, request)
    ...
finally: db.close()
```

So each in-flight request demands **two** connections from the pool.

### 18.4 Cause 1 — the snapshot fetch demanded more than the pool had

The broker read the world with two `asyncio.gather` calls, firing up to eight
requests concurrently:

```python
instance, depots, stations, routes, allocations, metrics = await asyncio.gather(...)
supply_arrivals, events = await asyncio.gather(...)
```

Eight concurrent requests × two connections each = **16 demanded**, from a pool of
**15**. The SSE subscription — which holds its connection for the life of the
response — made it 17.

**Fix:** bound the concurrency of the *individual reads*, not of the snapshot as a
whole. Gating the whole snapshot would still let its first batch of six run at
once, which is more than the bound allows.

```python
async def read(make_coro):
    async with semaphore:
        return await make_coro()

instance, depots, stations, routes, allocations, metrics, supply_arrivals, events = \
    await asyncio.gather(read(self.client.instance), read(self.client.depots), ...)
```

The coroutine is passed as a *factory*, not as a coroutine object — otherwise the
coroutine is created eagerly and, if the semaphore blocks, it is never awaited.

`FETCH_CONCURRENCY` defaults to **4**, which demands 8 connections at most and
leaves ample headroom for the SSE subscription and the watchdog's reachability
probe.

### 18.5 Cause 2 — the SSE subscription was closed lazily, not deterministically

The broker's lifetime cap recycled the SSE subscription every 15 minutes:

```python
async for event in self.client.stream(reconnect=False):
    ...
    if time.monotonic() - started > self.settings.sse_max_lifetime:
        break        # <-- abandons the generator
```

**Abandoning an async generator does not close it.** The generator is left
suspended at its `yield`; its `finally` — where the HTTP response is closed — does
not run at that moment. CPython finalises it later, via the event loop's
async-generator finaliser hook, *and only if the loop gets a scheduling slot*.

That is the whole problem. Under load the loop does not get a slot: a request
blocked on the exhausted pool holds the loop for 30 seconds at a time, and the
pending finalisers never run. **The pool can never drain.** It is a one-way
ratchet, and restarting the simulator was the only way out.

**Fix:** close the subscription deterministically, in a `finally`, and make the
close cancellation-safe.

```python
subscription = self.client.stream(reconnect=False)
try:
    async for event in subscription:
        ...
finally:
    await self._close_subscription(subscription)
```

`_close_subscription` swallows the ways closing can fail — including
`RuntimeError: coroutine ignored GeneratorExit`, which would otherwise mask a
cancellation — while still raising `CancelledError` so the broker's own shutdown
path is unaffected.

### 18.6 The lesson

Both bugs were on our side, and both were invisible to the healthcheck. The
simulator's Docker healthcheck passed throughout, because the healthcheck
endpoint was served from a cached response while every *other* endpoint blocked.

The general lesson is about **shared, finite resources**:

> A client that talks to a service with a small connection pool is responsible
> for staying inside that pool's capacity, whether or not the service documents
> the limit. And a resource that must be released — a connection, a socket, a
> file handle — must be released *deterministically*, not whenever the garbage
> collector next gets around to it.

The second point is the generalisable one. Deferring cleanup to a finaliser is
fine when the process is healthy. Under load, when the finaliser is needed
*most*, is exactly when it is least likely to run.

### 18.7 The regression tests

| Test | What it pins |
| :--- | :--- |
| `test_the_snapshot_fetch_does_not_overrun_the_simulator_connection_pool` | the eight reads are never all in flight at once |
| `test_the_fetch_concurrency_is_configurable` | the bound is a setting, not a constant |
| `test_the_sse_subscription_is_closed_before_the_next_one_opens` | a new subscription never opens while the previous still holds its connection |
| `test_the_sse_subscription_is_closed_when_the_broker_stops` | stopping the broker releases the subscription |

### 18.8 What was *not* changed

**The simulator was not modified.** It is the provided BUP simulator, and the
49-check verifier depends on it being exactly as delivered. The fix belongs on
our side, where the load is generated — and that is also where the bug was.

`verifier/` was not touched. The gate still reads **ALL 49 CHECKS PASSED**.

---

# Part III — Observability, resilience & load testing

## 19. The observability, resilience and scenario surface

Three subsystems were added in the final phase. They are documented in
their own documents; this section is the index and the API summary.

| Document | What it covers |
| :--- | :--- |
| `docs/OBSERVABILITY.md` | The metric catalogue, the §16 health table, the pool-saturation signal, the Prometheus + Grafana overlay. |
| `docs/RESILIENCE.md` | The circuit breaker's state machine, the failure classification, the degraded-mode contract per endpoint. |
| `docs/SCENARIO.md` | The scenario configuration schema, the resolution order, how to make a new scenario. |
| `docs/LOAD_TEST.md` | The workload definition, the report schema, the measured results. |
| `docs/DEMO.md` | The 14-beat live demonstration script. |
| `docs/ARCHITECTURE.md` | The as-built diagram and the data flow. |

### 19.1 The new endpoints

| Endpoint | Returns |
| :--- | :--- |
| `GET /metrics` | The Prometheus text exposition. **Bare path, no `/api` prefix.** `text/plain; version=0.0.4`. |
| `GET /api/observability` | The JSON mirror of the exposition, plus the component states and the error rate. The console's System tab reads this. |
| `GET /api/dlq` | The dead-letter queue's entries, with a count. |
| `POST /api/dlq/{id}/retry` | Replay one entry through its original handler. |
| `GET /api/health` | **Extended**, not replaced. The pre-existing keys are preserved; the §16 table is added alongside. |

> **`/metrics` and `/api/metrics` are different endpoints.**
> `/api/metrics` already existed and returns the simulator's *business* KPIs
> as JSON for the console. `/metrics` is the backend's own instrumentation in
> the Prometheus text format, for a scraper. Two audiences, two formats, two
> paths — and they must not drift.

### 19.2 The circuit breaker

`backend/app/circuit_breaker.py` **wraps** the simulator client rather than
editing it, because `backend/app/simulator_client.py` must stay
byte-identical to `verifier/simulator_client.py` (the SHA256 drift guard in
`tests/test_client_parity.py`).

| Application state | `app.state.client` | `app.state.simulator_client` |
| :--- | :--- | :--- |
| Normal | the breaker | the raw client |
| A call through it | protected by the gate | — |
| The health probe | **bypasses** the breaker | the raw client |

The health probe deliberately bypasses the gate: an open breaker means *we*
are not calling the upstream, not that the upstream is down. Probing through
the gate would report "UNHEALTHY" for a simulator that is answering fine.

**The degraded-mode contract:**

| Endpoint | While the breaker is OPEN |
| :--- | :--- |
| `GET /api/state`, `/api/stations`, `/api/depots`, `/api/routes` | **200** with the last known good world, plus `degraded: true` and `degraded_reason`. |
| `GET /api/health` | **200** with `status: "degraded"` and the circuit state. Never a non-200 — the container healthcheck must not kill the backend at the moment it should be serving the cached world. |
| `POST /api/plan`, `/api/optimize` | **503** with `{"error": {"code": "CIRCUIT_OPEN"}}`. A plan computed against a stale world is a *wrong* plan. |
| `POST /api/optimize/execute`, `/api/allocations` | **503**, same code. |

### 19.3 The metrics

`backend/app/metrics.py` — a hand-rolled Prometheus writer. The
`prometheus_client` package is deliberately **not** a dependency: the
backend image stays slim and offline-buildable, and the writer is small
enough to read.

Two details that are easy to get wrong and are therefore guarded by tests:

- **The path label is the route template**, never the raw path — otherwise
  `/api/stations/station-mirpur` and `/api/stations/station-tongi` become
  separate time series and the cardinality explodes.
- **The p95 pools the label sets.** A histogram's samples live under their
  labels, so reading the percentile with no labels returns the *empty* key.
  `percentile_across()` pools the buckets first, which is exactly what
  Prometheus does over an aggregated histogram. Before the fix, the health
  endpoint reported `p95_seconds: 0.0` while serving 12,681 requests.

### 19.4 The pool-saturation instrumentation

The backend cannot read the simulator's internal connection pool, but it
*owns* the semaphore that bounds its demand on it — so it measures its own
saturation exactly:

| Metric | Meaning |
| :--- | :--- |
| `backend_simulator_inflight_reads` | Reads currently in flight. |
| `backend_simulator_fetch_concurrency_limit` | The semaphore's limit (4). |
| `backend_simulator_pool_saturation_ratio` | `inflight / limit`. **Above 0.8 is the early-warning line.** |
| `backend_simulator_read_queue_depth` | Callers waiting on the semaphore. |

This is the signal §18's pool wedge did not have. It is now measured, and
`backend/tests/test_pool_saturation.py` turns it into an executable
invariant: the semaphore bound is never exceeded, and the saturation gauge
never exceeds 1.0.

### 19.5 The scenario configuration

`backend/app/scenario.py` loads and validates `backend/scenarios/baseline.yaml`
at boot. The forecast's public names (`PROFILES`, `REGION_DEMAND_FACTORS`,
`FORECAST_START_TIME`) are unchanged but are now *derived* from the loaded
config.

**The guard is the oracle test.** `backend/tests/test_forecast.py` asserts 60
of 60 exact matches against the live simulator, so if the YAML and the
simulator ever disagree, the test fails loudly. A config file that silently
disagrees with the world model produces a forecast that is wrong by
thousands of litres and looks perfectly plausible.

### 19.6 The scripts

| Script | What it does |
| :--- | :--- |
| `scripts/verify-all.sh` | The harness entry point. Runs the verifier suite, the backend tests, the frontend tests, the observability self-check and the load test, and emits one JSON object. Exit code 0 only if everything passed. |
| `scripts/check_observability.py` | 46 assertions over the exposition and the health table: every §15 family present, the right labels, the right content type, the §16 components in a known state set. |
| `scripts/demo-normal.sh` | Beats 1–8 of the demo. |
| `scripts/demo-crisis-fault-recovery.sh` | Beats 9–14, including the fault injection and the observed breaker transition. |
| `loadtest/run.sh` | The load test. Picks k6 if installed, the Python harness otherwise. Both emit the same JSON schema. |
| `.github/workflows/ci.yml` | The CI workflow. Runs `scripts/verify-all.sh` — the same command a human runs — on every push to `main` and every pull request, and uploads the JSON report as an artifact. The load test is included on `main` and skipped on pull requests, because it deliberately puts the backend under load. |
