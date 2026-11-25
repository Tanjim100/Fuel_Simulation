# Project Status Update: Fuel Supply Intelligence & Resilience Platform
**Event:** BUP CSE FEST 2026 — Final Hackathon
**Prepared For:** Senior / Team Lead
**Status:** Optimisation Engine, Forecasting, Anomaly Detection & Explanation Layer — Live-Verified & Regression-Gated

---

## 1. Executive Summary

Day 3 delivered the **validator** — a pure function that decides what is *legal* to
dispatch, proven correct against the live simulator by a randomised differential test.
Day 4 delivers the **optimiser** — what is *optimal* to dispatch — plus the intelligence
layer that makes the decision comprehensible: a **deterministic demand forecast**, six
**anomaly detectors**, and an **auditable explanation layer**.

The headline result is that the optimiser is **exact, not approximate**. The simulator's
demand is generated from a SHA-256-seeded RNG, so the formula is reproducible offline. That
means future demand over a 16-tick horizon can be computed **without calling the
simulator**, and the forecast was measured at **60/60 exact match** against live
`/v1/demand-history`. `GET /api/forecast/validate` exposes that oracle to judges.

The second headline result is a deliberate *negative* one. The LP formulation was written
out in full, measured, and then **deliberately not shipped**: on the baseline network the LP
achieves the *same* service level as the greedy solver (0.915702) with **13× the shipments**
(39 vs 3). Each shipment costs an idempotency key and a slice of the depot's per-tick
dispatch cap, so the LP's global optimality buys nothing and its operational cost is real.
The plan's rule was *enable the LP only if it is measurably better*. It is not — so `pulp`
stays out of `requirements.txt` and the image keeps no native binary. A test pins the
measurement, so if a future change makes the LP win by more than 1%, the build fails and
the question is re-asked.

---

## 2. Milestones Completed Till Now

### A. The optimiser (`backend/app/optimizer.py`)

A **pure function** — no I/O, no clock, no network:

```python
optimize(snap, forecast, *, config=None, horizon=None, now_tick=None) -> OptimizationResult
```

* **Variables:** `x[r, f, t]` = litres shipped on route `r`, fuel `f`, dispatch tick `t`.
* **Objective:** maximise **delivered service level**, not shipment count, minus a spill
  penalty. The objective has no incentive to fragment a plan into many small shipments.
* **Constraints C1–C10**, each one a measured simulator rule: per-shipment route max,
  depot inventory decrementing at dispatch time, the per-tick per-depot dispatch cap, tank
  capacity, route/depot/station status, station balance with unmet demand, and supply
  inflow with the measured `+1` lag.
* **Determinism:** every iteration is over a sorted collection with an explicit tie-break
  and the RNG is never touched. Two calls with the same inputs produce byte-identical
  output, and the plan id is a SHA-256 digest over the shipments — so a repeated
  `POST /api/optimize` is visibly the *same* plan, not a new one.

**Reuse, not duplication.** The greedy path imports the validator's `_route_rank`,
`DISPATCHABLE_DEPOT_STATUSES`, `dispatch_committed_this_tick`, `dispatch_remaining`,
`station_free_space` and `station_inbound`. The optimiser inherits the validator's route
preference and dispatch accounting rather than re-deriving them — which is why the two
can never disagree.

### B. The gate and the shared dispatch path (`backend/app/dispatch.py`)

```python
gate_plan(snap, shipments) -> GateResult
execute_shipments(client, journal, gated) -> ExecutionOutcome
```

`gate_plan` runs **every** candidate shipment through `validate_and_plan` with `route_id`
set explicitly (never silently rerouted) and `allow_split=False`. A shipment the validator
rejects is **dropped**, never retried with different parameters — the optimiser's
arithmetic is not trusted for legality.

`execute_shipments` is the journal→write→record loop **extracted** from
`routers/allocations.py` and now shared by both routers, so there is exactly one
implementation of the write-ahead sequence. A journal failure never stops a dispatch: the
audit trail is valuable, but the dispatch is the operator's actual request.

### C. Only current-tick shipments are ever written

The simulator stamps `created_tick = now` on every allocation. Writing a future-tick
shipment early would consume the current tick's dispatch cap, and the world will have
moved by the time that tick arrives anyway. So the plan is split:

| List | Meaning |
| :--- | :--- |
| `shipments_now` | `dispatch_tick == now_tick` — executable right now |
| `shipments_later` | advisory — recomputed next cycle, never written early |

`POST /api/optimize` is **read-only**: it computes the plan, gates every candidate, and
says what the gate *would* do — writing nothing at all. `POST /api/optimize/execute` writes
only the current-tick shipments.

### D. The forecast (`backend/app/forecast.py`)

Also pure. `forecast_series`, `inbound_schedule`, `project_world`, `stockout_risk`,
`build_forecast`.

**Stockout probability is analytic, not sampled.** The jitter is uniform on
`[−noise, +noise]`, so `std = mean · noise / √3`, and the cumulative demand is compared to
available stock through a standard normal CDF using `math.erf` — stdlib, so **no scipy**.
Keeping the module pure and deterministic is worth more than a sampled estimate.

**Spill is counted, never rejected.** `delivered = min(arriving, free_space)`,
`spilled = arriving − delivered`. Spill is penalised in the objective and reported as
`SPILL_RISK`, because the simulator accepts over-commits and silently spills — a stricter
optimiser would be a bad oracle.

### E. Anomaly detection (`backend/app/anomaly.py`)

Six detectors, pure and deterministic, with deterministic ids (`sha1(kind, entity, tick)`):

| Detector | Catches |
| :--- | :--- |
| **D1** demand spike | `z ≥ 3.0` / `≥ 5.0`; or `observed ≥ 2× median`; or `demand_multiplier > 1.05`. The event-driven path is the reliable one — the multiplier is directly observable. A `DEMAND_NORMALISED` note fires when it reverts. |
| **D2** inventory drop | fuel left the station *without being served* — a **silent spill**, a manual removal, or a booking. Cross-checked against `station_inbound` to name the cause. This makes the simulator's silent-spill behaviour **visible**. |
| **D3** depot bottleneck | `dispatch_remaining ≤ 500 L` or `used/cap ≥ 0.95`; escalates when the depot is also `CONSTRAINED`. |
| **D4** route bottleneck | `route.status ≠ AVAILABLE` → CRITICAL; the **single-connected** case (`tongi`, `coxsbazar`) is CRITICAL — no rerouting path exists. |
| **D5** supply shortfall | projected depot inventory at the next arrival < demand over the gap. |
| **D6** projected stockout | forwards `StockoutRisk` when `p ≥ 0.5` within the horizon — a projection, not an inference. |

**The `ObservationLog`** is a local bounded log (4096 entries) appended in the broker's
existing `refresh_snapshot` path. `/v1/demand-history` does not carry station inventory, so
a local log is the only source. `WorldSnapshot` itself gains **no fields** — it stays a
faithful mirror of the simulator; derived state lives in the new singletons.

### F. The explanation layer (`backend/app/explain.py`)

Deterministic, pure, and **auditable by construction**. The central discipline:

> **Every figure quoted in `headline` or `details` also appears in `numbers`.**

That single rule makes the prose auditable (a test asserts it) and is what makes the
optional LLM layer safe. The verdict is one of `RECOMMENDED` / `CAUTION` / `BLOCKED` /
`INFORMATIONAL`.

### G. The optional LLM layer (`backend/app/llm.py`)

**Disabled by default.** The system is fully functional with no language model anywhere in
the loop. The layer is **structurally incapable of being the decision authority**:

1. An **AST import-graph test** forbids `llm.py` from importing the optimiser, validator,
   state, journal, dispatch, forecast, anomaly, simulator_client, broker or config. The test
   parses the module's AST and fails the build if any of those appear.
2. It receives only a **finished immutable `Explanation`** — never a plan, never a snapshot.
3. It may return **prose only**.
4. **Unsafe prose is discarded** — any number with a decimal point or magnitude ≥ 1000 that
   is not declared in `numbers` causes the output to be rejected.

`GET /api/optimize/llm` reports the layer's state plainly, including
`role: "decorative_prose_only"`. `LLMConfig.to_dict()` reports `has_api_key` as a boolean —
the key itself is never echoed. **No new dependency**: the optional provider is called over
plain HTTP with `httpx`, already a dependency.

---

## 3. Key Engineering Findings — measured, not assumed

### Finding 1 — The demand formula is exactly reproducible offline

```
rng(seed, tick, entity, purpose) = random.Random(
    int.from_bytes(sha256(f"{seed}:{tick}:{entity}:{purpose}".encode()).digest()[:8], "big"))

demand(station, fuel, tick) = max(0, base · hour_factor · region_factor · multiplier · jitter)
   base   = daily_liters[fuel] / 96.0
   factor = _hour_factor(sim_time.hour, profile) · region.demand_factor · station.demand_multiplier
   jitter = 1 + rng(seed, tick, station_id, f"demand:{fuel}").uniform(−noise, +noise)
```

**Measured: 60/60 exact match** against live `/v1/demand-history`. This is the load-bearing
result of Phase 2: because demand is deterministic, the planning horizon is limited only by
CPU, not by simulator round-trips.

### Finding 2 — The Phase 1 brief's "realised daily ratio" figures were wrong

Measured over a full 96-tick day (1152 rows):

| Station | Profile | Realised/day | `daily_liters` | ratio |
| :--- | :--- | ---: | ---: | ---: |
| mirpur DIESEL | urban_high | 8022.3 | 8500 | **0.944** |
| mirpur PETROL | urban_high | 9933.5 | 10500 | **0.946** |
| mirpur OCTANE | urban_high | 5320.3 | 5600 | **0.950** |
| tongi DIESEL | industrial | 14002.9 | 14000 | **1.001** |
| tongi PETROL | industrial | 4503.3 | 4500 | **1.001** |
| karnaphuli DIESEL | highway | 11110.3 | 10500 | 1.058 (0.980 vs regional) |
| coxsbazar DIESEL | regional | 7750.9 | 7200 | 1.077 (0.997 vs regional) |

So the forecast **must** evaluate the formula tick by tick. Scaling `daily_liters` by a
fitted constant would be wrong by up to 11% for `karnaphuli`. A unit test pins these ratios
so the wrong approach cannot come back.

### Finding 3 — `demand_multiplier` is transient, not persistent

Measured with a `demand_spike` event (`start_tick=1`, `duration_ticks=3`, `multiplier=1.8`):

```
tick=0  mult=1.0    tick=1  mult=1.0    tick=2  mult=1.8
tick=3  mult=1.8    tick=4  mult=1.8    tick=5  mult=1.0  <-- REVERTS on resolve
```

The Day 3 rule ("re-read it every cycle") is now stronger than caching advice:
**`demand_multiplier` changes tick-to-tick and must never be cached as a constant.** A
cached multiplier would make the forecast 80% wrong for the entire duration of a spike —
exactly the window where it matters most. The router's `_fresh_snapshot()` always refreshes
for this reason.

### Finding 4 — The arrival lag: the simulator's own field is misleading

Measured: a shipment created at tick 0 on a 2-transit-tick route.

```
tick=1  IN_TRANSIT  expected_arrival_tick=2  actual_arrival_tick=None
tick=2  IN_TRANSIT  expected_arrival_tick=2  actual_arrival_tick=None
tick=3  ARRIVED     expected_arrival_tick=2  actual_arrival_tick=2  <-- FUEL CREDITED
```

The simulator stamps `actual_arrival_tick=2`, but the fuel becomes available at **tick 3**.
The same `+1` lag applies to supply arrivals: `supply-001` has `planned_tick=12` /
`actual_tick=12`, but the depot's inventory went 60000 → 78000 at **tick 13**.

So the projection model uses `created_tick + transit_ticks + 1` and `planned_tick + 1`.
Anything else over-forecasts depot inventory and under-forecasts stockouts. Demand is
consumed *after* arrivals in the same tick, because the simulator credits arrivals before
generating demand.

### Finding 5 — The LP is not worth 13× the shipments

| Solver | Service level | Shipments | Gate result |
| :--- | ---: | ---: | :--- |
| greedy | 0.915702 | 3 | 3 accepted / 0 rejected |
| LP | 0.915702 | 39 | 39 accepted / 0 rejected |

Identical service, 13× the operational cost. `pulp` is a 16.4 MB wheel bundling a native
CBC binary; adding it would put a native binary into an image that currently has none, and
add a second architecture-mismatch vector on an arm64 host that already carries the amd64
simulator warning. The formulation ships as an import-guarded, fully-written, tested module;
the dependency does not.

---

## 4. Two real bugs found by running the thing

1. **The observation log never ran.** `broker.py` called `build_observations` without
   importing it, so every call raised `NameError` — which a `LOGGER.debug` swallowed
   silently. The backend looked completely healthy while **every observation-driven
   anomaly detector was disabled**. The import was added and the log level raised to
   `warning`, with the comment: *a silent failure here would disable every
   observation-driven anomaly detector while leaving the rest of the backend looking
   healthy.*

2. **The forecast's own oracle reported a false mismatch.** `GET /api/forecast/validate`
   returned `MISMATCH | 12 of 12 differ | largest error 99.076`. The forecast was anchored
   at the snapshot's tick, but the demand history's rows started *earlier*, and
   `demand_at` returns `0.0` outside the horizon — so the comparison was measuring the
   forecast against zero. Fixed by giving `build_forecast` an explicit `start_tick` and
   anchoring the validation forecast at the history's earliest tick. Live result:
   `verdict: EXACT, matched 12, mismatched 0, largest_error_litres 0.0`.

Both are the kind of failure that only appears when the service is exercised against the
live world rather than in isolation.

---

## 5. Verification results

| Suite | Result |
| :--- | :--- |
| `client-verifier` — 49 checks across 6 groups | **49/49 PASSED** (regression gate, after all Phase 2 changes) |
| `backend` — full suite | **334 passed, 8 skipped** (default image) |
| `backend` — with `pulp` temporarily installed | **338 passed, 4 skipped** |
| — of which the optimiser oracle | 30 seeded random snapshots, live simulator as oracle |
| `docker compose ps` | 4 services, all **healthy** |
| Forecast oracle | `/api/forecast/validate` → `EXACT`, 12/12 matched, largest error 0.0 |
| Anomaly layer | `/api/anomalies/summary` → `ROUTE_BOTTLENECK: 1` |
| LLM isolation | `/api/optimize/llm` → `enabled: false`, `role: decorative_prose_only` |

**The 8 skips in the default image are the `pulp`-present group** — a *reported* skip, not a
silent one. The summary line is checked explicitly on every run.

> **Always run the backend suite in-container.** The host Python (3.12) lacks
> `pytest-asyncio`, so `asyncio_mode = auto` is ignored and every `async def` test is
> **silently skipped** — the run reports green while skipping the broker and
> simulator-oracle tests entirely. Check the summary line's skipped count before trusting a
> local run.

---

## 6. Documentation

| Document | Contents |
| :--- | :--- |
| `docs/BACKEND.md` | Part I: API contract, validator rules, every measured simulator semantic, broker design, configuration. **Part II: the optimiser, the forecast, anomaly detection, the explanation layer, the optional LLM, and the Phase 2 API surface.** |
| `README.md` | Runbook for all four services, design notes, troubleshooting |
| `backend/app/optimizer.py` | The optimiser — heavily commented, each constraint cites the measurement that established it |
| `backend/app/lp_solver.py` | The full LP formulation, import-guarded, with the measurement that keeps it uninstalled |
| `backend/app/explain.py` | The deterministic explanation layer |

---

## 7. Current Position Vs. the Phase Plan

| Target | Status |
| :--- | :--- |
| Backend service in the compose stack | ✅ Complete, 4 services healthy |
| SSE broker with drop detection + polling fallback | ✅ Complete, fault-injection rehearsal passed |
| `/api/plan` with inspectable reasoning | ✅ Complete |
| Pre-flight validator mirroring the simulator | ✅ Complete, proven by differential test |
| Idempotent dispatch + write-ahead journal | ✅ Complete |
| **Optimisation engine (greedy + optional LP)** | ✅ **Complete — proven against the live simulator** |
| **Demand forecasting** | ✅ **Complete — exact, not approximate (60/60)** |
| **Anomaly detection** | ✅ **Complete — six detectors + bounded observation log** |
| **Anomaly-to-explanation layer** | ✅ **Complete — deterministic and auditable; LLM isolated and off** |
| Operator dashboard frontend | ⬜ Not started — **next** |
| Observability (Prometheus / Grafana) & load testing | ⬜ Not started |

---

## 8. Targets To Fulfil

### Priority 1 — Operator Dashboard (Next)

Three-column operational UI: telemetry bar, network/topology panels, decision support with
human-in-the-loop approval. Live inventory, route status matrix, crisis alerts, projected
stockout timelines, service-level KPIs, stale-data warning banner. Inspectable
recommendations — signals, constraints, expected risk reduction, alternatives. The
backend's `/api/events/stream` fan-out and every Phase 2 endpoint already exist for it.

### Priority 2 — Observability & Resilience

* **Circuit breaker** wrapping simulator calls; degraded mode served from cached state.
* **Prometheus metrics + Grafana dashboards** — request rate, p95 latency, error rate,
  fallback activations, broker mode and probe failures.
* **Locust/k6 load test** on the decision endpoints; document p50/p95/p99 latency.
* **DLQ** for malformed payloads; graceful worker crash recovery.

### Priority 3 — Submission Readiness

* Harness-compatible entry point with deterministic, machine-parseable JSON output.
* Scenario configuration via documented structured config (no hardcoded values).
* Demo narrative: **normal operations → crisis → injected fault → recovery** — the
  fault-injection rehearsal is already the backbone of this.

---

## 9. Immediate Next Steps

1. Stand up the operator dashboard against the backend API — the decision-support panel is
   now fully fed by `/api/optimize`, `/api/forecast`, `/api/anomalies` and `/api/incidents`.
2. Add the Prometheus metrics endpoint and the circuit breaker, then the load test.
3. Record the demo narrative end to end against the live stack.

> **Note:** the 49-check harness, the validator oracle and the optimiser oracle built over
> Days 2–4 are reusable as a **regression suite** for every subsequent change — run all
> three after each commit to guarantee no integration contract has been broken.
