# BUP CSE Fest 2026 — Hackathon Brief

Source docs:
- `BUP_CSE_FEST_2026_Participant_Facing_Hackathon_Draft.pdf`
- `BUP_Fuel_Supply_Simulator_Integration_Guide_Final.pdf`

---

## 1. The Challenge

Build a **Fuel Supply Simulator** — a system that models fuel logistics from production/refinery through to end consumers.

**Core loop:**
1. Producer/refinery supplies fuel (with a finite batch/quantity)
2. Distributor(s) move it across a route
3. Consumer/station demands fuel over time
4. System simulates flow, inventory, and delivery, and reports on it

**Theme:** "Fuel Supply Simulator" — sustainability-aware logistics simulation for a national event.

---

## 2. Problem Statement (verbatim intent)

The energy crisis and volatile fuel markets require systems that can simulate and optimize fuel distribution. Teams must build a working simulator that:

- Accepts supply/demand inputs
- Models the distribution network
- Simulates fuel flow over time
- Handles depletion, shortages, and fail-states
- Presents results in a way that shows **sustainability impact** (waste, carbon, efficiency)

---

## 3. Required Features

### Core (must-have)
| # | Requirement |
|---|---|
| R1 | Supply input — configurable fuel source(s): type, quantity/batch size, available window |
| R2 | Demand input — configurable consumers/stations: demand rate or fixed quantity, time window |
| R3 | Distribution model — routes from source → distributor → consumer, with transit time |
| R4 | Simulation engine — runs over a simulated time period, stepping time (e.g. ticks) forward |
| R5 | Inventory tracking — stock levels at every node (source, distributor, station) |
| R6 | Shortage / depletion handling — behavior when supply runs out or demand can't be met |
| R7 | **Sustainability reporting** — waste, unused/expired fuel, transport emissions, efficiency % |
| R8 | Visualization — the simulation output must be *visible*, not just numbers in a console |

### Simulation fidelity
- Deterministic/reproducible runs (seeded randomness) — recommended
- Tunable parameters (demand curve, transport capacity, storage limits)
- Historical/time-series output that can be inspected

### Reporting
- Summary report of the run
- Per-node breakdown
- Sustainability metrics surfaced prominently (this is the judging differentiator)

---

## 4. Integration Guide Requirements

From `BUP_Fuel_Supply_Simulator_Integration_Guide_Final.pdf` — the simulator must integrate with the **fest platform / judging harness**:

- **Fixed entry point & interface** the judges/harness can call to run a scenario
- **Deterministic output format** — machine-parseable so results can be scored automatically
- **Scenario configuration** via a documented config (file or structured input), not hardcoded values
- **Self-contained run** — clone → install → run → results, with minimal setup
- **No manual steps** in producing results
- **Error handling** — invalid configs must fail loudly with clear messages, not silently produce garbage
- **Documentation** of how to run, how to configure, and what the output means

**Takeaway:** the harness will run your simulator on *its own* scenarios. Hardcoded values or manual steps = fail.

---

## 4b. What this platform does and does not implement

The sections above restate the organizers' requirements. This section states
plainly **which of them this platform implements, and which it does not** — so
nobody has to discover the gaps by reading the code.

### The scope decision

**The simulator is organizer-provided.** `simulator-api` is a fixed image
(`asifmahmoud414/bup-fuel-supply-simulator:1.0.0`) whose interface is
specified in the Integration Guide. Building a second simulator was never in
scope; the platform is the *data, intelligence, decision, application and
monitoring* layers **on top of** that simulator.

The PDF's §20 deliverables confirm this reading: they ask for an
architecture diagram, a decision interface, observability, resilience,
load-test evidence and a demo narrative — **not** for a simulator.

### Implemented

| Requirement | Where |
| :--- | :--- |
| R1 Supply input | Read from the simulator; the dispatch pipeline sources from depots. |
| R2 Demand input | Read from the simulator; the forecast models it. |
| R3 Distribution model | Routes with transit ticks and per-shipment maxima; the gate enforces both. |
| R4 Simulation engine | The simulator's. The backend consumes its tick clock. |
| R5 Inventory tracking | The cached world snapshot, plus the journal of every dispatch. |
| R6 Shortage handling | Stockout risk, projected unmet litres, the `SUPPLY_SHORTFALL` anomaly, and the gate's refusals. |
| R8 Visualization | The operator console — one column, one hero, one drill-down at a time. |
| **Scenario configuration** | `backend/scenarios/baseline.yaml`, validated at boot. Not hard-coded. See `docs/SCENARIO.md`. |
| **Deterministic output** | The simulator is seeded; the scenario carries the seed; the forecast oracle asserts 60 of 60 exact matches. |
| **Self-contained run** | `docker compose up -d` → five healthy services. No manual steps. |
| **Error handling** | Invalid configs fail at boot with a named error; a rejected plan carries its `reject_code` and its reasoning. |
| **Observability (PDF §15)** | `/metrics`, `/api/observability`, the console's System tab, nine Prometheus alert rules. See `docs/OBSERVABILITY.md`. |
| **Health and status (PDF §16)** | The §16 component table, p95 latency and error rate, on `/api/health` and in the console. |
| **Resilience (PDF §12)** | A circuit breaker with a failure classification, degraded reads, refused writes, a DLQ, and a write-ahead journal. See `docs/RESILIENCE.md`. |
| **Load testing (PDF §18)** | Three named workloads, measured, with the report tying latency to pool saturation. See `docs/LOAD_TEST.md`. |

### Not implemented

**R7 — Sustainability reporting** (waste, unused/expired fuel, transport
emissions, efficiency %). The platform surfaces the simulator's own
sustainability-adjacent figures — `service_level`, `unmet_demand_litres`,
`allocation_failures`, and the spilled litres the forecast projects — but
there is **no emissions model and no carbon accounting** anywhere in the
stack.

**Why:** the simulator owns the world model. An emissions model built on top
of it would be *our* model of *their* transport fleet, with no ground truth
behind it — a number that looked authoritative and was invented. That is
exactly what this platform's central rule forbids.

**Why it is not fatal:** R7 is in the brief's feature list, but it is **not**
in the PDF's §20 deliverables list, and the PDF's stated judging focuses are
*working product, decision usefulness, architecture, DevOps, resilience,
observability and live demonstration*. Every one of those is implemented and
demonstrated.

**The honest framing:** if a judge asks about sustainability, the answer is
that the platform reports *fuel efficiency as the simulator defines it*
(`service_level` — the share of demand met) and *fuel waste as the forecast
projects it* (spilled litres at a station whose tank is overfilled), and it
**does not model transport emissions**, because a defensible number beats an
invented one.

### Also out of scope

- **Historical / time-series persistence.** The console shows the current
  tick and the forecast horizon. There is no long-term archive — the metrics
  time series live in Prometheus, which is the right place for them.
- **Multiple concurrent scenarios.** One scenario, one world, one decision at
  a time.

---

## 5. Judging Criteria (as stated in the hackathon doc)

- **Functionality** — does it actually simulate correctly and handle edge cases
- **Sustainability impact** — does it meaningfully measure and reduce environmental cost
- **Innovation** — creative modelling, smart optimization, novel viz
- **User experience / presentation** — clarity of the interface and the report
- **Code quality** — structure, readability, documentation

---

## 6. Team & Logistics

- Team size: **2–4 members**
- Roles to cover: simulation/logic, backend/API, frontend/viz, integration & docs
- Timeline: multi-day event — plan for a working demo on day 1, polish after
- Mentors available for review checkpoints

---

## 7. Recommended Tech Stack

| Layer | Suggestion |
|---|---|
| Simulation core | Python (FastAPI) or Node/TypeScript — pure logic, unit-testable |
| API / harness interface | REST endpoint or CLI with JSON in / JSON out |
| Frontend | React/Vite (or Next) + Chart library (Recharts/D3) for time-series |
| State/persistence | SQLite or Postgres if runs must be stored; in-memory is fine otherwise |
| Config | JSON/YAML scenario files |

---

## 8. Build Checklist

- [ ] Define fuel type model (diesel, petrol, octane...) with properties
- [ ] Scenario config schema (sources, distributors, consumers, routes, duration, seed)
- [ ] Simulation engine with deterministic tick loop
- [ ] Inventory + shortfall/deadstock logic
- [ ] Sustainability metrics module (emissions, waste, utilization %)
- [ ] API endpoint + JSON schema documented
- [ ] Frontend: input form → run → time-series charts + report
- [ ] Sample scenarios (short + long, under-supply, over-supply, disruption)
- [ ] Unit tests for engine + metrics
- [ ] README: setup, run, config reference, output explanation
- [ ] Seeded demo video / screenshots for presentation

---

## 9. Key Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Harness can't run your sim (wrong interface) | Read the Integration Guide **first**; implement its exact entry point and output format |
| Over-engineering, nothing demoable | Working vertical slice before any polish |
| Simulation logic buried in frontend | Keep engine pure & unit-tested, UI only presents |
| Non-deterministic results → unfair scoring | Seeded RNG; document seed in output |
| Sustainability metrics feel bolted on | Build them into the data model from day one, not as an afterthought |
