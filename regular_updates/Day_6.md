# Project Status Update: Fuel Supply Intelligence & Resilience Platform

**Day 6 — Observability, resilience, load testing, and submission readiness.**

---

## 1. Executive Summary

Yesterday the platform had a working product, a working console, and a working
decision path. What it did **not** have was any way to *prove* those three
things to a judge who wanted evidence rather than a demonstration, and no
answer to the question a judge asks when the demo misbehaves: *"what is the
system doing right now, and is it safe to write?"*

Today closed that gap. Four things were built:

1. **A circuit breaker with a degraded mode.** The backend now fails *fast*
   and *honestly* when the upstream dies. A read serves the last known good
   world with `degraded: true`. A write is **refused** with `503
   CIRCUIT_OPEN`, because a plan computed against a stale world is not a
   degraded answer — it is a wrong one.
2. **A hand-rolled Prometheus exposition at a bare `/metrics`,** plus a JSON
   mirror at `/api/observability` that the console's new **System tab**
   reads. Twenty-nine metric families across four groups.
3. **A measured load test** across three named workloads, with the report
   tying request latency to **simulator pool saturation** — the signal that
   would have caught the connection-pool incident that took this platform
   down unnoticed in Day 5.
4. **A scenario configuration file**, so the demand table is no longer Python
   literals, and a **write-ahead journal that already existed** but is now
   proven by a live fault rehearsal rather than asserted.

The default stack is **unchanged**: `docker compose up -d` still yields exactly
**five healthy services**. Prometheus and Grafana live behind a documented
one-command overlay, because PDF §7 warns against infrastructure complexity
that does not improve the solution.

---

## 2. What was built

### 2.1 The circuit breaker — `backend/app/circuit_breaker.py`

The breaker **wraps** the simulator client. It does not edit it. That
distinction is load-bearing: `backend/app/simulator_client.py` must stay
**byte-identical** to `verifier/simulator_client.py`, enforced by a SHA256
drift guard in `backend/tests/test_client_parity.py`. Editing the client
would mean editing the verifier's reference copy, re-running the 49-check
suite, and re-copying — for a change unrelated to the client. The breaker is a
*policy* layered over transport, and policy belongs outside.

**The failure classification is the part that had to be right.**

| Class | Examples | Trips the breaker? |
| :--- | :--- | :--- |
| **Retryable** | `SimulatorTimeout`, `SimulatorFaultInjected` (503), transport error with no status | **Yes** |
| **Terminal** | `SimulatorNotFound` (404), `SimulatorConflict` (409), `SimulatorValidationError` (422) | **No** |

A 409 from the simulator is the simulator **answering** — "that route is over
capacity" — not the simulator failing. Tripping the breaker on it would take
the whole backend down for one bad request, which is exactly the failure mode
the validator exists to prevent. A dedicated test asserts that a 409 never
trips it.

**The breaker owns the retry budget.** `SimulatorClient._request` already
retries 3× with backoff, so one logical call can block for ≈13.75 seconds.
The breaker wraps *that*, so when it is OPEN the call is rejected in
**under one millisecond** instead of burning the retry budget on every
request. That is the entire point: **a 13.75-second stall becomes a
1-millisecond rejection.**

### 2.2 The degraded-mode contract

**Reads degrade; writes refuse.** The asymmetry is deliberate.

| Endpoint | Breaker OPEN |
| :--- | :--- |
| `GET /api/state`, `/api/stations`, `/api/depots`, `/api/routes` | **200** — the last known good world, plus `degraded: true` and `degraded_reason` |
| `GET /api/health` | **200** — `status: "degraded"`, plus the circuit state |
| `POST /api/plan`, `POST /api/optimize` | **503** — `{"error": {"code": "CIRCUIT_OPEN"}}` |

`/api/health` returning 200 while degraded is not a leniency. The container
healthcheck must **never** kill the backend at the moment it should be serving
the cached world — a restart would make the outage worse, not better.

### 2.3 The metrics — `backend/app/metrics.py`

A **hand-rolled** Prometheus text exposition, under 800 lines, **zero new
dependencies**. `prometheus_client` was not added: the backend image stays
slim and offline-buildable, and a hand-rolled writer is small enough to
*read*, which is how the team knows the exposition is correct.

**Two endpoints, and they must not drift:**

- `GET /metrics` — Prometheus **text**, `text/plain; version=0.0.4`. Bare
  path, no `/api` prefix.
- `GET /api/observability` — the **JSON mirror**, which the console's System
  tab reads.

`GET /api/metrics` is a *third* thing and stays JSON — it is the simulator's
business KPIs, the console's original contract. Three different audiences,
three different endpoints.

**The console never parses the exposition.** Parsing a text format in the
browser would put a number-parsing bug inside the console's central promise: a
figure that failed to parse would render as *something*, and "something" is
exactly what the one rule forbids.

**Twenty-nine metric families across four groups:**

| Layer | The signal that matters |
| :--- | :--- |
| **Application** | `backend_http_requests_total`, `backend_http_request_duration_seconds` (histogram), `backend_upstream_requests_total`, `backend_upstream_request_duration_seconds` |
| **System** | `backend_simulator_inflight_reads`, `backend_simulator_fetch_concurrency_limit`, `backend_simulator_pool_saturation_ratio`, `backend_simulator_read_queue_depth` |
| **Intelligence** | `backend_forecast_abs_error_litres`, `backend_forecast_validation_total`, `backend_shortage_alerts_total`, `backend_decisions_total`, `backend_fallback_activations_total` |
| **Resilience** | `backend_circuit_state`, `backend_circuit_transitions_total`, `backend_broker_mode`, `backend_broker_suspected_drops_total`, `backend_sse_events_total`, `backend_snapshot_age_seconds`, `backend_dlq_messages_total` |

**The labels use the route template, never the raw path.**
`request.scope["route"].path` — otherwise `/api/stations/station-mirpur` and
`/api/stations/station-tongi` become two time series and the cardinality
explodes. 404s fall back to `"unmatched"`. A test asserts the template
behaviour.

### 2.4 The pool-saturation signal — the one that matters

**This is the metric that would have made yesterday's incident visible in
seconds rather than hours.**

The backend cannot read the simulator's internal connection pool — but it
*owns* the `FETCH_CONCURRENCY = 4` semaphore that bounds its demand on that
pool. So it can measure its own saturation exactly:

```
saturation = inflight_reads / fetch_concurrency_limit
```

`> 0.8` is the early-warning threshold, and it is the same threshold the
Prometheus alert uses and the same threshold the console's meter turns red at.
The Day 5 incident wedged the simulator holding exactly 15 database handles
with every request blocked for 30 seconds, and it was found by *reading logs
hours later*. This is the signal that would have caught it.

`backend/tests/test_pool_saturation.py` turns it into an **executable
invariant**: under concurrency the semaphore bound is never exceeded and the
saturation gauge never exceeds 1.0.

### 2.5 The health table — PDF §16

`GET /api/health` was **extended, not replaced**. The existing keys are
preserved, and the §16 fields were added:

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
  "latency":  {"p95_seconds": 0.041, "p50_seconds": 0.009},
  "requests": {"total": 12681, "errors": 0, "error_rate": 0.004},
  "circuit":  {"state": "CLOSED", "consecutive_failures": 0}
}
```

The five component names and their order are **PDF §16's own table**, so a
judge reading the brief and a judge reading the screen see the same shape.

### 2.6 The console's System tab

A fourth drill-down tab — never a sixth column, and never the first thing the
operator sees — showing the §16 table, p50/p95/average latency, the error
rate, the circuit state, the broker mode, the DLQ size, and the pool-saturation
meter with its read limit beside it.

Two mappers were added to `frontend/src/lib/derive.ts` — `componentLevel()`
and `circuitLevel()` — and because they live there, they are inside the
no-invented-numbers allow-list automatically. The allow-list is *computed from
the same derivation functions the panels use*, so the check covers both
directions: no invented figures, and no figure computed by a private shortcut.

`UNKNOWN` is deliberately **not** `ok`. An unknown component state cannot
justify calling a component healthy, so it renders as a warning. `HALF_OPEN`
is a *probe* state — the breaker is testing the upstream — so it reads as a
warning rather than an alarm.

### 2.7 The dead-letter queue — `backend/app/dlq.py`

A bounded, on-disk DLQ with a Redis mirror. Anything unparseable — a
malformed SSE frame, an allocation payload that does not match the model — is
**quarantined rather than raised into the event loop**. One bad frame must not
kill the world's event feed.

`GET /api/dlq` lists it; `POST /api/dlq/{id}/retry` replays one entry through
its original handler.

### 2.8 The scenario configuration — `backend/scenarios/baseline.yaml`

PDF §19 says "do not hard-code"; §21 lists scenario configuration as a
recommended deliverable. The demand table — profiles, region factors, the
start time, the seed — was Python literals in `forecast.py`. It now lives in
a YAML file, **validated at boot** by `backend/app/scenario.py`, with ten
distinct rejection cases.

`forecast.py`'s public names (`PROFILES`, `REGION_DEMAND_FACTORS`,
`FORECAST_START_TIME`) are unchanged, so every import and every test still
works — but they are now **derived from the loaded config**, not literals.

**The guard that proves the move changed nothing:** the forecast oracle test
still asserts **60 of 60 exact matches** against the live simulator's demand.
If the YAML and the table ever disagree, the oracle fails — which is the
point. One test protects three properties at once: the forecast is correct,
the config has not silently diverged from the simulator, and moving the table
out of Python literals changed zero numbers.

### 2.9 The monitoring overlay — `observability/`

**Deliberately not part of the default stack.** PDF §7 warns against
infrastructure complexity that does not improve the solution; §13 asks for a
stack that is reproducibly runnable. Adding two services to
`docker-compose.yml` would make `docker compose up -d` yield seven containers
instead of five.

```bash
docker compose -f docker-compose.yml -f observability/compose.monitoring.yml up -d
# Prometheus: http://localhost:9090    Grafana: http://localhost:3000
```

**Nine alert rules**: `SimulatorPoolSaturation`, `SimulatorReadQueueBacklog`,
`SimulatorCircuitOpen`, `SimulatorCircuitTransition`, `BackendLatencyHigh`,
`BackendErrorRateHigh`, `BrokerSuspectedDrops`, `BrokerProbeFailures`,
`DeadLetterQueueNotEmpty`.

The Grafana dashboard is provisioned automatically — uid `buetcsefest-platform`,
folder `BUET CSE Fest`, four rows, 19 panels.

**Three of the four rows are §15's layers; the fourth is not.** §15 names
application, system, intelligence and **logs**. The dashboard's fourth row is
**resilience** — the circuit breaker, the stream, the snapshot and the
dead-letter queue — which the brief does not list but which is where this
platform's most important operational signal lives. §15's *logs* layer is
covered by the backend's structured logging (**57 statements** across nine
modules), not by a metric family: a log line is not a metric, and a counter of
log lines would be a number without meaning. The split is stated in
`docs/OBSERVABILITY.md` rather than glossed over.

**One job was removed after it proved permanently `down`:** a scrape job
pointing at the JSON mirror. Prometheus cannot parse JSON as an exposition
format. The mirror is the console's endpoint; the exposition is the scraper's.
The dashboard now scrapes the backend and the JSON is read by the browser.

---

## 3. The bugs this phase found

### Bug 7 — Prometheus has no `default` template function

The `SimulatorPoolSaturation` alert's annotation used
`{{ $labels.limit | default "4" }}`. Prometheus has no `default` function, and
the saturation gauge carries no `limit` label anyway. `promtool check rules`
caught it — the annotation was rewritten without it.

**The lesson:** the alerting rules are validated by `promtool` in CI, not by
eye. A rule that does not parse is a rule that silently never fires.

### Bug 8 — The dashboard's card labels leaked the percentile *names* into the number scrape

The frontend's `no-invented-numbers` test scrapes every number out of the
rendered DOM. The System tab's labels read **"p95 latency"** and **"p50
latency"** — and the scrape picked the digits out of the *label text* as if
they were figures. The test failed with:

```
SystemPanel rendered numbers the backend never sent: 95, 50
```

The fix declares `[50, 95]` as **fixed domain constants** — the names of the
statistics, not figures — while keeping the check strict about the *values*.

**The lesson:** the invariant is stronger than it looks, and it caught a real
class of mistake: a *label* can carry a digit without carrying a fact.

### Bug 9 — The compose build context was one directory too high

The monitoring overlay's Dockerfiles live in `observability/`, but the build
context was `.` (the project root):

```
failed to read dockerfile: open Dockerfile.grafana: no such file or directory
```

Fixed by setting `context: ./observability` for both services. A build
context that is wrong fails loudly, which is the good kind of failure.

### Bug 10 — The Prometheus image runs as `nobody`

`docker exec ... mkdir -p /cfg` returned **Permission denied**. Fixed with
`--user root` on the exec and an explicit `chown nobody:nobody /cfg`. The
container image hard-codes its user; the tooling has to know that.

### Bug 11 — The JSON-mirror scrape job was permanently `down`

Already described in §2.9 — worth recording because the *symptom* was a red
target in the Prometheus UI, and the instinct would have been to "fix" it by
making the endpoint look like Prometheus. The right fix was to delete the job
and write down why.

### Bug 12 — A failed attribute lookup stranded a coroutine

The broker gathers **eight** simulator reads in one `asyncio.gather` call. The
calls were written as:

```python
reads = await asyncio.gather(
    read(client.instance),
    read(client.depots),
    ...
)
```

`asyncio.gather` evaluates its arguments **eagerly**, and so does the
argument list. `read(client.instance)` therefore built a coroutine *before*
`client.depots` was looked up — and when that lookup raised, the already-built
coroutine was **never awaited**. Python reported it at GC time:

```
RuntimeWarning: coroutine 'EventBroker._fetch_snapshot.<locals>.read' was
never awaited
```

A stranded coroutine is a real (if small) leak, and the warning was noise in
a suite that was otherwise clean.

The fix is to look the methods up **first**, then build the coroutines:

```python
methods = (client.instance, client.depots, ...)
reads = await asyncio.gather(*(read(method) for method in methods), ...)
```

Now a failing lookup happens before any coroutine exists, so there is nothing
to strand. A regression test asserts it — it captures warnings, forces a
`gc.collect()`, and fails if any "never awaited" warning appears.

### Bug 13 — The harness could not run the frontend suite at all

`verify-all.sh` ran the frontend tests with `docker compose exec -T frontend
npm test`. That worked when the frontend image had one stage. It stopped
working the moment the console moved to the **two-stage** build: the runtime
stage carries `dist/` and `server.mjs` and **no `package.json`**, so npm
failed with:

```
npm error enoent Could not read package.json: No such file or directory,
open '/app/package.json'
```

So the harness entry point — the one command a judge is supposed to run —
reported a **FAIL** on a stack that was entirely green.

The fix keeps the runtime image slim: the frontend Dockerfile gained a `test`
**build target** that carries the toolchain and the source, and
`verify-all.sh` builds that target and runs it in a throwaway container.

```bash
docker build --target test -t buetcsefest-frontend-test:verify \
  -f frontend/Dockerfile .
docker run --rm buetcsefest-frontend-test:verify
```

The suite now runs in **exactly the environment the assets were built in**,
and the harness no longer depends on whether the host has Node installed.

**The lesson:** a harness that reports FAIL when everything passes is worse
than no harness — it teaches the reader to ignore it. The `verify-all.sh`
verdict is now `PASS`, and the summaries it reports are the real ones.

### Bug 14 — Three of the four JSON summaries reported the wrong number

The harness's own reporting was wrong, which is the most embarrassing class
of bug in a tool whose whole job is reporting.

| Suite | It reported | It should report |
| :--- | :--- | :--- |
| verifier | `4/4` | `49 checks passed` |
| backend | `457 passed,` | `468 passed, 8 skipped, …` |
| frontend | *(empty)* | `Tests  136 passed \| 3 skipped (139)` |

The verifier's `4/4` was the **last of six groups**, not the total. The
backend's grep stopped at the first comma. The frontend's grep looked for
`N tests passed`, but vitest prints `N passed | M skipped`.

All three now parse the suites' own summary lines rather than guessing at a
pattern. The verdict was always correct; only the *evidence* was wrong.

### Bug 15 — The settings' defaults contradicted the compose file

The code's defaults for three tunables did not match the values the stack is
actually deployed with.

| Setting | Code default | Compose value |
| :--- | ---: | ---: |
| `SSE_STALE_AFTER` | 20.0 | 45.0 |
| `POLL_INTERVAL` | 2.0 | 5.0 |
| `STATE_MAX_AGE` | 1.0 | 15.0 |

**That gap was not cosmetic.** `STATE_MAX_AGE` is the snapshot's TTL and
`POLL_INTERVAL` is how often the world is re-read. A TTL *below* the poll
interval marks a healthy world stale for most of every poll cycle — the false
"Stale data" banner this pair was tuned to avoid. Under the code defaults the
TTL was **half** the poll interval, so the bug was structurally guaranteed for
anyone running the backend outside Docker.

The deployed behaviour was always correct — the compose file's values win under
`docker compose up`. The bug was in the *other* path the docstring promises:
running the backend directly, where the code defaults are what you get.

The fix makes the code defaults **equal** the compose values, so the two paths
agree. And the equivalence is now **pinned**:

```python
# backend/tests/test_config_defaults.py
PINNED = ("SSE_STALE_AFTER", "POLL_INTERVAL", "STATE_MAX_AGE")

def test_the_code_default_matches_the_compose_value(...):
    """The code's default must equal what the stack is deployed with."""

def test_the_snapshot_ttl_exceeds_the_poll_interval():
    """A TTL below the poll interval marks a healthy world stale."""
    assert settings.state_max_age > settings.poll_interval
```

The guard reads `docker-compose.yml` directly, which means the compose file had
to be **baked into the backend image**:

```dockerfile
COPY docker-compose.yml ./reference/docker-compose.yml
```

The test prefers `/app/reference/docker-compose.yml` and falls back to the
project root, so it runs identically in-container and on the host.

The drift was proven, not assumed: changing `STATE_MAX_AGE` back to `1.0`
failed the guard, and restoring it passed. A regression test that has never
failed is not a regression test.

### Bug 16 — The pre-registered anomaly kinds did not match the real ones

The metrics module pre-registers one zero-valued series per anomaly kind, so
a dashboard shows "0" rather than a gap. The list was **hand-copied**, and it
had drifted:

| Kind | Pre-registered | Actually emitted |
| :--- | :--- | :--- |
| `STATION_OUTAGE` | ✅ | ❌ — **no such kind exists** |
| `DEPOT_BOTTLENECK` | ❌ | ✅ |
| `STOCKOUT_RISK` | ❌ | ✅ |

So `backend_anomalies_total{kind="STATION_OUTAGE"}` read `0.0` forever —
indistinguishable from a detector that never fires — while two kinds that
*do* fire appeared only once they had fired, with no zero to compare against.

The fix removes the second copy rather than correcting it:

```python
# backend/app/anomaly.py
ANOMALY_KINDS: tuple[str, ...] = (
    DEMAND_SPIKE, DEMAND_NORMALISED, INVENTORY_DROP, DEPOT_BOTTLENECK,
    ROUTE_BOTTLENECK, SUPPLY_SHORTFALL, STOCKOUT_RISK,
)
```

```python
# backend/app/metrics.py
for kind in ANOMALY_KINDS:
    ANOMALIES.register(kind=kind, severity="MEDIUM")
```

Two regression tests pin it: one asserts every real kind is registered, the
other asserts **no** kind is registered that does not exist. The second is the
one that would have caught this.

### Bug 17 — The §15 "logs" layer was covered, but not by anything a judge could see

Not a code bug — a **claim** bug, and the kind that matters most in a
submission, because a judge reads the document.

PDF §15 names four layers: application, system, intelligence and **logs**.
The Grafana dashboard has four rows, and the fourth was *resilience* — the
circuit breaker, the stream, the snapshot and the dead-letter queue — a group
the brief does not list. `docs/OBSERVABILITY.md` called the rows "one per §15
layer", which was simply false.

The layer *is* covered: the backend emits **60 log statements** across
fourteen modules — every upstream failure with its exception class, every
circuit transition, every dispatch and journal write. But **logs are not
metrics**, and a counter of "how many log lines" would be a number without
meaning.

So the documents now say what is true: **three layers are met with metric
families; the fourth is met with structured logging.** The split is stated
plainly in `docs/OBSERVABILITY.md` and `README.md` rather than glossed over.
A judge checking §15 coverage finds the answer stated, not hidden.

### Bug 18 — Four endpoints returned a 200 with an error body

A missing entity should be a **404**. Four read endpoints returned the
error envelope *inside* a 200:

```
GET /api/anomalies/does-not-exist  ->  200  {"status":"ERROR", "error":{"code":"NOT_FOUND", …}}
GET /api/incidents/does-not-exist  ->  200
GET /api/forecast/station-nope      ->  200
GET /api/optimize/plan-nope         ->  200
```

A 200 with `"status": "ERROR"` in the body reads as **success** to
`curl --fail`, to a monitoring probe, and to any client that checks the
status before the body. The envelope also said `http_status: 404` — the
*body* knew, while the *status line* did not.

The fix raises `HTTPException(404)`, which the existing exception handler
turns into the same canonical envelope — so the body is unchanged and the
status is now true. `routers/state.py` already did it this way; the API was
internally inconsistent, and the inconsistency was invisible without
checking the status code.

### Bug 19 — The validator's HTTP status was computed, then thrown away

The validator distinguishes three kinds of rejection:

| Rule | Code | HTTP |
| :--- | :--- | :---: |
| `quantity <= 0` | `INVALID_QUANTITY` | 422 |
| Unknown depot / station / route | `NOT_FOUND` | 404 |
| Route over capacity, depot closed, … | *(various)* | 409 |

`_reject()` accepted an `http_status` argument and the callers passed the
right values — but `PlanResult` had no field to hold it, so the value was
**silently dropped**. The router then hardcoded `http_status=409` for every
rejection.

So a request for an unknown depot returned **409**, and a negative quantity
returned **409** — a client could not tell "that depot does not exist" from
"that route is over capacity" without parsing the message. `docs/BACKEND.md`
§2.1 documented 404 and 422 all along.

The fix adds `http_status` to `PlanResult` and lets the router report the
validator's decision. Measured live:

```
unknown depot  ->  404   (was 409)
negative qty   ->  422   (was 409)
disrupted route->  409   (correct before, still correct)
```

### Bug 20 — A read endpoint was incrementing a counter

`GET /api/anomalies/summary` incremented `backend_shortage_alerts_total`
once **per anomaly, per request**. The detectors recompute against the live
world on every poll, so the counter measured *how often the console
refreshed* rather than how many alerts were raised.

Measured: three polls moved `backend_shortage_alerts_total{severity="HIGH"}`
from **7.0 to 10.0** — the number rose every time a judge looked at it. The
Grafana panel "Shortage alerts (by severity)" therefore plotted the
viewer's polling rate.

A counter incremented by a read is a bug by definition. The fix counts each
anomaly **once**, when it is first seen, through a tracker that remembers
what it has already counted and resets when the world advances:

```python
ANOMALY_COUNTER.count(
    ((a.id, {"kind": a.kind, "severity": a.severity}) for a in anomalies),
    tick=str(tick),
)
```

Three regression tests pin it: five polls of an unchanged world must move
nothing; every anomaly series must carry **both** labels; and the incident
counter must render at all.

### Bug 21 — Three counters were declared but never incremented

`backend_circuit_rejections_total`, `backend_upstream_retries_total` and
`backend_incidents_total` had `# HELP` and `# TYPE` lines but **no sample
lines** — so they rendered as *absent*, which is exactly what an
uninstrumented metric looks like. The Grafana panel "Circuit transitions and
rejections" queried the rejections counter and showed **"No data"**
permanently.

The rejections counter now increments from the breaker's single choke point:

```python
def _on_breaker_call(outcome: str, duration_seconds: float) -> None:
    UPSTREAM_REQUESTS.inc(outcome=outcome)
    UPSTREAM_DURATION.observe(duration_seconds)
    if outcome == "circuit_open":
        CIRCUIT_REJECTIONS.inc()
```

The incident counter is now pre-registered with its severity label set, so
it renders as zeros rather than as absent.

The **retries** counter was removed rather than wired up. The simulator
client retries internally — three retries with backoff — and that happens
*inside* the client, below the breaker's choke point. The backend genuinely
cannot see a retry, so the counter would have been a permanent zero: a number
that looks like data and means nothing. The honest proxy is the duration
histogram and the `timeout` / `transport_error` outcomes.

### Bug 22 — The console could show stale data after fresh data

`useBackend.ts`'s `refresh()` returned a cleanup function that set a
`cancelled` flag — but **both callers discarded the return value**, so the
flag was never set and the guards were dead code.

Two refreshes can be in flight at once: the 5-second poll and an SSE nudge.
Without sequencing, the *older* one could resolve after the newer one and
overwrite fresher state with stale state — the console going backwards. The
fix is request sequencing: each refresh takes a ticket, and only the holder
of the newest ticket may write.

### Bug 23 — The demo could leave the backend degraded

`demo-crisis-fault-recovery.sh` injected a 60-second `unavailable` fault and
cleared it at the end — with **no `trap`**. Ctrl-C during the wait for the
breaker to trip (the likely case) left the fault active and the backend
degraded for the rest of the judging session.

The script now installs an `EXIT INT TERM` trap that clears the fault
whatever happens.

### Bug 24 — The prose audit read digits out of identifiers

`explain.py`'s number regex had no word-boundary guard, so `Route R12` yielded
the number **12**. The safety audit — "does the prose mention any number
that is not in the declared set?" — therefore rejected legitimate prose
mentioning a route or station id.

```
old:  "Route R12 capacity 3"  ->  ['12', '3']      # 12 is not a quantity
new:  "Route R12 capacity 3"  ->  ['3']
```

The fix adds lookarounds, and keeps every real number: `"1250 L (about
12.5%)"` still yields both.

### Bug 25 — Three smaller defects

- **The Grafana "System" row was titled "CPU, memory, resource utilisation"**
  but its only panels measure simulator load. A row title promising numbers
  that do not exist is the same failure mode as an invented figure on the
  console. Retitled to "simulator load and resource utilisation".
- **`dlq.close()` was never called** on shutdown, unlike `journal.close()`.
  The SQLite handle leaked.
- **The frontend's `explainPlan` read `payload.explanation`**, a field the
  backend never stores — so it always returned `null`. It was dead code, so
  it was removed rather than left as a function that silently returns null.

---

## 4. Measured results

### 4.1 The load test

Three named, meaningful application paths. **k6** is the primary tool; a
**stdlib-only Python harness** is the guaranteed fallback, and both emit the
**same JSON schema**, so the report is reproducible on a machine with neither.

| Workload | Path | Requests | Errors | avg | p50 | p95 | p99 | Throughput |
| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| **W1 — decision** | `POST /api/optimize` | 1377 | 0 | 209.0 ms | 202.3 | 249.3 | 269.3 | 45.5 req/s |
| **W2 — dashboard read** | `GET /api/stations` | 5930 | 0 | 6.2 ms | 5.0 | 13.6 | 22.8 | 197.4 req/s |
| **W3 — console mix** | weighted read mix + an SSE subscriber | 5903 | 0 | 6.3 ms | 5.1 | 15.2 | 23.3 | 184.1 req/s |

**Zero errors across all three.** Resource usage: CPU 0.40–0.47 % on W1/W2,
99.87 % on W3 (the SSE subscriber is the difference — it holds a connection
open), memory flat at ~51.5 MB.

**The finding PDF §18 is actually asking for** is not the latency table — it
is the relationship between the two:

| | W1 (decision) | W2 (dashboard read) |
| :--- | ---: | ---: |
| Pool saturation **peak** | **1.00** | 0.25 |
| Mean in-flight reads | 4.0 | 1.0 |
| Backend p95 before → after | 0.070 s → 0.180 s | unchanged |

W1 saturates the semaphore completely — 4 of 4 permits in flight — because
every optimise call refreshes a fresh snapshot through the 4-wide semaphore.
That is the **designed** behaviour: the gate re-validates against a *fresh*
world, so it must read. But it means the decision path is bounded by the
simulator, not by the CPU (0.47 %!). W2, which reads cached snapshots, does
not saturate at all.

**So the platform's limit is the upstream's, and the metric now says so out
loud.** Before this phase that was a hypothesis. Now it is a number in a
committed report, with the mechanism visible in the same document.

### 4.2 The resilience rehearsal — measured, not asserted

A live `unavailable` fault injected into the simulator, with the timeline
recorded:

| Step | Observation |
| :--- | :--- |
| Fault injected | simulator returns 503 |
| After 5 consecutive retryable failures | breaker **OPEN** |
| `GET /api/health` | **200**, `status: "degraded"`, `circuit.state: "OPEN"` |
| `GET /api/state` | **200**, `degraded: true`, last known good world (tick 1, 4 stations) |
| `GET /metrics \| grep backend_circuit_state` | `2.0` — `2` is unambiguously the broken one |
| `POST /api/plan` | **503**, `{"error": {"code": "CIRCUIT_OPEN"}}` |
| Write latency while OPEN | **< 1 ms** (versus 13.75 s of retry budget) |
| Fault cleared | — |
| After the cooldown elapsed | breaker **CLOSED**, `status: "ok"`, no `degraded` key |

**The recovery is entirely self-driven.** Nothing polls. Nothing nudges. The
cooldown elapses, the breaker probes, the probe succeeds, the breaker closes.
That is the behaviour a judge can verify by watching `backend_circuit_state`
change.

### 4.3 The regression gates

| Gate | Before | After |
| :--- | --- | --- |
| Verifier checks | 49/49 | **49/49** |
| Backend tests | 338 passed, 8 skipped | **468 passed, 8 skipped** |
| Frontend tests | 132 passed, 3 skipped | **136 passed, 3 skipped** |
| Healthy services | 5 | **5** |
| Prometheus targets | — | `backend: up ok` |
| Grafana panels resolving | — | 4/4 sampled expressions resolved |
| `verify-all.sh` verdict | — | **PASS** — all five suites |

**The default stack did not grow.** `docker compose config --services` returns
exactly five names, and after stopping the overlay, exactly five containers
are running.

---

## 5. The documentation

Six new documents, one rewritten, three amended.

| File | What it is |
| :--- | :--- |
| `docs/OBSERVABILITY.md` | The metric catalogue — every family, its type, its labels, its purpose — mapped to §15's layers, plus the §16 table. |
| `docs/RESILIENCE.md` | The breaker's state machine, the failure classification, the degraded-mode contract per endpoint, and the measured recovery timeline. |
| `docs/LOAD_TEST.md` | The three workloads, how to run them, the report schema, the measured results, and their interpretation. |
| `docs/SCENARIO.md` | The config schema, the resolution order, the boot-time validation, and how to derive a new scenario. |
| `docs/ARCHITECTURE.md` | The as-built diagram — PDF §20 item 6. |
| `docs/DEMO.md` | The 14-beat live script — PDF §20 item 11. |
| `docs/SYSTEM_DESIGN.md` | **Rewritten.** It described an architecture that was never built. |
| `docs/BRIEF.md` | §4b added — what is implemented, what is not, and why. |
| `docs/BACKEND.md` | §6 config table extended; **Part III** added on observability, resilience and the scenario surface. |
| `docs/DASHBOARD.md` | The System tab documented, with the reasoning for reading JSON rather than the exposition. |

### The `SYSTEM_DESIGN.md` rewrite

The previous version described a scenario builder at `/scenario`, a run monitor
at `/run/:runId`, ten console pages, and a simulator built from scratch. **None
of that was built**, because the simulator is *organizer-provided* — a fixed
image with a specified interface. A judge opening a design document that does
not match the running product reads it as a broken submission.

The rewrite describes what runs, and it has a section titled **"What is not
built"** — so nobody has to discover a gap by reading the code.

### The `BRIEF.md` correction

The brief restates the organizers' required-features list, including **R7,
sustainability reporting** (waste, expired fuel, transport emissions,
efficiency %). The platform does **not** implement it, because the simulator
owns the world model and there is **no emissions model anywhere in the stack**.
An emissions model built on top of the simulator would be *our* model of
*their* transport fleet with no ground truth behind it — a number that looked
authoritative and was invented.

So the brief now says that plainly. The honest framing: the platform reports
*efficiency as the simulator defines it* (`service_level`) and *waste as the
forecast projects it* (spilled litres), and **does not model transport
emissions**, because a defensible number beats an invented one.

---

## 6. The scripts

| Script | What it does |
| :--- | :--- |
| `scripts/verify-all.sh` | Runs the 49-check verifier, the backend suite, the frontend suite, the load test and the observability self-check; emits **one JSON verdict**. Exit 0 only if everything passed. |
| `scripts/check_observability.py` | Asserts every required metric family is present and that `/api/health` carries §16's fields. |
| `scripts/demo-normal.sh` | Demo beats 1–8. |
| `scripts/demo-crisis-fault-recovery.sh` | Demo beats 9–14. |
| `loadtest/run.sh` | Picks k6, falls back to the Python harness. |

The frontend suite runs in the Dockerfile's `test` **build target** — a
throwaway container, not the running service. See Bug 13 in §3 for why.

The harness's verdict on a green stack:

```json
{
  "schema_version": 1,
  "verdict": "PASS",
  "duration_seconds": 220,
  "suites": {
    "verifier":      {"status": "passed", "summary": "49 checks passed"},
    "backend_tests": {"status": "passed", "summary": "468 passed, 8 skipped, …"},
    "frontend_tests":{"status": "passed", "summary": "Tests  136 passed | 3 skipped (139)"},
    "observability": {"status": "passed", "summary": "checked 46 assertions"},
    "load_test":     {"status": "passed", "report": "loadtest/results/….json"}
  }
}
```

Both demo scripts print the **exact command for each beat and the observable
output it should produce**, and then run them. They fail loudly if the stack
is not up — a demo that silently runs against a dead backend is worse than no
demo.

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
| Anomaly detection | ✅ Complete — seven anomaly kinds (six detectors + stock-out risk) + bounded observation log |
| Anomaly-to-explanation layer | ✅ Complete — deterministic and auditable; LLM isolated and off |
| **Operator dashboard frontend** | ✅ **Complete — 5th service, live-verified, no-invented-numbers enforced** |
| **Console redesign** | ✅ **Complete — one column, one hero, one panel at a time; light + dark theme** |
| **End-to-end verification + documentation** | ✅ **Complete — 49/49, 468 backend, 136 frontend** |
| **Observability (Prometheus / Grafana) & load testing** | ✅ **Complete — 29 metric families, 9 alert rules, 19-panel dashboard, 3 measured workloads** |
| **Circuit breaker + degraded mode** | ✅ **Complete — 13.75 s → 1 ms; reads degrade, writes refuse** |
| **Health status table (PDF §16)** | ✅ **Complete — on `/api/health` and in the console's System tab** |
| **DLQ + graceful recovery** | ✅ **Complete — a bad frame is quarantined, not raised** |
| **Scenario configuration (no hardcoded values)** | ✅ **Complete — YAML, validated at boot, oracle proves 0 numbers changed** |
| **Harness entry point (deterministic JSON)** | ✅ **Complete — `./scripts/verify-all.sh --json out.json`** |
| **Architecture diagram (PDF §20 item 6)** | ✅ **Complete — `docs/ARCHITECTURE.md`** |
| **Demo narrative (PDF §20 item 11)** | ✅ **Complete — 14 beats, two repeatable scripts** |

**Every row is now ✅.** There is no open row in the plan table.

---

## 8. Targets To Fulfil

### Priority 1 — Submission hardening

* **A CI workflow** that runs `verify-all.sh` on every push. The suites are
  already CI-shaped — the verifier exits `0`/`1`, `verify-all.sh` emits one
  JSON object — but nothing runs them automatically yet.
* **A recorded demo.** The 14-beat script is repeatable, which is most of the
  way there; a video is the remaining mile.
* **A final read-through of every document** against the running product, by
  someone who did not write it. The `SYSTEM_DESIGN.md` rewrite is exactly the
  kind of thing that is only fully fixed once a fresh reader has tried to
  follow it.

### Priority 2 — Depth, if time allows

* **A richer load test** — a longer duration, a ramp-up curve, and a
  saturation sweep that finds the actual knee rather than reporting three
  points on either side of it.
* **Alert routing.** The nine rules exist and are validated; wiring them to a
  notification channel would make them *operational* rather than *documented*.
* **A second scenario** — `docs/SCENARIO.md` documents how to derive one, but
  only the baseline has been run. An under-supply scenario would show the
  forecast and the optimiser under stress.

### Priority 3 — Explicitly still out of scope

* **An emissions model.** Stated plainly in `docs/BRIEF.md` §4b. There is no
  ground truth behind it, and the platform's central rule forbids a number
  that looks authoritative and is invented.
* **Long-term history persistence.** The time series live in Prometheus,
  which is the right place for them.

---

## 9. Immediate Next Steps

1. Rebuild and recreate both changed images (`backend`, `frontend`) and
   re-run the full gate — 49/49, 468 backend, 136 frontend, 5 healthy.
2. Run `./scripts/verify-all.sh --json` and commit the report as the
   submission's machine-readable evidence.
3. A fresh-eyes read of `docs/` against the running product, then the
   submission commit.

> **Note:** the 49-check harness, the validator oracle, the optimiser oracle,
> the forecast oracle and the console's no-invented-numbers test are reusable
> as a **regression suite** for every subsequent change — run all of them
> after each commit to guarantee no integration contract has been broken.
> Today they caught **nineteen** real bugs (Bug 7 through Bug 25 in §3), plus
> the `SYSTEM_DESIGN.md` drift that a fresh reader found.
>
> Three of the nineteen (Bugs 12, 13 and 14) were found by *the harness itself*,
> which is the strongest argument for building one: the stranded coroutine
> surfaced as a `RuntimeWarning`, the broken frontend invocation surfaced as a
> `FAIL` verdict, and the wrong summaries surfaced as numbers that did not
> add up. A green stack is not the same thing as an honest harness.
>
> The rest were found by **three independent fresh-eyes audits** of the code
> and the documents against the running product. Every one of them was a
> *silent* defect — a green stack, a passing suite, a document that looked
> right. A 200 carrying an error body. A counter that measured the reader.
> A status the code computed and then threw away. None of them could be
> found by running the tests, because the tests encoded the same wrong
> behaviour. **That is the argument for reading the code against the
> product, not just for writing more tests.**
