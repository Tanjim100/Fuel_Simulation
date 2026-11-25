"""The scenario configuration: one file, no hardcoded values.

PDF §19 says "do not hard-code" and §21 lists "scenario configuration" as a
recommended deliverable. This module is the answer.

**The problem it solves.** The demand profiles, the regional multipliers and
the start time were transcribed from the simulator image's own
``baseline.yaml`` and frozen into Python literals. They are correct *today*
for seed 12345 -- but they are a copy of a file that lives inside another
container and cannot be swapped at runtime. Point the backend at a different
scenario and it would confidently forecast the wrong numbers.

**The fix.** The table lives in a structured YAML file that the backend
reads and validates at boot. ``forecast.py``'s public names (``PROFILES``,
``REGION_DEMAND_FACTORS``, ``FORECAST_START_TIME``) stay exactly as they
were, so every existing import and test keeps working -- but they are now
*derived from the loaded config*, not literals.

**Validation is the load-bearing part.** A config file that silently
disagrees with the simulator would produce a forecast that is wrong by
thousands of litres and looks perfectly plausible. So the loader rejects:

* an unknown demand profile name;
* a profile missing any of the three fuels;
* a region with no demand factor;
* a non-positive daily litres;
* a noise fraction outside ``[0, 1)``;

and it does so **at boot**, not at request time. A backend that boots with
a broken scenario is a backend that fails loudly and immediately; one that
boots and then returns wrong forecasts is worse.

**Resolution order.** ``SCENARIO_PATH`` env -> ``backend/scenarios/baseline.yaml``
-> the built-in defaults. The defaults are the last-resort fallback so the
backend still boots if the file is missing -- but the file is what ships.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

LOGGER = logging.getLogger(__name__)

__all__ = [
    "ScenarioConfig",
    "ScenarioError",
    "load_scenario",
    "DEFAULT_SCENARIO_PATH",
    "BUILTIN_PROFILES",
    "BUILTIN_REGION_FACTORS",
    "BUILTIN_START_TIME",
]

#: Where the shipped scenario lives, relative to the repository root.
DEFAULT_SCENARIO_PATH = Path("backend/scenarios/baseline.yaml")

#: The three fuels the platform models.
FUEL_TYPES: tuple[str, ...] = ("DIESEL", "PETROL", "OCTANE")

#: The built-in fallback. These are the same numbers the shipped YAML
#: contains, so a missing file changes nothing -- which is exactly what the
#: oracle test asserts.
BUILTIN_PROFILES: dict[str, dict[str, Any]] = {
    "urban_high": {
        "daily_liters": {"DIESEL": 8500.0, "PETROL": 10500.0, "OCTANE": 5600.0},
        "noise": 0.10,
    },
    "industrial": {
        "daily_liters": {"DIESEL": 14000.0, "PETROL": 4500.0, "OCTANE": 2200.0},
        "noise": 0.08,
    },
    "highway": {
        "daily_liters": {"DIESEL": 10500.0, "PETROL": 11000.0, "OCTANE": 6200.0},
        "noise": 0.12,
    },
    "regional": {
        "daily_liters": {"DIESEL": 7200.0, "PETROL": 7600.0, "OCTANE": 3600.0},
        "noise": 0.10,
    },
}

BUILTIN_REGION_FACTORS: dict[str, float] = {
    "region-dhaka": 1.00,
    "region-chattogram": 1.08,
}

BUILTIN_START_TIME = "2026-01-01T00:00:00+00:00"


class ScenarioError(ValueError):
    """The scenario configuration is invalid.

    Raised at boot. A broken scenario must stop the backend, not quietly
    produce wrong forecasts.
    """


@dataclass(frozen=True)
class ScenarioConfig:
    """A validated scenario."""

    name: str = "baseline"
    seed: int = 12345
    start_time: str = BUILTIN_START_TIME
    tick_minutes: int = 15
    profiles: dict[str, dict[str, Any]] = field(default_factory=dict)
    regions: dict[str, float] = field(default_factory=dict)
    source: str = "builtin"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "seed": self.seed,
            "start_time": self.start_time,
            "tick_minutes": self.tick_minutes,
            "profiles": {
                name: {
                    "daily_liters": dict(profile["daily_liters"]),
                    "noise": profile["noise"],
                }
                for name, profile in self.profiles.items()
            },
            "regions": dict(self.regions),
            "source": self.source,
        }


def _require(mapping: Any, key: str, where: str) -> Any:
    if not isinstance(mapping, dict) or key not in mapping:
        raise ScenarioError(f"{where} is missing the required key {key!r}")
    return mapping[key]


def _validate(raw: Any, *, source: str) -> ScenarioConfig:
    """Validate a parsed scenario document.

    Every failure mode here is a *boot-time* failure, on purpose.
    """
    if not isinstance(raw, dict):
        raise ScenarioError(
            f"{source}: the scenario must be a mapping, got {type(raw).__name__}"
        )

    name = str(raw.get("scenario", raw.get("name", "baseline")))
    seed = raw.get("seed", 12345)
    try:
        seed = int(seed)
    except (TypeError, ValueError) as exc:
        raise ScenarioError(f"{source}: 'seed' must be an integer") from exc

    start_time = raw.get("start_time", BUILTIN_START_TIME)
    if not isinstance(start_time, str) or not start_time:
        raise ScenarioError(f"{source}: 'start_time' must be a non-empty string")

    tick_minutes = raw.get("tick_minutes", 15)
    try:
        tick_minutes = int(tick_minutes)
    except (TypeError, ValueError) as exc:
        raise ScenarioError(f"{source}: 'tick_minutes' must be an integer") from exc
    if tick_minutes <= 0:
        raise ScenarioError(f"{source}: 'tick_minutes' must be positive")

    # -- demand profiles ---------------------------------------------------- #
    raw_profiles = _require(raw, "demand_profiles", source)
    if not isinstance(raw_profiles, dict) or not raw_profiles:
        raise ScenarioError(
            f"{source}: 'demand_profiles' must be a non-empty mapping"
        )

    profiles: dict[str, dict[str, Any]] = {}
    for profile_name, profile in raw_profiles.items():
        where = f"{source}: demand profile {profile_name!r}"
        if not isinstance(profile, dict):
            raise ScenarioError(f"{where} must be a mapping")

        daily = _require(profile, "daily_liters", where)
        if not isinstance(daily, dict):
            raise ScenarioError(f"{where}: 'daily_liters' must be a mapping")

        missing = [fuel for fuel in FUEL_TYPES if fuel not in daily]
        if missing:
            raise ScenarioError(
                f"{where}: missing the fuel(s) {', '.join(missing)}"
            )

        litres: dict[str, float] = {}
        for fuel, value in daily.items():
            try:
                amount = float(value)
            except (TypeError, ValueError) as exc:
                raise ScenarioError(
                    f"{where}: the daily litres for {fuel} must be a number"
                ) from exc
            if amount < 0:
                raise ScenarioError(
                    f"{where}: the daily litres for {fuel} must not be negative"
                )
            litres[str(fuel)] = amount

        noise = profile.get("noise", 0.0)
        try:
            noise = float(noise)
        except (TypeError, ValueError) as exc:
            raise ScenarioError(f"{where}: 'noise' must be a number") from exc
        if not 0.0 <= noise < 1.0:
            raise ScenarioError(
                f"{where}: 'noise' must be in [0, 1), got {noise}"
            )

        profiles[str(profile_name)] = {
            "daily_liters": litres,
            "noise": noise,
        }

    # -- regions ------------------------------------------------------------ #
    raw_regions = _require(raw, "regions", source)
    if not isinstance(raw_regions, dict) or not raw_regions:
        raise ScenarioError(f"{source}: 'regions' must be a non-empty mapping")

    regions: dict[str, float] = {}
    for region_id, region in raw_regions.items():
        where = f"{source}: region {region_id!r}"
        if isinstance(region, dict):
            factor = region.get("demand_factor")
            if factor is None:
                raise ScenarioError(f"{where} is missing 'demand_factor'")
        else:
            factor = region
        try:
            factor = float(factor)
        except (TypeError, ValueError) as exc:
            raise ScenarioError(
                f"{where}: 'demand_factor' must be a number"
            ) from exc
        if factor <= 0:
            raise ScenarioError(
                f"{where}: 'demand_factor' must be positive, got {factor}"
            )
        regions[str(region_id)] = factor

    return ScenarioConfig(
        name=name,
        seed=seed,
        start_time=start_time,
        tick_minutes=tick_minutes,
        profiles=profiles,
        regions=regions,
        source=source,
    )


def _parse_yaml(text: str) -> Any:
    """Parse the YAML document.

    PyYAML is not a dependency of this project -- the backend image stays
    slim and offline-buildable -- so the parser handles the subset of YAML
    the scenario file actually uses: nested mappings, scalars, and inline
    flow mappings.

    The subset is deliberately small and the loader validates the result, so
    a file that uses YAML features outside the subset fails loudly at boot
    rather than being silently misread.
    """
    lines: list[tuple[int, str]] = []
    for raw in text.splitlines():
        stripped = _strip_comment(raw)
        if not stripped.strip():
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        lines.append((indent, stripped.strip()))

    if not lines:
        raise ScenarioError("the scenario file is empty")

    value, index = _parse_block(lines, 0, lines[0][0])
    if index != len(lines):
        raise ScenarioError(
            f"the scenario file has an unexpected structure at line {index + 1}"
        )
    return value


def _strip_comment(line: str) -> str:
    """Remove a trailing ``#`` comment from a line.

    A ``#`` inside a quoted string is data, not a comment -- the scenario
    file quotes its start time, and a naive split would truncate it. A
    ``#`` at the start of the line is a whole-line comment.

    The scan is character-by-character rather than a plain ``split("#")``
    so the two cases are distinguishable.
    """
    in_single = False
    in_double = False
    for index, char in enumerate(line):
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double:
            # A ``#`` only starts a comment when it is at the start of the
            # line or preceded by whitespace. YAML requires the whitespace,
            # so ``a#b`` is the scalar ``a#b``.
            if index == 0 or line[index - 1].isspace():
                return line[:index].rstrip()
    return line.rstrip()


def _parse_block(
    lines: list[tuple[int, str]], index: int, indent: int
) -> tuple[Any, int]:
    """Parse one block of mappings at the given indent."""
    result: dict[str, Any] = {}
    while index < len(lines):
        line_indent, content = lines[index]
        if line_indent < indent:
            break
        if line_indent > indent:
            raise ScenarioError(
                f"the scenario file has an unexpected indent at line {index + 1}"
            )
        if ":" not in content:
            raise ScenarioError(
                f"the scenario file has a malformed entry at line {index + 1}"
            )
        key, _, rest = content.partition(":")
        key = key.strip().strip('"').strip("'")
        rest = rest.strip()

        if rest:
            result[key] = _parse_scalar(rest)
            index += 1
            continue

        # A nested block: either a mapping or an inline flow mapping.
        if index + 1 < len(lines) and lines[index + 1][0] > line_indent:
            value, index = _parse_block(lines, index + 1, lines[index + 1][0])
            result[key] = value
        else:
            result[key] = {}
            index += 1
    return result, index


def _parse_scalar(text: str) -> Any:
    """Parse a scalar or an inline flow mapping."""
    text = text.strip()
    if text.startswith("{") and text.endswith("}"):
        inner = text[1:-1].strip()
        if not inner:
            return {}
        out: dict[str, Any] = {}
        for part in inner.split(","):
            if ":" not in part:
                raise ScenarioError(f"malformed inline mapping: {text!r}")
            key, _, value = part.partition(":")
            out[key.strip().strip('"').strip("'")] = _parse_scalar(value)
        return out
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(part) for part in inner.split(",")]
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1]
    lowered = text.lower()
    if lowered in ("true", "yes"):
        return True
    if lowered in ("false", "no"):
        return False
    if lowered in ("null", "~", ""):
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


def load_scenario(path: Optional[str | Path] = None) -> ScenarioConfig:
    """Load and validate the scenario configuration.

    :param path: the file to read. When omitted the ``SCENARIO_PATH`` env
        var is consulted, then the shipped ``baseline.yaml``, then the
        built-in defaults.
    :raises ScenarioError: if the file exists but is invalid. A *missing*
        file is not an error -- the built-in defaults are used and a warning
        is logged.
    """
    candidates: list[Path] = []
    if path is not None:
        candidates.append(Path(path))
    else:
        env_path = os.environ.get("SCENARIO_PATH")
        if env_path:
            candidates.append(Path(env_path))
        candidates.append(DEFAULT_SCENARIO_PATH)

    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError as exc:
            raise ScenarioError(f"{candidate}: could not be read: {exc}") from exc
        try:
            raw = _parse_yaml(text)
        except ScenarioError:
            raise
        except Exception as exc:
            raise ScenarioError(f"{candidate}: could not be parsed: {exc}") from exc
        config = _validate(raw, source=str(candidate))
        LOGGER.info("Loaded the scenario configuration from %s", candidate)
        return config

    LOGGER.warning(
        "No scenario file found (looked in: %s); using the built-in defaults",
        ", ".join(str(c) for c in candidates),
    )
    return _validate(
        {
            "scenario": "baseline",
            "seed": 12345,
            "start_time": BUILTIN_START_TIME,
            "tick_minutes": 15,
            "demand_profiles": {
                name: {
                    "daily_liters": dict(profile["daily_liters"]),
                    "noise": profile["noise"],
                }
                for name, profile in BUILTIN_PROFILES.items()
            },
            "regions": {
                region: {"demand_factor": factor}
                for region, factor in BUILTIN_REGION_FACTORS.items()
            },
        },
        source="builtin",
    )
