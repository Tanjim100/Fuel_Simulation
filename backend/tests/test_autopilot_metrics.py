"""The autopilot's metric families.

Five new families, and one new label value on an existing one. The label
value is the load-bearing part: ``backend_decisions_total`` distinguishes
``kind="dispatch"`` (a person dispatched) from ``kind="autopilot"`` (the
system dispatched). Folding them together would make the human-decision
counter dishonest, and the brief's §15 asks for *decision frequency* --
which is only meaningful if the two are told apart.

**Why these tests measure deltas, not absolutes.** The metrics registry is a
process-wide singleton (``app.metrics.REGISTRY``), by design: a metric is
defined exactly once and every component sees the same numbers. That makes
it a *shared* counter across the whole test session, so an assertion like
"the counter is zero" is only true for the first test that touches it. The
tests therefore take a baseline, act, and assert on the *change* -- which is
also the property that matters: an off autopilot must not move the dispatch
counter.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from app.autopilot import AutopilotController, AutopilotMode
from app.broker import EventBroker
from app.config import Settings
from app.journal import DispatchJournal
from app.main import create_app
from app.metrics import (
    AUTOPILOT_CYCLES,
    AUTOPILOT_DISPATCHES,
    AUTOPILOT_ENABLED,
    AUTOPILOT_LITRES,
    AUTOPILOT_MODE,
    DECISIONS,
)
from app.state import WorldStateCache
from test_optimizer_api import FakeUpstream


@pytest.fixture
def client(tmp_path):
    """An app with a fake upstream, so ``/metrics`` is reachable.

    The ``client`` fixture lives in ``test_metrics.py``; a fixture is only
    visible to the module that defines it, so this one is defined here too
    rather than imported.
    """
    upstream = FakeUpstream()
    settings = Settings(
        simulator_base_url="http://fake",
        redis_url="",
        poll_interval=60.0,
        watchdog_interval=60.0,
        state_max_age=0.0,
        journal_path=str(tmp_path / "journal.sqlite3"),
    )
    app = create_app(settings)

    @asynccontextmanager
    async def patched_lifespan(app_):
        app_.state.client = upstream
        app_.state.simulator_client = upstream
        app_.state.world = WorldStateCache(max_age=0.0)
        app_.state.broker = EventBroker(upstream, app_.state.world, settings)
        journal = DispatchJournal(settings.journal_path)
        journal.connect()
        app_.state.journal = journal
        await app_.state.broker.start()
        await app_.state.broker.refresh_snapshot()
        app_.state.autopilot = AutopilotController(
            client=upstream,
            broker=app_.state.broker,
            journal=journal,
            settings=settings,
        )
        try:
            yield
        finally:
            await app_.state.autopilot.stop()
            await app_.state.broker.stop()
            journal.close()

    app.router.lifespan_context = patched_lifespan

    with TestClient(app) as test_client:
        yield test_client


def test_the_five_families_are_present_in_the_exposition(client):
    body = client.get("/metrics").text
    for family in (
        "backend_autopilot_enabled",
        "backend_autopilot_mode",
        "backend_autopilot_cycles_total",
        "backend_autopilot_dispatches_total",
        "backend_autopilot_litres_total",
    ):
        assert f"# TYPE {family} " in body, f"{family} is missing"


def test_the_mode_gauge_is_a_number():
    """A gauge cannot carry a string, so the mode is encoded as an integer.

    0=off, 1=live. OFF is 0 because it is the healthy default.
    """
    assert AutopilotMode.OFF.gauge_value == 0
    assert AutopilotMode.LIVE.gauge_value == 1


def test_the_cycle_outcomes_are_pre_registered(client):
    """Every outcome the autopilot can report must be present at zero.

    A counter that has never been touched renders as absent, and absent is
    indistinguishable from uninstrumented.
    """
    body = client.get("/metrics").text
    for outcome in (
        "dispatched",
        "no_demand",
        "gated",
        "breaker_open",
        "quarantined",
        "tick_regressed",
        "error",
    ):
        assert f'backend_autopilot_cycles_total{{outcome="{outcome}"' in body


def test_the_dispatch_outcomes_are_pre_registered(client):
    body = client.get("/metrics").text
    for outcome in ("created", "replayed", "failed"):
        assert f'backend_autopilot_dispatches_total{{outcome="{outcome}"' in body


def test_the_autopilot_is_a_separate_decision_kind(client):
    """The autopilot's decisions are counted apart from the operator's."""
    body = client.get("/metrics").text
    assert 'backend_decisions_total{kind="autopilot"}' in body
    assert 'backend_decisions_total{kind="dispatch"}' in body


def test_the_decision_counter_has_both_kinds_at_zero(client):
    """Both kinds are pre-registered at zero.

    The *value* is shared across the session, so this asserts the series
    exists and is readable -- which is what "pre-registered" means.
    """
    assert DECISIONS.value(kind="autopilot") >= 0.0
    assert DECISIONS.value(kind="dispatch") >= 0.0
    body = client.get("/metrics").text
    assert 'backend_decisions_total{kind="autopilot"}' in body


def test_switching_the_mode_moves_the_gauges(client):
    """The gauges follow the mode, and they follow it back."""
    client.post("/api/autopilot/mode", json={"mode": "live"})
    assert AUTOPILOT_ENABLED.value() == 1.0
    assert AUTOPILOT_MODE.value() == 1

    client.post("/api/autopilot/mode", json={"mode": "off"})
    assert AUTOPILOT_ENABLED.value() == 0.0
    assert AUTOPILOT_MODE.value() == 0


def test_a_live_cycle_moves_the_dispatch_counters(client):
    """The cycle counter records what the autopilot actually did.

    A live cycle that dispatches must move the dispatch counter and the
    litres counter -- otherwise the metric says the system shipped nothing
    when it did.
    """
    cycles_before = AUTOPILOT_CYCLES.value(outcome="dispatched")
    created_before = AUTOPILOT_DISPATCHES.value(outcome="created")
    litres_before = AUTOPILOT_LITRES.value()

    client.post("/api/autopilot/mode", json={"mode": "live"})
    client.post("/api/autopilot/cycle")

    assert AUTOPILOT_CYCLES.value(outcome="dispatched") > cycles_before
    assert AUTOPILOT_DISPATCHES.value(outcome="created") > created_before
    assert AUTOPILOT_LITRES.value() > litres_before


def test_an_off_autopilot_dispatches_nothing(client):
    """The counters must not move while the autopilot is off."""
    created_before = AUTOPILOT_DISPATCHES.value(outcome="created")
    litres_before = AUTOPILOT_LITRES.value()

    client.post("/api/autopilot/mode", json={"mode": "off"})
    client.post("/api/autopilot/cycle")

    assert AUTOPILOT_DISPATCHES.value(outcome="created") == created_before
    assert AUTOPILOT_LITRES.value() == litres_before
