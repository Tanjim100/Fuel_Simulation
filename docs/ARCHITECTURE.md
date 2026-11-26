# Architecture

PDF §20 item 6 asks for an **architecture diagram**. This document is the
as-built one.

> **A note on `docs/SYSTEM_DESIGN.md`.** That document described an
> architecture that was never built — a scenario builder at `/scenario`, a
> simulation monitor at `/run/:runId`, ten console pages. The actual product
> is a five-service stack with one console. It has been rewritten to the
> as-built design; this document is the diagram.

---

## The chain PDF §20 names

```
simulator → data/backend → intelligence → decision → application → monitoring
```

And that is exactly the stack:

```
┌────────────────────────────────────────────────────────────────────────────┐
│  simulator-api :8000          (organizer-provided image)                    │
│  the world model: depots, stations, routes, demand, the tick clock          │
└───────────────────────────────┬────────────────────────────────────────────┘
                                │ HTTP (REST + SSE)
                                ▼
┌────────────────────────────────────────────────────────────────────────────┐
│  backend :8002                                                                │
│                                                                            │
│   data ────────────▶ WorldStateCache      the last known good world         │
│                        │                                                   │
│   intelligence ─────▶ forecast.py          demand series, stockout risk    │
│                        │                 anomaly.py       deviation detect  │
│                        ▼                                                   │
│   decision ────────▶ optimizer.py         shipment plan                   │
│                        │                 validator.py     pre-flight gate  │
│                        ▼                                                   │
│                        journal.py         write-ahead, idempotent replay  │
│                                                                            │
│   metrics.py ──────▶ /metrics (Prometheus text)                             │
│                   ▶ /api/observability (JSON mirror)                        │
└──────────┬──────────────────────────────────────────────┬──────────────────┘
           │ /api (proxied)                                │ /metrics
           ▼                                              ▼
┌────────────────────────────┐              ┌─────────────────────────────┐
│  frontend :8003            │              │  prometheus :9090  (opt-in)│
│  the operator console      │              │  grafana :3000      (opt-in)│
└────────────────────────────┘              └─────────────────────────────┘
           │
           ▼
      the browser

┌────────────────────────────┐
│  redis :6379               │  the event broker's fan-out and the DLQ mirror
└────────────────────────────┘
```

### The five services

| Service | Image | Port | What it owns |
| :--- | :--- | ---: | :--- |
| `simulator-api` | organizer-provided | 8000 | The world. Depots, stations, routes, demand, the tick clock. **Not ours to instrument.** |
| `redis` | `redis:alpine` | 6379 | The event broker's fan-out, and the DLQ's mirror. |
| `client-verifier` | built from `verifier/` | 8001 | The 49-check conformance suite against the simulator. |
| `backend` | built from `backend/` | 8002 | The snapshot cache, the forecast, the anomaly detection, the optimiser, the gate, the journal, the metrics. |
| `frontend` | built from `frontend/` | 8003 | The operator console. |

`docker compose up -d` yields **exactly these five**, all healthy.

---

## The layers, and what each may never do

This is the rule the whole system is built on. It is stated here because it
is the thing a judge is most likely to probe.

| Layer | Owns | **May never** |
| :--- | :--- | :--- |
| **Data** | The cached world snapshot, the journal, the event stream. | Serve a world it has not read. |
| **Intelligence** | The demand forecast, the stockout risk, the anomaly detection. | Invent a demand figure the simulator did not produce. |
| **Decision** | The shipment plan, the pre-flight gate, the dispatch. | Write a plan computed against a stale world. |
| **Application** | The console's rendering. | Display a figure the backend did not send. |
| **Monitoring** | The metrics, the health table, the alerts. | Report a health claim it cannot substantiate. |

The console's version of that last rule is **executable**:
`frontend/src/test/no-invented-numbers.test.tsx` scrapes every number rendered
by every panel and fails if any of them is not a backend number or a
documented pure derivation. The allow-list is *computed from the same
derivation functions the panels use*, so the check covers both directions: no
invented figures, and no figure computed by a private shortcut.

---

## The decision path

The path that matters. Console → backend → gate → simulator → journal.

```
 operator                    backend                        simulator
    │                            │                                │
    │  POST /api/optimize       │                                │
    │──────────────────────────▶ │                                │
    │                            │  refresh the snapshot          │
    │                            │───────────────────────────────▶│
    │                            │◀───────────────────────────────│
    │                            │                                │
    │                            │  forecast()   ← demand series │
    │                            │  detect()     ← anomalies      │
    │                            │  optimise()   ← shipment plan  │
    │                            │  gate()       ← pre-flight     │
    │                            │                                │
    │  200 {plan, explanation}   │                                │
    │◀────────────────────────── │                                │
    │                            │                                │
    │  [ the operator reads the ]                                │
    │  [ explanation, then clicks ]                               │
    │                            │                                │
    │  POST /api/optimize/execute│                                │
    │──────────────────────────▶ │  journal the intent   ◀── the  │
    │                            │  write the shipment        journal
    │                            │───────────────────────────────▶│
    │                            │◀───────────────────────────────│
    │  200 {created, replayed}   │  refresh the snapshot         │
    │◀────────────────────────── │                                │
```

### The gate

The gate is what makes the write path safe. It re-validates every shipment
against a **fresh** snapshot, before anything is written:

| Check | The ceiling it enforces |
| :--- | :--- |
| Route status | A `DISRUPTED` route cannot carry a shipment. |
| Route capacity | `max_shipment` per shipment. |
| Depot inventory | The depot cannot send what it does not have. |
| Depot dispatch cap | `dispatch_capacity_per_tick`, minus what is already committed. |
| Station free space | The station's tank cannot overflow. |
| Fuel type | The route must carry that fuel. |

**The gate's rejection is a feature.** A rejected plan carries the *reason* —
`reject_code`, `reject_message`, `diagnostics` and a full `explanation`. The
operator sees *why* it cannot be done, not just that it cannot.

The demo proves this: during the route-disruption beat, the plan is rejected
with `ROUTE_DISRUPTED` — "route route-gazipur-mirpur is DISRUPTED and cannot
carry a shipment".

---

## The degraded path

What happens when the upstream stops answering. This is the path PDF §12
cares about.

```
    request                     backend                      simulator
       │                           │                             │
       │  GET /api/state           │                             │
       │──────────────────────────▶│  breaker: OPEN              │
       │                           │  ── reject (1 ms) ─────────▶│  never called
       │                           │                             │
       │                           │  serve the cached snapshot  │
       │  200 {degraded: true}     │                             │
       │◀──────────────────────────│                             │
       │                           │                             │
       │  POST /api/plan           │                             │
       │──────────────────────────▶│  breaker: OPEN              │
       │                           │  ── reject (1 ms) ─────────▶│  never called
       │  503 CIRCUIT_OPEN         │                             │
       │◀──────────────────────────│                             │
```

**Reads degrade; writes refuse.** A stale read with an honest flag beats a
502. A write computed against a stale world would be a *wrong* plan, and
writing it would be worse than an error.

Full detail in `docs/RESILIENCE.md`.

---

## The data flow

| Flow | Path | Notes |
| :--- | :--- | :--- |
| **World refresh** | simulator → `SimulatorClient` → breaker → `WorldStateCache` | Bounded by a 4-wide semaphore. This is the pool-saturation metric's subject. |
| **Event stream** | simulator SSE → broker → Redis fan-out → console | A *nudge*, never a source of truth. The console re-reads over REST. |
| **Decision** | console → optimiser → gate → journal → simulator | Two clicks. Nothing is written without an explicit confirmation. |
| **Forecast** | scenario config + cached world → demand series → stockout risk | Pure. Offline. Validated against the live oracle. |
| **Metrics** | every layer → `metrics.py` → `/metrics` + `/api/observability` | The same numbers, two formats, two audiences. |

---

## The design decisions worth arguing about

**The breaker wraps the client rather than editing it.**
`backend/app/simulator_client.py` must stay byte-identical to
`verifier/simulator_client.py` — a SHA256 drift guard enforces it. Editing
the client would mean editing the verifier's reference copy, re-running the
49-check suite, then re-copying, for a change unrelated to the client. The
breaker is a *policy* layered over transport, and policy belongs outside.

**The metrics writer is hand-rolled.**
The `prometheus_client` package is not a dependency. A hand-rolled writer is
under 800 lines with zero new dependencies, and the backend image stays slim
and offline-buildable. It is also small enough to *read*, which is how the
team knows the exposition is correct.

**Monitoring is an overlay, not a default service.**
PDF §7 warns against infrastructure complexity that does not improve the
solution; §13 asks for a stack that is reproducibly runnable. Adding two
services to `docker-compose.yml` would make the default `docker compose up -d`
yield seven containers instead of five. So Prometheus and Grafana live behind
`observability/compose.monitoring.yml` — one command away, never in the way.

**The scenario table is a config file, not Python literals.**
PDF §19 says "do not hard-code". The table moved to
`backend/scenarios/baseline.yaml`, validated at boot, with the forecast's
public names *derived* from it. The forecast oracle (60 of 60 exact matches)
is the guard that the move changed no numbers.

**The console reads JSON, never a Prometheus exposition.**
Parsing a text format in the browser would put a number-parsing bug inside
the guarantee that every figure on screen came from the backend.

---

## The repository map

```
.
├── backend/                  the backend service
│   ├── app/
│   │   ├── circuit_breaker.py    the breaker + degraded mode
│   │   ├── metrics.py            the Prometheus writer + the registry
│   │   ├── dlq.py                the dead-letter queue
│   │   ├── scenario.py            the scenario loader + validator
│   │   ├── broker.py              the SSE/polling event broker
│   │   ├── forecast.py            the demand forecast
│   │   ├── optimizer.py           the shipment planner
│   │   ├── validator.py           the pre-flight gate
│   │   ├── dispatch.py            the shared journal-then-write path
│   │   └── routers/               the HTTP surface
│   ├── scenarios/baseline.yaml   the scenario configuration
│   └── tests/                    the backend suite
├── frontend/                 the operator console
│   ├── src/components/          the panels
│   ├── src/lib/derive.ts        the documented derivations
│   └── src/test/                the console's suite
├── verifier/                  the 49-check conformance suite
├── loadtest/                  the k6 scripts + the Python harness
├── observability/             the Prometheus + Grafana overlay
├── scripts/                   verify-all.sh, the demo scripts
├── .github/workflows/ci.yml   the CI workflow — runs verify-all.sh on every push
├── docs/                      this documentation set
└── docker-compose.yml         the five services
```

---

## See also

- `docs/BACKEND.md` — the endpoints, in detail.
- `docs/DASHBOARD.md` — the console.
- `docs/RESILIENCE.md` — the breaker and degraded mode.
- `docs/OBSERVABILITY.md` — the metrics and the health table.
- `docs/DEMO.md` — the 14-beat live script.
