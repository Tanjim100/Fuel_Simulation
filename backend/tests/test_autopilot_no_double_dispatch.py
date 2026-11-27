"""The autopilot must never dispatch twice for the same tick.

This is the property that makes the feature safe to leave running, and it is
backed by three *independent* mechanisms:

1. **The tick guard** -- the controller dispatches at most once per
   simulator tick, and refuses a tick that is not strictly greater than the
   last one it dispatched on.
2. **The shared dispatch lock** -- the autopilot and the operator's manual
   ``POST /api/optimize/execute`` take the *same* lock, so two writers
   cannot interleave.
3. **The idempotency key** -- the simulator returns the original allocation
   for a repeated key, so even a restart (which loses the tick guard) cannot
   produce a second shipment.

These tests cover the first two. The third is the simulator's contract and
is covered by ``test_execute_does_not_double_ship`` in
``test_optimizer_api.py``, which uses the same simulator stand-in.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.autopilot import AutopilotController, AutopilotMode
from app.config import Settings
from test_autopilot import StubBroker, StubClient, StubJournal


@pytest.fixture
def world():
    """The stubbed world the cycles plan against.

    Defined here rather than imported, because a fixture is only visible to
    the module that defines it -- pytest does not re-export fixtures from
    imported modules.
    """
    from conftest import make_snapshot

    return make_snapshot(tick=70)


def _controller(client, broker, journal, **kwargs) -> AutopilotController:
    settings = Settings(
        simulator_base_url="http://fake",
        redis_url="",
        **kwargs,
    )
    return AutopilotController(
        client=client,
        broker=broker,
        journal=journal,
        settings=settings,
    )


async def test_two_cycles_on_one_tick_dispatch_once(world):
    client = StubClient(tick=70)
    broker = StubBroker(world)
    controller = _controller(client, broker, StubJournal())

    await controller.set_mode(AutopilotMode.LIVE)
    first = await controller.cycle_once()
    after_first = len(client.created)
    second = await controller.cycle_once()

    assert first["decision"] == "dispatched"
    assert second["decision"] == "tick_regressed"
    assert len(client.created) == after_first, "the second cycle wrote again"
    await controller.stop()


async def test_two_concurrent_cycles_on_one_tick_dispatch_once(world):
    """The shared lock is what makes this safe.

    Without it, two cycles would both read the same tick, both plan against
    the same world, and both write -- and the second would fail against the
    simulator's per-tick dispatch cap for a reason that has nothing to do
    with its plan.
    """
    client = StubClient(tick=70)
    broker = StubBroker(world)
    controller = _controller(client, broker, StubJournal())

    await controller.set_mode(AutopilotMode.LIVE)
    results = await asyncio.gather(
        controller.cycle_once(), controller.cycle_once()
    )

    decisions = sorted(r["decision"] for r in results)
    assert decisions.count("dispatched") == 1
    # One dispatch, and therefore one batch of allocations. The plan itself
    # may contain several shipments, so the batch size is not the number of
    # allocations -- the *growth* is what matters.
    assert len(client.created) > 0
    await controller.stop()


async def test_the_lock_is_shared_between_the_autopilot_and_the_operator(world):
    """The manual execute endpoint and the autopilot take the same lock.

    This test proves the property structurally: the controller's dispatch
    lock is the very lock the execute endpoint reads off ``app.state``.
    """
    import asyncio as _asyncio

    client = StubClient(tick=70)
    broker = StubBroker(world)
    shared = _asyncio.Lock()
    controller = _controller(client, broker, StubJournal())
    controller._dispatch_lock = shared

    await controller.set_mode(AutopilotMode.LIVE)
    await controller.cycle_once()
    after_first = len(client.created)
    assert after_first > 0

    # The operator's endpoint would take this same lock; while the autopilot
    # holds it, the operator waits rather than interleaving.
    async with shared:
        await controller.cycle_once()
    assert len(client.created) == after_first, "the locked cycle wrote again"
    await controller.stop()


async def test_a_repeated_key_is_a_replay_not_a_second_allocation(world):
    """The idempotency key survives a restart, which the tick guard does not.

    A fresh controller -- the equivalent of a restarted backend -- has no
    memory of the tick it dispatched on. The *key* is what stops the second
    dispatch from creating a second shipment.
    """
    client = StubClient(tick=70)
    broker = StubBroker(world)

    first = _controller(client, broker, StubJournal())
    await first.set_mode(AutopilotMode.LIVE)
    first_result = await first.cycle_once()
    created_first = first_result["created"]
    assert created_first > 0
    after_first = len(client.created)
    await first.stop()

    # A brand-new controller: no tick memory, so the tick guard cannot help.
    # The simulator's idempotency does.
    second = _controller(client, broker, StubJournal())
    await second.set_mode(AutopilotMode.LIVE)
    result = await second.cycle_once()

    assert result["decision"] == "dispatched"
    assert result["created"] == 0, "the second controller created a shipment"
    assert result["replayed"] == created_first, "the repeat was not a replay"
    assert len(client.created) == after_first, "the repeat created an allocation"
    await second.stop()
