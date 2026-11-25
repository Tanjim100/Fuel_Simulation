# The monitoring stack

**Optional.** The default stack is still five services and
`docker compose up -d` still brings up exactly those five. Prometheus and
Grafana live behind a compose overlay, so they are one command away and
never in the way.

```bash
# The default stack — unchanged.
docker compose up -d

# The monitoring stack, alongside it.
docker compose -f docker-compose.yml -f observability/compose.monitoring.yml up -d
```

| Service | URL | What it is |
| :--- | :--- | :--- |
| Prometheus | <http://localhost:9090> | The scraper and the alert engine. `Status → Targets` shows both jobs. |
| Grafana | <http://localhost:3000> | The dashboard. Anonymous admin access — no login. |

The Grafana dashboard is **"Fuel Supply Intelligence — Platform Observability"**,
in the `BUET CSE Fest` folder.

## What it reads

The backend's own Prometheus exposition, at a **bare** `/metrics`:

```
backend:8002/metrics  →  text/plain; version=0.0.4
```

That path is deliberate. `/api/metrics` already exists and returns the
simulator's *business* KPIs as JSON for the console. Two endpoints, two
audiences, two formats — repurposing either one breaks something.

The console's **System tab** reads the JSON mirror at `/api/observability`
instead. The console never parses a text exposition format: doing so would
put a number-parsing bug inside the guarantee that every figure on screen
came from the backend.

## The four layers

PDF §15 names four layers of observability. The dashboard has one row per
layer, and every panel reads a metric the backend actually publishes:

| Row | Panels |
| :--- | :--- |
| **Application** | request rate by route · p50/p95/p99 latency · error rate · availability · upstream outcomes |
| **System** | **simulator pool saturation** · in-flight reads and queue depth |
| **Intelligence** | forecast error · validation verdicts · shortage alerts · decision frequency · **fallback activations** |
| **Resilience** | circuit state · transitions and rejections · SSE events · broker health · snapshot age · dead-letter queue |

"Fallback activation" is named in the brief, so it has a panel.

## The alert rules

`prometheus-alerts.yml` carries nine rules, each tied to a failure this
platform's own history says it needed:

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

The thresholds are deliberately conservative. An alert that fires on a
healthy system teaches the operator to ignore alerts, which costs more than
a missed one.

## The pool saturation signal

The metric this whole stack exists for.

The backend cannot read the simulator's internal connection pool — but it
*owns* the semaphore that bounds its demand on it. So it measures its own
saturation exactly:

```
backend_simulator_pool_saturation_ratio = inflight_reads / concurrency_limit
```

Above `0.8`, the simulator's 15-wide SQLAlchemy pool is one notch from being
the bottleneck. This platform once wedged there — every request blocked for
30 seconds, found by reading logs hours later. The saturation gauge would
have made it visible in seconds.

The load test confirms the mechanism: the decision path (`POST /api/optimize`)
saturates the pool (peak 1.0), while the dashboard read path never does
(0.0) because it is served from the cached snapshot.

## Rebuilding

The Prometheus and Grafana configuration is **baked into small images**
rather than bind-mounted — this project's path contains spaces, and Docker
Desktop cannot mount it. The same constraint made the base stack bake its
code into images.

```bash
docker compose -f docker-compose.yml -f observability/compose.monitoring.yml \
  build --force-recreate prometheus grafana
docker compose -f docker-compose.yml -f observability/compose.monitoring.yml up -d
```

`docker compose build` does **not** recreate the running container — the
`--force-recreate` is required.

## Validating the configuration

```bash
docker run --rm --entrypoint promtool prom/prometheus:v2.54.1 \
  check rules /etc/prometheus/alerts.yml
```

## Without Prometheus

The observability *evidence* does not depend on this stack:

- `curl localhost:8002/metrics` — the exposition, plain text.
- `curl localhost:8002/api/observability` — the JSON mirror.
- The console's **System tab** — the §16 table, rendered.
- `loadtest/results/*.json` — the measured load-test report.
- `./scripts/verify-all.sh` — the whole verification story in one JSON.

Prometheus and Grafana make those numbers *visible over time*. They are not
where the numbers come from.
