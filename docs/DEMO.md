# The demo

PDF §23 gives the story the demonstration should tell. PDF §20 item 11 asks
for a demo narrative. This is it — **14 beats**, each mapped to the command
that produces it and the output a judge should see.

Two scripts run the whole thing, so the demo is repeatable and never
memorised:

```bash
./scripts/demo-normal.sh                      # beats 1–8
./scripts/demo-crisis-fault-recovery.sh       # beats 9–14
./scripts/demo-normal.sh --narrate            # print the commands only
```

Both scripts **print the exact command for each beat and the observable
output it should produce**, and then run them. They fail loudly if the stack
is not up — a demo that silently runs against a dead backend is worse than no
demo.

---

## Before the demo

```bash
docker compose up -d --build --force-recreate
docker compose ps        # five services, all healthy
```

Open the console at <http://localhost:8003>.

**Optional** — bring up the monitoring stack so the judge can watch the
metrics move as the story happens:

```bash
docker compose -f docker-compose.yml -f observability/compose.monitoring.yml up -d
# Prometheus: http://localhost:9090    Grafana: http://localhost:3000
```

---

## Beats 1–8 — normal operations

### Beat 1 · Normal operations

*The platform is running. Nothing is wrong.*

**On the console:** the Status bar shows the service level, the tick, and the
stream state. The Systems tab shows all five components `HEALTHY`.

**The command:**

```bash
curl -s http://localhost:8002/api/state
```

**What to point at:** tick advancing, four stations, service level 100%.

### Beat 2 · The operator dashboard

*The console answers "what should I do, and is it safe?" in one column.*

**On the console:** the Action card at the top carries the recommended
shipment with a one-line reason and a single button. The drill-down below
takes turns — Network, Forecast, Problems, System — so nothing is hidden
behind a wall of tables.

**The command:**

```bash
curl -s http://localhost:8002/api/health | head -c 400
```

### Beat 3 · Demand is increasing

*A station's demand multiplier rises above 1.*

**On the console:** the Forecast tab. The station row shows a `×1.8` badge,
and the forecast series steepens.

**The command:**

```bash
curl -s http://localhost:8002/api/forecast/summary
```

### Beat 4 · Risk is detected

*The platform notices the deviation.*

**On the console:** the Problems tab shows a `DEMAND_SPIKE` anomaly with its
metrics and its recommended action. The tab carries a count, so the problem
is never hidden behind a tab nobody opens.

**The command:**

```bash
curl -s http://localhost:8002/api/anomalies/summary
```

### Beat 5 · Prediction

*The forecast says what will happen, and how sure it is.*

**On the console:** the forecast series, the stockout risk, and the analytic
probability. The forecast's own validation verdict is on the System tab.

**The command:**

```bash
curl -s http://localhost:8002/api/forecast | head -c 600
```

**The point to make:** the forecast is *validated against the live oracle*.
`backend_forecast_validation_total{verdict="EXACT"}` is on the System tab,
and the oracle test asserts **60 of 60 exact matches**.

### Beat 6 · Recommendation

*One shipment, one reason, one number.*

**On the console:** the Action card's headline reads the plan in a sentence:
"Ship 7000 L of DIESEL from Patiya Depot to Mirpur Fuel Station now."

**The command:**

```bash
curl -s http://localhost:8002/api/optimize/solver
```

### Beat 7 · The operator inspects

*The reasoning is auditable.*

**On the console:** "Why this plan" discloses the binding constraint, the
risks, the next actions, and the **declared numbers** — the figures quoted in
the prose, so a reader can check the sentence against them.

### Beat 8 · The allocation is simulated, then dispatched

*Nothing is written until the operator says so.*

**The plan — read-only, no write:**

```bash
curl -s -X POST http://localhost:8002/api/plan \
  -H 'Content-Type: application/json' \
  -d '{"source_depot_id":"depot-gazipur","destination_station_id":"station-mirpur","fuel_type":"DIESEL","quantity":500}'
```

**On the console:** two clicks. The first *arms* the dispatch; the second
confirms it. The journal records the intent before the write, so a crash
mid-dispatch replays as an idempotent `201` rather than a duplicate shipment.

---

## Beats 9–14 — crisis, fault, recovery

```bash
./scripts/demo-crisis-fault-recovery.sh
```

### Beat 9 · A crisis event

*A route is disrupted.*

```bash
curl -s -X POST http://localhost:8000/admin/events \
  -H 'Content-Type: application/json' \
  -d '{"type":"route_disruption","start_tick":0,"duration_ticks":6}'
```

### Beat 10 · The system adapts

*The plan changes — and the gate tells the truth.*

```bash
curl -s -X POST http://localhost:8002/api/plan \
  -H 'Content-Type: application/json' \
  -d '{"source_depot_id":"depot-gazipur","destination_station_id":"station-mirpur","fuel_type":"DIESEL","quantity":500}'
```

**What to point at:** the plan is rejected with `reject_code: ROUTE_DISRUPTED`
— *"route route-gazipur-mirpur is DISRUPTED and cannot carry a shipment"*.
The platform did **not** pretend it could route around a broken link; it said
no, and said why.

### Beat 10b · The anomaly appears

```bash
curl -s http://localhost:8002/api/anomalies/summary
```

### Beat 11 · A fault is injected into the upstream

*The simulator stops answering.*

```bash
curl -s -X POST http://localhost:8000/admin/faults \
  -H 'Content-Type: application/json' \
  -d '{"type":"unavailable","duration_seconds":60}'
```

### Beat 12 · The monitoring detects it

```bash
curl -s http://localhost:8002/api/health
curl -s http://localhost:8002/metrics | grep backend_circuit_state
```

**What to point at:**

- `status: "degraded"` — and still **HTTP 200**, because the container
  healthcheck must never kill the backend at the moment it should be serving
  the cached world.
- `circuit.state: "OPEN"` — the backend has stopped calling the simulator.
- `backend_circuit_state 2.0` — the gauge. `2` is unambiguously the broken
  one.
- On the console's System tab: `fuel_simulator` reads **DEGRADED**.

**On Grafana (if it is up):** the `SimulatorCircuitOpen` alert fires.

### Beat 13 · The degraded mode keeps the console alive

*A read degrades. A write refuses.*

```bash
curl -s http://localhost:8002/api/state
```

**What to point at:** **HTTP 200**, with the last known good world and an
honest flag:

```json
{
  "status": "OK",
  "tick": 1,
  "degraded": true,
  "degraded_reason": "The simulator circuit breaker is OPEN; this is the last known good world snapshot.",
  "stations": [ ...4 stations... ]
}
```

And the write:

```bash
curl -s -X POST http://localhost:8002/api/plan \
  -H 'Content-Type: application/json' \
  -d '{"source_depot_id":"depot-gazipur","destination_station_id":"station-mirpur","fuel_type":"DIESEL","quantity":500}'
```

**What to point at:** **HTTP 503**, `{"error": {"code": "CIRCUIT_OPEN"}}`.

**The asymmetry is the point.** A stale read with an honest flag beats a 502.
A write computed against a stale world would be a *wrong* plan — so it is
refused.

Also worth showing: the whole 13.75-second retry budget collapsed to a
**1-millisecond** rejection.

### Beat 14 · Recovery

*The fault clears, and the system returns on its own.*

```bash
curl -s -X POST http://localhost:8000/admin/faults/clear
```

**What to point at:** the breaker closes **by itself**. Nothing polls, nothing
nudges. The cooldown elapses, the breaker probes, the probe succeeds, the
breaker closes:

```bash
curl -s http://localhost:8002/api/health
# status: "ok", circuit.state: "CLOSED", no "degraded" key
```

The tick keeps advancing. Operations continue.

---

## The three things to say out loud

If the judge only remembers three sentences, these are the ones worth saying:

1. **Every number on the console came from the backend.** That is enforced by
   a test, not by a convention — `no-invented-numbers.test.tsx` scrapes every
   rendered number and fails if the backend did not send it.

2. **The platform says no when it must.** During the crisis it refused the
   dispatch and explained why, rather than writing a plan against a broken
   route. The gate is what makes the write path safe.

3. **The failure was visible before it was an outage.** The pool-saturation
   gauge is the early-warning signal for the failure that once wedged this
   platform unnoticed, and the circuit breaker turns a 13.75-second stall
   into a 1-millisecond rejection.

---

## The demo checklist

| Before | |
| :--- | :--- |
| ☐ | `docker compose ps` — five services, all healthy |
| ☐ | The console loads at <http://localhost:8003> |
| ☐ | The System tab shows all five components `HEALTHY` |
| ☐ | The Action card shows a plan |
| ☐ | *(optional)* Prometheus and Grafana are up |

| During | |
| :--- | :--- |
| ☐ | Beats 1–8 narrated by `./scripts/demo-normal.sh` |
| ☐ | Beat 10 shows the `ROUTE_DISRUPTED` rejection |
| ☐ | Beat 12 shows `degraded` + `backend_circuit_state 2.0` |
| ☐ | Beat 13 shows `degraded: true` on the read and `503 CIRCUIT_OPEN` on the write |
| ☐ | Beat 14 shows the breaker closed on its own |

---

## See also

- `docs/RESILIENCE.md` — the breaker's state machine and the degraded-mode contract.
- `docs/OBSERVABILITY.md` — the metric catalogue and the health table.
- `docs/ARCHITECTURE.md` — the as-built diagram.
