"""Unit tests for the multi-tick idempotency key.

Phase 1 minted keys against ``snap.tick``. Phase 2 plans across a horizon,
so a shipment may be created at a tick *later* than the snapshot's current
tick -- and the simulator stamps ``created_tick = now`` on every allocation,
so the key must carry that future tick.

The Phase 1 key format is frozen. These tests pin the additive behaviour
(``make_key_at``) and prove the namespace is shared, not forked.
"""

from __future__ import annotations

from app.keys import make_key, make_key_at, request_hash
from conftest import make_request, make_snapshot


# --------------------------------------------------------------------------- #
# The wrapper and the explicit-tick factory agree
# --------------------------------------------------------------------------- #
def test_make_key_is_make_key_at_the_snapshot_tick():
    snapshot = make_snapshot(tick=9)
    request = make_request(quantity=1000.0)
    assert make_key(snapshot, request, 1) == make_key_at(9, request, 1)


def test_make_key_at_embeds_the_explicit_tick():
    request = make_request(quantity=1000.0)
    key = make_key_at(12, request, 1)
    assert key.startswith("alloc-t12-")
    assert "station-mirpur" in key
    assert "diesel" in key


def test_make_key_at_uses_the_future_tick_not_the_snapshot_tick():
    snapshot = make_snapshot(tick=3)
    request = make_request(quantity=1000.0)
    key = make_key_at(7, request, 1)
    assert key.startswith("alloc-t7-")
    assert key != make_key(snapshot, request, 1)


# --------------------------------------------------------------------------- #
# Distinctness -- a collision would self-inflict IDEMPOTENCY_KEY_MISMATCH
# --------------------------------------------------------------------------- #
def test_two_ticks_produce_two_keys():
    request = make_request(quantity=1000.0)
    assert make_key_at(4, request, 1) != make_key_at(5, request, 1)


def test_two_sequences_produce_two_keys_in_the_same_tick():
    request = make_request(quantity=1000.0)
    assert make_key_at(4, request, 1) != make_key_at(4, request, 2)


def test_the_digest_is_shared_across_ticks():
    """Only the tick changes between two ticks -- the identity hash does not."""
    request = make_request(quantity=1000.0)
    assert request_hash(request) == request_hash(request)
    key_a = make_key_at(4, request, 1)
    key_b = make_key_at(5, request, 1)
    assert key_a.rsplit("-", 1)[-1] == key_b.rsplit("-", 1)[-1]


def test_different_quantities_produce_different_keys_in_the_same_tick():
    assert make_key_at(4, make_request(quantity=1000.0), 1) != make_key_at(
        4, make_request(quantity=2000.0), 1
    )


def test_different_routes_produce_different_keys_in_the_same_tick():
    """A reroute is a different request, so it must not collide."""
    request = make_request(quantity=1000.0)
    assert make_key_at(4, request, 1, route_id="route-gazipur-mirpur") != make_key_at(
        4, request, 1, route_id="route-patiya-mirpur"
    )


# --------------------------------------------------------------------------- #
# Stability -- a replay after a crash must reuse the key
# --------------------------------------------------------------------------- #
def test_the_key_is_stable_for_the_same_explicit_tick():
    request = make_request(quantity=1000.0)
    assert make_key_at(11, request, 2) == make_key_at(11, request, 2)


def test_the_key_is_independent_of_the_snapshot():
    """The key is a function of (tick, request) -- not of the world state."""
    request = make_request(quantity=1000.0)
    assert make_key_at(6, request, 1) == make_key_at(6, request, 1)
