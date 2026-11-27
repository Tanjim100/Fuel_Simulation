"""The autopilot's HTTP surface.

The API is how an operator turns the autopilot on, and how the console reads
what it is doing. Three things matter here:

- ``GET /api/autopilot`` reports the truth -- including that the autopilot
  is *off*, which is the shipped default.
- ``POST /api/autopilot/mode`` switches the mode, and refuses an unknown one
  with a 400 rather than silently clamping it.
- ``POST /api/autopilot/cycle`` reports what one cycle decided.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.autopilot import AutopilotController, AutopilotMode
from app.broker import EventBroker
from app.config import Settings
from app.journal import DispatchJournal
from app.main import create_app
from app.state import WorldStateCache
from conftest import make_snapshot
from test_optimizer_api import FakeUpstream


@pytest.fixture
def app_client(tmp_path):
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

    with TestClient(app) as client:
        client.upstream = upstream  # type: ignore[attr-defined]
        yield client


def test_the_autopilot_is_off_by_default(app_client):
    response = app_client.get("/api/autopilot")
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "off"
    assert body["enabled"] is False


def test_the_status_carries_the_fields_the_console_reads(app_client):
    body = app_client.get("/api/autopilot").json()
    for field in (
        "mode",
        "enabled",
        "running",
        "cycle",
        "last_decision",
        "last_created",
        "last_replayed",
        "last_failed",
        "last_litres",
        "last_escalated",
        "last_shipments",
    ):
        assert field in body, f"the status is missing {field!r}"


def test_the_mode_can_be_set_to_live(app_client):
    response = app_client.post("/api/autopilot/mode", json={"mode": "live"})
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "live"
    assert body["enabled"] is True

    # The change is real, not just echoed.
    assert app_client.get("/api/autopilot").json()["mode"] == "live"


def test_the_mode_can_be_set_back_to_off(app_client):
    app_client.post("/api/autopilot/mode", json={"mode": "live"})
    response = app_client.post("/api/autopilot/mode", json={"mode": "off"})
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "off"
    assert body["enabled"] is False


def test_an_unknown_mode_is_a_400_with_the_error_envelope(app_client):
    response = app_client.post("/api/autopilot/mode", json={"mode": "lve"})
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "INVALID_AUTOPILOT_MODE"
    assert "off, live" in body["error"]["message"]


def test_dry_run_is_no_longer_a_mode(app_client):
    # The rehearsal mode was removed. An old console still posting it gets a
    # clear error rather than a silent no-op.
    response = app_client.post("/api/autopilot/mode", json={"mode": "dry_run"})
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "INVALID_AUTOPILOT_MODE"


def test_a_missing_mode_is_a_400(app_client):
    response = app_client.post("/api/autopilot/mode", json={})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_AUTOPILOT_MODE"


def test_the_cycle_endpoint_reports_a_decision(app_client):
    app_client.post("/api/autopilot/mode", json={"mode": "live"})
    response = app_client.post("/api/autopilot/cycle")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "OK"
    assert body["decision"] in (
        "dispatched",
        "gated",
        "no_demand",
        "breaker_open",
        "quarantined",
        "tick_regressed",
        "error",
    )
    assert "autopilot" in body


def test_a_live_cycle_writes(app_client):
    upstream = app_client.upstream  # type: ignore[attr-defined]
    app_client.post("/api/autopilot/mode", json={"mode": "live"})
    app_client.post("/api/autopilot/cycle")
    assert upstream.created, "a live cycle must dispatch"


def test_the_root_lists_the_autopilot_endpoints(app_client):
    body = app_client.get("/").json()
    assert "GET  /api/autopilot" in body["endpoints"]
    assert "POST /api/autopilot/mode, /api/autopilot/cycle" in body["endpoints"]
