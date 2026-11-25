# Scenario configuration

PDF §19 says *"do not hard-code"*. PDF §21 lists **scenario configuration**
among the recommended deliverables.

This document is that deliverable: what the scenario configuration is, where
it lives, how it is resolved, and how to make a new one.

---

## What was hard-coded, and why it moved

`backend/app/forecast.py` used to carry the demand table as Python literals:

```python
PROFILES = {
    "urban_high": DemandProfile(daily_liters={"DIESEL": 8500.0, "PETROL": 10500.0, ...}),
    ...
}
REGION_DEMAND_FACTORS = {"region-dhaka": 1.00, "region-chattogram": 1.08}
FORECAST_START_TIME = "2026-01-01T00:00:00+00:00"
```

Those values were transcribed from the simulator image's own `baseline.yaml`.
That file lives **inside** the simulator container and cannot be read at
runtime — so the transcription was unavoidable *at the time*. But it meant:

- The scenario could not be swapped without a code change and a redeploy.
- The backend and the simulator had two copies of the same numbers, with
  nothing to say so.
- Changing the scenario meant editing Python, which reads as hard-coding even
  when the literal is correct today.

Now the table lives in `backend/scenarios/baseline.yaml`, is **validated at
boot**, and the Python names are *derived* from it:

```python
SCENARIO: ScenarioConfig = load_scenario()
FORECAST_START_TIME = SCENARIO.start_time
TICK_MINUTES = SCENARIO.tick_minutes
PROFILES = {name: DemandProfile(...) for name, profile in SCENARIO.profiles.items()}
REGION_DEMAND_FACTORS = dict(SCENARIO.regions)
```

The public names are unchanged, so every existing import and test keeps
working. **Only the source of the numbers moved.**

---

## The file

`backend/scenarios/baseline.yaml`:

```yaml
scenario: baseline
seed: 12345

# The clock origin. The simulator's tick 0 is this instant.
start_time: "2026-01-01T00:00:00+00:00"

# Minutes per tick. 15 minutes => 96 ticks per day.
tick_minutes: 15

demand_profiles:
  urban_high:
    noise: 0.10
    daily_liters:
      DIESEL: 8500.0
      PETROL: 10500.0
      OCTANE: 5600.0
  industrial:
    noise: 0.08
    daily_liters: { ... }
  highway:
    noise: 0.12
    daily_liters: { ... }
  regional:
    noise: 0.15
    daily_liters: { ... }

regions:
  region-dhaka:      {demand_factor: 1.00}
  region-chattogram: {demand_factor: 1.08}
```

### The schema

| Key | Type | Required | Meaning |
| :--- | :--- | :--- | :--- |
| `scenario` | string | no (default `baseline`) | The scenario's name. |
| `seed` | integer | no (default 12345) | The world seed. |
| `start_time` | ISO-8601 string | no | The clock origin — the simulator's tick 0. |
| `tick_minutes` | integer > 0 | no (default 15) | Minutes per tick. 15 → 96 ticks per day. |
| `demand_profiles` | mapping | **yes** | The station archetypes. |
| `demand_profiles.<name>.noise` | 0 ≤ n < 1 | **yes** | The uniform jitter fraction, ±. |
| `demand_profiles.<name>.daily_liters` | mapping | **yes** | The nominal daily volume per fuel. |
| `regions` | mapping | **yes** | The per-region demand multipliers. |
| `regions.<id>.demand_factor` | number > 0 | **yes** | The multiplier for that region. |

> **`daily_liters` is the *nominal* daily volume.** The simulator derives the
> per-tick demand from it with an hour-of-day factor, so the realised daily
> volume is **not** `daily_liters`. This is stated in the YAML file itself,
> because it is the easiest thing to get wrong when writing a new scenario.

---

## The resolution order

`backend/app/scenario.py`'s `load_scenario()`:

1. **An explicit path** — `load_scenario(Path(...))`.
2. **The `SCENARIO_PATH` env var** — if set and the file exists.
3. **The shipped file** — `backend/scenarios/baseline.yaml`, or
   `/app/scenarios/baseline.yaml` inside the container (the Dockerfile sets
   `ENV SCENARIO_PATH=/app/scenarios/baseline.yaml`).
4. **The built-in defaults** — the last resort, so the backend still boots if
   the file is missing. A warning is logged when this happens.

**The file is what actually ships.** The built-ins exist so a missing file is
a degraded boot, not a crash.

---

## Validation happens at boot

A broken scenario must **stop the backend**, not quietly produce wrong
forecasts. Every failure mode raises `ScenarioError` during module import:

| Rejected | Why |
| :--- | :--- |
| A missing `demand_profiles` or `regions` | The forecast would have no basis. |
| A profile missing a fuel | The forecast would under-count that fuel. |
| Negative litres | Physically impossible. |
| `noise` ≥ 1 | The jitter would exceed the signal. |
| A missing or zero `demand_factor` | The region's demand would vanish or be inverted. |
| `tick_minutes: 0` | Every tick would be the same instant. |
| An empty file, a malformed line, a non-mapping document | Unparseable. |
| A region with no factor | The multiplier would be undefined. |

**Ten boot-time rejection cases are pinned by `backend/tests/test_scenario_config.py`.**

---

## The oracle guard

**A new config file must not silently change the numbers.**

The guard is the forecast oracle test — `backend/tests/test_forecast.py`
against `tests/fixtures/demand_golden.json`, which asserts **60 of 60 exact
matches** against the live simulator's demand.

If the YAML and the simulator's own scenario ever disagree, the oracle fails.
That failure is the point: a config file that silently disagrees with the
world model produces a forecast that is wrong by thousands of litres and
looks perfectly plausible.

Two more tests pin the equivalence directly:

- `test_the_shipped_file_matches_the_built_in_defaults` — the YAML must
  equal what the hard-coded table used to say.
- `test_the_forecast_module_derives_its_table_from_the_config` — the
  forecast's public names must be derived from the loaded config, not from
  literals.

---

## Making a new scenario

1. **Find the simulator's scenario.** The simulator image ships its own
   `baseline.yaml`. That file is the source of truth — it defines the world
   the backend will forecast against.

2. **Copy it into `backend/scenarios/`** under a new name, e.g.
   `winter_peak.yaml`.

3. **Adapt the shape** to the schema above. The simulator's file may use a
   different layout; the backend's schema is the one documented here.

4. **Point the backend at it:**

   ```bash
   SCENARIO_PATH=/app/scenarios/winter_peak.yaml docker compose up -d --build backend
   ```

   Or in `docker-compose.yml`:

   ```yaml
   backend:
     environment:
       SCENARIO_PATH: /app/scenarios/winter_peak.yaml
   ```

5. **Regenerate the oracle fixture** if the scenario genuinely changed the
   demand, and confirm the forecast matches the simulator's actual demand:

   ```bash
   docker compose exec -T backend pytest tests/test_forecast.py -v
   ```

6. **Restart** — the config is read at import, so the backend must be
   recreated:

   ```bash
   docker compose up -d --build --force-recreate backend
   ```

   (`docker compose build` does **not** recreate the running container.)

---

## Why PyYAML is not a dependency

The backend image stays slim and offline-buildable: `backend/requirements.txt`
is unchanged by this work.

So `backend/app/scenario.py` carries a **minimal YAML parser** — enough for
this schema: nested mappings, scalars, comments, quoted strings. It is about
150 lines and handles exactly the constructs the scenario file uses.

The trade-off is deliberate: a general YAML parser brings a dependency, a
supply-chain surface and a larger image for the sake of reading one file with
a fixed schema. If the schema ever needs to grow into arbitrary YAML, the
parser is the thing to replace — and it is isolated in one module for exactly
that reason.

The parser strips comments correctly, including `#` inside a quoted string —
a bug that was found and fixed during this work, and is now covered by the
scenario tests.

---

## See also

- `backend/scenarios/baseline.yaml` — the shipped scenario.
- `backend/app/scenario.py` — the loader and the validator.
- `backend/tests/test_scenario_config.py` — the guard.
- `docs/BACKEND.md` §6 — the configuration table.
