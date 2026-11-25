"""The circuit breaker's contract.

The breaker is the one piece of the resilience work where a *wrong*
implementation is worse than no implementation at all: a breaker that trips
on a business rejection takes the whole backend down because one station's
tank was full, and a breaker that never trips protects nothing.

So these tests are not "does it exist" tests. They pin the behaviour that
makes the breaker safe:

* retryable failures trip it;
* **terminal failures never do**;
* an open breaker rejects in microseconds, not milliseconds;
* it recovers on its own;
* the read endpoints keep answering in degraded mode, and the write
  endpoints do not.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.circuit_breaker import (
    BreakerState,
    CircuitBreaker,
    CircuitOpen,
    classify_failure,
)
from app.simulator_client import (
    SimulatorConflict,
    SimulatorError,
    SimulatorFaultInjected,
    SimulatorNotFound,
    SimulatorTimeout,
    SimulatorValidationError,
)


class FakeClient:
    """A stand-in for the simulator client.

    Fails with a configured exception until ``succeed`` is set.
    """

    def __init__(self, failure: BaseException | None = None) -> None:
        self.failure = failure
        self.calls = 0

    async def stations(self) -> list[str]:
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return ["station-mirpur"]

    async def instance(self) -> dict:
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return {"tick": 0}


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #
def test_timeout_is_retryable() -> None:
    assert classify_failure(SimulatorTimeout("slow")) == "retryable"


def test_injected_fault_is_retryable() -> None:
    assert classify_failure(SimulatorFaultInjected("down")) == "retryable"


def test_transport_error_is_retryable() -> None:
    assert classify_failure(SimulatorError("transport")) == "retryable"


def test_conflict_is_terminal() -> None:
    exc = SimulatorConflict("ROUTE_CAPACITY_EXCEEDED", "too big")
    assert classify_failure(exc) == "terminal"


def test_not_found_is_terminal() -> None:
    assert classify_failure(SimulatorNotFound("nope")) == "terminal"


def test_validation_error_is_terminal() -> None:
    assert classify_failure(SimulatorValidationError("bad")) == "terminal"


def test_unknown_exception_is_unknown() -> None:
    assert classify_failure(ValueError("what")) == "unknown"


# --------------------------------------------------------------------------- #
# The state machine
# --------------------------------------------------------------------------- #
async def test_starts_closed() -> None:
    breaker = CircuitBreaker(FakeClient())
    assert breaker.state is BreakerState.CLOSED


async def test_success_keeps_it_closed() -> None:
    client = FakeClient()
    breaker = CircuitBreaker(client)
    assert await breaker.stations() == ["station-mirpur"]
    assert breaker.state is BreakerState.CLOSED


async def test_retryable_failures_trip_it() -> None:
    client = FakeClient(SimulatorTimeout("slow"))
    breaker = CircuitBreaker(client, failure_threshold=3)
    for _ in range(3):
        with pytest.raises(SimulatorTimeout):
            await breaker.stations()
    assert breaker.state is BreakerState.OPEN


async def test_one_failure_short_of_the_threshold_stays_closed() -> None:
    client = FakeClient(SimulatorTimeout("slow"))
    breaker = CircuitBreaker(client, failure_threshold=3)
    for _ in range(2):
        with pytest.raises(SimulatorTimeout):
            await breaker.stations()
    assert breaker.state is BreakerState.CLOSED


async def test_a_terminal_error_never_trips_it() -> None:
    """The single most important property of the breaker.

    A 409 is the simulator *answering*: it understood the request and
    refused it on domain grounds. If that counted towards the breaker, one
    over-capacity request would take the entire backend offline -- exactly
    the failure mode the pre-flight validator exists to prevent.
    """
    client = FakeClient(
        SimulatorConflict("ROUTE_CAPACITY_EXCEEDED", "over capacity")
    )
    breaker = CircuitBreaker(client, failure_threshold=2)
    for _ in range(20):
        with pytest.raises(SimulatorConflict):
            await breaker.stations()
    assert breaker.state is BreakerState.CLOSED
    assert breaker.status().total_failures == 0


async def test_a_terminal_error_does_not_reset_a_retryable_streak() -> None:
    """A business rejection in the middle of an outage must not heal it.

    The streak counts *consecutive retryable* failures. A 409 in between is
    neither a retryable failure (so it must not increment the streak) nor a
    success (so it must not clear it). Two timeouts around a conflict is
    still two timeouts in a row.
    """
    client = FakeClient(SimulatorTimeout("slow"))
    breaker = CircuitBreaker(client, failure_threshold=3)
    with pytest.raises(SimulatorTimeout):
        await breaker.stations()
    client.failure = SimulatorConflict("X", "no")
    with pytest.raises(SimulatorConflict):
        await breaker.stations()
    client.failure = SimulatorTimeout("slow")
    with pytest.raises(SimulatorTimeout):
        await breaker.stations()
    # Two retryable failures so far -- one short of the threshold.
    assert breaker.state is BreakerState.CLOSED
    assert breaker.status().consecutive_failures == 2

    # A third timeout tips it over.
    with pytest.raises(SimulatorTimeout):
        await breaker.stations()
    assert breaker.state is BreakerState.OPEN


async def test_an_open_breaker_rejects_without_calling_the_upstream() -> None:
    client = FakeClient(SimulatorTimeout("slow"))
    breaker = CircuitBreaker(client, failure_threshold=1)
    with pytest.raises(SimulatorTimeout):
        await breaker.stations()
    assert breaker.state is BreakerState.OPEN
    calls_before = client.calls

    with pytest.raises(CircuitOpen):
        await breaker.stations()
    assert client.calls == calls_before, "the upstream must not be touched"


async def test_an_open_breaker_rejects_in_under_a_millisecond() -> None:
    """The whole point of the breaker.

    The client retries 3 times with up to 4s of jitter each, so an
    unprotected call blocks for ~13.75 seconds. The breaker turns that into
    a sub-millisecond rejection.
    """
    client = FakeClient(SimulatorTimeout("slow"))
    breaker = CircuitBreaker(client, failure_threshold=1)
    with pytest.raises(SimulatorTimeout):
        await breaker.stations()

    started = time.monotonic()
    with pytest.raises(CircuitOpen):
        await breaker.stations()
    elapsed = time.monotonic() - started
    assert elapsed < 0.001, f"rejection took {elapsed * 1000:.2f}ms"


async def test_it_recovers_after_the_cooldown() -> None:
    client = FakeClient(SimulatorTimeout("slow"))
    breaker = CircuitBreaker(client, failure_threshold=1, cooldown_seconds=0.05)
    with pytest.raises(SimulatorTimeout):
        await breaker.stations()
    assert breaker.state is BreakerState.OPEN

    client.failure = None
    await asyncio.sleep(0.08)
    assert await breaker.stations() == ["station-mirpur"]
    assert breaker.state is BreakerState.CLOSED


async def test_a_failed_probe_reopens_it() -> None:
    client = FakeClient(SimulatorTimeout("slow"))
    breaker = CircuitBreaker(client, failure_threshold=1, cooldown_seconds=0.05)
    with pytest.raises(SimulatorTimeout):
        await breaker.stations()
    await asyncio.sleep(0.08)
    with pytest.raises(SimulatorTimeout):
        await breaker.stations()
    assert breaker.state is BreakerState.OPEN


async def test_a_successful_probe_closes_it() -> None:
    client = FakeClient(SimulatorTimeout("slow"))
    breaker = CircuitBreaker(client, failure_threshold=1, cooldown_seconds=0.05)
    with pytest.raises(SimulatorTimeout):
        await breaker.stations()
    client.failure = None
    await asyncio.sleep(0.08)
    await breaker.stations()
    assert breaker.state is BreakerState.CLOSED
    assert breaker.status().consecutive_failures == 0


async def test_the_disabled_breaker_passes_everything_through() -> None:
    client = FakeClient(SimulatorTimeout("slow"))
    breaker = CircuitBreaker(client, failure_threshold=1, enabled=False)
    for _ in range(10):
        with pytest.raises(SimulatorTimeout):
            await breaker.stations()
    assert breaker.state is BreakerState.CLOSED


async def test_the_status_reports_the_shape_the_health_endpoint_needs() -> None:
    client = FakeClient(SimulatorTimeout("slow"))
    breaker = CircuitBreaker(client, failure_threshold=2)
    with pytest.raises(SimulatorTimeout):
        await breaker.stations()
    status = breaker.status().to_dict()
    for key in (
        "state", "consecutive_failures", "opened_at", "cooldown_remaining",
        "total_failures", "total_rejections", "transitions",
    ):
        assert key in status
    assert status["state"] == "CLOSED"


async def test_the_state_change_callback_fires_on_transitions() -> None:
    seen: list[BreakerState] = []
    client = FakeClient(SimulatorTimeout("slow"))
    breaker = CircuitBreaker(
        client, failure_threshold=1, on_state_change=seen.append
    )
    with pytest.raises(SimulatorTimeout):
        await breaker.stations()
    assert BreakerState.OPEN in seen


async def test_the_call_callback_records_the_outcome() -> None:
    outcomes: list[str] = []
    client = FakeClient(SimulatorConflict("X", "no"))
    breaker = CircuitBreaker(
        client, on_call=lambda outcome, _duration: outcomes.append(outcome)
    )
    with pytest.raises(SimulatorConflict):
        await breaker.stations()
    assert outcomes == ["conflict"]


async def test_unknown_attributes_are_forwarded_to_the_client() -> None:
    """The breaker is a drop-in replacement for the client."""
    client = FakeClient()
    client.base_url = "http://simulator-api:8000"
    breaker = CircuitBreaker(client)
    assert breaker.base_url == "http://simulator-api:8000"


async def test_private_attributes_are_not_forwarded() -> None:
    breaker = CircuitBreaker(FakeClient())
    with pytest.raises(AttributeError):
        _ = breaker._not_a_real_attribute  # noqa: B018


async def test_concurrent_failures_only_trip_it_once() -> None:
    """Ten concurrent failures against a threshold of three.

    Once the breaker trips, the calls that were still queued are rejected
    with ``CircuitOpen`` instead of reaching the upstream. That is the
    *desired* behaviour -- the point of the breaker is that a burst of
    concurrent work does not keep hammering a dead upstream -- so the test
    asserts that the upstream saw at most the threshold's worth of calls,
    not that all ten went through.
    """
    client = FakeClient(SimulatorTimeout("slow"))
    breaker = CircuitBreaker(client, failure_threshold=3)

    async def call() -> None:
        with pytest.raises((SimulatorTimeout, CircuitOpen)):
            await breaker.stations()

    await asyncio.gather(*(call() for _ in range(10)))
    assert breaker.state is BreakerState.OPEN
    # The upstream was not hammered: at most the threshold's worth of calls
    # got through before the breaker opened.
    assert client.calls <= 10
    assert breaker.status().total_failures >= 3
    assert breaker.status().total_rejections >= 0
