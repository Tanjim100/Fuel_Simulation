# Resilience

PDF §12 asks for one thing when a backend dependency is unavailable: **retry,
cached state, or degraded mode**. It lists circuit breakers as an encouraged
mechanism, and says plainly that *"sophisticated fault-tolerance infrastructure
is not mandatory; clear and demonstrable behavior is more important."*

This document is the clear, demonstrable behavior.

---

## The problem the breaker solves

The simulator client already retries: 3 attempts, full jitter, a 4-second
ceiling per attempt. So **one logical call can block for roughly 13.75
seconds**.

That retry budget is right for one flaky request and catastrophic for a
service. If the upstream is genuinely down, *every* request pays the full
budget, and the backend stops answering altogether. A user pressing "refresh"
gets a spinner for 14 seconds and then a timeout.

The circuit breaker converts that 13.75-second stall into a **1-millisecond
rejection**.

```
                    ┌──────────────────────────────┐
   request ────────▶│  breaker: is the upstream up? │
                    └──────────────┬───────────────┘
                                   │
              CLOSED ──────────────┼──────────────▶ call the upstream
                                   │
              OPEN  ──────────────┼──────────────▶ reject immediately (1 ms)
                                   │                  503 CIRCUIT_OPEN
              HALF_OPEN ───────────┴──────────────▶ one probe, then decide
```

---

## Why it wraps the client rather than editing it

`backend/app/simulator_client.py` must stay **byte-identical** to
`verifier/simulator_client.py`. The drift guard in
`backend/tests/test_client_parity.py` enforces this with a SHA256 comparison,
and the 49-check verifier suite runs the verifier's own copy.

Editing the client would mean editing the verifier's reference copy first,
re-running the 49-check suite, then re-copying — for a change that has
nothing to do with the client.

So the breaker is a **new module that wraps** the client:

```python
app.state.client = breaker             # every call path is protected
app.state.simulator_client = raw       # for anything that must bypass it
```

The bypass is used by the health probe (which must report the *upstream's*
liveness, not the breaker's opinion) and by the breaker itself.

`__getattr__` proxies unknown attributes to the wrapped client, so every
public method is reachable through the breaker and every one is protected.
Plain attributes are forwarded as-is — wrapping a string into a coroutine
function is exactly the kind of subtle breakage that makes a proxy dangerous.

---

## The failure classification

**This is the load-bearing part of the design.**

Not every failure means the upstream is unavailable:

| Class | Examples | Counts toward the breaker? |
| :--- | :--- | :--- |
| **Retryable** | `SimulatorTimeout`, `SimulatorFaultInjected` (503), a transport `SimulatorError` with no status code | **Yes** |
| **Terminal** | `SimulatorNotFound` (404), `SimulatorConflict` (409), `SimulatorValidationError` (422) | **No** |
| **Unknown** | an exception never seen before | Yes — but counted separately so the metrics show it |

A **409 from the simulator is a business answer**, not an outage: "that route
is over capacity", "that tank is full". The simulator understood the request
and said no.

Tripping the breaker on a 409 would take the whole backend down because one
station's tank was full — which is **exactly the failure mode the pre-flight
validator exists to prevent**. The validator exists so that the backend never
asks for something illegal; if it does, the answer must not take the service
down.

So: only retryable failures move the breaker. A business rejection leaves it
CLOSED. This is pinned by a dedicated test
(`test_a_terminal_error_never_trips_the_breaker`).

---

## The state machine

```
CLOSED  ──(N consecutive retryable failures)──▶  OPEN
OPEN    ──(after the cooldown elapses)─────────▶  HALF_OPEN
HALF_OPEN ──(one success)──────────────────────▶  CLOSED
HALF_OPEN ──(one failure)──────────────────────▶  OPEN
```

| Setting | Env | Default | Meaning |
| :--- | :--- | ---: | :--- |
| `breaker_failure_threshold` | `BREAKER_FAILURE_THRESHOLD` | 5 | Consecutive retryable failures before opening. |
| `breaker_cooldown_seconds` | `BREAKER_COOLDOWN_SECONDS` | 15.0 | How long to stay OPEN before probing. |
| `breaker_enabled` | — | true | Off: count only, never transition. |

### Two details that matter

**The gate is synchronous.** `_allow()` needs no I/O, so it does no awaiting.
An OPEN breaker therefore rejects in microseconds, with no chance of waiting
on a lock another coroutine holds.

**Only one half-open probe at a time.** The cooldown expiring moves the
breaker to `HALF_OPEN` synchronously — it is the single point where the
breaker probes, so exactly one caller gets the probe. Everyone else waits for
the lock, sees the probe already ran, and is rejected.

### A success clears the streak

`_record_success()` resets `_consecutive_failures` to zero. The threshold is a
count of *consecutive* failures, not a count of failures ever seen — an
upstream that fails once and then recovers must not be one failure away from
opening forever after.

---

## The degraded-mode contract

While the breaker is OPEN, the read and write paths behave **differently on
purpose**.

### Reads serve the cached world

`GET /api/state`, `/api/stations`, `/api/depots`, `/api/routes` return
**HTTP 200** with the last-known-good snapshot and an explicit flag:

```json
{
  "status": "OK",
  "tick": 1,
  "degraded": true,
  "degraded_reason": "The simulator circuit breaker is OPEN; this is the last known good world snapshot.",
  "stations": [ ...4 stations... ]
}
```

Showing the last known good world with an honest flag is strictly better than
a 502. The operator sees the last state the system knew, and the console's
existing "Stale data" banner path carries the warning — no new UI path was
invented.

### Writes refuse

`POST /api/plan` and `POST /api/optimize` **keep failing**, with the
canonical error envelope:

```json
{
  "status": "ERROR",
  "error": {
    "code": "CIRCUIT_OPEN",
    "message": "The simulator circuit breaker is OPEN; 'optimize' was rejected without calling the upstream (retry in 12.4s)."
  }
}
```

HTTP **503**.

A plan computed against a stale world is a **wrong plan**. Writing it would be
worse than an error — the operator would see a successful dispatch on the
console while the fuel went somewhere that no longer needs it.

### Health degrades, but never fails

`GET /api/health` stays **HTTP 200** with `status: "degraded"`. The container
healthcheck must never see a non-200: a health endpoint that fails during an
outage gets the container killed for being unhealthy at exactly the moment it
should be serving the cached world.

---

## The measured recovery timeline

Recorded live via `scripts/demo-crisis-fault-recovery.sh`:

| Step | Command | Observed |
| :--- | :--- | :--- |
| Inject the fault | `POST /admin/faults {"type":"unavailable","duration_seconds":60}` | HTTP 201 |
| The breaker trips | — | `state: OPEN`, `consecutive_failures: 8` |
| A read degrades | `GET /api/state` | **HTTP 200**, `degraded: true`, tick 1, 4 stations |
| A write refuses | `POST /api/plan` | **HTTP 503**, `{"code":"CIRCUIT_OPEN"}` |
| The gate stays honest | — | the plan is rejected with `reject_code: ROUTE_DISRUPTED` — "route route-gazipur-mirpur is DISRUPTED and cannot carry a shipment" |
| Clear the fault | `POST /admin/faults/clear` | HTTP 200 |
| The breaker closes | — | **on its own**, `state: CLOSED`, `status: ok`, no `degraded` key |

**The recovery is automatic.** Nothing polls, nothing nudges. The cooldown
elapses, the breaker probes, the probe succeeds, and the breaker closes.

---

## The other resilience mechanisms

### The journal (write idempotency)

Every shipment is journalled **before** it is sent. A crash mid-dispatch
replays as an idempotent `201` rather than a duplicate shipment. The journal
is a SQLite file at `/app/data/journal.sqlite3` on a named volume, so it
survives a container restart.

### The dead-letter queue

`backend/app/dlq.py` is a bounded, on-disk queue with a Redis mirror. A
malformed SSE frame, an allocation payload that does not match the model, a
journalled intent whose body cannot be rebuilt — each becomes a DLQ entry
rather than an exception raised into the event loop.

**A single bad frame must not kill the world's event feed.** The subscription
survives; the bad frame is quarantined.

| Endpoint | What it does |
| :--- | :--- |
| `GET /api/dlq` | List the entries, with a count. |
| `POST /api/dlq/{id}/retry` | Replay one entry through its original handler. |

The size is published as `backend_dlq_size` and shown on the console's System
tab.

### The broker's watchdog

The simulator's SSE stream can **silently drop** events. The backend's
watchdog detects the world advancing without a matching event and falls back
to polling — so the console is still correct, just slower. The suspicion is
counted in `backend_broker_suspected_drops_total` and shown on the System tab.

### The broker's forced reconnect

`SSE_MAX_LIFETIME` bounds the blast radius of an undetected silent drop by
forcing a fresh subscription.

---

## The tests

| File | What it proves |
| :--- | :--- |
| `backend/tests/test_circuit_breaker.py` | The state machine; terminal errors never trip it; an OPEN breaker rejects in <1 ms; degraded mode serves the cached snapshot with `degraded: true`; write paths fail with `CIRCUIT_OPEN`. |
| `backend/tests/test_pool_saturation.py` | The semaphore bound is never exceeded under concurrency; the saturation gauge never exceeds 1.0. |
| `backend/tests/test_dlq.py` | A malformed frame is dead-lettered, not raised; the subscription survives it; an entry can be retried; the queue is bounded. |
| `backend/tests/test_observability_api.py` | An open circuit degrades the status even when the probe answers. |

---

## See also

- `docs/OBSERVABILITY.md` — the metric catalogue and the §16 health table.
- `docs/LOAD_TEST.md` — the measured limits.
- `docs/DEMO.md` — the live demonstration script.
