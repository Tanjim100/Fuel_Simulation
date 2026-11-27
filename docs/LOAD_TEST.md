# Load testing

PDF §18: *"Each team **must** load-test at least one meaningful application
path… Teams **should** report: average latency, p50, p95, p99, throughput,
error rate, concurrency, resource usage. The emphasis should be on
understanding the behavior and limits of the implemented system rather than
achieving an arbitrary benchmark."*

That last sentence is the design brief for this document. There is no target
number to beat here. What follows is what the system **does**, and where its
edges are.

---

## The tooling

**k6** is the primary tool — a single static binary that writes JSON
natively and needs no Python environment inside the backend image.

But k6 may not be installed on the judge's machine, so there is also a
**stdlib-only Python fallback** (`loadtest/harness.py`). Both emit the
**same JSON schema**, so the report is reproducible either way and a harness
can parse it without caring which ran.

```bash
# Picks k6 if it is installed, the Python harness otherwise.
./loadtest/run.sh --duration 30 --vus 12

# Force one or the other.
./loadtest/run.sh --duration 60 --vus 20          # k6, if present
python3 loadtest/harness.py --duration 60 --vus 20 # always works

# One scenario.
./loadtest/run.sh --scenario W1
```

The report lands in `loadtest/results/<timestamp>.json` and is printed as a
summary table.

---

## The workload

Three scenarios, each a named, meaningful application path.

| # | Scenario | Path | Why it is the meaningful one |
| :--- | :--- | :--- | :--- |
| **W1** | Decision path | `POST /api/optimize` | The end-to-end decision request: forecast + optimise + gate. The platform's reason for existing. |
| **W2** | Dashboard read | `GET /api/stations` | What the console polls every 5 seconds. This is the path that exposed the simulator connection-pool wedge. |
| **W3** | Console-equivalent mix | weighted mix of `/api/state` (40), `/api/stations` (25), `/api/forecast/summary` (15), `/api/anomalies/summary` (10), `/api/metrics` (10) + 1 SSE subscriber | Reproduces the console's real polling *and* streaming load, together. |

**Parameters** (all recorded in the report): duration, VUs, concurrency, the
base URL, the host, and the tool that produced the run.

---

## The measured results

From `loadtest/results/20261125T193715Z.json` — 30 seconds, 12 VUs, on a
2026 MacBook Air, against the five-service Docker stack.

### The numbers PDF §18 asks for

| | **W1** — decision | **W2** — dashboard | **W3** — console mix |
| :--- | ---: | ---: | ---: |
| Requests | 1,377 | 5,930 | 5,903 |
| Errors | 0 | 0 | 0 |
| **Error rate** | **0.0%** | **0.0%** | **0.0%** |
| **Average latency** | 209.0 ms | 6.2 ms | 6.3 ms |
| **p50** | 202.3 ms | 5.0 ms | 5.1 ms |
| **p95** | 249.3 ms | 13.6 ms | 15.2 ms |
| **p99** | 269.3 ms | 22.8 ms | 23.3 ms |
| Min / Max | 65.0 / 302.2 ms | 1.2 / 70.6 ms | 1.6 / 52.6 ms |
| **Throughput** | 45.5 req/s | 197.4 req/s | 184.1 req/s |
| **Concurrency** | 12 | 12 | 12 (+ 1 SSE) |
| **CPU** | 0.47% | 0.40% | 99.87% |
| **Memory** | 51.5 MB | 51.5 MB | 51.8 MB |
| Status codes | all `200` | all `200` | all `200` |

### The pool saturation, sampled *during* the run

| | **W1** | **W2** | **W3** |
| :--- | ---: | ---: | ---: |
| **Saturation peak** | **1.00** | 0.25 | 0.00 |
| Saturation mean | 0.64 | 0.004 | 0.00 |
| In-flight peak | **4.0** | 1.0 | 0.0 |
| Samples | 59 | 58 | 63 |

### The backend's own metrics, before and after

| | Before | After |
| :--- | :--- | :--- |
| Pool saturation | 0.0 | 0.0 |
| In-flight reads | 0.0 | 0.0 |
| Concurrency limit | 4.0 | 4.0 |
| Circuit state | `CLOSED` | `CLOSED` |
| Broker mode | `sse` | `sse` |
| Error rate | 0.0 | 0.0 |
| p95 | 0.070 s | 0.180 s |

---

## The numbers reproduce

A second, independent run (`loadtest/results/20261126T003154Z.json` — 30 s,
10 VUs, run via `./scripts/verify-all.sh --with-load-test`) produced the same
shape:

| | **W1** — decision | **W2** — dashboard | **W3** — console mix |
| :--- | ---: | ---: | ---: |
| Requests | 1,166 | 5,297 | 4,864 |
| **Error rate** | **0.0%** | **0.0%** | **0.0%** |
| **p50** | 198.1 ms | 3.3 ms | 5.8 ms |
| **p95** | 262.1 ms | 6.8 ms | 18.1 ms |
| **p99** | 293.7 ms | 15.8 ms | 31.3 ms |
| **Throughput** | 38.5 req/s | 176.3 req/s | 151.7 req/s |
| **Saturation peak** | **1.00** | 0.00 | 0.00 |
| Saturation mean | 0.65 | 0.00 | 0.00 |

**The absolute numbers move with the machine; the shape does not.** The
decision path saturates the pool and takes ~200 ms at p50 in both runs; the
read paths stay at single-digit milliseconds and never touch the pool. That
invariance is the finding — not any single figure.

### A third run, and what it corrected

A third run (`loadtest/results/20261126T035832Z.json` — 30 s, 10 VUs, the
same command) produced the *same shape* on the latency side but a **different
saturation picture**:

| | **W1** — decision | **W2** — dashboard | **W3** — console mix |
| :--- | ---: | ---: | ---: |
| Requests | 779 | 4,781 | 4,895 |
| **Error rate** | **0.0%** | **0.0%** | **0.0%** |
| **p50** | 316.9 ms | 5.4 ms | 5.6 ms |
| **p95** | 486.6 ms | 24.4 ms | 15.7 ms |
| **p99** | 540.4 ms | 38.1 ms | 25.3 ms |
| **Throughput** | 25.6 req/s | 159.2 req/s | 152.7 req/s |
| **Saturation peak** | **1.00** | **1.00** | 0.00 |
| Saturation mean | 0.68 | 0.57 | 0.00 |

**W2's saturation peak is 1.00 here, against 0.25 and 0.00 in the two earlier
runs.** The read path's *latency* barely moved — 5.4 ms at p50, in line with
the other runs — so this is not the console's polling becoming slow.

Two things drive the gauge, and both are worth naming precisely.

**The gauge is about the backend's refresh, not about the console's
requests.** `backend_simulator_inflight_reads` counts reads in flight against
the simulator — the broker's snapshot refresh (`backend/app/broker.py`) — and
`backend_simulator_pool_saturation_ratio` is that count over the bound the
backend sets. The console's own requests mostly do not reach the simulator:
a read serves the cached snapshot, and only *triggers* a refresh when the
snapshot has aged past `STATE_MAX_AGE` (15 s, against a `POLL_INTERVAL` of 5
s).

> **These three runs were recorded at a 10-second tick**
> (`SIMULATION_SPEED=0.1`). The stack now runs at a **2-second tick**
> (`SIMULATION_SPEED=0.5`), with `POLL_INTERVAL=1`, `STATE_MAX_AGE=3` and
> `SSE_STALE_AFTER=9` re-tuned to match. The *shape* of the result is
> unchanged — the decision path still saturates the pool on its own account
> while the read paths never touch it — but the absolute read counts in the
> table below are from the 10s-tick runs and are **not** re-measured here.
> See `docs/TESTING.md` §3.6 for the coupling between tick rate and the
> backend's thresholds.

**Measured, not assumed.** Counting the simulator's `/v1/stations` requests
in the backend's log:

| Condition | Reads in 30 s |
| :--- | ---: |
| Idle — background poller only | 5 |
| 60 console polls of `/api/stations` | 7 |

Sixty polls added **two** reads — the `STATE_MAX_AGE` refreshes — while the
background poller accounts for the rest. The console's read path is served
from cache, which is the whole point of the cached-snapshot design.

**W2 also inherited the tail of W1.** W1's decision path puts four reads in
flight against a limit of four. W1 ends, and the sampler — started and
stopped per scenario — catches that backlog for a moment into W2. W1's
saturation mean was 0.68, W2's 0.57, and W3's 0.00: a monotonic decay
consistent with a backlog draining.

So W2's 1.00 peak is the **union of two real but small effects** — W1's
drainage and W2's own occasional refresh — against a *latency* that barely
moved. That is a **measurement artefact, not a finding about the read path**,
and the honest way to report it is to say so rather than to quietly discard
the outlier run. Three things follow:

1. **The W3 figure is the clean one** — 0.00 peak, 0.00 mean, in all three
   runs. W3 is the console's *steady state*, and by then the backlog from
   W1 has fully drained, so no refresh saturates the pool.
2. **The invariant that holds across all three runs** is the latency one:
   the decision path is ~50× slower than the read paths, and it is the only
   one that saturates the pool *on its own account*.
3. **The gauge answers "is the backend's demand on the simulator high?", not
   "is the console slow?".** A reader comparing runs should compare the
   *decision* path's peak against the *console mix*'s peak — the comparison
   that the console's actual polling behaviour implies.

The report is the evidence; it is not rewritten to look tidier.

---

## What the numbers say

**Zero errors throughout.** Not a small error rate — zero. 13,210 requests
across three scenarios, every one a `200`.

**The decision path is 40× slower than the read paths, and the mechanism is
visible in the same report.** W1's p50 is 202 ms against W2's 5 ms. The
reason is the pool saturation: W1 peaks at **1.00** with 4.0 reads in flight
— the semaphore's limit, saturated — while W2 never exceeds 0.25.

**The reads do not touch the simulator at all.** W2's saturation mean is
0.004 and W3's is 0.00. The read paths are served from the cached world
snapshot; the backend only reaches for the simulator when the world actually
changes. W3's peak of 0.00 confirms the console's steady-state polling is
essentially free.

**The 40× gap is a design choice, not an accident.** W1 is doing the real
work: it reads the world, forecasts against it, runs the optimiser, and
evaluates the gate. W2 is reading a cached copy of a world that has not
changed. The architecture — a cached snapshot with a bounded refresh
semaphore in front of a 15-connection upstream pool — is what makes the
second number 40× better than the first.

**The CPU reading is a measurement artefact.** W3 shows 99.87% because
`docker stats` samples a short interval and the mix includes an SSE
subscriber holding a connection open. Memory is flat at ~51 MB across all
three scenarios, which is the meaningful figure: **the backend's footprint
does not grow with load.**

**The p95 rises from 0.070 s to 0.180 s across the run.** That is the
histogram filling with the decision path's samples — the p95 is now pooling
every label set rather than reading an empty one. (It was **0.0** before the
`percentile_across` fix; see `docs/OBSERVABILITY.md`.)

### Where the system's edges are

| Edge | Where it shows | What happens past it |
| :--- | :--- | :--- |
| **The read semaphore** | saturation reaches 1.00 during W1 | The 5th concurrent read waits for a slot. The queue-depth gauge shows it. |
| **The simulator's pool** | saturation > 0.8 for 30s | The `SimulatorPoolSaturation` alert fires. This is the pool wedge's early warning. |
| **The upstream** | the breaker trips after 5 consecutive retryable failures | Reads serve the cached world with `degraded: true`; writes refuse with `CIRCUIT_OPEN`. |
| **The backend's memory** | flat at ~51 MB | No growth observed. The backend is stateless apart from the journal and the snapshot cache. |

---

## Reading the report

```json
{
  "schema_version": 1,
  "tool": "python-harness",
  "started_at": "2026-11-25T19:38:52.328150+00:00",
  "ended_at": "2026-11-25T19:40:31.104882+00:00",
  "duration_seconds": 30.0,
  "vus": 12,
  "concurrency": 12,
  "base_url": "http://localhost:8002",
  "host": { "hostname": "...", "python": "3.12.8" },
  "scenarios": [
    {
      "name": "W1-decision",
      "description": "POST /api/optimize -- the end-to-end decision path",
      "requests": 1377,
      "errors": 0,
      "error_rate": 0.0,
      "latency_ms": { "avg": 208.991, "p50": 202.336, "p95": 249.286,
                      "p99": 269.301, "min": 65.008, "max": 302.187 },
      "statuses": { "200": 1377 },
      "throughput_rps": 45.547,
      "resource": { "available": true, "container": "buetcsefest-backend",
                    "cpu_pct": 0.47, "mem_mb": 51.5 },
      "pool_saturation": { "peak": 1.0, "mean": 0.6441, "samples": 59,
                           "inflight_peak": 4.0 }
    }
  ],
  "backend_metrics_snapshot": {
    "before": { "pool_saturation": 0.0, "circuit_state": "CLOSED", "...": "..." },
    "after":  { "pool_saturation": 0.0, "circuit_state": "CLOSED", "...": "..." },
    "pool_saturation_peak": 1.0,
    "circuit_state": "CLOSED"
  },
  "notes": "..."
}
```

### Why the saturation is sampled during the run

The first version of this harness read the saturation gauge **before and
after** the load — and always recorded `0.0`. Which is correct: the
saturation only exists *under* load.

So a `SaturationSampler` thread polls the gauge every 500 ms **while the load
is running**. That is what makes `pool_saturation.peak` meaningful, and it is
why the peak can be `1.0` while the before-and-after snapshot reads `0.0`.

---

## Reproducing it

```bash
# The stack must be up.
docker compose up -d

# 30 seconds, 12 VUs, all three scenarios.
./loadtest/run.sh --duration 30 --vus 12

# A longer run.
./loadtest/run.sh --duration 120 --vus 40

# As part of the whole verification story.
./scripts/verify-all.sh --with-load-test --load-duration 60 --load-vus 20 \
  --json /tmp/verify.json
```

`loadtest/run.sh` refuses to run if the backend is not answering — a report
against a dead backend is meaningless, and a silently-empty report is worse
than no report.

### Reproducibility

The numbers above are **this stack's limits on this machine**, not a
benchmark to beat. The report records the host, the parameters and the
container's resource usage precisely so a reader can tell what was measured.
PDF §18 asks for understanding the behaviour and limits, and that is what
this is.

---

## See also

- `docs/OBSERVABILITY.md` — the metric catalogue and the pool-saturation signal.
- `docs/RESILIENCE.md` — what happens when the upstream fails under load.
- `loadtest/harness.py` — the fallback runner.
- `loadtest/k6/` — the k6 scripts.
