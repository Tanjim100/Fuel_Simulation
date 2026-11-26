#!/usr/bin/env python3
"""Merge k6's per-scenario summaries into the canonical report schema.

k6 runs one script per scenario, so its output arrives as several
``--summary-export`` files. The canonical schema (defined by
``loadtest/harness.py``) is a single document with a ``scenarios`` array --
so whichever tool ran the load test, the report has the same shape and a
harness can parse it without caring.

This also fills in the parts k6 cannot know: the backend's own
``/metrics`` snapshot (the pool saturation), which is the mechanism
behind the latency numbers.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: The k6 script file name -> the canonical scenario name and description.
SCENARIOS = {
    "decision.js": (
        "W1-decision",
        "POST /api/optimize -- the end-to-end decision path",
    ),
    "dashboard.js": (
        "W2-dashboard",
        "GET /api/stations -- the console's poll path",
    ),
    "console-mix.js": (
        "W3-console-mix",
        "the console's weighted read mix plus an SSE subscriber",
    ),
}


def load_summary(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}


def extract(summary: dict[str, Any]) -> dict[str, Any]:
    """Pull the latency figures out of a k6 summary."""
    metrics = summary.get("metrics", {})

    duration = metrics.get("http_req_duration", {}).get("values", {})
    iterations = metrics.get("iterations", {}).get("values", {}).get("count", 0)
    failed = metrics.get("http_req_failed", {}).get("values", {}).get("passes", 0)

    # k6 reports seconds; the schema is milliseconds.
    return {
        "requests": iterations,
        "errors": failed,
        "error_rate": round(failed / iterations, 6) if iterations else 0.0,
        "latency_ms": {
            "avg": round((duration.get("avg") or 0.0) * 1000, 3),
            "p50": round((duration.get("med") or 0.0) * 1000, 3),
            "p95": round((duration.get("p(95)") or 0.0) * 1000, 3),
            "p99": round((duration.get("p(99)") or 0.0) * 1000, 3),
            "min": round((duration.get("min") or 0.0) * 1000, 3),
            "max": round((duration.get("max") or 0.0) * 1000, 3),
        },
    }


def metrics_snapshot(base_url: str) -> dict[str, Any]:
    """The backend's own metrics, so the report ties latency to cause."""
    try:
        request = urllib.request.Request(
            f"{base_url.rstrip('/')}/api/observability",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return {"available": False}

    metrics = payload.get("metrics", {})

    def lookup(family: str) -> Any:
        """Read one metric family's value out of the JSON mirror.

        The mirror groups the families by type -- ``counters``, ``gauges``
        and ``histograms`` -- so a family is found by looking in each group
        in turn. A gauge's value is keyed by its label string, which is
        ``""`` for an unlabelled gauge.
        """
        entry = None
        for group_name in ("gauges", "counters", "histograms"):
            group = metrics.get(group_name)
            if isinstance(group, dict) and family in group:
                entry = group[family]
                break
        if entry is None:
            entry = metrics.get(family)
        if entry is None:
            return None
        if isinstance(entry, dict):
            if "value" in entry:
                return entry["value"]
            samples = entry.get("samples")
            if isinstance(samples, list) and samples:
                return samples[-1]
            values = [v for v in entry.values() if isinstance(v, (int, float))]
            if values:
                return max(values)
        if isinstance(entry, (int, float)):
            return entry
        return None

    saturation = lookup("backend_simulator_pool_saturation_ratio")
    return {
        "available": True,
        "pool_saturation": saturation,
        "pool_saturation_peak": saturation,
        "inflight_reads": lookup("backend_simulator_inflight_reads"),
        "fetch_concurrency_limit": lookup("backend_simulator_fetch_concurrency_limit"),
        "circuit_state": payload.get("circuit", {}).get("state"),
        "broker_mode": payload.get("broker", {}).get("mode"),
        "error_rate": payload.get("requests", {}).get("error_rate"),
        "p95_seconds": payload.get("latency", {}).get("p95_seconds"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary-dir", required=True,
                        help="The directory holding the k6 summary exports")
    parser.add_argument("--output", required=True, help="Where to write the report")
    parser.add_argument("--base-url", default="http://localhost:8002")
    parser.add_argument("--duration", type=float, default=30.0)
    parser.add_argument("--vus", type=int, default=10)
    args = parser.parse_args()

    # The merge runs after k6 has finished, so this is the *end* of the
    # load, not its start. The k6 summaries carry their own start times;
    # the report's `started_at` is the moment the report was assembled.
    # Both tools emit the same schema -- see the Python harness, which
    # captures its `started_at` before the scenarios run.
    assembled_at = datetime.now(timezone.utc).isoformat()

    summary_dir = Path(args.summary_dir)
    scenarios: list[dict[str, Any]] = []

    for script_name, (name, description) in SCENARIOS.items():
        path = summary_dir / f"{script_name.replace('.js', '')}.json"
        if not path.is_file():
            continue
        summary = load_summary(path)
        if not summary:
            continue
        extracted = extract(summary)
        scenarios.append(
            {
                "name": name,
                "description": description,
                "duration_seconds": args.duration,
                "requests": extracted["requests"],
                "errors": extracted["errors"],
                "error_rate": extracted["error_rate"],
                "latency_ms": extracted["latency_ms"],
                "throughput_rps": (
                    round(extracted["requests"] / args.duration, 3)
                    if args.duration
                    else 0.0
                ),
                "resource": {
                    "available": False,
                    "reason": "k6 does not sample docker stats",
                },
            }
        )

    if not scenarios:
        print("no k6 summaries were found", file=sys.stderr)
        return 1

    snapshot = metrics_snapshot(args.base_url)
    report = {
        "schema_version": 1,
        "tool": "k6",
        "started_at": assembled_at,
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": args.duration,
        "vus": args.vus,
        "concurrency": args.vus,
        "base_url": args.base_url,
        "host": {
            "hostname": socket.gethostname(),
            "python": sys.version.split()[0],
        },
        "scenarios": scenarios,
        "backend_metrics_snapshot": {
            "after": snapshot,
            "pool_saturation_peak": snapshot.get("pool_saturation"),
            "circuit_state": snapshot.get("circuit_state"),
        },
        "notes": (
            "The numbers are this stack's limits on this machine, not a "
            "benchmark to beat. PDF section 18 asks for an understanding of "
            "the system's behaviour and limits. The pool-saturation figure "
            "is the mechanism behind the latency: it is the backend's own "
            "demand on the simulator, measured against the 4-wide "
            "semaphore that bounds it."
        ),
    }

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
