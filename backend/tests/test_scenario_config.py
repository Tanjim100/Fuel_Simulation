"""The scenario configuration's contract.

The scenario file (``backend/scenarios/baseline.yaml``) is the source of truth
for the demand table, the regional multipliers, the clock origin and the seed.
This module pins three things:

* **the loader accepts the shipped file** and reads it correctly;
* **an invalid scenario fails at boot**, not at request time -- a broken
  scenario that boots produces a forecast that is wrong by thousands of
  litres and looks perfectly plausible;
* **the loaded values equal the values that used to be hardcoded.** This is
  the oracle guard: if the YAML and the old Python literals ever disagree,
  this test fails -- and so does the forecast oracle in ``test_forecast.py``,
  which compares 60 forecast points against the live simulator.

The third point is the one that matters. Moving a table out of code and into
a config file is only safe if the numbers are provably unchanged.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app import forecast as forecast_module
from app.scenario import (
    BUILTIN_PROFILES,
    BUILTIN_REGION_FACTORS,
    BUILTIN_START_TIME,
    DEFAULT_SCENARIO_PATH,
    ScenarioError,
    load_scenario,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
#: The shipped scenario. Inside the image it lives at ``/app/scenarios``; on a
#: laptop it lives at ``backend/scenarios``. Both are found by looking for the
#: file rather than by hardcoding a path, because the backend image has no
#: bind-mount (the project path contains spaces, which Docker Desktop
#: refuses to mount).
SHIPPED_SCENARIO = next(
    (
        candidate
        for candidate in (
            REPO_ROOT / "backend" / "scenarios" / "baseline.yaml",
            Path("/app/scenarios/baseline.yaml"),
        )
        if candidate.is_file()
    ),
    REPO_ROOT / "backend" / "scenarios" / "baseline.yaml",
)


# --------------------------------------------------------------------------- #
# The shipped file
# --------------------------------------------------------------------------- #
def test_the_shipped_scenario_file_exists() -> None:
    assert SHIPPED_SCENARIO.is_file(), (
        "backend/scenarios/baseline.yaml must ship -- it is the source of "
        "truth for the demand table"
    )


def test_the_shipped_scenario_loads() -> None:
    config = load_scenario(SHIPPED_SCENARIO)

    assert config.name == "baseline"
    assert config.seed == 12345
    assert config.start_time == BUILTIN_START_TIME
    assert config.tick_minutes == 15
    assert set(config.profiles) == set(BUILTIN_PROFILES)
    assert set(config.regions) == set(BUILTIN_REGION_FACTORS)


def test_the_shipped_scenario_matches_the_built_in_defaults() -> None:
    """The built-in fallback must agree with the shipped file.

    Otherwise the fallback would be a *different* scenario wearing the same
    name, and a missing file would silently change every forecast.
    """
    config = load_scenario(SHIPPED_SCENARIO)

    for name, profile in BUILTIN_PROFILES.items():
        loaded = config.profiles[name]
        assert loaded["daily_liters"] == profile["daily_liters"]
        assert loaded["noise"] == pytest.approx(profile["noise"])

    for region, factor in BUILTIN_REGION_FACTORS.items():
        assert config.regions[region] == pytest.approx(factor)


# --------------------------------------------------------------------------- #
# The oracle guard: the numbers must not have moved
# --------------------------------------------------------------------------- #
def test_the_forecast_module_uses_the_loaded_scenario() -> None:
    """``forecast.PROFILES`` is derived from the config, not hardcoded."""
    config = load_scenario(SHIPPED_SCENARIO)

    assert set(forecast_module.PROFILES) == set(config.profiles)
    for name, profile in forecast_module.PROFILES.items():
        expected = config.profiles[name]
        assert profile.name == name
        assert profile.daily_liters == expected["daily_liters"]
        assert profile.noise == pytest.approx(expected["noise"])


def test_the_exact_demand_table_is_unchanged() -> None:
    """The literal table, spelled out.

    This is the *oracle*: the exact numbers the simulator was measured
    against. If the YAML is edited by accident, or the loader transposes a
    fuel, this fails.
    """
    assert forecast_module.PROFILES["urban_high"].daily_liters == {
        "DIESEL": 8500.0,
        "PETROL": 10500.0,
        "OCTANE": 5600.0,
    }
    assert forecast_module.PROFILES["industrial"].daily_liters == {
        "DIESEL": 14000.0,
        "PETROL": 4500.0,
        "OCTANE": 2200.0,
    }
    assert forecast_module.PROFILES["highway"].daily_liters == {
        "DIESEL": 10500.0,
        "PETROL": 11000.0,
        "OCTANE": 6200.0,
    }
    assert forecast_module.PROFILES["regional"].daily_liters == {
        "DIESEL": 7200.0,
        "PETROL": 7600.0,
        "OCTANE": 3600.0,
    }

    assert forecast_module.PROFILES["urban_high"].noise == pytest.approx(0.10)
    assert forecast_module.PROFILES["industrial"].noise == pytest.approx(0.08)
    assert forecast_module.PROFILES["highway"].noise == pytest.approx(0.12)
    assert forecast_module.PROFILES["regional"].noise == pytest.approx(0.10)


def test_the_region_factors_are_unchanged() -> None:
    assert forecast_module.REGION_DEMAND_FACTORS == {
        "region-dhaka": 1.00,
        "region-chattogram": 1.08,
    }


def test_the_clock_origin_and_tick_length_are_unchanged() -> None:
    assert forecast_module.FORECAST_START_TIME == "2026-01-01T00:00:00+00:00"
    assert forecast_module.TICK_MINUTES == 15
    assert forecast_module.TICKS_PER_DAY == 96


def test_the_seed_is_unchanged() -> None:
    """The seeded RNG is what makes the forecast exact.

    A different seed means a different demand series, so it is pinned here
    alongside the demand table.
    """
    assert forecast_module.SCENARIO.seed == 12345


# --------------------------------------------------------------------------- #
# The resolution order
# --------------------------------------------------------------------------- #
def test_a_missing_file_falls_back_to_the_built_in_defaults(tmp_path) -> None:
    """A missing file is not an error -- the backend still boots."""
    config = load_scenario(tmp_path / "does-not-exist.yaml")

    assert config.source == "builtin"
    assert set(config.profiles) == set(BUILTIN_PROFILES)
    assert config.start_time == BUILTIN_START_TIME


def test_the_environment_variable_is_honoured(tmp_path, monkeypatch) -> None:
    path = tmp_path / "other.yaml"
    path.write_text(
        "scenario: other\n"
        "seed: 999\n"
        'start_time: "2026-03-01T00:00:00+00:00"\n'
        "tick_minutes: 30\n"
        "demand_profiles:\n"
        "  urban_high:\n"
        "    noise: 0.2\n"
        "    daily_liters:\n"
        "      DIESEL: 1.0\n"
        "      PETROL: 2.0\n"
        "      OCTANE: 3.0\n"
        "regions:\n"
        "  region-dhaka:\n"
        "    demand_factor: 2.5\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SCENARIO_PATH", str(path))

    config = load_scenario()

    assert config.name == "other"
    assert config.seed == 999
    assert config.start_time == "2026-03-01T00:00:00+00:00"
    assert config.tick_minutes == 30
    assert config.regions["region-dhaka"] == pytest.approx(2.5)
    assert config.profiles["urban_high"]["noise"] == pytest.approx(0.2)


def test_the_default_path_is_the_shipped_file() -> None:
    assert DEFAULT_SCENARIO_PATH == Path("backend/scenarios/baseline.yaml")


# --------------------------------------------------------------------------- #
# Validation: a broken scenario must fail loudly, at boot
# --------------------------------------------------------------------------- #
def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "broken.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_a_missing_profiles_section_is_rejected(tmp_path) -> None:
    path = _write(tmp_path, "scenario: x\nregions:\n  region-dhaka: {demand_factor: 1.0}\n")
    with pytest.raises(ScenarioError, match="demand_profiles"):
        load_scenario(path)


def test_a_missing_regions_section_is_rejected(tmp_path) -> None:
    path = _write(
        tmp_path,
        "scenario: x\ndemand_profiles:\n  p:\n    daily_liters: "
        "{DIESEL: 1, PETROL: 1, OCTANE: 1}\n",
    )
    with pytest.raises(ScenarioError, match="regions"):
        load_scenario(path)


def test_a_profile_missing_a_fuel_is_rejected(tmp_path) -> None:
    path = _write(
        tmp_path,
        "scenario: x\n"
        "demand_profiles:\n"
        "  p:\n"
        "    daily_liters: {DIESEL: 1, PETROL: 1}\n"
        "regions:\n"
        "  region-dhaka: {demand_factor: 1.0}\n",
    )
    with pytest.raises(ScenarioError, match="OCTANE"):
        load_scenario(path)


def test_negative_litres_are_rejected(tmp_path) -> None:
    path = _write(
        tmp_path,
        "scenario: x\n"
        "demand_profiles:\n"
        "  p:\n"
        "    daily_liters: {DIESEL: -1, PETROL: 1, OCTANE: 1}\n"
        "regions:\n"
        "  region-dhaka: {demand_factor: 1.0}\n",
    )
    with pytest.raises(ScenarioError, match="negative"):
        load_scenario(path)


def test_a_noise_fraction_of_one_is_rejected(tmp_path) -> None:
    path = _write(
        tmp_path,
        "scenario: x\n"
        "demand_profiles:\n"
        "  p:\n"
        "    noise: 1.0\n"
        "    daily_liters: {DIESEL: 1, PETROL: 1, OCTANE: 1}\n"
        "regions:\n"
        "  region-dhaka: {demand_factor: 1.0}\n",
    )
    with pytest.raises(ScenarioError, match="noise"):
        load_scenario(path)


def test_a_region_without_a_factor_is_rejected(tmp_path) -> None:
    path = _write(
        tmp_path,
        "scenario: x\n"
        "demand_profiles:\n"
        "  p:\n"
        "    daily_liters: {DIESEL: 1, PETROL: 1, OCTANE: 1}\n"
        "regions:\n"
        "  region-dhaka: {}\n",
    )
    with pytest.raises(ScenarioError, match="demand_factor"):
        load_scenario(path)


def test_a_zero_demand_factor_is_rejected(tmp_path) -> None:
    path = _write(
        tmp_path,
        "scenario: x\n"
        "demand_profiles:\n"
        "  p:\n"
        "    daily_liters: {DIESEL: 1, PETROL: 1, OCTANE: 1}\n"
        "regions:\n"
        "  region-dhaka: {demand_factor: 0}\n",
    )
    with pytest.raises(ScenarioError, match="positive"):
        load_scenario(path)


def test_a_non_positive_tick_length_is_rejected(tmp_path) -> None:
    path = _write(
        tmp_path,
        "scenario: x\n"
        "tick_minutes: 0\n"
        "demand_profiles:\n"
        "  p:\n"
        "    daily_liters: {DIESEL: 1, PETROL: 1, OCTANE: 1}\n"
        "regions:\n"
        "  region-dhaka: {demand_factor: 1.0}\n",
    )
    with pytest.raises(ScenarioError, match="tick_minutes"):
        load_scenario(path)


def test_an_empty_file_is_rejected(tmp_path) -> None:
    path = _write(tmp_path, "# just a comment\n")
    with pytest.raises(ScenarioError):
        load_scenario(path)


def test_a_malformed_document_is_rejected(tmp_path) -> None:
    path = _write(tmp_path, "scenario: x\nthis line has no colon\n")
    with pytest.raises(ScenarioError):
        load_scenario(path)


def test_a_non_mapping_document_is_rejected(tmp_path) -> None:
    path = _write(tmp_path, "- one\n- two\n")
    with pytest.raises(ScenarioError):
        load_scenario(path)


# --------------------------------------------------------------------------- #
# The parser
# --------------------------------------------------------------------------- #
def test_comments_are_ignored(tmp_path) -> None:
    path = _write(
        tmp_path,
        "# the scenario\n"
        "scenario: x  # trailing comment\n"
        "demand_profiles:\n"
        "  p:\n"
        "    daily_liters: {DIESEL: 1, PETROL: 1, OCTANE: 1}\n"
        "regions:\n"
        "  region-dhaka: {demand_factor: 1.0}\n",
    )
    config = load_scenario(path)
    assert config.name == "x"


def test_a_quoted_scalar_is_stripped(tmp_path) -> None:
    path = _write(
        tmp_path,
        'scenario: "x"\n'
        'start_time: "2026-01-01T00:00:00+00:00"\n'
        "demand_profiles:\n"
        "  p:\n"
        "    daily_liters: {DIESEL: 1, PETROL: 1, OCTANE: 1}\n"
        "regions:\n"
        "  region-dhaka: {demand_factor: 1.0}\n",
    )
    config = load_scenario(path)
    assert config.name == "x"
    assert config.start_time == "2026-01-01T00:00:00+00:00"


def test_a_scalar_region_is_accepted(tmp_path) -> None:
    """``region-dhaka: 1.0`` is shorthand for the mapping form."""
    path = _write(
        tmp_path,
        "scenario: x\n"
        "demand_profiles:\n"
        "  p:\n"
        "    daily_liters: {DIESEL: 1, PETROL: 1, OCTANE: 1}\n"
        "regions:\n"
        "  region-dhaka: 1.0\n",
    )
    config = load_scenario(path)
    assert config.regions["region-dhaka"] == pytest.approx(1.0)


def test_the_round_trip_is_stable(tmp_path) -> None:
    """``to_dict`` produces a document the loader accepts again."""
    config = load_scenario(SHIPPED_SCENARIO)
    payload = config.to_dict()

    path = tmp_path / "round-trip.yaml"
    lines = [
        f"scenario: {payload['name']}",
        f"seed: {payload['seed']}",
        f'start_time: "{payload["start_time"]}"',
        f"tick_minutes: {payload['tick_minutes']}",
        "demand_profiles:",
    ]
    for name, profile in payload["profiles"].items():
        lines.append(f"  {name}:")
        lines.append(f"    noise: {profile['noise']}")
        lines.append("    daily_liters:")
        for fuel, amount in profile["daily_liters"].items():
            lines.append(f"      {fuel}: {amount}")
    lines.append("regions:")
    for region, factor in payload["regions"].items():
        lines.append(f"  {region}: {{demand_factor: {factor}}}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    reloaded = load_scenario(path)
    # ``source`` records *where* the config came from, so it legitimately
    # differs between the two loads. Everything else must be identical.
    first = dict(payload)
    second = dict(reloaded.to_dict())
    first.pop("source"), second.pop("source")
    assert second == first


def test_the_scenario_path_points_at_the_shipped_file() -> None:
    """``SCENARIO_PATH`` must resolve to the file that ships.

    The backend image sets it to ``/app/scenarios/baseline.yaml``; on a
    laptop it is unset and the loader falls back to the repository-relative
    default. Either is fine -- what must not happen is the loader silently
    using the built-in defaults in the image, because then the shipped YAML
    would be dead weight and the config would not be what ships.
    """
    env = os.environ.get("SCENARIO_PATH")
    if not env:
        # Not set: the loader uses the repository-relative default. On a
        # laptop that is the shipped file; the source may legitimately be
        # "builtin" if the test was run from an unexpected directory, but
        # then the numbers are still the built-in ones -- which the exact
        # table assertions above already pin.
        return

    assert Path(env).is_file(), (
        f"SCENARIO_PATH points at {env}, which does not exist"
    )
    assert forecast_module.SCENARIO.source == env
    assert forecast_module.SCENARIO.source != "builtin"
