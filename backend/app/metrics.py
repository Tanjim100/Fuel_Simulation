"""Prometheus metrics: the exposition format, the registry, and the middleware.

The participant-facing brief makes observability a hard requirement (§15), and
names four layers the metrics must cover:

============  =====================================================
Layer         Examples the brief names
============  =====================================================
Application   request rate, latency, error rate, service availability
System        CPU, memory, resource utilization
Intelligence  prediction error, model confidence, shortage-alert rate,
              decision frequency, fallback activation
Logs          important actions, integration failures, decision events,
              recoveries
============  =====================================================

This module implements the *quantitative* half. The qualitative half -- the
log lines -- already exists: ``docs/BACKEND.md`` §4 documents the broker's
event vocabulary, and the backend logs every upstream failure with the
exception's class.

**Why hand-rolled.** The ``prometheus_client`` package would do this, but
adding a dependency to a stack that is built offline and must stay slim is a
cost with no benefit here: the exposition format is a few hundred lines, the
metric set is small and known, and a registry we control is easier to test
than one we configure. The brief says "teams are free to choose their stack".

**Where the numbers come from.** Every metric here is a *count* of something
that happened, or a *gauge* of the current state of a thing we own. Nothing
is estimated, nothing is sampled from a log file, and nothing is derived from
a number the backend invented.

The one metric that matters most is
``backend_simulator_pool_saturation_ratio``. The simulator's SQLAlchemy pool
is 15 connections wide and every request it serves holds two, so eight
concurrent reads demand 16 -- more than the pool has. The backend bounds its
own demand with a semaphore (``FETCH_CONCURRENCY``, 4), and *that semaphore is
ours*. So we can measure our own saturation exactly, and a ratio above 0.8 is
the early-warning signal for the incident documented in ``docs/BACKEND.md``
§18, which was invisible until someone read the logs hours later.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence

from .anomaly import ANOMALY_KINDS

__all__ = [
    "Counter",
    "Gauge",
    "Histogram",
    "MetricsRegistry",
    "REGISTRY",
    "render",
    "CONTENT_TYPE",
    "METRIC_FAMILIES",
]

#: The Prometheus text exposition format's content type. The version is
#: mandatory and Prometheus rejects the response without it.
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

#: Default histogram buckets, in seconds. The lower bound matches the
#: backend's own sub-10ms floor for a cached read; the upper bound matches the
#: client's 3s request timeout, so a hung upstream lands in the `+Inf` bucket
#: rather than being clipped.
DEFAULT_BUCKETS: tuple[float, ...] = (
    0.005, 0.010, 0.025, 0.050, 0.100, 0.250,
    0.500, 1.000, 2.500, 5.000, 10.000,
)


# --------------------------------------------------------------------------- #
# Metric primitives
# --------------------------------------------------------------------------- #
def _label_key(labels: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    """A hashable, order-independent key for a label set."""
    return tuple(sorted((str(k), str(v)) for k, v in labels.items()))


def _escape(value: str) -> str:
    """Escape a label value for the exposition format."""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace('"', '\\"')
    )


@dataclass
class Counter:
    """A monotonically increasing counter, partitioned by label set."""

    name: str
    help_text: str
    values: dict[tuple[tuple[str, str], ...], float] = field(default_factory=dict)
    #: Label sets that are always rendered, even at zero. A counter that has
    #: never been incremented must still be *present* in the exposition, or
    #: the family is missing from the output entirely and a dashboard cannot
    #: tell "no errors" from "not instrumented".
    pre_registered: list[tuple[tuple[str, str], ...]] = field(default_factory=list)

    def register(self, **labels: str) -> None:
        """Declare a label set that should always be rendered."""
        key = _label_key(labels)
        if key not in self.values and key not in self.pre_registered:
            self.pre_registered.append(key)
            self.values.setdefault(key, 0.0)

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        key = _label_key(labels)
        self.values[key] = self.values.get(key, 0.0) + amount

    def value(self, **labels: str) -> float:
        return self.values.get(_label_key(labels), 0.0)

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help_text}", f"# TYPE {self.name} counter"]
        for key, value in sorted(self.values.items()):
            if not key:
                lines.append(f"{self.name} {value}")
                continue
            rendered = ",".join(f'{k}="{_escape(v)}"' for k, v in key)
            lines.append(f"{self.name}{{{rendered}}} {value}")
        return lines


@dataclass
class Gauge:
    """A value that goes up and down, partitioned by label set."""

    name: str
    help_text: str
    values: dict[tuple[tuple[str, str], ...], float] = field(default_factory=dict)
    #: Label sets that are always rendered, even at zero.
    pre_registered: list[tuple[tuple[str, str], ...]] = field(default_factory=list)

    def register(self, **labels: str) -> None:
        """Declare a label set that should always be rendered."""
        key = _label_key(labels)
        if key not in self.values and key not in self.pre_registered:
            self.pre_registered.append(key)
            self.values.setdefault(key, 0.0)

    def set(self, value: float, **labels: str) -> None:
        self.values[_label_key(labels)] = float(value)

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        key = _label_key(labels)
        self.values[key] = self.values.get(key, 0.0) + amount

    def dec(self, amount: float = 1.0, **labels: str) -> None:
        self.inc(-amount, **labels)

    def value(self, **labels: str) -> float:
        return self.values.get(_label_key(labels), 0.0)

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help_text}", f"# TYPE {self.name} gauge"]
        for key, value in sorted(self.values.items()):
            if not key:
                lines.append(f"{self.name} {value}")
                continue
            rendered = ",".join(f'{k}="{_escape(v)}"' for k, v in key)
            lines.append(f"{self.name}{{{rendered}}} {value}")
        return lines


@dataclass
class Histogram:
    """A histogram with cumulative buckets, plus a sum and a count."""

    name: str
    help_text: str
    buckets: Sequence[float] = DEFAULT_BUCKETS
    counts: dict[tuple[tuple[str, str], ...], list[int]] = field(default_factory=dict)
    sums: dict[tuple[tuple[str, str], ...], float] = field(default_factory=dict)
    totals: dict[tuple[tuple[str, str], ...], int] = field(default_factory=dict)

    def observe(self, value: float, **labels: str) -> None:
        """Record one observation.

        The per-bucket counts stored here are **not** cumulative: a sample of
        0.02 increments only the ``le=0.025`` bucket. The cumulative counts
        Prometheus expects are produced at render time by summing the
        buckets up to each bound.

        Storing cumulative counts *and* summing again at render time
        double-counts every sample. The symptom is subtle and worth
        spelling out: the ``le=10.0`` bucket reads far higher than the
        ``+Inf`` bucket, and the ``+Inf`` bucket disagrees with the
        ``_count`` series -- which is what a monitoring bug looks like until
        you check the arithmetic.
        """
        key = _label_key(labels)
        counts = self.counts.get(key)
        if counts is None:
            counts = [0] * (len(self.buckets) + 1)
            self.counts[key] = counts
            self.sums[key] = 0.0
            self.totals[key] = 0
        # The sample lands in exactly one bucket: the first finite bound it
        # is under, or the +Inf slot when it is over every bound. The
        # renderer then turns these per-bucket counts into the cumulative
        # series Prometheus expects.
        for index, bound in enumerate(self.buckets):
            if value <= bound:
                counts[index] += 1
                break
        else:
            counts[-1] += 1
        self.sums[key] += float(value)
        self.totals[key] += 1

    def count(self, **labels: str) -> int:
        return self.totals.get(_label_key(labels), 0)

    def count_across(self) -> int:
        """The total number of observations, over every label set."""
        return sum(self.totals.values())

    def total_seconds(self, **labels: str) -> float:
        return self.sums.get(_label_key(labels), 0.0)

    def total_seconds_across(self) -> float:
        """The sum of every observation, over every label set."""
        return sum(self.sums.values())

    def percentile(self, fraction: float, **labels: str) -> float:
        """The requested percentile for one label set.

        This is an approximation -- a histogram only knows which bucket a
        sample fell into. It is the same approximation Prometheus makes when
        it computes a quantile, and it is exact enough for a health panel.

        The value is interpolated within the bucket, which is closer than
        reporting the bucket's upper edge.
        """
        key = _label_key(labels)
        counts = self.counts.get(key)
        if not counts:
            return 0.0
        return self._percentile_of(counts, self.totals.get(key, 0), fraction)

    def percentile_across(self, fraction: float) -> float:
        """The requested percentile over *every* label set.

        The PDF's section 16 table asks for a system-wide p95 latency, not
        a per-route one. The samples are pooled first and the percentile
        is then computed over the pooled histogram -- which is exactly
        what Prometheus does when it aggregates a histogram across its
        label sets.

        Calling ``percentile()`` without labels would return 0.0, because
        the samples live under labelled keys (method, path) and the
        unlabelled key holds nothing. That is a silent wrong answer, so
        this method exists to be the obvious one to call.
        """
        pooled = [0] * (len(self.buckets) + 1)
        total = 0
        for key, counts in self.counts.items():
            label_total = self.totals.get(key, 0)
            total += label_total
            for index, count in enumerate(counts):
                pooled[index] += count
        if total == 0:
            return 0.0
        return self._percentile_of(pooled, total, fraction)

    def _percentile_of(
        self, counts: list[int], total: int, fraction: float
    ) -> float:
        """The percentile of one (pooled) bucket array."""
        if total <= 0 or not counts:
            return 0.0
        target = fraction * total
        cumulative = 0
        for index, bound in enumerate(self.buckets):
            # The stored counts are per-bucket, so the running total has to
            # be accumulated here -- exactly as the renderer does.
            cumulative += counts[index]
            if cumulative >= target:
                previous_bound = 0.0 if index == 0 else self.buckets[index - 1]
                within = counts[index]
                if within <= 0:
                    return bound
                # Interpolate inside the bucket.
                position = (target - (cumulative - within)) / within
                return previous_bound + position * (bound - previous_bound)
        # Everything landed in +Inf -- report the largest finite bound.
        return self.buckets[-1] if self.buckets else 0.0

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help_text}", f"# TYPE {self.name} histogram"]
        for key, counts in sorted(self.counts.items()):
            base = ",".join(f'{k}="{_escape(v)}"' for k, v in key)
            cumulative = 0
            for index, bound in enumerate(self.buckets):
                cumulative += counts[index]
                label = _escape(str(bound))
                if base:
                    labels = f"{base},le=\"{label}\""
                else:
                    labels = f"le=\"{label}\""
                lines.append(f"{self.name}_bucket{{{labels}}} {cumulative}")
            # The +Inf bucket holds every sample, so it is the total.
            cumulative += counts[-1]
            inf_labels = f"{base},le=\"+Inf\"" if base else "le=\"+Inf\""
            lines.append(f"{self.name}_bucket{{{inf_labels}}} {cumulative}")
            suffix = f"{{{base}}}" if base else ""
            lines.append(f"{self.name}_sum{suffix} {self.sums.get(key, 0.0)}")
            lines.append(f"{self.name}_count{suffix} {self.totals.get(key, 0)}")
        return lines


# --------------------------------------------------------------------------- #
# The registry
# --------------------------------------------------------------------------- #
class MetricsRegistry:
    """Every metric the backend publishes, in one place.

    The registry is a process-wide singleton, so a metric is defined exactly
    once and every component that touches it sees the same numbers.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, Counter] = {}
        self._gauges: dict[str, Gauge] = {}
        self._histograms: dict[str, Histogram] = {}

    # -- definitions -------------------------------------------------------- #
    def counter(self, name: str, help_text: str) -> Counter:
        with self._lock:
            existing = self._counters.get(name)
            if existing is None:
                existing = Counter(name=name, help_text=help_text)
                self._counters[name] = existing
            return existing

    def gauge(self, name: str, help_text: str) -> Gauge:
        with self._lock:
            existing = self._gauges.get(name)
            if existing is None:
                existing = Gauge(name=name, help_text=help_text)
                self._gauges[name] = existing
            return existing

    def histogram(self, name: str, help_text: str) -> Histogram:
        with self._lock:
            existing = self._histograms.get(name)
            if existing is None:
                existing = Histogram(name=name, help_text=help_text)
                self._histograms[name] = existing
            return existing

    # -- rendering ---------------------------------------------------------- #
    def render(self) -> str:
        """The whole registry in the Prometheus text exposition format."""
        lines: list[str] = []
        with self._lock:
            counters = list(self._counters.values())
            gauges = list(self._gauges.values())
            histograms = list(self._histograms.values())
        for metric in sorted(counters, key=lambda m: m.name):
            lines.extend(metric.render())
        for metric in sorted(gauges, key=lambda m: m.name):
            lines.extend(metric.render())
        for metric in sorted(histograms, key=lambda m: m.name):
            lines.extend(metric.render())
        return "\n".join(lines) + "\n"

    def to_json(self) -> dict[str, Any]:
        """The same numbers, as JSON.

        The console reads this rather than parsing the exposition format: the
        console's contract is JSON, and putting a text-format parser inside
        the UI would put a number-parsing bug inside the guarantee that every
        number on screen came from the backend.
        """
        payload: dict[str, Any] = {"counters": {}, "gauges": {}, "histograms": {}}
        with self._lock:
            counters = list(self._counters.values())
            gauges = list(self._gauges.values())
            histograms = list(self._histograms.values())

        def label_key(key: Iterable[tuple[str, str]]) -> str:
            """The label set as a stable, human-readable string.

            A JSON object cannot have a dict as a key, so the label set is
            rendered the way the exposition renders it -- ``method=GET,
            path=/api/health`` -- which is also what a reader expects to
            see. The alternative (a list of label objects) is harder to
            scan and harder to look up by eye.
            """
            return ",".join(f"{k}={v}" for k, v in key)

        for metric in counters:
            payload["counters"][metric.name] = {
                label_key(key): value for key, value in metric.values.items()
            }
        for metric in gauges:
            payload["gauges"][metric.name] = {
                label_key(key): value for key, value in metric.values.items()
            }
        for metric in histograms:
            payload["histograms"][metric.name] = {
                label_key(key): {
                    "count": metric.totals.get(key, 0),
                    "sum": metric.sums.get(key, 0.0),
                    "buckets": list(metric.buckets),
                    "counts": list(counts),
                }
                for key, counts in metric.counts.items()
            }
        return payload


#: The process-wide registry.
REGISTRY = MetricsRegistry()


# --------------------------------------------------------------------------- #
# The metric catalogue
#
# Every metric the backend publishes, defined once, in one table. The names
# are prefixed ``backend_`` so a Prometheus instance scraping several
# services cannot confuse them.
# --------------------------------------------------------------------------- #

# -- Application layer ------------------------------------------------------ #
HTTP_REQUESTS = REGISTRY.counter(
    "backend_http_requests_total",
    "Total HTTP requests handled by the backend, by method, route and status.",
)
HTTP_DURATION = REGISTRY.histogram(
    "backend_http_request_duration_seconds",
    "HTTP request duration in seconds, by method and route.",
)
UPSTREAM_REQUESTS = REGISTRY.counter(
    "backend_upstream_requests_total",
    "Total calls to the simulator, by outcome.",
)
UPSTREAM_DURATION = REGISTRY.histogram(
    "backend_upstream_request_duration_seconds",
    "Duration of a call to the simulator, in seconds.",
)
#: There is deliberately **no** `backend_upstream_retries_total` counter.
#:
#: The simulator client retries internally -- three retries with backoff --
#: and that happens *inside* the client, below the breaker's choke point.
#: The backend genuinely cannot see a retry, so a counter for one would be a
#: permanent zero: a number that looks like data and means nothing. The
#: proxy for "the upstream was slow or flaky" is
#: `backend_upstream_request_duration_seconds` and the `timeout` /
#: `transport_error` outcomes on `backend_upstream_requests_total`.

# -- System layer ----------------------------------------------------------- #
SIMULATOR_INFLIGHT = REGISTRY.gauge(
    "backend_simulator_inflight_reads",
    "World reads currently in flight against the simulator.",
)
SIMULATOR_CONCURRENCY_LIMIT = REGISTRY.gauge(
    "backend_simulator_fetch_concurrency_limit",
    "The bound on concurrent world reads (the fetch semaphore's limit).",
)
SIMULATOR_SATURATION = REGISTRY.gauge(
    "backend_simulator_pool_saturation_ratio",
    "In-flight reads divided by the concurrency limit. Above 0.8 the "
    "simulator's connection pool is close to saturation.",
)
SIMULATOR_QUEUE_DEPTH = REGISTRY.gauge(
    "backend_simulator_read_queue_depth",
    "Callers waiting for the world-read semaphore.",
)

# -- Intelligence layer ----------------------------------------------------- #
FORECAST_ERROR = REGISTRY.histogram(
    "backend_forecast_abs_error_litres",
    "Absolute error between the forecast and the live demand oracle, in litres.",
)
FORECAST_VALIDATION = REGISTRY.counter(
    "backend_forecast_validation_total",
    "Forecast validations run against the live oracle, by verdict.",
)
SHORTAGE_ALERTS = REGISTRY.counter(
    "backend_shortage_alerts_total",
    "Stockout risks and shortage alerts raised, by severity.",
)
DECISIONS = REGISTRY.counter(
    "backend_decisions_total",
    "Operator decisions taken, by kind (plan, dispatch, cancel, autopilot).",
)
AUTOPILOT_ENABLED = REGISTRY.gauge(
    "backend_autopilot_enabled",
    "1 when the autopilot is in a mode other than off, 0 otherwise.",
)
AUTOPILOT_MODE = REGISTRY.gauge(
    "backend_autopilot_mode",
    "The autopilot's mode: 0=off, 1=live.",
)
AUTOPILOT_CYCLES = REGISTRY.counter(
    "backend_autopilot_cycles_total",
    "Autopilot cycles run, by outcome.",
)
AUTOPILOT_DISPATCHES = REGISTRY.counter(
    "backend_autopilot_dispatches_total",
    "Allocations the autopilot attempted, by outcome (created, replayed, failed).",
)
AUTOPILOT_LITRES = REGISTRY.counter(
    "backend_autopilot_litres_total",
    "Cumulative litres the autopilot has dispatched.",
)
FALLBACK_ACTIVATIONS = REGISTRY.counter(
    "backend_fallback_activations_total",
    "Times a fallback path was taken instead of the primary path, by kind.",
)
ANOMALIES = REGISTRY.counter(
    "backend_anomalies_total",
    "Anomalies detected, by kind and severity.",
)
INCIDENTS = REGISTRY.counter(
    "backend_incidents_total",
    "Incidents opened from grouped anomalies.",
)


class _OnceCounter:
    """Count a thing once, when it first appears -- not once per read.

    A counter that a *read* endpoint increments is measuring the reader, not
    the world. The anomaly detectors run against the live world on every
    request, so the same anomaly is recomputed on every poll; incrementing
    the counter there would make it a function of how often the console
    refreshed.

    This records the ids it has already counted and increments only for the
    ones it has not seen. The first read that sees an anomaly counts it; every
    later read of the same anomaly does not.

    The set is bounded: it keeps the ids seen since the last world tick, so
    it cannot grow without limit as the world advances.
    """

    def __init__(self, counter: "Counter", *labels: str) -> None:
        self._counter = counter
        self._labels = labels or ("kind",)
        self._seen: dict[str, str] = {}

    def count(
        self, entries: Iterable[tuple[str, Mapping[str, str]]], *, tick: str
    ) -> int:
        """Count the entries not yet seen at this tick.

        ``entries`` is a sequence of ``(id, labels)`` pairs -- the id
        identifies the thing, the labels are the counter's label values.
        Returns the number newly counted.
        """
        # A new tick means a new world: anything from an earlier tick has
        # already been counted and cannot be counted again, so the record is
        # replaced rather than grown.
        if self._seen.get("_tick") != tick:
            self._seen = {"_tick": tick}
        newly = 0
        for entry_id, labels in entries:
            if entry_id in self._seen:
                continue
            self._seen[entry_id] = ""
            self._counter.inc(**labels)
            newly += 1
        return newly


class _SeverityOnceCounter(_OnceCounter):
    """``_OnceCounter`` for a counter labelled by severity."""

    def __init__(self, counter: "Counter") -> None:
        super().__init__(counter, "severity")


class _KindOnceCounter(_OnceCounter):
    """``_OnceCounter`` for a counter labelled by kind."""

    def __init__(self, counter: "Counter") -> None:
        super().__init__(counter, "kind")


class _KindSeverityOnceCounter(_OnceCounter):
    """``_OnceCounter`` for a counter labelled by kind *and* severity."""

    def __init__(self, counter: "Counter") -> None:
        super().__init__(counter, "kind", "severity")


# -- Resilience layer ------------------------------------------------------- #
CIRCUIT_STATE = REGISTRY.gauge(
    "backend_circuit_state",
    "The simulator circuit breaker's state: 0=CLOSED, 1=HALF_OPEN, 2=OPEN.",
)
CIRCUIT_TRANSITIONS = REGISTRY.counter(
    "backend_circuit_transitions_total",
    "Circuit breaker state transitions, by the state entered.",
)
CIRCUIT_REJECTIONS = REGISTRY.counter(
    "backend_circuit_rejections_total",
    "Calls rejected by the open breaker without touching the upstream.",
)
BROKER_MODE = REGISTRY.gauge(
    "backend_broker_mode",
    "The broker's upstream mode: 0=sse, 1=polling, 2=offline.",
)
BROKER_PROBE_FAILURES = REGISTRY.counter(
    "backend_broker_probe_failures_total",
    "Failed SSE reachability probes.",
)
BROKER_SUSPECTED_DROPS = REGISTRY.counter(
    "backend_broker_suspected_drops_total",
    "Suspected silent SSE drops detected by the watchdog.",
)
BROKER_RECONNECTS = REGISTRY.counter(
    "backend_broker_reconnects_total",
    "Upstream SSE reconnects performed.",
)
SSE_EVENTS = REGISTRY.counter(
    "backend_sse_events_total",
    "Upstream SSE events received.",
)
SSE_FANOUT = REGISTRY.counter(
    "backend_sse_events_fanned_out_total",
    "Events fanned out to dashboard subscribers.",
)
SSE_DROPPED = REGISTRY.counter(
    "backend_sse_events_dropped_total",
    "Events dropped because a subscriber's queue was full.",
)
SNAPSHOT_AGE = REGISTRY.gauge(
    "backend_snapshot_age_seconds",
    "The age of the cached world snapshot, in seconds.",
)
SNAPSHOT_REFRESH = REGISTRY.counter(
    "backend_snapshot_refresh_total",
    "World snapshot refreshes attempted, by result.",
)
DLQ_MESSAGES = REGISTRY.counter(
    "backend_dlq_messages_total",
    "Payloads dead-lettered, by reason.",
)
DLQ_SIZE = REGISTRY.gauge(
    "backend_dlq_size",
    "The number of entries currently in the dead-letter queue.",
)

# -- the "count once" trackers ----------------------------------------------- #
#: Counts each anomaly once, when it is first seen, rather than once per
#: read. The detectors recompute on every request, so an unguarded counter
#: would measure the console's polling rate instead of the anomaly rate.
ANOMALY_COUNTER = _KindSeverityOnceCounter(ANOMALIES)
SHORTAGE_ALERT_COUNTER = _SeverityOnceCounter(SHORTAGE_ALERTS)
INCIDENT_COUNTER = _SeverityOnceCounter(INCIDENTS)

#: The metric families the observability self-check requires to be present.
#: Derived from the brief's §15 layer list -- a missing family means a layer
#: of the requirement is not covered.
METRIC_FAMILIES: dict[str, tuple[str, ...]] = {
    "application": (
        "backend_http_requests_total",
        "backend_http_request_duration_seconds",
        "backend_upstream_requests_total",
        "backend_upstream_request_duration_seconds",
    ),
    "system": (
        "backend_simulator_inflight_reads",
        "backend_simulator_fetch_concurrency_limit",
        "backend_simulator_pool_saturation_ratio",
        "backend_simulator_read_queue_depth",
    ),
    "intelligence": (
        "backend_forecast_abs_error_litres",
        "backend_forecast_validation_total",
        "backend_shortage_alerts_total",
        "backend_decisions_total",
        "backend_fallback_activations_total",
        "backend_autopilot_enabled",
        "backend_autopilot_mode",
        "backend_autopilot_cycles_total",
        "backend_autopilot_dispatches_total",
        "backend_autopilot_litres_total",
    ),
    "resilience": (
        "backend_circuit_state",
        "backend_circuit_transitions_total",
        "backend_broker_mode",
        "backend_broker_probe_failures_total",
        "backend_broker_suspected_drops_total",
        "backend_sse_events_total",
        "backend_snapshot_age_seconds",
        "backend_snapshot_refresh_total",
        "backend_dlq_messages_total",
    ),
}


def _pre_register() -> None:
    """Declare the label sets that must always appear in the exposition.

    A metric that has never been touched renders as *absent*, and an absent
    metric is indistinguishable from an uninstrumented one. So every family
    is given its zero-valued series up front: every upstream outcome, every
    severity, every decision kind, every fallback kind. A dashboard can then
    show "0 errors" rather than a gap.
    """
    for outcome in (
        "success", "timeout", "fault_injected", "transport_error",
        "not_found", "conflict", "validation_error", "circuit_open", "unknown",
    ):
        UPSTREAM_REQUESTS.register(outcome=outcome)

    for verdict in ("EXACT", "MISMATCH", "UNAVAILABLE"):
        FORECAST_VALIDATION.register(verdict=verdict)

    for severity in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
        SHORTAGE_ALERTS.register(severity=severity)
        INCIDENTS.register(severity=severity)

    for kind in ("plan", "dispatch", "cancel", "autopilot"):
        DECISIONS.register(kind=kind)

    for outcome in (
        "dispatched", "no_demand", "gated", "breaker_open",
        "quarantined", "tick_regressed", "error", "off",
    ):
        AUTOPILOT_CYCLES.register(outcome=outcome)

    for outcome in ("created", "replayed", "failed"):
        AUTOPILOT_DISPATCHES.register(outcome=outcome)

    for kind in (
        "degraded_mode", "poll_fallback", "breaker_open", "forecast_fallback",
    ):
        FALLBACK_ACTIVATIONS.register(kind=kind)

    for kind in ANOMALY_KINDS:
        for severity in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
            ANOMALIES.register(kind=kind, severity=severity)

    for result in ("ok", "error"):
        SNAPSHOT_REFRESH.register(result=result)

    for reason in ("malformed_frame", "unparseable_payload", "unknown"):
        DLQ_MESSAGES.register(reason=reason)

    for state in ("CLOSED", "HALF_OPEN", "OPEN"):
        CIRCUIT_TRANSITIONS.register(to_state=state)

    # The gauges that must read 0 before anything has happened.
    CIRCUIT_STATE.set(0)
    BROKER_MODE.set(0)
    SIMULATOR_INFLIGHT.set(0)
    SIMULATOR_QUEUE_DEPTH.set(0)
    SNAPSHOT_AGE.set(0)
    DLQ_SIZE.set(0)
    AUTOPILOT_ENABLED.set(0)
    AUTOPILOT_MODE.set(0)


_pre_register()


# --------------------------------------------------------------------------- #
# The middleware
# --------------------------------------------------------------------------- #
def route_template(request: Any) -> str:
    """The route *template* for a request, e.g. ``/api/stations/{station_id}``.

    Using the raw path would make every station its own time series --
    ``/api/stations/station-mirpur`` and ``/api/stations/station-tongi`` are
    different paths but the same endpoint, and a metric that cannot tell a
    busy endpoint from a busy *id* is not a metric a dashboard can use.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if path:
        return str(path)
    # Unmatched requests (404s) have no route. Use the raw path but cap it,
    # so a scanner probing random URLs cannot inflate the cardinality.
    raw = request.url.path
    if len(raw) > 64:
        return "unmatched"
    return raw or "unmatched"


def install_http_middleware(app: Any) -> None:
    """Install the request-counting and latency-recording middleware.

    Added *after* the app's routes exist so the middleware wraps the
    router. Starlette applies middleware in reverse registration order, so
    registering this last means it is the outermost -- which is what we
    want: it should observe the total time including the error handlers.
    """

    @app.middleware("http")
    async def _observe(request: Any, call_next: Any) -> Any:
        started = time.monotonic()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            elapsed = time.monotonic() - started
            template = route_template(request)
            HTTP_REQUESTS.inc(
                method=request.method, path=template, status=str(status_code)
            )
            HTTP_DURATION.observe(elapsed, method=request.method, path=template)
