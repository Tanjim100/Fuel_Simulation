"""Degraded mode: serving the last known good world when the upstream is gone.

PDF §12 lists "retry / cached state / degraded mode" as the required response
to an unavailable backend dependency. This module is the shared implementation
of the *cached state* and *degraded mode* halves, so every read endpoint
behaves the same way.

**The contract, in two halves.**

*Reads* keep answering. They serve the last known good world and label it
honestly with ``degraded: true`` and a ``degraded_reason``. A 502 would tell
an operator nothing; a stale-but-labelled world tells them the world they are
looking at is the last one the system managed to read.

*Writes* keep failing. A plan computed against a stale world is a wrong
plan, and silently writing it would be worse than an error. The write paths
let :class:`CircuitOpen` propagate; the global error handler renders it as
``503 CIRCUIT_OPEN`` with the canonical envelope.

**Why the helpers live here and not in a router.** Four routers serve reads
(``state``, ``forecast``, ``anomaly``, and the observability mirror). If each
one grew its own copy of "is the breaker open, and what do I say about it",
they would drift -- and a drift here is not cosmetic: it means one endpoint
serving a stale world *without saying so*. One implementation, one contract.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import Request

from .circuit_breaker import BreakerState, CircuitOpen
from .metrics import FALLBACK_ACTIVATIONS

__all__ = [
    "degraded_reason",
    "degraded_fields",
    "read_snapshot",
]


def degraded_reason(request: Request) -> Optional[str]:
    """Why the read paths are serving stale data, if they are.

    Two situations produce a degraded read:

    * the circuit breaker is OPEN -- the upstream is not answering, and the
      cached snapshot is the last known good world;
    * the breaker is CLOSED but the last refresh failed -- the upstream is
      refusing, and the cached snapshot is still the best we have.

    Either way the answer is the same: serve the cached snapshot and say
    plainly that it is cached.
    """
    breaker = getattr(request.app.state, "breaker", None)
    if breaker is not None and breaker.status().state is BreakerState.OPEN:
        return (
            "The simulator circuit breaker is OPEN; this is the last known "
            "good world snapshot."
        )
    world = getattr(request.app.state, "world", None)
    if world is not None and world.last_error:
        return (
            "The simulator could not be reached; this is the last known good "
            "world snapshot."
        )
    return None


def degraded_fields(request: Request) -> dict[str, Any]:
    """The ``degraded`` / ``degraded_reason`` fields every read carries.

    An empty dict when the system is healthy, so a caller can spread it
    unconditionally: ``{**payload, **degraded_fields(request)}``.
    """
    reason = degraded_reason(request)
    if reason is None:
        return {}
    FALLBACK_ACTIVATIONS.inc(kind="degraded_mode")
    return {"degraded": True, "degraded_reason": reason}


async def read_snapshot(request: Request):
    """The current snapshot for a *read*, refreshed if it has aged out.

    When the breaker is OPEN the refresh is skipped entirely: the refresh
    would fail immediately (that is what the breaker is for), and a failed
    refresh would replace the good snapshot with nothing.

    **The exception is the cooldown.** Once the cooldown has elapsed the
    breaker is *willing* to probe, and a read is a perfectly good probe --
    the world it wants is exactly what the refresh would fetch. So a read
    after the cooldown attempts the refresh; the breaker's own gate decides
    whether it goes through, and a successful probe closes the breaker.

    That is what makes recovery automatic rather than dependent on the
    background poller happening to tick at the right moment.
    """
    broker = request.app.state.broker
    world = request.app.state.world
    breaker = getattr(request.app.state, "breaker", None)
    if breaker is not None:
        status = breaker.status()
        if status.state is BreakerState.OPEN and status.cooldown_remaining > 0:
            # The cooldown has not elapsed: the breaker would reject the
            # refresh anyway, and attempting it would only spend time.
            return world.current()
    if world.is_stale():
        try:
            await broker.refresh_snapshot()
        except CircuitOpen:
            # The breaker rejected the probe. The cached world stands.
            pass
    return world.current()
