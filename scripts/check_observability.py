#!/usr/bin/env python3
"""The observability self-check.

PDF section 15 requires "meaningful observability covering the application"
across four layers -- application, system, intelligence and logs -- and
section 16 requires a component health table with p95 latency and an error
rate. This script asserts that the running backend actually carries them.

It is deliberately a *check*, not a demo: it reads the live endpoints and
asserts on what is there. If the metrics endpoint loses a family, or the
health table loses a component, this fails -- which is the only way a
requirement like "must implement" stays true over time.

Stdlib only, so it runs anywhere Python does.

    python3 scripts/check_observability.py --base-url http://localhost:8002
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from typing import Any

#: The metric families the backend must publish, grouped by the PDF's four
#: observability layers. Each entry is (family, required_label_names).
REQUIRED_FAMILIES: dict[str, list[tuple[str, tuple[str, ...]]]] = {
    "application": [
        ("backend_http_requests_total", ("method", "path", "status")),
        ("backend_http_request_duration_seconds", ("method", "path")),
        ("backend_upstream_requests_total", ("outcome",)),
        ("backend_upstream_request_duration_seconds", ()),
    ],
    "system": [
        ("backend_simulator_inflight_reads", ()),
        ("backend_simulator_fetch_concurrency_limit", ()),
        ("backend_simulator_pool_saturation_ratio", ()),
        ("backend_simulator_read_queue_depth", ()),
    ],
    "intelligence": [
        ("backend_forecast_abs_error_litres", ()),
        ("backend_forecast_validation_total", ("verdict",)),
        ("backend_shortage_alerts_total", ("severity",)),
        ("backend_decisions_total", ("kind",)),
        ("backend_fallback_activations_total", ("kind",)),
    ],
    "resilience": [
        ("backend_circuit_state", ()),
        ("backend_circuit_transitions_total", ("to_state",)),
        ("backend_broker_mode", ()),
        ("backend_broker_probe_failures_total", ()),
        ("backend_broker_suspected_drops_total", ()),
        ("backend_sse_events_total", ()),
        ("backend_snapshot_age_seconds", ()),
        ("backend_snapshot_refresh_total", ("result",)),
        ("backend_dlq_messages_total", ("reason",)),
    ],
}

#: The components PDF section 16 names.
REQUIRED_COMPONENTS = (
    "backend_api",
    "redis",
    "fuel_simulator",
    "prediction_service",
    "decision_engine",
)

#: The health states the console understands.
VALID_STATES = {"HEALTHY", "DEGRADED", "UNHEALTHY", "UNKNOWN", "DISABLED"}

#: The Prometheus exposition content type. The version is mandatory --
#: Prometheus rejects a response without it.
EXPECTED_CONTENT_TYPE = "text/plain; version=0.0.4"


class Check:
    """A running tally of the assertions."""

    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.failures: list[str] = []

    def ok(self, message: str) -> None:
        self.passed += 1
        print(f"  ok   {message}")

    def fail(self, message: str) -> None:
        self.failed += 1
        self.failures.append(message)
        print(f"  FAIL {message}")

    def assert_true(self, condition: bool, message: str) -> bool:
        if condition:
            self.ok(message)
        else:
            self.fail(message)
        return condition


def get(url: str, timeout: float = 10.0) -> tuple[int, str, dict[str, str]]:
    """GET a URL. Returns (status, body, headers)."""
    request = urllib.request.Request(url, headers={"Accept": "*/*"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            headers = {k.lower(): v for k, v in response.headers.items()}
            return response.status, body, headers
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        headers = {k.lower(): v for k, v in exc.headers.items()}
        return exc.code, body, headers
    except Exception as exc:  # noqa: BLE001 - the check must report, not crash
        return 0, str(exc), {}


def parse_exposition(text: str) -> dict[str, dict[str, Any]]:
    """Parse the Prometheus text exposition into ``{family: {...}}``.

    The exposition format is simple: ``# TYPE <name> <type>`` declares a
    family, and ``<name>{labels} <value>`` is a sample. Only the family's
    presence and its label names matter for this check, so the parser
    deliberately does not try to be complete.

    A histogram's samples are published as ``<name>_bucket``,
    ``<name>_sum`` and ``<name>_count`` -- three *series* belonging to one
    *family*. They are folded back into the declared family here, because
    the PDF's p95 requirement depends on the family's buckets, not on the
    individual series.
    """
    families: dict[str, dict[str, Any]] = {}
    declared: dict[str, str] = {}

    # First pass: the declarations, so a sample can be attributed to its
    # family even when it appears before the declaration.
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#"):
            parts = line.split()
            if len(parts) >= 4 and parts[1] == "TYPE":
                declared[parts[2]] = parts[3]
                families.setdefault(
                    parts[2], {"type": parts[3], "samples": [], "labels": set()}
                )

    def family_of(sample_name: str) -> str:
        """The family a sample belongs to.

        A histogram's three series share the declared family name; a
        counter's or gauge's samples keep their own name.
        """
        if sample_name in families:
            return sample_name
        for suffix in ("_bucket", "_sum", "_count"):
            if sample_name.endswith(suffix):
                base = sample_name[: -len(suffix)]
                if base in families:
                    return base
        return sample_name

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{(.*)\})?\s+(.*)$", line)
        if not match:
            continue
        sample_name, _, label_text, _value = match.groups()
        name = family_of(sample_name)
        family = families.setdefault(
            name, {"type": declared.get(name, "untyped"), "samples": [], "labels": set()}
        )
        family["samples"].append(line)
        for key in re.findall(r'([a-zA-Z_][a-zA-Z0-9_]*)="', label_text or ""):
            family["labels"].add(key)
    return families


def check_metrics(base_url: str, check: Check) -> None:
    """Every required metric family must be present, with its labels."""
    status, body, headers = get(f"{base_url}/metrics")

    if not check.assert_true(
        status == 200, f"GET /metrics returns 200 (got {status})"
    ):
        return

    content_type = headers.get("content-type", "")
    check.assert_true(
        content_type.startswith(EXPECTED_CONTENT_TYPE),
        f"GET /metrics content-type is {EXPECTED_CONTENT_TYPE!r} "
        f"(got {content_type!r})",
    )

    families = parse_exposition(body)
    check.assert_true(
        len(families) > 0, f"the exposition contains {len(families)} metric families"
    )

    for layer, requirements in REQUIRED_FAMILIES.items():
        for name, label_names in requirements:
            family = families.get(name)
            if family is None:
                check.fail(f"[{layer}] the metric family {name!r} is missing")
                continue
            missing = [label for label in label_names if label not in family["labels"]]
            if missing:
                check.fail(
                    f"[{layer}] {name!r} is missing the label(s) "
                    f"{', '.join(missing)}"
                )
            else:
                check.ok(f"[{layer}] {name} is published")

    # A histogram must carry buckets -- a histogram without buckets is not
    # a histogram, and the PDF's p95 requirement depends on them.
    for name in ("backend_http_request_duration_seconds",):
        family = families.get(name)
        if family is None:
            continue
        has_buckets = any("le=" in sample for sample in family["samples"])
        check.assert_true(
            has_buckets, f"{name!r} publishes histogram buckets"
        )


def check_health(base_url: str, check: Check) -> None:
    """The PDF section 16 health table."""
    status, body, _headers = get(f"{base_url}/api/health")

    if not check.assert_true(
        status == 200, f"GET /api/health returns 200 (got {status})"
    ):
        return

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        check.fail(f"GET /api/health returns JSON ({exc})")
        return

    check.assert_true(
        payload.get("status") in ("ok", "degraded"),
        f"the health status is 'ok' or 'degraded' (got {payload.get('status')!r})",
    )

    components = payload.get("components")
    if not check.assert_true(
        isinstance(components, dict), "the health payload carries a component table"
    ):
        return

    for name in REQUIRED_COMPONENTS:
        component = components.get(name)
        if not isinstance(component, dict):
            check.fail(f"the component {name!r} is missing")
            continue
        state = component.get("state")
        if state not in VALID_STATES:
            check.fail(
                f"the component {name!r} has the unknown state {state!r}"
            )
        else:
            check.ok(f"the component {name!r} is {state}")

    latency = payload.get("latency")
    if isinstance(latency, dict) and "p95_seconds" in latency:
        check.ok("the health payload carries the p95 latency")
    else:
        check.fail("the health payload is missing the p95 latency")

    requests = payload.get("requests")
    if isinstance(requests, dict) and "error_rate" in requests:
        check.ok("the health payload carries the error rate")
    else:
        check.fail("the health payload is missing the error rate")

    circuit = payload.get("circuit")
    if isinstance(circuit, dict) and "state" in circuit:
        check.ok(f"the health payload carries the circuit state ({circuit['state']})")
    else:
        check.fail("the health payload is missing the circuit state")


def check_observability(base_url: str, check: Check) -> None:
    """The JSON mirror the console reads."""
    status, body, _headers = get(f"{base_url}/api/observability")

    if not check.assert_true(
        status == 200, f"GET /api/observability returns 200 (got {status})"
    ):
        return

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        check.fail(f"GET /api/observability returns JSON ({exc})")
        return

    for key in ("components", "latency", "requests", "circuit", "broker", "metrics"):
        check.assert_true(
            key in payload, f"the observability payload carries {key!r}"
        )

    # The console's contract: /api/metrics stays JSON. Repurposing it for
    # the Prometheus exposition would break the console.
    status, body, headers = get(f"{base_url}/api/metrics")
    check.assert_true(
        status == 200, f"GET /api/metrics returns 200 (got {status})"
    )
    check.assert_true(
        "json" in headers.get("content-type", ""),
        "GET /api/metrics is still JSON (the console's contract)",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default="http://localhost:8002",
        help="The backend's base URL (default: http://localhost:8002)",
    )
    args = parser.parse_args()

    check = Check()

    print("The metrics endpoint")
    check_metrics(args.base_url, check)

    print("\nThe health status table")
    check_health(args.base_url, check)

    print("\nThe observability JSON mirror")
    check_observability(args.base_url, check)

    print(f"\nchecked {check.passed + check.failed} assertions")
    if check.failed:
        print(f"{check.failed} failed")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
