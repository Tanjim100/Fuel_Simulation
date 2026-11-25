# The operator dashboard

The console is the fifth service in the stack. It renders the world the backend
publishes, and it lets an operator act on the backend's recommendation — with a
confirmation step, because a dispatch writes to the simulator.

```
browser ──▶ frontend (:8003) ──▶ backend (:8002) ──▶ simulator-api (:8000)
              │                       │
              └───── /api/events/stream (SSE) ◀──┘
```

---

## 1. The one rule

> **Every number on screen is a number the backend sent, or a documented pure
> derivation of numbers the backend sent.**

This is not a style preference. It is the console's entire claim to credibility: a
fuel dispatcher looking at this screen must be able to trust that the litres on
it are the litres the optimiser computed. A panel that quietly smoothed a
number, or scaled one, or filled in a blank with an estimate, would make every
other number on the screen untrustworthy too.

The rule is **executable**, not aspirational. `src/test/no-invented-numbers.test.tsx`
scrapes every leaf element out of the rendered DOM, extracts every number, and
checks each one against an allow-list that is *computed from the same shared
derivation functions the components use*. A new number appears on screen only if
the backend sent it, or if a named function in `src/lib/derive.ts` produced it.

The live variant (`src/test/live.test.tsx`) does the same against the **running**
stack, re-fetching all eight live payloads and checking the rendered console
against them. It is skipped unless `LIVE_BACKEND` is set.

### The three-layer split

| Layer | File | May it invent a number? |
| :--- | :--- | :--- |
| Transport | `src/api/client.ts` | Never — it only moves payloads |
| Derivation | `src/lib/derive.ts` | Only by pure function of backend numbers |
| Rendering | `src/lib/format.ts` | Never — it only formats |

`format.ts` renders; it never derives. `derive.ts` derives; it never formats.
Keeping them apart is what makes the allow-list in the test finite and
reviewable: there is exactly one place where a number can be born.

### Unknown is not zero

The rule has a corollary that matters operationally:

```ts
fillRatio(5000, 0)  // => 0, not Infinity
serviceLevel(null)  // => 'warn', never 'ok'
```

A station whose capacity the backend did not report cannot be called healthy.
Reporting "full" would be the dangerous answer, so an unknown reads as empty and
is coloured as a warning, never as fine. The same holds for the service level:
**absence of data is not evidence of health.**

---

## 2. The architecture

### 2.1 A proxy, not CORS

The browser talks to exactly one origin: the console's own server. That server
proxies `/api/*` to the backend as a **raw byte stream** — no buffering, no
rewriting.

```js
// frontend/server.mjs
const proxy = http.createProxyServer({ target: BACKEND_ORIGIN })
```

That single decision removes an entire class of problem:

- **No CORS configuration.** The browser never makes a cross-origin request.
- **The backend's internal hostname never reaches the browser.** `backend:8002`
  is a compose-network name; exposing it would leak the topology and break the
  moment the service was deployed anywhere else.
- **SSE stays live.** A proxy that buffers would batch the event stream, and
  the console would look frozen while the backend was perfectly healthy. The
  proxy sets `Accept-Encoding: identity` and pipes the bytes straight through.

The dev server (`vite.config.ts`) uses the same shape via Vite's proxy, so
development and production behave identically.

### 2.2 Two stages, no runtime dependencies

```
frontend/Dockerfile
├── stage 1: node:22-alpine  →  npm ci  →  tsc -b --force  →  vite build
└── stage 2: node:22-alpine  →  copy dist/ + server.mjs  →  node server.mjs
```

The runtime image contains no `node_modules` beyond Node itself. There is no
package to audit in production and no dependency to keep patched.

### 2.3 SSE is advisory

The console treats `/api/events/stream` as a **nudge**, never as world state.
An event triggers a REST re-read; the event payload is never rendered as truth.

This mirrors the backend's own contract exactly — REST is the truth, SSE is a
nudge — and it is what makes the console correct under a silent stream drop. A
subscription that *looks* healthy but delivers nothing produces no events, so no
refresh, so the console goes quiet — and the backend's watchdog notices the
disagreement between tick progress and event arrival and forces a reconnect.

```ts
// src/lib/useBackend.ts
source.onmessage = () => { setStreamState('live'); refreshRef.current() }
source.onerror  = () => { setStreamState('polling'); source.close(); /* reconnect in 3s */ }
```

Polling continues underneath at `POLL_MS = 4000`, so a dropped stream degrades to
a slower but still-correct console — and the telemetry bar *says* it has done so
rather than pretending everything is fine.

---

## 3. The screen

Three columns, read left to right: **the network**, **the decision**, **the
intelligence**.

```
┌─ Telemetry bar ──────────────────────────────────────────────────────────┐
│ tick 42 · service 91.6% · disrupted 1 · closed 0 · mode SSE · 2s old     │
└──────────────────────────────────────────────────────────────────────────┘
┌─ Network ──────────────┐ ┌─ Decision ──────────────┐ ┌─ Intelligence ─────┐
│ Depots                 │ │ Recommended plan        │ │ Forecast            │
│  Gazipur Depot  ●      │ │ Ship 7,000 L DIESEL     │ │  10,302 L  82%      │
│  Patiya Depot   ●      │ │  Gazipur → Mirpur       │ │  ▁▂▃▄▅ sparkline   │
│ Stations               │ │                         │ │ Demand by fuel      │
│  Mirpur Fuel Station   │ │ total_litres  7,000     │ │  DIESEL  ████░      │
│  ×1.8 demand spike     │ │ service_level  0.92     │ │  PETROL  ███░░      │
│ Routes                 │ │                         │ │ Anomalies           │
│  gazipur → mirpur  ⚠   │ │ [Dispatch current-tick] │ │  route disrupted    │
│  DISRUPTED             │ │                         │ │ Incidents           │
└─────────────────────────┘ └─────────────────────────┘ └─────────────────────┘
```

### The panels

| Panel | Shows | Backend source |
| :--- | :--- | :--- |
| `TelemetryBar` | tick, service level, disrupted/closed counts, stream mode, snapshot age | `/api/metrics`, `/api/stream-health` |
| `StaleBanner` | "Stale data" / "Upstream unreachable" | `/api/stream-health` |
| `DepotPanel` | depot name, status, inventory total, per-tick dispatch cap | `/api/depots` |
| `StationPanel` | station name, status, fill meter, single-connected flag, demand multiplier | `/api/stations` |
| `RoutePanel` | depot → station, status, transit ticks, disrupted count | `/api/routes` |
| `DecisionPanel` | the plan headline, the declared numbers, dispatch control | `/api/optimize` |
| `ForecastPanel` | total demand, stockout probability, ticks to stockout, sparkline | `/api/forecast/summary` |
| `DemandPanel` | demand broken down by fuel, against coverage | `/api/forecast/summary` |
| `AnomalyPanel` | anomaly title, detail, the metrics behind it, recommended action | `/api/anomalies` |
| `IncidentPanel` | grouped incidents | `/api/incidents` |

### The sparkline is the backend's own projection

The forecast panel draws a small inline SVG from the backend's
`projected_inventory` series. It does not compute a trend, fit a line, or
smooth the values — it plots the array the backend sent, in order. The
"projected" series is the forecast module's own `project_world` output, which
already accounts for the measured `+1` arrival lag.

### The dispatch button takes two clicks

```
[ Dispatch current-tick shipments ]   →  [ Confirm dispatch ]  [ Cancel ]
```

The first click **arms** the action and dispatches nothing. The second confirms.
A fuel dispatch is not undoable — the simulator records it, the journal records
it, and the tick's dispatch cap is consumed. So the console makes the operator
say it twice.

The plan is also split into what is written now and what is advisory:

- **`shipments_now`** — `dispatch_tick == now_tick`. Gated by the validator and
  written on confirmation.
- **`shipments_later`** — future ticks. Rendered under an explicit *"not
  written now"* heading, because a plan for a future tick is a forecast, not an
  order. The world will have moved by then; the backend recomputes it next
  cycle.

### The dispatch is a read-only plan until it is not

The console computes a plan **on load** (read-only) and only writes when the
operator confirms. `POST /api/optimize` writes nothing. `POST
/api/optimize/execute` is the only path that does, and it is only reached from
the confirmed button.

---

## 4. The tests

```
frontend/src/test/
├── derive.test.ts             43   the pure derivations
├── format.test.ts             29   the formatting
├── panels.test.tsx            34   every panel, against real payloads
├── stream.test.ts              3   SSE is advisory; drop degrades; reconnect works
├── no-invented-numbers.test.tsx 7   the central invariant
├── live.test.tsx               3   the same, against the running stack
└── fixtures.ts                     real backend payloads, copied verbatim
```

**116 passed, 3 skipped** (the live tests skip without `LIVE_BACKEND`).

The fixtures are **copied verbatim from real backend responses** — not
hand-written approximations. A test that passes against a made-up payload proves
nothing about a payload the backend actually sends.

### Running the live tests

```bash
cd frontend
LIVE_BACKEND=http://localhost:8002 npm test
```

The live test renders the whole `App` with `fetch` pointed at the running
backend, so it exercises the real payload shapes, the real panel wiring and the
real no-invented-numbers guarantee — end to end, against the live simulator's
world.

### Two scripts that guard the build

```json
"build": "tsc -b --force && vite build",
"typecheck": "tsc -b --force"
```

`--force` is not decoration. `tsc -b` uses an incremental `tsbuildinfo` file,
and a stale one will happily skip a check that should have failed — so a broken
test file would sail through `tsc --noEmit` and then fail the build. This bit
us once during development, and the fix is permanent.

---

## 5. Operational notes

### The console degrades, and says so

| Condition | What the operator sees |
| :--- | :--- |
| Simulator unreachable | "Upstream unreachable" banner, polling mode, stale snapshot age |
| Snapshot older than the threshold | "Stale data" banner |
| SSE dropped | Mode reads "polling", not "live" |
| A read fails | That panel shows an error; the rest keep working |
| No anomalies | "Nothing anomalous" — not an empty box |

**The console never shows a stale number as if it were fresh.** Every panel that
renders a figure says how old that figure is.

### `ZERO` is not the same as `unknown`

`format.ts` renders a real zero as `0`. Only an *absent* value renders as `—`.
A depot with zero stock and a depot the backend did not report look different on
screen, because they mean different things.

### The static server contains its paths

`server.mjs` resolves every request against the build directory and refuses
anything that escapes it:

```js
const candidate = resolve(join(DIST, normalize(pathname)))
if (!candidate.startsWith(DIST)) { res.writeHead(403); res.end('Forbidden'); return }
```

A request for `/../../etc/passwd` gets a 403 and the server's own source is not
readable over HTTP.

---

## 6. What is deliberately absent

| Not built | Why |
| :--- | :--- |
| A chart library | The sparkline is 20 lines of SVG over the backend's own array. A charting dependency would add megabytes and a second rendering model to a screen whose whole point is that it shows only what it was told. |
| Client-side state management | One `useBackend` hook holds one world snapshot. Redux would be a solution to a problem this screen does not have. |
| Optimistic updates | The world is the simulator's. Rendering a guess as if it were the truth would break the one rule. |
| A login screen | The console is bound to the compose network and the operator is whoever can reach it. Authentication belongs at the edge, in front of the whole stack. |
| Mobile layout | This is an operator console, read at a desk during a shift. |

---

## 7. Where the code lives

```
frontend/
├── Dockerfile                two-stage build
├── server.mjs                the proxy + static server
├── vite.config.ts            dev proxy, test config
├── src/
│   ├── App.tsx               the three-column layout
│   ├── api/
│   │   ├── types.ts           hand-written types mirroring the backend
│   │   └── client.ts          the only place that talks to the backend
│   ├── lib/
│   │   ├── format.ts          rendering only
│   │   ├── derive.ts          the pure derivations
│   │   └── useBackend.ts      the world hook, SSE + polling
│   ├── components/
│   │   ├── common.tsx         Panel, Badge, Stat, Meter, Empty, ErrorBox
│   │   ├── TelemetryBar.tsx   TelemetryBar, StaleBanner
│   │   ├── NetworkPanel.tsx   DepotPanel, StationPanel, RoutePanel
│   │   ├── DecisionPanel.tsx  the plan and the dispatch control
│   │   ├── ForecastPanel.tsx  ForecastPanel, DemandPanel
│   │   └── AnomalyPanel.tsx   AnomalyPanel, IncidentPanel, JournalPanel
│   └── test/                  the suite
```

The API types are **hand-written**, not generated. That is deliberate: the
console and the backend are separate deployables, and a hand-written type makes
a contract change a *compile error* rather than an `undefined` on screen at
runtime.
