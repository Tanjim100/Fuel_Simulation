# Testing by injection

The simulator exposes two injection surfaces. **Domain events** change the
*world* — a route breaks, a station goes dark, demand spikes. **API faults**
change the *transport* — the simulator becomes slow, or unavailable, or starts
lying about how fresh its data is.

The second kind is the one that matters for this stack. Every guarantee the
console makes — REST is the truth, SSE is advisory, a stale number is never
shown as a fresh one — exists because the transport is expected to fail. A
guide that only injects domain events never exercises any of it.

Everything below was measured against the running container, not read from the
integration guide PDF.

---

## 1. The controls

```bash
# A pristine world: tick 0, PAUSED, seed 12345
curl -s -X POST http://localhost:8000/admin/reset

# Advance exactly one tick (deterministic)
curl -s -X POST http://localhost:8000/admin/step

# Run / pause the background ticker
curl -s -X POST http://localhost:8000/admin/run
curl -s -X POST http://localhost:8000/admin/pause

# What is currently scheduled
curl -s http://localhost:8000/admin/events
curl -s http://localhost:8000/admin/faults

# The audit trail — every event start, every tick, every allocation
curl -s "http://localhost:8000/admin/audit?limit=40"
```

There is also a **built-in web console** at `http://localhost:8000/admin` with
dropdowns for both surfaces and a live audit pane. It is the fastest way to
see the whole surface; the `curl` forms below are the scriptable equivalent.

> `POST /admin/reset` **pauses** the world and returns it to tick 0. The
> verifier calls it too, so after a verifier run the world is always paused —
> start it again with `POST /admin/run`.

### Why pause and step

The world ticks every **10 real seconds** and each tick advances the simulated
clock by 15 minutes. That is too slow to test against when you want to know
what happened at tick 34, and too fast to observe a 3-tick event by hand.

`POST /admin/pause` + `POST /admin/step` gives you exact control: pause the
world, inject, then step one tick at a time and watch each step land. This is
the only way to test the timing semantics below reliably.

---

## 2. Domain events

```bash
curl -s -X POST http://localhost:8000/admin/events \
  -H 'Content-Type: application/json' \
  -d '{"type":"route_disruption","start_tick":0,"duration_ticks":10,
       "parameters":{"route_ids":["route-gazipur-mirpur"]}}'
```

The body is always `type`, `start_tick`, `duration_ticks`, `parameters`. The
response echoes the event with a computed `end_tick = start_tick + duration_ticks`.

### The six event types

| Type | Parameters | Effect | Detected by the backend as |
| :--- | :--- | :--- | :--- |
| `demand_spike` | `multiplier` (default 1.5), `station_ids`, `region_ids` | multiplies `station.demand_multiplier` | `DEMAND_SPIKE` (HIGH) |
| `route_disruption` | `route_ids` | route → `DISRUPTED` | `ROUTE_BOTTLENECK` (HIGH or CRITICAL) |
| `station_outage` | `station_ids` | station → `OUTAGE` | *no direct anomaly* — see §2.2 |
| `depot_constraint` | `depot_ids` | depot → `CONSTRAINED` | *no direct anomaly* — see §2.2 |
| `shipment_delay` | `delay_ticks` (default 2), `depot_ids`, `fuel_types` | supply `planned_tick += delay`, status `DELAYED` | *no direct anomaly* — see §2.6 |
| `supply_shortfall` | `factor` (default 0.5), `depot_ids`, `fuel_types` | supply `quantity *= factor` | *no direct anomaly* — see §2.6 |

A parameter list left empty applies to **all** entities of that kind — that is
the default for `demand_spike` (`multiplier: 1.5` on every station), but for
the other five the list is read literally, so an empty list is a no-op.

### 2.1 The timing rule — this is the one that bites

`start_events()` runs at the **top** of a tick, before the tick counter
increments, and it activates every event where `start_tick <= current_tick`.

So:

| You inject when the world is at… | With `start_tick`… | The event becomes active at… |
| :--- | :--- | :--- |
| tick 0 (freshly reset) | `0` | tick 0 — the next tick's `start_events` pass |
| tick 29 | `29` | tick **30** — tick 29's pass has already run |
| tick 29 | `28` | tick 29 — it was already due |

**The rule: `start_tick` is inclusive of the *next* tick's pass.** Injecting
`start_tick: N` while the world sits at tick `N` costs you one tick of delay.
Inject `start_tick: N-1` if you want it live at tick `N`.

Resolution is symmetric and has no off-by-one: the event reverses when the
world reaches `end_tick`, so an event with `start_tick: 0, duration_ticks: 3`
is active for ticks 0–2 and resolved at tick 3.

Verified from the audit log:

```
29  event.started   event 7     ← start_tick 29, injected while at tick 29
33  event.resolved  event 7     ← end_tick 33
```

### 2.2 `station_outage` and `depot_constraint` have no detector

This surprises people, so it is worth stating plainly. The backend's anomaly
detector (`backend/app/anomaly.py`) implements seven kinds — `DEMAND_SPIKE`,
`DEMAND_NORMALISED`, `INVENTORY_DROP`, `DEPOT_BOTTLENECK`, `ROUTE_BOTTLENECK`,
`SUPPLY_SHORTFALL`, `STOCKOUT_RISK`. **None of them reads a station's or a
depot's status.**

That is not a gap. It is the correct design, and it is worth understanding why
before you write a test that asserts otherwise:

- A station outage is a **capacity** fact, not an anomaly. The station is
  simply not a candidate for dispatch. The backend already enforces this in
  two places — `optimizer.py` (constraint C7) and `validator.py` — so a
  shipment to an outaged station is rejected at the gate, not flagged.
- A `CONSTRAINED` depot is **still dispatchable**. Only `OUTAGE` and `CLOSED`
  are not. Constraining a depot reduces its allowance; it does not shut it.

So the correct assertions for these two events are **behavioural**, not
anomaly-based:

```bash
# station_outage: the station flips OUTAGE and the gate refuses it
curl -s http://localhost:8002/api/stations | jq '.stations[] | {id, status}'

curl -s -X POST http://localhost:8000/v1/allocations \
  -H 'Content-Type: application/json' \
  -d '{"idempotency_key":"t1","source_depot_id":"depot-gazipur",
       "destination_station_id":"station-tongi","route_id":"route-gazipur-tongi",
       "fuel_type":"DIESEL","quantity":1000}'
# → 409 {"detail":{"code":"STATION_CLOSED","message":"Station unavailable"}}
```

```bash
# route_disruption: the route flips DISRUPTED and the gate refuses it
curl -s -X POST http://localhost:8000/v1/allocations \
  -H 'Content-Type: application/json' \
  -d '{"idempotency_key":"t2","source_depot_id":"depot-gazipur",
       "destination_station_id":"station-mirpur","route_id":"route-gazipur-mirpur",
       "fuel_type":"DIESEL","quantity":1000}'
# → 409 {"detail":{"code":"ROUTE_DISRUPTED","message":"Route unavailable"}}
```

The route must actually connect the depot and station you named, or you get
`409 ROUTE_MISMATCH` instead — which is a different gate, firing earlier.

### 2.3 Severity is a gradient, not a flag

A disrupted route is `HIGH` if the station has another route serving it, and
`CRITICAL` if it does not — because a single-connected station has no
rerouting path. Verified by disrupting two routes at once:

```
CRITICAL  route-patiya-coxsbazar   ← coxsbazar is served by one route only
HIGH      route-gazipur-karnaphuli  ← karnaphuli has a second route
```

The recommended action differs accordingly: *"Reroute through the alternative
depot"* versus *"No alternative exists; source the fuel elsewhere or accept the
stockout."*

### 2.4 `demand_multiplier` reverts — do not cache it

`demand_spike` multiplies the station's demand, and when the event resolves the
multiplier is **divided back out**. The backend notices the change and emits a
`DEMAND_NORMALISED` anomaly recording both the old and new multiplier:

```
DEMAND_NORMALISED LOW  station-mirpur:DIESEL
  {"previous_multiplier": 2.5, "current_multiplier": 1.0}
```

This is the mechanism that caught a real bug: a forecast that cached
`demand_multiplier` as a constant would be **80% wrong for the entire duration
of the spike** — exactly the window where it matters most. Any test of the
forecast must read the multiplier fresh each cycle.

The forecast's response is directly measurable, which makes `demand_spike` the
easiest of the six to assert on:

```
                          baseline          with 2.5× spike on mirpur
mirpur DIESEL demand      974.551    →      2456.668
mirpur PETROL demand      1227.574   →      3095.034
mirpur OCTANE demand      646.175    →      1621.762
tongi  DIESEL demand      1047.913   →      1047.913   (untouched)
total_demand_litres       10284.044  →      14594.37
```

Note that `tongi` is unchanged — a `demand_spike` scoped by `station_ids`
affects only the stations you named. Omit `station_ids` and `region_ids`
entirely and it applies to **every** station.

### 2.5 `shipment_delay` and `supply_shortfall` act on supplies, not on shipments

Despite the name, `shipment_delay` delays **incoming supply arrivals**, not
outbound dispatches. Both act on `supply-arrivals` that are still `SCHEDULED`
or `DELAYED`:

```bash
# Before
supply-001  depot-gazipur  DIESEL  planned 12  SCHEDULED  qty 18000
supply-105  depot-patiya  PETROL  planned 80  SCHEDULED  qty 8000

curl -s -X POST http://localhost:8000/admin/events -H 'Content-Type: application/json' \
  -d '{"type":"shipment_delay","start_tick":0,"duration_ticks":30,
       "parameters":{"delay_ticks":5,"depot_ids":["depot-gazipur"],"fuel_types":["DIESEL"]}}'

curl -s -X POST http://localhost:8000/admin/events -H 'Content-Type: application/json' \
  -d '{"type":"supply_shortfall","start_tick":0,"duration_ticks":30,
       "parameters":{"factor":0.5,"depot_ids":["depot-patiya"],"fuel_types":["PETROL"]}}'

# After
supply-001   depot-gazipur  DIESEL  planned 17  DELAYED  qty 18000   ← +5 ticks
supply-101   depot-gazipur  DIESEL  planned 69  DELAYED  qty 12000
supply-105   depot-patiya   PETROL  planned 80  SCHEDULED qty 4000   ← halved
supply-205   depot-patiya   PETROL  planned 144 SCHEDULED qty 4000
```

Two consequences worth knowing:

- **The delay is applied to every matching supply, including recurring
  resupply** — `supply-101`, `supply-201`, `supply-301` all moved by 5 ticks.
- **The shortfall is applied at event start, not at arrival.** A supply that
  has already arrived is not affected. Inject the shortfall *before* the
  arrival tick you want to shrink.

The baseline has no `depot-patiya` + `PETROL` supply in its first four
arrivals, but it does in the recurring series — so target `supply-105` or
later.

### 2.6 Neither supply event raises an anomaly on its own

`shipment_delay` and `supply_shortfall` change *how much* and *when* fuel
arrives. Neither is a condition the backend flags on its own, and a test that
waits for an anomaly after injecting one of them will wait forever.

This is worth being precise about, because the `SUPPLY_SHORTFALL` anomaly kind
does exist and the name invites the assumption. Read the detector
(`backend/app/anomaly.py`, `_detect_supply_shortfall`) and the condition is
narrower than it sounds:

> A depot that **runs dry before its next resupply lands** — tested as
> `demand_on_depot_per_tick × gap_ticks > depot_inventory`, with the measured
> `+1` credit lag applied.

So it fires when a depot is *empty of a fuel it owes*, not when a shipment is
merely smaller than planned. At tick 1 every depot is full, so a shortfall
injected there changes nothing the detector can see — correctly.

The honest assertions are on the **world state**, not on an anomaly:

```bash
# The quantity actually shrank
curl -s http://localhost:8000/v1/supply-arrivals | jq '.[] | select(.id=="supply-001")'
# → qty 5400.0   (baseline 18000, factor 0.3)
```

To drive the anomaly itself, you need the depot to actually empty. The
shortest reliable route is to let the world run without dispatching and wait
for the first station to fall short — which takes ~64 ticks from a reset,
because the baseline is well-stocked for its first sixty. That is a slow test,
which is exactly why the supply events are not the right tool for it: shrink
the supply *and* run the world dry, and the detector fires for the reason it
was written.

---

## 3. API faults

```bash
curl -s -X POST http://localhost:8000/admin/faults \
  -H 'Content-Type: application/json' \
  -d '{"type":"unavailable","duration_seconds":45,"parameters":{}}'

curl -s -X POST http://localhost:8000/admin/faults/clear
```

The body is `type`, `duration_seconds`, `parameters`. Faults expire on their
own; `clear` removes them immediately.

### The five fault types

| Type | Parameters | Effect | Bypassed by |
| :--- | :--- | :--- | :--- |
| `latency` | `delay_ms` (default 500) | sleeps before every `/v1/*` request | `/admin/*`, `/v1/health` |
| `unavailable` | — | every `/v1/*` request → `503 FAULT_INJECTED` | `/admin/*`, `/v1/health` |
| `error_rate` | `rate` (default 0.25) | a fraction of requests → `503 FAULT_INJECTED` | `/admin/*`, `/v1/health` |
| `stale_data` | — | adds `X-Simulator-Stale: true` to `/v1/*` GETs → backend flushes its Redis cache | `/admin/*`, `/v1/health` |
| `stream_disconnect` | — | `GET /v1/stream` → `503` for **new** connections | `/admin/*`, `/v1/health` |

**`/admin/*` and `/v1/health` always bypass fault injection.** That is what
makes the fault surface controllable — you can always inject a fault and still
clear it, and the backend's liveness probe never lies to you.

### 3.1 `unavailable` — the headline case

```bash
curl -s -X POST http://localhost:8000/admin/faults -H 'Content-Type: application/json' \
  -d '{"type":"unavailable","duration_seconds":45,"parameters":{}}'

curl -s -w "\nhttp=%{http_code}\n" http://localhost:8000/v1/stations
# → {"error":{"code":"FAULT_INJECTED","message":"Simulator API temporarily unavailable."}}
# → http=503
```

Watch the backend degrade:

```bash
curl -s http://localhost:8002/api/stream-health
```

```
mode: polling     sse_reachable: false   snapshot.is_stale: true
mode: polling     sse_reachable: false   snapshot.is_stale: true   age_seconds: 18.2
mode: sse         sse_reachable: false   snapshot.is_stale: true   age_seconds: 26.3
```

The backend drops to polling, keeps serving the last known snapshot, and marks
it stale. When the fault expires it recovers on its own — `mode: sse`,
`sse_reachable: true`, snapshot age back under 2s. **No restart required.**

### 3.2 `latency` — the one that breaks timeouts

```bash
curl -s -X POST http://localhost:8000/admin/faults -H 'Content-Type: application/json' \
  -d '{"type":"latency","duration_seconds":60,"parameters":{"delay_ms":3000}}'

curl -s -o /dev/null -w "time=%{time_total}s\n" http://localhost:8000/v1/stations
# → time=3.010s

curl -s -o /dev/null -w "time=%{time_total}s\n" http://localhost:8000/admin/events
# → time=0.004s     ← admin still fast
```

The backend's per-request timeout is 3.0s. A `delay_ms` at or above that
makes the request *time out* rather than merely slow down — which is a
different code path and a different failure mode. Use `delay_ms: 3000` to
probe the timeout itself; use something like `500` to probe the slow path.

### 3.3 `error_rate` — partial failure

```bash
curl -s -X POST http://localhost:8000/admin/faults -H 'Content-Type: application/json' \
  -d '{"type":"error_rate","duration_seconds":40,"parameters":{"rate":0.6}}'

for i in $(seq 8); do
  curl -s -o /dev/null -w "req$i http=%{http_code}\n" http://localhost:8000/v1/stations
done
```

```
req1 http=503   req2 http=200   req3 http=503   req4 http=200
req5 http=200   req6 http=503   req7 http=503   req8 http=200
```

The failure is random per request, so a single sample proves nothing. This is
the fault that exposes anything which assumes one bad response means the
upstream is down.

### 3.4 `stale_data` — the honest-freshness fault

```bash
curl -s -X POST http://localhost:8000/admin/faults -H 'Content-Type: application/json' \
  -d '{"type":"stale_data","duration_seconds":40,"parameters":{}}'

curl -s -D - -o /dev/null http://localhost:8000/v1/stations | grep -i simulator-stale
# → x-simulator-stale: true
```

The header is the simulator's way of saying *"the numbers I am giving you are
older than I would like"*. The backend reads it and **flushes the whole
`sim:*` Redis cache namespace** — not just the key it happened to be reading,
because a stale response means *any* cached snapshot may be out of date
(`backend/app/simulator_client.py`, `_check_stale`).

What it does **not** do is set the backend's own `snapshot.is_stale` flag —
that flag is the backend's judgement about *its own* snapshot's age, a
separate concern. So with `stale_data` alone you will typically see:

```
is_stale: false   sse_reachable: true   resync_required: false
```

and the console showing no banner, because the backend is telling the truth:
it has just thrown away its cache and re-fetched. The *upstream* is stale;
the backend's copy is fresh.

The way to actually see this fault working is in the backend's log:

```bash
docker compose logs backend --since 30s 2>&1 | grep -i "stale simulator"
```

```
WARNING  simulator_client: Stale simulator data on /v1/metrics (X-Simulator-Stale: true); invalidating cache
WARNING  simulator_client: Stale simulator data detected; invalidated 0 cached key(s)
```

Note `invalidated 0 cached key(s)` — that is normal. The count is zero when
nothing was cached at that moment, because the flush is a *safety* measure:
it guarantees no stale value can survive, whether or not one existed. A
non-zero count means it caught something real.

The header appears on `/v1/*` GETs only — `/v1/health` never carries it.

> If you want the console's **"Stale data"** banner specifically, inject
> `unavailable` (§3.1), not `stale_data`. That fault genuinely prevents the
> backend from refreshing, so its snapshot really does age out.

### 3.5 `stream_disconnect` — and why it does *not* drop a live stream

```bash
curl -s -X POST http://localhost:8000/admin/faults -H 'Content-Type: application/json' \
  -d '{"type":"stream_disconnect","duration_seconds":45,"parameters":{}}'

curl -s -o /dev/null -w "http=%{http_code}\n" --max-time 5 http://localhost:8000/v1/stream
# → http=503
```

The critical detail: this fault **rejects new stream connections only**. It
does not tear down an already-established subscription. An SSE subscriber that
connected before the injection keeps receiving events.

Measured consequence — the backend's watchdog did **not** fire:

```
t+10s  mode=polling  drops=0  forced_reconnects=4  last_tick=17
t+30s  mode=polling  drops=0  forced_reconnects=4  last_tick=19
t+60s  mode=sse      drops=0  forced_reconnects=4  last_tick=22
```

The tick kept advancing *and* events kept arriving, so the backend had no
evidence of a drop. `suspected_drops` stayed at 0 — correctly. The watchdog
(`broker.py`) requires **both** conditions:

1. the tick advanced, and
2. silence exceeded `SSE_STALE_AFTER` (9s).

A stream that is still delivering events fails neither. The fault only becomes
visible at the next **reconnect** attempt, which returns 503.

So `stream_disconnect` is not a good test of the *silent-drop* path. For
that, the transport has to actually go silent while the world keeps moving —
which is what `unavailable` approximates, and what the watchdog's tick-progress
check exists to catch.

### 3.6 The watchdog, and the tick interval

```bash
curl -s http://localhost:8002/api/stream-health
```

The broker's watchdog samples `/v1/instance` every `WATCHDOG_INTERVAL` (1s) and
compares the tick to the last one it saw. Events arrive **once per tick**, and
a tick is **2 real seconds** — the simulator's runner sleeps
`max(0.05, 1.0 / max(0.1, speed))`, and the default `SIMULATION_SPEED=0.5`
gives a 2s tick. (That inner `max(0.1, ...)` is the floor: the slowest
reachable speed is `0.1`, a 10s tick.)

This is why `SSE_STALE_AFTER` is **9 seconds**, not 4. A threshold near the
tick period would fire on a *healthy* but slow world and report a drop that
never happened. The threshold must exceed the tick interval with slack —
9s is about 4 ticks.

If you change `SIMULATION_SPEED`, re-check `SSE_STALE_AFTER` and
`POLL_INTERVAL` together. A mismatch in the other direction makes a healthy
world look like a dropped stream.

### 3.7 The freshness threshold has the same problem, in reverse

`STATE_MAX_AGE` decides when the backend calls its own snapshot stale. It has
an awkward constraint that is easy to get wrong:

> **The snapshot can only ever be as fresh as the poll that captured it.**

The backend refreshes its snapshot every `POLL_INTERVAL` (1s). If
`STATE_MAX_AGE` is *below* that, the snapshot is flagged stale for most of
every poll cycle — and the console, faithfully, renders a red **"Stale data"**
banner on a system that is working perfectly.

This is not hypothetical. It is exactly what this project shipped for a while:
`STATE_MAX_AGE=1.0` against `POLL_INTERVAL=5`, which made the banner fire
**80% of the time** on a healthy stack. Sampling `/api/stream-health` ten
times in a row showed `is_stale` true on eight of them.

The fix is `STATE_MAX_AGE = 3` — three poll intervals of slack. (It was 15
when `POLL_INTERVAL` was 5; the ratio is what matters, not the number.) The
correctness rule is simple:

> **`STATE_MAX_AGE` must be greater than `POLL_INTERVAL`.**

Any threshold that is not comfortably above the refresh interval turns the
freshness contract into a permanent false alarm. And because a permanently
visible warning is indistinguishable from a real one, it also destroys the
signal: after a week of seeing "Stale data" on a healthy system, nobody
looks up when it is true.

That is the deeper reason to run these injections at all. A fault you inject
proves the failure path works. A **healthy** system you inject nothing into
proves the success path is quiet — and the second one is the one that finds
misconfigurations like this.

---

## 4. A full pass, end to end

```bash
# 1. A clean, paused world
curl -s -X POST http://localhost:8000/admin/reset

# 2. Break a route that has no alternative
curl -s -X POST http://localhost:8000/admin/events \
  -H 'Content-Type: application/json' \
  -d '{"type":"route_disruption","start_tick":0,"duration_ticks":10,
       "parameters":{"route_ids":["route-patiya-coxsbazar"]}}'

# 3. Run the world
curl -s -X POST http://localhost:8000/admin/run

# 4. Wait one tick, then look at what the system thinks
sleep 12
curl -s http://localhost:8002/api/anomalies    # → ROUTE_BOTTLENECK, CRITICAL
curl -s http://localhost:8002/api/metrics      # → kpis.disrupted_routes: 1
curl -s http://localhost:8002/api/incidents    # → one grouped incident

# 5. And what the console shows
open http://localhost:8003
#    StatusBar → "Disrupted 1"
#    Problems tab → "Route Bottleneck affecting route-patiya-coxsbazar"
```

Then break the transport underneath it:

```bash
# 6. The upstream goes away
curl -s -X POST http://localhost:8000/admin/faults \
  -H 'Content-Type: application/json' \
  -d '{"type":"unavailable","duration_seconds":45,"parameters":{}}'

sleep 15
curl -s http://localhost:8002/api/stream-health   # → mode: polling, is_stale: true
#    Console → red "Stale data" banner, mode reads "polling"

# 7. Let it recover on its own — no restart, no intervention
sleep 45
curl -s http://localhost:8002/api/stream-health   # → mode: sse, is_stale: false
#    Console → banner gone
```

The banner text depends on which condition the backend reports first, and it
checks staleness before unreachability. With `unavailable`, `sse_reachable`
goes false *and* the snapshot ages out — the banner reads **"Stale data"**.
"Upstream unreachable" is reserved for the case where the stream is down but
the snapshot is still fresh enough to trust.

`stale_data` is a different mechanism entirely and produces no banner: it
flushes the backend's cache rather than aging its snapshot (§3.4).

And prove the invariant survived the whole thing:

```bash
cd frontend && LIVE_BACKEND=http://localhost:8002 npm test
# → 136 passed
```

The live suite renders the console against the **running, injected** world and
checks that every number on screen came from a backend response. It is the
strongest single check that the stack behaves under stress — a passing run
against a broken world means more than a passing run against a healthy one.

---

## 5. What each surface should show

| Injection | Simulator | Backend | Console |
| :--- | :--- | :--- | :--- |
| `route_disruption` | route → `DISRUPTED` | `ROUTE_BOTTLENECK` anomaly + incident | StatusBar "Disrupted 1"; Problems tab |
| `station_outage` | station → `OUTAGE` | *no anomaly*; allocations → `409 STATION_CLOSED` | Network tab: the station's dot turns red |
| `depot_constraint` | depot → `CONSTRAINED` | *no anomaly*; depot still dispatchable | Network tab: the depot's dot turns amber |
| `demand_spike` | `demand_multiplier` × multiplier | `DEMAND_SPIKE` anomaly; forecast demand ×multiplier | Forecast tab |
| `shipment_delay` | supply `planned_tick` += delay, `DELAYED` | *no anomaly*; depot resupply arrives later | Forecast tab (indirectly) |
| `supply_shortfall` | supply `quantity` × factor | *no anomaly*; depot holds less | Forecast tab (indirectly) |
| `unavailable` | `503` on `/v1/*` | `mode: polling`, `is_stale: true` | "Upstream unreachable", mode "polling" |
| `latency` | every request delayed | timeouts above 3s | panels slow, then error |
| `error_rate` | random `503`s | partial failures retried | intermittent, still correct |
| `stale_data` | `X-Simulator-Stale: true` | Redis cache flushed; snapshot re-fetched | no banner (backend is honest) |
| `stream_disconnect` | new `/v1/stream` → `503` | reconnect blocked | no visible change until reconnect |

---

## 6. Two rules that will save you a debugging session

**Reset the world before you test.** `POST /admin/reset` returns the baseline
scenario at tick 0 with the seed back to 12345. Without it you are debugging a
world somebody else has already perturbed — and the baseline is well-stocked
for its first ~60 ticks, so an empty plan at tick 5 is *correct*, not a bug.

**Use `duration_ticks` generously.** The events resolve on their own, and a
short duration means the effect has vanished by the time you look at it. Ten
ticks is 150 simulated minutes — about 100 real seconds at the current speed —
which is long enough to observe anything and short enough not to interfere with
the next test.
