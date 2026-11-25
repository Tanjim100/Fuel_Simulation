# Observability

PDF §15 requires *"meaningful observability covering the application"*, and
names four layers: **application**, **system**, **intelligence** and **logs**.
It also names "fallback activation" explicitly. PDF §16 then asks for a
specific health-status table. This document is the catalogue.

---

## The two endpoints

| Endpoint | Format | Audience |
| :--- | :--- | :--- |
| `GET /metrics` | `text/plain; version=0.0.4` | A Prometheus scraper. **Bare path, no `/api` prefix.** |
| `GET /api/observability` | `application/json` | The console's System tab, and the load-test harness. |

The split is deliberate and must not drift:

- `/api/metrics` **already exists** and returns the simulator's *business*
  KPIs as JSON. The console's Status bar reads it. Repurposing it breaks the
  console.
- The console **never parses a Prometheus text exposition**. Doing so would
  put a number-parsing bug inside the guarantee that every figure on screen
  came from the backend — the console's central promise.

So there are three endpoints, three jobs:

```
/metrics              → Prometheus exposition, for a scraper
/api/metrics          → the simulator's business KPIs, JSON, for the console
/api/observability    → the backend's own instrumentation, JSON, for the console
```

---

## The implementation

`backend/app/metrics.py` is a **hand-rolled** Prometheus text writer. The
`prometheus_client` package is not a dependency, on purpose:

- The PDF says the stack is the team's choice. A hand-rolled writer is about
  400 lines with **zero new dependencies**, and the backend image stays slim
  and offline-buildable.
- The writer is small enough to be *read*, and reading it is how the team
  knows the exposition is correct.

It provides `Counter`, `Gauge` and `Histogram` with the semantics
Prometheus expects, and a `MetricsRegistry` that holds them all.

### The histogram's percentile semantics

This is the one subtlety worth stating plainly, because getting it wrong
produces a dashboard that reports **zero latency while serving thousands of
requests**.

A histogram's samples are stored under their *label set*. The HTTP duration
histogram is observed with `method` and `path` labels, so its samples live
under keys like `("GET", "/api/health")` — **not** under an empty key.

So:

```python
histogram.percentile(0.95)         # reads the EMPTY key → 0.0. Always.
histogram.percentile_across(0.95)   # pools every label set → the real p95.
```

`percentile_across` pools the buckets and then computes the percentile, which
is exactly what Prometheus's own `histogram_quantile` does over an aggregated
histogram. The two agree.

**This was a real bug in this project.** The health endpoint reported
`p95_seconds: 0.0` while serving 12,681 requests, because it called
`percentile()` instead of `percentile_across()`. It is now guarded by three
regression tests in `backend/tests/test_metrics.py` and one in
`backend/tests/test_observability_api.py`.

### Route-template labels

The HTTP metrics label `path` with the **route template**
(`request.scope["route"].path`), never the raw path:

```
backend_http_requests_total{method="GET",path="/api/stations/{station_id}",status="200"}
```

Without this, `/api/stations/station-mirpur` and `/api/stations/station-tongi`
become separate time series and the metric cardinality explodes — a scanner
probing random URLs would create an unbounded number of series. Unmatched
paths fall back to `"unmatched"`, and a path longer than 64 characters is
capped to the same value.

### Declared-but-absent

A metric that has never been touched renders as *absent*, and an absent metric
is indistinguishable from an uninstrumented one. Every family is therefore
**pre-registered** with its zero-valued series at import time:

```
backend_fallback_activations_total{kind="degraded_mode"} 0.0
backend_fallback_activations_total{kind="poll_fallback"} 0.0
backend_fallback_activations_total{kind="breaker_open"} 0.0
backend_fallback_activations_total{kind="forecast_fallback"} 0.0
```

So a dashboard can show "0 fallbacks" rather than a gap.

---

## The metric catalogue

### Application layer

| Metric | Type | Labels | What it is for |
| :--- | :--- | :--- | :--- |
| `backend_http_requests_total` | counter | `method`, `path`, `status` | The request rate, by route template. |
| `backend_http_request_duration_seconds` | histogram | `method`, `path` | The latency distribution. Buckets 0.005s … 10s. |
| `backend_upstream_requests_total` | counter | `outcome` | Every call to the simulator, by outcome. |
| `backend_upstream_request_duration_seconds` | histogram | — | How long the upstream takes. |
| `backend_upstream_retries_total` | counter | `reason` | Retries the client issued. |

The `outcome` label takes: `success`, `timeout`, `fault_injected`,
`transport_error`, `not_found`, `conflict`, `validation_error`,
`circuit_open`, `unknown`.

> **Why the outcome label matters.** `not_found`, `conflict` and
> `validation_error` are the simulator *answering*. `timeout`,
> `transport_error` and `fault_injected` are the simulator *failing*. Only the
> second group trips the circuit breaker — see `docs/RESILIENCE.md`.

### System layer

This is the layer that would have caught the pool wedge.

| Metric | Type | What it is for |
| :--- | :--- | :--- |
| `backend_simulator_inflight_reads` | gauge | World reads currently in flight against the simulator. |
| `backend_simulator_fetch_concurrency_limit` | gauge | The bound on concurrent world reads (`FETCH_CONCURRENCY`, 4). |
| `backend_simulator_pool_saturation_ratio` | gauge | `inflight / limit`. **Above 0.8 is the early-warning line.** |
| `backend_simulator_read_queue_depth` | gauge | Callers waiting on the semaphore. |

### Intelligence layer

| Metric | Type | Labels | What it is for |
| :--- | :--- | :--- | :--- |
| `backend_forecast_abs_error_litres` | histogram | — | The forecast's error against the live oracle. |
| `backend_forecast_validation_total` | counter | `verdict` | `EXACT` / `MISMATCH` / `UNAVAILABLE`. |
| `backend_shortage_alerts_total` | counter | `severity` | Stockout risks raised, by severity. |
| `backend_decisions_total` | counter | `kind` | `plan` / `dispatch` / `cancel`. |
| `backend_fallback_activations_total` | counter | `kind` | `degraded_mode` / `poll_fallback` / `breaker_open` / `forecast_fallback`. |
| `backend_anomalies_total` | counter | `kind`, `severity` | Anomalies detected. |
| `backend_incidents_total` | counter | — | Incidents opened from grouped anomalies. |

**On `backend_decisions_total`.** A plan is a *decision request*, so it is
counted whether it was accepted or rejected — a run of rejections is itself
a finding.

**On `backend_fallback_activations_total`.** PDF §15 names it explicitly. A
non-zero count is **not** an error: it is the record of the system choosing
to serve a degraded answer rather than no answer at all.

### Resilience layer

| Metric | Type | Labels | What it is for |
| :--- | :--- | :--- | :--- |
| `backend_circuit_state` | gauge | — | `0`=CLOSED, `1`=HALF_OPEN, `2`=OPEN. |
| `backend_circuit_transitions_total` | counter | `to_state` | Every state change. |
| `backend_circuit_rejections_total` | counter | — | Calls rejected by the open breaker, without touching the upstream. |
| `backend_broker_mode` | gauge | — | `0`=sse, `1`=polling, `2`=offline. |
| `backend_broker_probe_failures_total` | counter | — | Failed SSE reachability probes. |
| `backend_broker_suspected_drops_total` | counter | — | Silent SSE drops caught by the watchdog. |
| `backend_broker_reconnects_total` | counter | — | Upstream SSE reconnects. |
| `backend_sse_events_total` | counter | — | Upstream SSE events received. |
| `backend_sse_events_fanned_out_total` | counter | — | Events fanned out to dashboard subscribers. |
| `backend_sse_events_dropped_total` | counter | — | Events dropped because a subscriber's queue was full. |
| `backend_snapshot_age_seconds` | gauge | — | The age of the cached world snapshot. |
| `backend_snapshot_refresh_total` | counter | `result` | Snapshot refreshes, by `ok`/`error`. |
| `backend_dlq_messages_total` | counter | `reason` | Payloads dead-lettered. |
| `backend_dlq_size` | gauge | — | Entries currently in the dead-letter queue. |

**Why the states are integers.** A Prometheus gauge cannot carry a string, and
`CLOSED` — the healthy state — is deliberately `0`, so a dashboard reading
"2" is unambiguously the broken one.

---

## The §16 health-status table

`GET /api/health` carries the table a judge reads. The pre-existing keys are
**preserved** — the console and the container healthcheck both read them —
and the new ones are added alongside.

```json
{
  "status": "ok",
  "components": {
    "backend_api":         {"state": "HEALTHY"},
    "redis":               {"state": "HEALTHY"},
    "fuel_simulator":      {"state": "HEALTHY"},
    "prediction_service":  {"state": "HEALTHY"},
    "decision_engine":     {"state": "HEALTHY"}
  },
  "latency":  {"p95_seconds": 0.024, "p50_seconds": 0.004},
  "requests": {"total": 12681, "errors": 0, "error_rate": 0.0},
  "circuit":  {"state": "CLOSED", "consecutive_failures": 0, "total_rejections": 0},
  "upstream": {"reachable": true, "status": "ok", "error": null},
  "broker":   {"mode": "sse", "events_received": 4392},
  "journal":  {"path": "/app/data/journal.sqlite3", "total": 6, "by_status": {"CREATED": 6}}
}
```

The component states come from a known set — `HEALTHY`, `DEGRADED`,
`UNHEALTHY`, `UNKNOWN`, `DISABLED` — so a dashboard's colour mapping cannot
break on an unexpected string.

**`avg_seconds` is not in `/api/health`.** It is in `/api/observability`,
which is the endpoint that carries the *instrumentation* mirror; `/api/health`
carries the §16 table, and §16 asks for p95 and an error rate. The console's
System tab reads the observability mirror, so it is the one that shows the
average.

### Two claims, deliberately kept apart

`/api/health`'s liveness probe and the data path are **different claims**:

- The simulator's `/health` is a liveness check on the *process*.
- A fault injected against the *data* endpoints leaves it answering while
  every read the backend cares about fails.

Reporting "ok" then would be the worst possible answer — the backend is up
and the world is unreachable. So **an open circuit degrades the status**
regardless of what the probe says, and the simulator's component state reads
`DEGRADED`.

The converse is also handled: the probe deliberately **bypasses** the
breaker. An open breaker means *we* are not calling the upstream, not that
the upstream is down — so probing through the gate would report "UNHEALTHY"
for a simulator that is answering fine. The circuit state is reported
separately, and the two together are the honest picture.

---

## The console's System tab

The console's fourth tab renders the §16 table plus the figures that explain
*why* it looks the way it does. It reads `/api/observability` — the JSON
mirror — and renders only what it finds.

The tab shows:

1. **Health and status** — the five components, p95 / p50 / avg latency, the
   error rate, the request and error counts.
2. **Upstream and stream** — simulator reachability, the broker's mode and
   event counts, the cached world's age, the dead-letter queue's size.
3. **Simulator load and fallbacks** — the pool-saturation meter, the
   fallback-activation counters, the circuit's rejection and failure counts.

Every figure on that tab is a backend number. The no-invented-numbers test
(`frontend/src/test/no-invented-numbers.test.tsx`) covers it, like every other
panel.

---

## The pool-saturation signal

**This is the metric the whole stack exists for.**

The backend cannot read the simulator's internal connection pool. But it
*owns* the semaphore that bounds its demand on that pool — so it measures its
own saturation exactly:

```
backend_simulator_pool_saturation_ratio = inflight_reads / fetch_concurrency_limit
```

Above **0.8**, the simulator's 15-wide SQLAlchemy pool is one notch from
being the bottleneck.

### Why it exists

This platform once wedged there. The simulator held exactly 15 database
handles, every request blocked for 30 seconds. It was found by reading logs
hours later — the failure mode had no signal.

### The regression guard

`backend/tests/test_pool_saturation.py` drives concurrent snapshot refreshes
against a stubbed client and asserts the semaphore bound is **never
exceeded** and the saturation gauge **never exceeds 1.0**. That turns the
incident into an executable invariant.

### What the load test shows

From `loadtest/results/20261125T193715Z.json`:

| Scenario | p50 | Saturation peak | Saturation mean |
| :--- | ---: | ---: | ---: |
| **W1** — `POST /api/optimize` | 202 ms | **1.00** | 0.64 |
| **W2** — `GET /api/stations` | 5 ms | 0.25 | 0.004 |
| **W3** — the console mix | 5 ms | 0.00 | 0.00 |

The mechanism is visible: the decision path saturates the pool and pays for
it in latency; the read paths are served from the cached snapshot and never
touch it at all. See `docs/LOAD_TEST.md`.

---

## The monitoring stack

**Optional.** The default stack is still five services, and
`docker compose up -d` still brings up exactly those five.

```bash
docker compose -f docker-compose.yml -f observability/compose.monitoring.yml up -d
```

| Service | URL |
| :--- | :--- |
| Prometheus | <http://localhost:9090> |
| Grafana | <http://localhost:3000> — anonymous admin, no login |

The Grafana dashboard is **"Fuel Supply Intelligence — Platform Observability"**,
in the `BUET CSE Fest` folder: four rows, one per §15 layer, nineteen panels.

`observability/prometheus-alerts.yml` carries nine rules:

| Alert | Fires when | Severity |
| :--- | :--- | :--- |
| `SimulatorPoolSaturation` | saturation > 0.8 for 30s | warning |
| `SimulatorReadQueueBacklog` | queue depth > 2 for 30s | warning |
| `SimulatorCircuitOpen` | `backend_circuit_state == 2` | critical |
| `SimulatorCircuitTransition` | any transition in 5m | info |
| `BackendLatencyHigh` | pooled p95 > 1.0s | warning |
| `BackendErrorRateHigh` | 5xx share > 5% | critical |
| `BrokerSuspectedDrops` | suspected drops in 10m | warning |
| `BrokerProbeFailures` | probe failures > 3 in 5m | warning |
| `DeadLetterQueueNotEmpty` | DLQ size > 0 for 60s | warning |

The thresholds are deliberately conservative. An alert that fires on a healthy
system teaches the operator to ignore alerts, which costs more than a missed
one.

> **The configuration is baked into images, not bind-mounted.** This
> project's path contains spaces and Docker Desktop cannot mount it — the same
> constraint that made the base stack bake its code into images. Prometheus's
> config is baked into `observability/Dockerfile.prometheus`; Grafana's
> provisioning into `observability/Dockerfile.grafana`.

---

## Verifying it

```bash
# The exposition.
curl -s http://localhost:8002/metrics | head -40

# The JSON mirror.
curl -s http://localhost:8002/api/observability | python3 -m json.tool

# The health table.
curl -s http://localhost:8002/api/health | python3 -m json.tool

# The self-check: 46 assertions covering every §15 layer, the §16 table,
# the content type, and the label sets.
./scripts/check_observability.py

# Or as part of the whole story.
./scripts/verify-all.sh --json /tmp/verify.json
```

`scripts/check_observability.py` is the gate. It fails if a metric family
disappears, if a required label goes missing, if the content type drifts, or
if a component's state leaves the known set.

---

## See also

- `docs/RESILIENCE.md` — the circuit breaker and degraded mode.
- `docs/LOAD_TEST.md` — the measured limits.
- `observability/README.md` — the monitoring stack's own guide.
