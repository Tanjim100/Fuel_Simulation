"""Deterministic idempotency keys.

A key must satisfy two properties:

1. **Stable across retries.** The same logical request replayed after a crash
   must produce the *same* key, so the simulator treats it as an idempotent
   replay rather than a second shipment.
2. **Distinct for distinct requests.** Two genuinely different dispatches must
   never collide, because a collision makes the simulator answer
   ``409 IDEMPOTENCY_KEY_MISMATCH`` -- a rejection we caused ourselves.

The key format is::

    alloc-t{tick}-{depot}-{station}-{fuel}-{seq}-{hash8}

* ``tick`` -- the key is tick-scoped because a cancelled key stays
  *permanently occupied* in the simulator. Reusing a key in a later tick would
  trip ``IDEMPOTENCY_KEY_MISMATCH``.
* ``seq`` -- the chunk sequence, so a split into N shipments yields N distinct
  keys.
* ``hash8`` -- the first 8 hex characters of the SHA-256 of the canonical JSON
  of the request's identity fields (including ``route_id`` and ``quantity``).
  This is what guarantees property 2: a different request produces a different
  hash even if the human-readable prefix collides.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .models import PlanRequest
from .state import WorldSnapshot

__all__ = ["identity_fields", "request_hash", "make_key", "make_key_at"]


def identity_fields(req: PlanRequest, route_id: str | None = None) -> dict[str, Any]:
    """The canonical identity of a dispatch request."""
    return {
        "source_depot_id": req.source_depot_id,
        "destination_station_id": req.destination_station_id,
        "fuel_type": req.fuel_type,
        "quantity": round(float(req.quantity), 3),
        "route_id": route_id if route_id is not None else req.route_id,
    }


def request_hash(req: PlanRequest, route_id: str | None = None) -> str:
    """Stable 8-hex-character fingerprint of a request's identity."""
    payload = json.dumps(
        identity_fields(req, route_id),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]


def make_key_at(
    tick: int,
    req: PlanRequest,
    sequence: int,
    *,
    route_id: str | None = None,
) -> str:
    """Build the idempotency key for one shipment of a plan.

    ``tick`` is the tick at which the shipment is **created** -- which, for a
    multi-tick plan, is not necessarily the snapshot's current tick. The
    simulator stamps ``created_tick = now`` on every allocation, so a key
    minted for a future tick must carry that future tick.

    The rest of the format is byte-identical to :func:`make_key`, so a
    single-tick plan and a multi-tick plan share one key namespace.
    """
    digest = request_hash(req, route_id)
    return (
        f"alloc-t{tick}"
        f"-{req.source_depot_id}"
        f"-{req.destination_station_id}"
        f"-{req.fuel_type.lower()}"
        f"-{sequence}"
        f"-{digest}"
    )


def make_key(snap: WorldSnapshot, req: PlanRequest, sequence: int) -> str:
    """Build the idempotency key for one shipment of a plan.

    ``sequence`` is 1-based and identifies the chunk within a split plan.
    """
    return make_key_at(snap.tick, req, sequence)
