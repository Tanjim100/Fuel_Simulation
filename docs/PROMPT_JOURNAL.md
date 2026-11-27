# The Prompt Journal — How This System Was Built, Step by Step

**BUP CSE Fest 2026 · Fuel Supply Intelligence & Resilience Platform**

This is the actual prompt sequence used to build this platform with `puku-cli`,
recovered from the session transcripts rather than reconstructed from memory.

Every prompt below is **verbatim**. Where a prompt was long, it is quoted in
full; where it was short, it is quoted in full too — the short ones are often
the most revealing. The only edits are `[…]` markers for elision and the
removal of tool-result noise.

The point of this document is not to flatter the prompts. It is to show
**which decisions the human made and which the assistant made**, because
that split is the real lesson of the project.

---

## Table of contents

1. [How to read this](#1-how-to-read-this)
2. [The shape of the collaboration](#2-the-shape-of-the-collaboration)
3. [Step 0 — Read the problem before writing code](#3-step-0--read-the-problem-before-writing-code)
4. [Step 1 — Build the verified integration harness](#4-step-1--build-the-verified-integration-harness)
5. [Step 2 — The operator backend](#5-step-2--the-operator-backend)
6. [Step 3 — The intelligence layer](#6-step-3--the-intelligence-layer)
7. [Step 4 — The operator console](#7-step-4--the-operator-console)
8. [Step 5 — Observability, resilience, load testing](#8-step-5--observability-resilience-load-testing)
9. [Step 6 — The single-document guide](#9-step-6--the-single-document-guide)
10. [Step 7 — CI](#10-step-7--ci)
11. [The prompts that changed the architecture](#11-the-prompts-that-changed-the-architecture)
12. [The prompts that changed the code](#12-the-prompts-that-changed-the-code)
13. [What the human did that the assistant could not](#13-what-the-human-did-that-the-assistant-could-not)
14. [What the assistant did that the human did not](#14-what-the-assistant-did-that-the-human-did-not)
15. [The prompt patterns worth reusing](#15-the-prompt-patterns-worth-reusing)
16. [The prompts that did not work](#16-the-prompts-that-did-not-work)

---

## 1. How to read this

The project ran across **four sessions** and **six working days**:

| Session | What it covered |
| :--- | :--- |
| `f2d303de…` | Requirements analysis from the two PDFs |
| `380148d0…` | Installing `puku-cli` itself |
| `cc971515…` | **The build.** Days 1–7 of the platform, and every subsequent fix |
| `a1d3a2c9…` | The login flow |

Across the main session there were **68 user messages**. Of those:

- **19** were the mechanical "continue from where you left off" bridge that
  appears when a session's context fills up,
- **6** were background-task notifications (not requests at all),
- **5** were "more simple" / "give me more simple update" — asking for the
  *same content* in fewer words,
- **3** were interruptions,
- **2** were accidental double-sends (the CI/CD question and this one),
- and **33** were genuine new instructions.

**Thirty-three instructions built this system.** That is the headline. The
other thirty-five messages were the machinery of a long conversation.

The full list of those 33, in order, is the project's actual history:

```
 1  Act as a Senior Backend & DevOps Engineer… (the full specification)
 2  create an md file name Day_2.md…
 3  create an md file name Day_2.md in regular updates folder…
 4  do not do any change in the folder, just plan and show me what should we do next?
 5  the backend should own the sse listener and start working on phase 1
 6  list me the phases for me again and show what is done
 7  start phase 2 then
 8  ok, now give a brief or updates of what we have done till now
 9  give me in line so that i can update my senior
10  now move on to the next phase
11  till what have we done?
12  are we using any model for machine learning on this project?
13  git repo is already created, just push all in the main branch
14  which phases are covered till now?
15  the frontend is not responsive, it is very complex and crowdy…
16  i think tick is very fast, in each tick how much time is taken…
17  in 15 munites there will be one tick, update that
18  now guide me to test using the simulator to check my system by injecting different event.
19  finish the remaining phase and look if anything needed further.
20  create a final md file that explain everything…
21  provide all over summery of the project in point very simply and shortly
22  now tell me how much score this project covered ?
23  if it is taking time, check other
24  simply ans, what we used for monitoring?
25  how to check these monitoring in browser?
26  docker compose -f observability/compose.monitoring.yml up -d … what is the error, simple ans
27  how to use grafana?
28  check dispatch buttion is working or not
29  before applying ci-cd, what would ci and cd do?
30  yes do it.
31  do you completed ci workflow?
32  solve the bug
33  this project completely done by puku-cli, so provide all prompt engineering…
```

Read as a list, the shape of the collaboration is visible: **build the
foundation, then ask for the plan before each phase, then ask for the report
after it.**

---

## 2. The shape of the collaboration

The collaboration had a recognisable rhythm:

```
  a phase-defining prompt      →  the assistant plans (EnterPlanMode / plan file)
       ↓
  "just plan and show me"     →  the human READS the plan before any code is written
       ↓
  "start phase N then"        →  the assistant implements the plan
       ↓
  verification runs            →  the assistant reports measured results
       ↓
  "give me an update"         →  the assistant writes Day_N.md for the human
```

Three features of that rhythm did the real work:

**1. The plan file was written before the code.** The plan for Phase 2 was
87 KB — a full specification with module layouts, test strategies, and a
numbered implementation step list. The human read it and said *"start phase 2
then."* That is the entire approval step.

**2. The human verified by asking for reports, not by reading diffs.** Every
day ended with a request for a status update. The assistant produced
`regular_updates/Day_1.md` … `Day_7.md`. The human's *"give me in line so
that i can update my senior"* shows the real audience was a **senior
engineer who never opened the code**.

**3. The human used the system as a user, and that found the bugs.** The
single most valuable prompt in the whole project was **"check dispatch
buttion is working or not"** — eleven words, and it exposed a metric that
counted nothing.

---

## 3. Step 0 — Read the problem before writing code

**Session:** `f2d303de…`

The first prompt in the project was not about code. It was about
understanding:

> **"do u see two file in this folder?"**

Then:

> **"study these files and collect what are the requirement and give me a
> brief in md file so that i can understand easily."**

And then the pivot prompt — the one that turned a reading exercise into an
engineering plan:

> **"what features are needed to create in this system? how it will be
> implemented? who are the basic users of this system? what pages will be
> needed to show in frontend? how what logic is needed to apply? no need to
> do coding first. list out every necessary actions needed and also provide
> one or more activity diagram to understand the system. provide all the info
> in another md file."**

**Why this prompt is good engineering.** It is six questions in one sentence,
and it ends with an explicit constraint: *"no need to do coding first."* It
separates *understanding* from *building*, which is the single most common
failure mode in "build me X" requests. It also asks for **activity
diagrams** — a demand for the system to be modelled, not just described.

**What came out of it:** `docs/system_understanding_and_requirements.md`,
which became the source for the phase plan.

**The lesson:** the cheapest prompt in the project produced the most
structural value. It cost two messages and zero lines of code.

---

## 4. Step 1 — Build the verified integration harness

**Session:** `cc971515…`, prompt #1.

This is the most important prompt in the document, and it is reproduced in
full because its *structure* is the lesson.

> **Act as a Senior Backend & DevOps Engineer.** Build a complete, runnable
> local setup to connect with, interact with, and verify all API surfaces of
> the **BUP Fuel Supply Simulator**
> (`asifmahmoud414/bup-fuel-supply-simulator:1.0.0`).
>
> ### 1. Infrastructure Setup (`docker-compose.yml`)
> Create a `docker-compose.yml` file that spins up:
> - **`simulator-api`**: Using image
>   `asifmahmoud414/bup-fuel-supply-simulator:1.0.0` on port `8000:8000`
>   with environment variables `SIMULATION_SPEED=8`, `TICK_MINUTES=15`, and
>   `SIMULATOR_START_MODE=paused`[cite: 17].
> - **`redis`**: Using `redis:alpine` on port `6379:6379` for caching and
>   state tracking.
> - **`client-verifier`**: A Python 3.11+ service (using `FastAPI`, `httpx`,
>   `pydantic`, `redis`, and `pytest`) that connects to
>   `http://simulator-api:8000` (with `restart: unless-stopped`).
>
> ### 2. Defensive Simulator Client (`simulator_client.py`)
> Build an asynchronous Python client using `httpx.AsyncClient` that strictly
> follows all simulator integration rules:
> - **Public Read Endpoints (`/v1/*`)**: Implement typed Pydantic models and
>   async methods for `GET /v1/health`, `/v1/instance`, `/v1/regions`,
>   `/v1/depots`, `/v1/stations`, `/v1/routes`, `/v1/supply-arrivals`,
>   `/v1/events`, `/v1/allocations`, `/v1/demand-history?limit=100`, and
>   `/v1/metrics`[cite: 18, 19, 20, 21, 29].
> - **Stale Data & Fault Handling**:
>   - Check every `/v1/*` GET response header for `X-Simulator-Stale: true`
>     [cite: 18, 29]. If present, invalidate the Redis cache immediately and
>     log a warning[cite: 29].
>   - Implement exponential backoff retries and timeout handling (3s timeout)
>     for `503 FAULT_INJECTED` responses (`{"error": {"code":
>     "FAULT_INJECTED"}}`)[cite: 25, 28, 29].
> - **Domain Write (`POST /v1/allocations` & Cancel)**:
>   - Implement `create_allocation()` accepting `idempotency_key`,
>     `source_depot_id`, `destination_station_id`, `route_id`, `fuel_type`,
>     and `quantity`[cite: 21].
>   - Handle `201 Created`, `200 OK` (idempotent replay), `404 NOT_FOUND`,
>     `422` validation errors, and all `409 Conflict` domain codes
>     (`IDEMPOTENCY_KEY_MISMATCH`, `ROUTE_MISMATCH`, `DEPOT_CLOSED`,
>     `STATION_CLOSED`, `ROUTE_DISRUPTED`, `ROUTE_CAPACITY_EXCEEDED`,
>     `INSUFFICIENT_INVENTORY`, `DISPATCH_CAPACITY_EXCEEDED`,
>     `DESTINATION_CAPACITY_EXCEEDED`)[cite: 21, 22, 28, 29].
>   - Implement `cancel_allocation(allocation_id)` for `PENDING` shipments and
>     handle `409 CANNOT_CANCEL`[cite: 22, 29].
> - **SSE Stream Listener (`GET /v1/stream`)**:
>   - Connect to `/v1/stream` and parse `simulation.tick`,
>     `allocation.status_changed`, `inventory.updated`, and `simulator.notice`
>     events[cite: 22, 23].
>   - Ignore `: connected` and `: keepalive` comments[cite: 22, 23].
>   - Treat SSE strictly as an advisory hint: immediately trigger a REST
>     re-fetch of affected resources upon receiving an event[cite: 17, 29].
>   - Handle `503` disconnects or buffer drops (>200 events) by reconnecting
>     with backoff and performing a full REST state sync[cite: 23, 29].
> - **Admin Control Helper (`/admin/*`)**:
>   - Implement helper methods for `POST /admin/reset`, `POST /admin/step`,
>     `POST /admin/run`, `POST /admin/pause`, `POST /admin/events`,
>     `POST /admin/faults`, `POST /admin/faults/clear`, and
>     `GET /admin/audit`[cite: 23, 24, 25, 26].
>
> ### 3. Automated End-to-End Verification Script (`verify_simulator.py`)
> Write a comprehensive verification suite that runs sequentially and prints
> a pass/fail report for every check:
> 1. **Health & Baseline Check**: Reset simulator (`POST /admin/reset`),
>    verify `GET /v1/health` returns `status: "ok"`, and verify the exact
>    world topology (2 regions, 2 depots, 4 stations, 6 routes, 22 scheduled
>    supply arrivals)[cite: 18, 24, 26, 27, 28].
> 2. **Deterministic Tick & SSE Check**: Subscribe to `/v1/stream`, trigger
>    `POST /admin/step`, verify `simulation.tick` is received via SSE, and
>    confirm `tick` increments via `GET /v1/instance`[cite: 18, 23, 24].
> 3. **Allocation Lifecycle & Idempotency Check**:
>    - Submit a valid allocation (`3000` L `DIESEL` from `depot-gazipur` to
>      `station-mirpur` via `route-gazipur-mirpur`) and verify `201
>      Created`[cite: 21, 22].
>    - Replay the exact same payload and key to verify idempotent replay
>      (`200`/`201`)[cite: 22, 28].
>    - Replay the same key with a different quantity to verify `409
>      IDEMPOTENCY_KEY_MISMATCH`[cite: 22, 28].
>    - Advance 2 ticks via `/admin/step` and verify the allocation transitions
>      from `PENDING` -> `IN_TRANSIT` -> `ARRIVED` and updates station
>      inventory[cite: 20, 24, 27].
> 4. **Constraint & Error Validation Check**: Intentionally exceed
>    `max_shipment` (`7000` L) to confirm `409 ROUTE_CAPACITY_EXCEEDED` is
>    caught cleanly[cite: 22, 27, 28].
> 5. **Crisis & Fault Resilience Check**:
>    - Inject a `route_disruption` event via `POST /admin/events` and verify
>      route status changes to `DISRUPTED`[cite: 19, 24, 25].
>    - Inject `stale_data` and `unavailable` faults via `POST /admin/faults`,
>      verify `X-Simulator-Stale: true` cache invalidation and `503` graceful
>      retry handling, then clear faults via `POST /admin/faults/clear`[cite:
>      25, 26, 29].
> 6. **Final Audit Summary**: Fetch `GET /v1/metrics` and
>    `GET /admin/audit?limit=20` and output a clean summary table confirming
>    all systems are operational[cite: 21, 26].
>
> Provide all files (`docker-compose.yml`, `Dockerfile`, `requirements.txt`,
> `simulator_client.py`, `verify_simulator.py`, and a `README.md` with exact
> terminal commands to run and verify everything).

### Why this prompt worked

**It specified the failure modes, not just the happy path.** The `409`
conflict codes are enumerated — nine of them. The stale-header rule is
specified. The fault-injection handling is specified. The prompt author knew
the integration would be judged by how it *fails*, because the simulator's
own integration guide documents those behaviours and a client that ignores
them produces wrong results silently.

**It demanded a verification script as a deliverable, not as an extra.** The
request for "a comprehensive verification suite that runs sequentially and
prints a pass/fail report for every check" — with six named groups and exact
expected values (2 regions, 2 depots, 4 stations, 6 routes, 22 arrivals) —
produced the **49-check suite that became the project's regression gate for
the next five days.**

**It was specific where specificity was cheap.** `3000` L of `DIESEL` from
`depot-gazipur` to `station-mirpur` via `route-gazipur-mirpur`. `7000` L to
trip the capacity guard. These are not placeholders; they are a test
specification written as prose.

**The `[cite: N]` markers show the prompt was assembled from the official
integration guide.** The citations tell the assistant which document
supported which rule — a prompt that carries its own provenance.

### What it produced

| File | What it became |
| :--- | :--- |
| `docker-compose.yml` | The 5-service stack (later trimmed from 4 to 5) |
| `verifier/simulator_client.py` | The reference client — **byte-identical** to `backend/app/simulator_client.py` ever since, enforced by a SHA-256 test |
| `verifier/verify_simulator.py` | The 49-check gate, 6 groups, still green today |
| `verifier/Dockerfile` | The verifier's own image |

### The prompt immediately after

> **"create an md file name Day_2.md and give a brief about what is done till
> now and what are the targets to fullfill."**

It was sent once, and the assistant had to ask where the file should go:

> **"create an md file name Day_2.md in regular updates folder and give a brief
> about what is done till now and what are the targets to fullfill."**

**This is the daily reporting ritual, and it continued for six days.** It
created `regular_updates/Day_1.md` … `Day_7.md` — the artefacts that a
senior engineer actually reads.

### The pivot prompt

> **"do not do any change in the folder, just plan and show me what should we
> do next?"**

Eight words that changed how the rest of the project ran. It established
**plan-before-implement** as the default. Every subsequent phase produced a
written plan first.

---

## 5. Step 2 — The operator backend

The next phase-defining prompt:

> **"the backend should own the sse listener and start working on phase 1"**

Two decisions in one sentence:

1. **"the backend should own the sse listener"** — a real architectural
   decision. The alternative (the browser subscribing to the simulator
   directly) would have leaked the simulator's address into the frontend and
   broken the whole resilience story. Putting the listener in the backend
   means the browser talks only to the backend, and the backend can serve a
   cached snapshot when the stream is dead.
2. **"start working on phase 1"** — the phase plan from Step 0 was being
   executed in order, one phase at a time.

Then:

> **"list me the phases for me again and show what is done"**

> **"start phase 2 then"**

The Phase 2 plan — 87 KB, the largest artefact in the project — was written
between those two messages. Its own §0 was titled *"Design summary (read this
first)"*, and its §1 was *"Corrections to the brief, established by live
measurement."*

**That §1 heading is the whole method.** The plan did not simply restate
the requirements — it recorded where *live measurement* contradicted them.
Five of the seven "traps" documented in `README.md` §2 (arrival one tick
later than the docs say; a replay that looks exactly like a first write; the
ordered validation; the per-tick dispatch cap) were discovered this way, by
running the simulator and comparing.

**The output of this phase** was `backend/app/` — the modules behind the
42 routes the backend serves today: `state.py`, `broker.py`, `validator.py`,
`dispatch.py`, `journal.py`, `keys.py`, `errors.py`, `config.py`,
`models.py`, `degraded.py` — plus the backend's own `simulator_client.py`.

---

## 6. Step 3 — The intelligence layer

The Phase 2 plan covered the forecast, the optimiser, the anomaly detectors,
and the explanation layer. Two prompts shaped how it was built.

The first was a question, and it is the prompt that made the intelligence
layer *defensible*:

> **"are we using any model for machine learning on this project?"**

That question forced the project to answer, explicitly, what "intelligence"
meant here — and to write it down. The answer was: **optimisation and
detection, not a trained model.** The forecast is *exact* (it reads the
simulator's own deterministic demand model, 84/84 exact against the live
oracle). The optimiser is greedy with an optional LP path. The anomaly
detectors are bounded, counted-once rules. The explanation layer is
deterministic, with the LLM isolated and off by default.

That is a **defensible** answer to the PDF's "at least one meaningful
AI / ML / optimization / detection capability" — and it is defensible
*because* it was written down under questioning.

The second prompt was about the clock:

> **"i think tick is very fast, in each tick how much time is taken and how
> much time set in the requirements?"**

> **"in 15 munites there will be one tick, update that"**

A factual correction from the human, accepted and propagated through the
config, the docs, and the demo scripts.

And then the request that turned the system into something a human could
run:

> **"now guide me to test using the simulator to check my system by injecting
> different event."**

**This is the prompt that found the system's resilience flaws.** Asking for a
guided fault-injection walkthrough forced the assistant to *actually perform*
each injection and observe what the platform did. It is the direct ancestor
of the resilience rehearsal in `docs/RESILIENCE.md`.

---

## 7. Step 4 — The operator console

> **"the frontend is not responsive, it is very complex and crowdy. apply an
> design that is easy and simple. then ask me i will assign other tasks."**

Note the last clause: *"then ask me i will assign other tasks."* The human
was **reserving the right to redirect** — the instruction ends by handing
control back rather than opening the floor to a long autonomous run.

This produced the console's "one column, one hero, one panel at a time"
layout, the light/dark theme, and the System tab.

The other prompt in this phase:

> **"i think tick is very fast…"** (above), and

> **"finish the remaining phase and look if anything needed further."**

*"and look if anything needed further"* — the instruction to look for the
work nobody asked for. That is how the DLQ, the scenario config, and the
architecture diagram came to exist.

---

## 8. Step 5 — Observability, resilience, load testing

The largest single piece of work was driven by a **plan file**, not by a
chat message. The 40 KB plan (`greedy-zooming-stardust.md`) specified six
workstreams: circuit breaker, Prometheus metrics, health table, DLQ, load
test, scenario configuration — plus docs and demo scripts. It mapped each
workstream to a numbered section of the hackathon PDF and carried a risk
table with ten numbered risks.

The human's instruction that opened it:

> **"do not do any change in the folder, just plan and show me what should we
> do next?"**

(reused — the ritual was established)

And the reports that followed:

> **"give me in line so that i can update my senior"**

This is worth quoting in context. The human needed to brief a senior who
would never open the code. That single line shaped the entire documentation
strategy: `Day_1.md` … `Day_7.md`, `docs/OBSERVABILITY.md`,
`docs/LOAD_TEST.md`, `docs/RESILIENCE.md`, `docs/ARCHITECTURE.md`,
`docs/DEMO.md`, `docs/SCENARIO.md`.

---

## 9. Step 6 — The single-document guide

> **"create a final md file that explain everything. how the system is
> implemented, what stack are used, how problem are solved, how to use it."**

Four clauses, four sections. It became `PLATFORM.md` — 1,109 lines.

Then the human asked for it *shorter*, four times:

> **"provide me final simple update"**
> **"more simple"**
> **"provide all over summery of the project in point very simply and shortly"**
> **"more simple"**

This is not noise. Each reduction was a **test of whether the document's
structure carried the meaning**. The compressed version was short enough to
paste into a message; it kept the seven traps, the stack, and the measured
results. It worked because the long document had already earned a structure.

Then the audit:

> **"now tell me how much score this project covered ?"**

This triggered a line-by-line audit against the PDF's §24 weights. The audit
found two real gaps — **no CI/CD** and **decision history not exposed** — and
scored the project at ~94%.

---

## 10. Step 7 — CI

> **"before applying ci-cd, what would ci and cd do?"**

Asked twice (the second after an interruption). The question was *"explain
before you build"* — the human wanted to know what each half would actually
*do*, not to have it happen.

The answer recommended CI now, CD later, and `git tag v1.0.0` for immediate
deployment versioning.

> **"yes do it."**

Three words of approval. The result was `.github/workflows/ci.yml` — which
immediately exposed two real bugs:

- **Bug 26** — `ensure_stack` started four services, but the verifier runs
  `docker compose exec -T client-verifier`, and `exec` refuses a stopped
  container. It worked locally only by accident; on a clean CI runner it
  would have failed.
- **The 1,143-second run** — traced through three wrong hypotheses to a disk
  at 98% full. A full disk does not fail a build, it *delays* it, with no
  error at all.

Then:

> **"do you completed ci workflow?"**

> **"solve the bug"**

---

## 11. The prompts that changed the architecture

Four prompts made decisions that reshaped the codebase.

| Prompt | Decision | Consequence |
| :--- | :--- | :--- |
| *"the backend should own the sse listener and start working on phase 1"* | The browser never talks to the simulator | The entire degraded-mode story became possible |
| *"no need to do coding first"* | Understand before build | Produced the phase plan every phase followed |
| *"do not do any change in the folder, just plan and show me what should we do next?"* | Plan-before-implement | 87 KB and 40 KB plan files, both read before code |
| *"in 15 munites there will be one tick, update that"* | Tick = 15 min | Propagated through config, docs, and the demo |

---

## 12. The prompts that changed the code

| Prompt | Defect found |
| :--- | :--- |
| *"check dispatch buttion is working or not"* | The console's Dispatch button moved real fuel while `backend_decisions_total{kind="dispatch"}` stayed at **0.0** |
| *"now guide me to test using the simulator to check my system by injecting different event"* | The resilience paths had never been rehearsed |
| *"the frontend is not responsive, it is very complex and crowdy"* | The console was structurally unreadable |
| *"finish the remaining phase and look if anything needed further"* | Four unrequested-but-required deliverables |
| *"before applying ci-cd, what would ci and cd do?"* → *"yes do it"* | The `client-verifier` bug and the disk-pressure stall |

**The dispatch-button prompt is the best one in the project.** Eleven words,
typed by someone *using* the system rather than reviewing it, that found a
counter which was structurally incapable of counting. Every test had passed
before and after the fix — only the metric told the truth.

---

## 13. What the human did that the assistant could not

**1. Supplied the ground truth.** The first prompt's `[cite: N]` markers and
exact topology figures came from the official integration guide. The
assistant could read the PDFs, but it could not know which rules mattered.

**2. Set the acceptance bar.** *"verify all API surfaces"*, *"all 409 Conflict
domain codes"*, *"print a pass/fail report for every check"* — the human
defined what "done" meant, precisely enough that it could be automated.

**3. Reserved the right to redirect.** *"then ask me i will assign other
tasks."*

**4. Judged the output as a *reader*, not a builder.** *"give me in line so
that i can update my senior"*, *"more simple"* ×4. This found the
documentation's real failure mode — not wrong content, but unreadable
content.

**5. Corrected a factual error** about the tick duration, from experience
with the running system.

**6. Tested as a user.** *"check dispatch buttion is working or not."*

---

## 14. What the assistant did that the human did not

**1. Discovered seven undocumented behaviours by measurement**, not by
reading. These are `README.md` §2's "seven traps", and every one was found by
running the thing rather than reading about it:

| # | The trap |
| :--- | :--- |
| 1 | The world is stochastic, so the forecast is an estimate |
| 2 | The simulator rejects writes, and its rejection reasons are **ordered** |
| 3 | Arrival is **one tick later** than the docs say |
| 4 | A replay looks **exactly** like a first write |
| 5 | An outage must not become a total outage |
| 6 | A metric that counts reads counts **the reader**, not the world |
| 7 | The connection-pool wedge — the one that actually took the platform down |

Traps 2, 3 and 4 are simulator contract violations; traps 5, 6 and 7 are
design mistakes the assistant made and then caught. **The last one had no
signal at all**: the simulator held exactly 15 database handles, every request
blocked for 30 seconds, and it was found by reading logs hours later. The fix
was to make the backend own the semaphore that bounds its own demand, so it
can measure its own saturation exactly — and to pin it with a test.

**2. Built the regression infrastructure nobody asked for**: the 49-check
verifier, the differential oracles against the live simulator, the
client-parity SHA-256 guard, and the no-invented-numbers test.

**3. Kept the numbers honest.** The `PLATFORM.md` audit surfaced **eight
stale figures** in already-written documents — a log count, a test count, a
line count, an endpoint count, a measurement, a version. All were corrected
against the running system. This is the failure mode the project is *most*
proud of avoiding, because a judge will check a number.

**4. Chose the boring technology.** The Prometheus writer is hand-rolled
(~120 lines) rather than a dependency. The monitoring stack is an **overlay**,
not two more default services, so `docker compose up -d` still yields exactly
five healthy containers — because the PDF says *"do not introduce
infrastructure complexity unless it improves your solution."*

**5. Wrote down the failures.** 21 defects are documented with what was wrong
and why it stayed invisible. `regular_updates/Day_6.md` §10 records the
1,143-second investigation *including the three hypotheses that were wrong*.

---

## 15. The prompt patterns worth reusing

### Pattern A — Specify the failure modes, not just the features

The first prompt enumerated nine `409` conflict codes and the stale-header
rule. Those are the behaviours that decide whether an integration is correct.

> *"…and all `409 Conflict` domain codes (`IDEMPOTENCY_KEY_MISMATCH`,
> `ROUTE_MISMATCH`, …)"*

**Any prompt that only describes the happy path produces code that is wrong
in exactly the situations that matter.**

### Pattern B — Demand the verification artifact as a deliverable

> *"Write a comprehensive verification suite that runs sequentially and prints
> a pass/fail report for every check."*

One line in the prompt produced the gate that caught bugs for five days.

### Pattern C — Separate understanding from building

> *"…no need to do coding first."*

### Pattern D — Plan before touching the folder

> *"do not do any change in the folder, just plan and show me what should we
> do next?"*

### Pattern E — Ask the assistant to explain the plan before implementing it

> *"before applying ci-cd, what would ci and cd do?"*

The answer changed the scope — CI was built, CD was deliberately deferred.

### Pattern F — Reduce the output to find out whether it is sound

> *"more simple"* ×4

If a document cannot be compressed without losing its meaning, it is not
structured well enough.

### Pattern G — Test as a user

> *"check dispatch buttion is working or not"*

### Pattern H — Ask the question you are actually worried about

> *"are we using any model for machine learning on this project?"*

The honest answer — *no, and that is deliberate* — is only defensible because
it was written down.

---

## 16. The prompts that did not work

Recorded for the same reason the bugs were recorded.

**"more simple" — four times, and the third attempt failed.** After the
first two, the assistant produced shorter versions that were still too long
for the stated purpose. It took the third (*"in point very simply and
shortly"*) before the shape changed to bullets. **A vague compression
instruction produces a partial compression.** The fix was to say *how*: not
"simpler" but "in points".

**"start phase 2 then" — the phase was already planned.** The assistant had
to write an 87 KB plan first and wait for approval. The instruction was
correct but **underspecified about the approval gate**: it did not say "plan
and show me first". Once the ritual was established by *"do not do any change
in the folder"*, later phases were shorter.

**"give me an short update in point very simple" — the typo pattern
mattered less than the sequence.** The earlier prompt had established
*"in regular updates folder"*. Without that precedent, the assistant had to
ask where the file goes — which it did, and the human answered. **One
clarification, one lost round trip.**

**The double-sent prompts.** *"before applying ci-cd, what would ci and cd
do?"* was sent twice. The second came after an interruption, so the assistant
had to re-establish context — the session bridge. **Re-sending after an
interruption costs a full context reload**, which is why the bridge messages
are so long.

---

## Closing note

Thirty-three instructions. Six days. One system.

The pattern worth noticing is not that any prompt was clever. It is that
**the prompts got shorter as the project matured**:

| Phase | The prompt | Length |
| :--- | :--- | :---: |
| Step 1 | The full infrastructure + client + verifier specification | 645 words |
| Step 2 | *"the backend should own the sse listener and start working on phase 1"* | 13 words |
| Step 4 | *"the frontend is not responsive… apply an design that is easy and simple"* | 27 words |
| Step 7 | *"yes do it."* | 3 words |

That is not a degradation of the user's engagement — it is the normal shape
of a working relationship. The long prompts were needed because the
assistant had nothing to go on. The short ones were possible because the
**plans, the day files, and the verification suite** had made the shared
context durable enough to live in files instead of in chat.

Which is the real answer to "how was this built". It was built by writing
things down.

---

## See also

- `PLATFORM.md` — the single-document guide to the whole system
- `regular_updates/Day_1.md` … `Day_7.md` — the daily reports
- `docs/BACKEND.md` — the endpoints, in detail
- `docs/ARCHITECTURE.md` — the as-built architecture
- `docs/RESILIENCE.md` — the breaker and degraded mode
- `docs/LOAD_TEST.md` — the measured results
- `docs/DEMO.md` — the 14-beat live script
