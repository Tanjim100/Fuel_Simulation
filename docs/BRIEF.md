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
