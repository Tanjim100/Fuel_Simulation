"""The settings' defaults must match the compose file.

`config.py` says so in its own docstring:

    Defaults match the compose file so ``uvicorn app.main:app`` works with
    no environment at all.

That was not true. `SSE_STALE_AFTER`, `POLL_INTERVAL` and `STATE_MAX_AGE`
had code defaults of 20.0, 2.0 and 1.0, while `docker-compose.yml` set
45, 5 and 15.

**That gap was not cosmetic.** `STATE_MAX_AGE` is the snapshot's TTL and
`POLL_INTERVAL` is how often the world is re-read. A TTL *below* the poll
interval marks a healthy world stale for most of every poll cycle -- the
false "Stale data" banner this pair was tuned to avoid. Under the code
defaults the TTL was half the poll interval, so the bug was structurally
guaranteed for anyone running the backend outside Docker.

The deployed behaviour was always correct -- the compose file's values win
under `docker compose up`. The bug was in the *other* path the docstring
promises: running the backend directly, where the code defaults are what
you get.

This test pins the two lists together so they cannot drift apart again.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from app.config import Settings

COMPOSE_PATH = Path(__file__).resolve().parents[2] / "docker-compose.yml"

#: Inside the container the compose file is baked in beside the reference
#: client (the Dockerfile copies it to /app/reference/). On the host it sits
#: at the project root.
_IN_IMAGE = Path("/app/reference/docker-compose.yml")


def _compose_path() -> Path:
    if _IN_IMAGE.exists():
        return _IN_IMAGE
    return COMPOSE_PATH


def _compose_env() -> dict[str, str]:
    """The backend service's environment block, as a flat mapping.

    The file is parsed with a regex rather than a YAML parser on purpose:
    PyYAML is not a dependency of the backend image (see docs/SCENARIO.md
    for why), so the test must not need it either. The pattern is narrow
    enough to match only the backend's own `KEY: "value"` lines.
    """
    text = _compose_path().read_text(encoding="utf-8")

    # Isolate the backend service's block.
    match = re.search(r"^  backend:\n(.*?)(?=^  \S|\Z)", text, re.M | re.S)
    assert match is not None, "the compose file has no backend service"

    env: dict[str, str] = {}
    for key, value in re.findall(r'^\s+([A-Z][A-Z0-9_]*):\s*"([^"]*)"',
                                 match.group(1), re.M):
        env[key] = value
    return env


# The variables whose compose value and code default must agree. Every one
# of these is a value an operator reads in the docs' configuration table.
PINNED = (
    "SSE_STALE_AFTER",
    "POLL_INTERVAL",
    "STATE_MAX_AGE",
)


@pytest.mark.parametrize("name", PINNED)
def test_the_code_default_matches_the_compose_value(name: str) -> None:
    compose = _compose_env()
    assert name in compose, f"{name} is not set in docker-compose.yml"

    # from_env with the variable *unset* must fall back to the same number
    # the compose file supplies.
    previous = os.environ.pop(name, None)
    try:
        loaded = Settings.from_env()
    finally:
        if previous is not None:
            os.environ[name] = previous

    default = getattr(loaded, name.lower())
    assert float(default) == float(compose[name]), (
        f"{name}: config.py defaults to {default}, but docker-compose.yml "
        f"sets {compose[name]}. The two must agree -- see "
        f"docs/BACKEND.md §6."
    )


def test_the_dataclass_default_matches_the_compose_value() -> None:
    """`Settings(...)` built directly must agree with `from_env()` too.

    The tests construct `Settings(...)` by hand in a dozen places, so the
    dataclass field default is the one that actually reaches them.
    """
    compose = _compose_env()
    direct = Settings()
    for name in PINNED:
        assert float(getattr(direct, name.lower())) == float(compose[name]), (
            f"{name}: the dataclass default differs from the compose value"
        )


def test_the_snapshot_ttl_exceeds_the_poll_interval() -> None:
    """The invariant that the stale-banner bug was really about.

    The snapshot is only ever as fresh as the last poll, so a TTL at or
    below the poll interval guarantees that a perfectly healthy world
    reads as stale for a large part of every cycle.
    """
    settings = Settings.from_env()
    assert settings.state_max_age > settings.poll_interval, (
        "STATE_MAX_AGE must exceed POLL_INTERVAL, or a healthy world is "
        "marked stale for most of every poll cycle"
    )
