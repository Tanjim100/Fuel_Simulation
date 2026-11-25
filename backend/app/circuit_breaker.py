"""Circuit breaker around the simulator client.

The backend talks to the simulator over HTTP, and the simulator can fail in
ways that are *not* transient. The client already retries (3 attempts, full
jitter, up to a 4s ceiling per attempt), which means a single logical call
can block for roughly 13.75 seconds. That retry budget is right for one
flaky request and catastrophic for a service: if the upstream is genuinely
down, *every* request pays the full budget, and the backend stops answering
altogether.

This module is the fix. It wraps the client -- it does not modify it -- and
turns "the upstream is down" from a 13.75s stall into a 1ms rejection.

**Why it wraps rather than edits.** ``backend/app/simulator_client.py`` must
stay byte-identical to ``verifier/simulator_client.py``; the drift guard in
``tests/test_client_parity.py`` enforces it with a SHA256 comparison. Editing
the client would mean editing the verifier's reference copy first, re-running
the 49-check suite, and re-copying -- for a change that has nothing to do
with the client itself. A wrapper is a better shape for the same reason: the
breaker is a *policy* layered over transport, and policy belongs outside.

**The classification is the load-bearing part.** Not every failure means the
upstream is unavailable:

- **Retryable** -- a timeout, an injected 503, a transport error. These mean
  "the simulator is not answering right now". The breaker counts them.
- **Terminal** -- a 404, a 409, a 422. These are *answers*. The simulator
  understood the request and said no. Tripping the breaker on a 409 would
  take the whole backend down because one station's tank was full -- which is
  exactly the failure mode the pre-flight validator exists to prevent.

So only retryable failures move the breaker. A business rejection leaves it
CLOSED.

**States.** ``CLOSED`` -> ``OPEN`` after N consecutive retryable failures.
``OPEN`` -> ``HALF_OPEN`` once the cooldown elapses. ``HALF_OPEN`` -> ``CLOSED``
on the first success, back to ``OPEN`` on the first failure.

**Degraded mode.** While the breaker is OPEN the read endpoints serve the
last-known-good snapshot with an explicit ``degraded: true`` flag rather than
failing. The *write* endpoints keep failing -- a plan computed against a stale
world is a wrong plan, and silently writing it would be worse than an error.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Optional, TypeVar

from .simulator_client import (
    SimulatorConflict,
    SimulatorError,
    SimulatorFaultInjected,
    SimulatorNotFound,
    SimulatorTimeout,
    SimulatorValidationError,
)

__all__ = [
    "BreakerState",
    "CircuitOpen",
    "CircuitBreaker",
    "BreakerStatus",
    "classify_failure",
]

LOGGER = logging.getLogger(__name__)

T = TypeVar("T")


class BreakerState(str, Enum):
    """The three states of the breaker."""

    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"

    @property
    def gauge_value(self) -> int:
        """The numeric value published as ``backend_circuit_state``.

        Prometheus gauges cannot carry a string, so the state is encoded as an
        integer. CLOSED is the healthy state and therefore 0 -- a dashboard
        reading "2" is unambiguously the broken one.
        """
        return {"CLOSED": 0, "HALF_OPEN": 1, "OPEN": 2}[self.value]


class CircuitOpen(SimulatorError):
    """The breaker is OPEN -- the call was rejected without touching the wire.

    A 503 with code ``CIRCUIT_OPEN``. It is deliberately the *same* HTTP
    status as an injected fault, because from the caller's point of view the
    two are the same situation: the upstream is not answering.
    """

    def __init__(self, message: str, *, retry_after: float = 1.0) -> None:
        super().__init__(message, status_code=503, code="CIRCUIT_OPEN")
        self.retry_after = retry_after


def classify_failure(exc: BaseException) -> str:
    """Classify an exception as ``retryable``, ``terminal`` or ``unknown``.

    Returns one of three strings rather than a bool because the *unknown*
    case has to be handled explicitly: an exception we have never seen is
    treated as retryable (it is more likely to be an outage than a business
    rejection), but it is counted separately so the metrics can show it.
    """
    if isinstance(exc, SimulatorTimeout):
        return "retryable"
    if isinstance(exc, SimulatorFaultInjected):
        return "retryable"
    if isinstance(exc, (SimulatorConflict, SimulatorNotFound, SimulatorValidationError)):
        return "terminal"
    if isinstance(exc, SimulatorError):
        # A bare SimulatorError with no status code is a transport error
        # (``_request`` raises it for httpx.HTTPError). One with a status
        # code is some other upstream error we do not model explicitly --
        # also worth retrying.
        return "retryable"
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError)):
        return "retryable"
    return "unknown"


@dataclass(frozen=True)
class BreakerStatus:
    """A snapshot of the breaker's state, for the health endpoint."""

    state: BreakerState
    consecutive_failures: int
    opened_at: Optional[float]
    cooldown_remaining: float
    total_failures: int
    total_rejections: int
    transitions: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "consecutive_failures": self.consecutive_failures,
            "opened_at": self.opened_at,
            "cooldown_remaining": round(self.cooldown_remaining, 3),
            "total_failures": self.total_failures,
            "total_rejections": self.total_rejections,
            "transitions": self.transitions,
        }


class CircuitBreaker:
    """An async circuit breaker that wraps one callable.

    The wrapped callable is the client's request method. The breaker is
    *not* a context manager and does not own the client's lifecycle -- it only
    decides whether a call is allowed to proceed.
    """

    def __init__(
        self,
        client: Any,
        *,
        failure_threshold: int = 5,
        cooldown_seconds: float = 15.0,
        enabled: bool = True,
        on_state_change: Optional[Callable[[BreakerState], None]] = None,
        on_call: Optional[Callable[[str, float], None]] = None,
    ) -> None:
        """
        :param client: the object whose attribute calls are protected. Every
            public method on it is wrapped, so the breaker is transparent.
        :param failure_threshold: consecutive retryable failures before the
            breaker opens.
        :param cooldown_seconds: how long the breaker stays OPEN before
            allowing a probe.
        :param enabled: when false the breaker passes everything through and
            only counts. Used by tests and by the "breaker off" escape hatch.
        :param on_state_change: called with the new state on every
            transition. The metrics layer uses it to count transitions.
        :param on_call: called with ``(outcome, duration_seconds)`` after
            every attempted call. The metrics layer uses it to record the
            upstream request counters and the latency histogram.
        """
        self._client = client
        self.failure_threshold = max(1, int(failure_threshold))
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self.enabled = bool(enabled)
        self._on_state_change = on_state_change
        self._on_call = on_call

        self._state = BreakerState.CLOSED
        self._consecutive_failures = 0
        self._opened_at: Optional[float] = None
        self._total_failures = 0
        self._total_rejections = 0
        self._transitions = 0
        # Guards the state transitions so two concurrent failures cannot both
        # trip the breaker, and so a half-open probe cannot be duplicated.
        self._lock = asyncio.Lock()

    # -- introspection ------------------------------------------------------ #
    @property
    def state(self) -> BreakerState:
        return self._state

    @property
    def client(self) -> Any:
        """The wrapped client. Callers that must bypass the breaker use this."""
        return self._client

    def status(self) -> BreakerStatus:
        """The breaker's current state, as a snapshot."""
        remaining = 0.0
        if self._state is BreakerState.OPEN and self._opened_at is not None:
            elapsed = time.monotonic() - self._opened_at
            remaining = max(0.0, self.cooldown_seconds - elapsed)
        return BreakerStatus(
            state=self._state,
            consecutive_failures=self._consecutive_failures,
            opened_at=self._opened_at,
            cooldown_remaining=remaining,
            total_failures=self._total_failures,
            total_rejections=self._total_rejections,
            transitions=self._transitions,
        )

    # -- the gate ----------------------------------------------------------- #
    def _allow(self) -> bool:
        """Whether a call may proceed right now.

        Synchronous by design: the decision needs no I/O, and making it
        synchronous means an OPEN breaker rejects in microseconds with no
        chance of awaiting a lock that another coroutine holds.
        """
        if not self.enabled:
            return True
        if self._state is BreakerState.CLOSED:
            return True
        if self._state is BreakerState.OPEN:
            if self._opened_at is None:
                return True
            if time.monotonic() - self._opened_at >= self.cooldown_seconds:
                # The cooldown elapsed. Move to HALF_OPEN *synchronously* --
                # this is the single point where the breaker probes, so
                # exactly one caller gets the probe.
                self._transition(BreakerState.HALF_OPEN)
                return True
            return False
        # HALF_OPEN: only one probe at a time. The lock around the call
        # enforces that; further callers wait for it and then see the result.
        return True

    def _transition(self, new_state: BreakerState) -> None:
        if new_state is self._state:
            return
        old = self._state
        self._state = new_state
        self._transitions += 1
        if new_state is BreakerState.OPEN:
            self._opened_at = time.monotonic()
        elif new_state is BreakerState.HALF_OPEN:
            # The probe window has its own timestamp so the OPEN state's
            # cooldown is not extended by the transition.
            self._opened_at = time.monotonic()
        else:  # CLOSED
            self._opened_at = None
            self._consecutive_failures = 0
        LOGGER.warning(
            "Circuit breaker %s -> %s (consecutive failures: %d)",
            old.value, new_state.value, self._consecutive_failures,
        )
        if self._on_state_change is not None:
            try:
                self._on_state_change(new_state)
            except Exception:  # pragma: no cover - a metric must never break a call
                LOGGER.exception("Breaker state-change callback failed")

    def _record_success(self) -> None:
        """Record a successful call.

        A success in the HALF_OPEN state is the probe succeeding, and it
        closes the breaker -- that is the whole recovery path. A success in
        the CLOSED state simply clears the failure streak.
        """
        self._consecutive_failures = 0
        if self._state is BreakerState.HALF_OPEN:
            self._transition(BreakerState.CLOSED)

    def _record_failure(self, kind: str) -> None:
        if kind == "terminal":
            # A business rejection says nothing about the upstream's health.
            # The breaker must not move.
            return
        if not self.enabled:
            # A disabled breaker counts but never transitions.
            self._total_failures += 1
            return
        self._total_failures += 1
        self._consecutive_failures += 1
        if self._state is BreakerState.HALF_OPEN:
            # The probe failed: straight back to OPEN.
            self._transition(BreakerState.OPEN)
            return
        if (
            self._state is BreakerState.CLOSED
            and self._consecutive_failures >= self.failure_threshold
        ):
            self._transition(BreakerState.OPEN)

    # -- the call ----------------------------------------------------------- #
    async def call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """Invoke a client method through the breaker.

        ``method`` is the *client's* method name (e.g. ``stations``), not an
        HTTP verb. The name is only used for logging.
        """
        if not self._allow():
            self._total_rejections += 1
            remaining = self.status().cooldown_remaining
            raise CircuitOpen(
                f"The simulator circuit breaker is OPEN; "
                f"'{method}' was rejected without calling the upstream "
                f"(retry in {remaining:.1f}s).",
                retry_after=max(0.1, remaining),
            )

        started = time.monotonic()
        outcome = "success"
        try:
            if self._state is BreakerState.HALF_OPEN:
                # Only one half-open probe at a time. Everyone else waits,
                # then sees the result of the probe.
                async with self._lock:
                    if self._state is not BreakerState.HALF_OPEN:
                        # The probe already ran while we waited.
                        self._total_rejections += 1
                        raise CircuitOpen(
                            f"The simulator circuit breaker is OPEN; "
                            f"'{method}' was rejected without calling the "
                            f"upstream.",
                            retry_after=max(
                                0.1, self.status().cooldown_remaining
                            ),
                        )
                    result = await self._invoke(method, *args, **kwargs)
                self._record_success()
                return result

            result = await self._invoke(method, *args, **kwargs)
            # A success in the CLOSED state clears the failure streak. This
            # is what makes the threshold a count of *consecutive* failures
            # rather than a count of failures ever seen -- an upstream that
            # fails once and then recovers must not be one failure away from
            # opening forever after.
            self._record_success()
            return result
        except CircuitOpen:
            outcome = "circuit_open"
            raise
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            kind = classify_failure(exc)
            outcome = _outcome_label(kind, exc)
            self._record_failure(kind)
            raise
        finally:
            if self._on_call is not None:
                try:
                    self._on_call(outcome, time.monotonic() - started)
                except Exception:  # pragma: no cover
                    LOGGER.exception("Breaker call callback failed")

    async def _invoke(self, method: str, *args: Any, **kwargs: Any) -> Any:
        target = getattr(self._client, method)
        result = target(*args, **kwargs)
        if asyncio.iscoroutine(result) or isinstance(result, Awaitable):
            return await result
        return result

    # -- proxying ----------------------------------------------------------- #
    def __getattr__(self, name: str) -> Any:
        """Route unknown attributes to the wrapped client.

        This is what makes the breaker a drop-in replacement: every public
        method on the client is reachable through the breaker, and every one
        of them is protected. ``__getattr__`` is only consulted for
        attributes the breaker itself does not define, so the breaker's own
        state is never shadowed.

        Only *callables* are wrapped. A plain attribute (``base_url``,
        ``timeout``) is forwarded as-is -- wrapping it would turn a string
        into a coroutine function, which is exactly the kind of subtle
        breakage that makes a proxy dangerous.
        """
        if name.startswith("_"):
            raise AttributeError(name)
        client = self.__dict__.get("_client")
        if client is None:  # pragma: no cover - only during construction
            raise AttributeError(name)

        attribute = getattr(client, name)
        if not callable(attribute):
            return attribute

        async def protected(*args: Any, **kwargs: Any) -> Any:
            return await self.call(name, *args, **kwargs)

        return protected


def _outcome_label(kind: str, exc: BaseException) -> str:
    """A short label for the upstream outcome, used as a metric label."""
    if kind == "terminal":
        if isinstance(exc, SimulatorConflict):
            return "conflict"
        if isinstance(exc, SimulatorNotFound):
            return "not_found"
        if isinstance(exc, SimulatorValidationError):
            return "validation_error"
        return "terminal"
    if kind == "retryable":
        if isinstance(exc, SimulatorTimeout):
            return "timeout"
        if isinstance(exc, SimulatorFaultInjected):
            return "fault_injected"
        if isinstance(exc, SimulatorError):
            return "transport_error"
        return "transport_error"
    return "unknown"
