"""The dead-letter queue's contract.

The queue exists for one reason: **a malformed payload must not kill the
thing that was carrying it**. An event feed that aborts on one bad frame is
an event feed that stops delivering events -- and the operator finds out
from a console that has quietly stopped updating.

These tests pin:

* a malformed SSE frame is **quarantined**, not raised;
* the **subscription survives it** -- the next good frame still arrives;
* an entry can be **replayed**, and a successful replay removes it;
* the queue is **bounded** -- oldest entries are evicted first;
* the queue **survives a restart** (the SQLite file is the source of truth).
"""

from __future__ import annotations

import gc
import json
import warnings
from typing import Any

import pytest

from app.broker import EventBroker
from app.config import Settings
from app.dlq import DeadLetterQueue


@pytest.fixture
def queue(tmp_path):
    dlq = DeadLetterQueue(tmp_path / "dlq.sqlite3", max_entries=5)
    dlq.connect()
    yield dlq
    dlq.close()


@pytest.fixture
def broker(queue):
    settings = Settings(
        simulator_base_url="http://fake",
        redis_url="",
        poll_interval=60.0,
        watchdog_interval=60.0,
        state_max_age=0.0,
    )
    broker = EventBroker(
        _NullUpstream(), _NullState(), settings, dlq=queue
    )
    return broker


class _NullState:
    """A world cache that never has to do anything."""

    def current(self, **_: Any):
        return None

    def is_stale(self, **_: Any) -> bool:
        return False

    async def replace(self, snapshot):
        return snapshot

    async def mark_error(self, error: str) -> None:
        return None


class _NullUpstream:
    async def instance(self):
        return None


# --------------------------------------------------------------------------- #
# The queue itself
# --------------------------------------------------------------------------- #
def test_an_entry_is_stored_and_read_back(queue) -> None:
    entry = queue.put(
        "{not json", reason="malformed_frame", source="sse:tick"
    )
    assert entry.id
    assert entry.reason == "malformed_frame"
    assert entry.source == "sse:tick"

    stored = queue.get(entry.id)
    assert stored is not None
    assert stored.payload == "{not json"
    assert queue.count == 1


def test_the_entries_are_newest_first(queue) -> None:
    first = queue.put("one", reason="malformed_frame", source="sse:a")
    second = queue.put("two", reason="malformed_frame", source="sse:b")
    entries = queue.entries()
    assert entries[0].id == second.id
    assert entries[1].id == first.id


def test_the_queue_is_bounded(queue) -> None:
    """Oldest entries are evicted first.

    A dead-letter queue that grows forever is a sign that something is
    systematically broken, which is a page, not a queue.
    """
    ids = [queue.put(f"payload-{i}", reason="malformed_frame").id for i in range(9)]
    assert queue.count == 5
    remaining = {entry.id for entry in queue.entries(limit=100)}
    assert remaining == set(ids[4:])


def test_an_unknown_reason_becomes_unknown(queue) -> None:
    """The reason label has a bounded cardinality."""
    entry = queue.put("x", reason="something-nobody-defined")
    assert entry.reason == "unknown"


def test_a_non_string_payload_is_rendered_as_text(queue) -> None:
    entry = queue.put({"a": 1}, reason="schema_mismatch")
    assert json.loads(entry.payload) == {"a": 1}


def test_a_resolve_removes_the_entry(queue) -> None:
    entry = queue.put("x", reason="malformed_frame")
    assert queue.resolve(entry.id, success=True) is True
    assert queue.get(entry.id) is None
    assert queue.count == 0


def test_a_failed_resolve_keeps_the_entry_and_counts_the_attempt(queue) -> None:
    """A payload that still has not been handled must stay in the queue."""
    entry = queue.put("x", reason="malformed_frame")
    assert queue.resolve(entry.id, success=False) is True
    stored = queue.get(entry.id)
    assert stored is not None
    assert stored.attempts == 1


def test_the_queue_survives_a_restart(tmp_path) -> None:
    """The SQLite file is the source of truth.

    A queue that evaporates on restart is a log, not a queue.
    """
    path = tmp_path / "dlq.sqlite3"
    first = DeadLetterQueue(path, max_entries=10)
    first.connect()
    entry = first.put("survive me", reason="malformed_frame")
    first.close()

    second = DeadLetterQueue(path, max_entries=10)
    second.connect()
    stored = second.get(entry.id)
    assert stored is not None
    assert stored.payload == "survive me"
    assert second.count == 1
    second.close()


# --------------------------------------------------------------------------- #
# The broker's use of it
# --------------------------------------------------------------------------- #
async def test_a_malformed_frame_is_dead_lettered_not_raised(broker, queue) -> None:
    """The whole point: a bad frame must not propagate."""
    await broker._handle_sse_event("tick", "{this is not json")

    assert queue.count == 1
    entry = queue.entries()[0]
    assert entry.reason == "malformed_frame"
    assert entry.source == "sse:tick"
    assert entry.payload == "{this is not json"


async def test_the_subscription_survives_a_malformed_frame(broker, queue) -> None:
    """A good frame after a bad one still arrives.

    This is the property that separates a dead-letter queue from a crash.
    """
    await broker._handle_sse_event("tick", "{broken")
    await broker._handle_sse_event("tick", json.dumps({"tick": 42}))

    assert queue.count == 1, "the bad frame was quarantined"
    assert broker.status.events_received == 1, (
        "the good frame was still received -- the feed did not die"
    )
    recent = broker.recent_events(limit=10)
    assert len(recent) == 1
    assert recent[0].tick == 42


async def test_a_non_object_payload_is_dead_lettered(broker, queue) -> None:
    """Valid JSON that is not an object is still unusable."""
    await broker._handle_sse_event("tick", json.dumps([1, 2, 3]))
    assert queue.count == 1
    assert queue.entries()[0].reason == "unparseable_payload"


async def test_a_valid_frame_is_not_dead_lettered(broker, queue) -> None:
    await broker._handle_sse_event("tick", json.dumps({"tick": 7}))
    assert queue.count == 0


async def test_a_replay_succeeds_for_a_now_valid_payload(broker, queue) -> None:
    """A replay hands the payload back to the handler that produced it."""
    await broker._handle_sse_event("tick", "{broken")
    assert queue.count == 1
    entry = queue.entries()[0]

    # The operator fixes the source and the entry is edited to the good
    # payload before the replay.
    with queue._lock:
        queue._db().execute(
            "UPDATE dead_letters SET payload = ? WHERE id = ?",
            (json.dumps({"tick": 99}), entry.id),
        )
        queue._connection.commit()

    await broker.replay_dead_letter(queue.get(entry.id))

    assert queue.resolve(entry.id, success=True) is True
    assert queue.count == 0
    assert broker.status.events_received == 1
    assert broker.recent_events(limit=10)[0].tick == 99


async def test_a_replay_of_a_still_broken_payload_keeps_the_entry(
    broker, queue
) -> None:
    """A payload that is still malformed has not been handled.

    So it stays in the queue, with its attempt counter incremented. A replay
    that *removes* a still-broken payload would be a queue that silently
    loses data.
    """
    await broker._handle_sse_event("tick", "{still broken")
    entry = queue.entries()[0]

    await broker.replay_dead_letter(entry)

    # The replay re-dead-lettered it, so there are now two entries.
    assert queue.count == 2
    assert queue.resolve(entry.id, success=False) is True


async def test_the_broker_survives_a_malformed_frame_without_a_dlq() -> None:
    """The queue is optional; the broker must still not crash."""
    settings = Settings(
        simulator_base_url="http://fake", redis_url="",
        poll_interval=60.0, watchdog_interval=60.0,
    )
    broker = EventBroker(_NullUpstream(), _NullState(), settings, dlq=None)
    await broker._handle_sse_event("tick", "{broken")
    # No exception, and the broker is still usable.
    await broker._handle_sse_event("tick", json.dumps({"tick": 5}))
    assert broker.status.events_received == 1


async def test_a_missing_client_method_strands_no_coroutine() -> None:
    """A failing attribute lookup must not abandon the other reads.

    The eight reads are gathered together, and their client methods are
    looked up *before* the gather. If one lookup raises, the coroutines
    already built would never be awaited -- a leaked coroutine and a
    RuntimeWarning at GC time.

    The attribute lookups now happen before any coroutine is constructed,
    so a failing lookup cannot strand anything.
    """
    settings = Settings(
        simulator_base_url="http://fake", redis_url="",
        poll_interval=60.0, watchdog_interval=60.0,
    )
    broker = EventBroker(_NullUpstream(), _NullState(), settings, dlq=None)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(AttributeError):
            await broker._fetch_snapshot()
        # Force the GC that would report the dangling coroutine.
        gc.collect()

    unawaited = [
        w for w in caught
        if issubclass(w.category, RuntimeWarning)
        and "never awaited" in str(w.message)
    ]
    assert unawaited == [], (
        "a read coroutine was stranded: "
        f"{[str(w.message) for w in unawaited]}"
    )
