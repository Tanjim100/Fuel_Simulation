// W1 -- the decision path.
//
// The end-to-end decision request: forecast + optimise + gate. This is the
// heaviest meaningful path the platform has, so it is where the system's
// real limits show.
//
// The k6 script and the Python fallback (`loadtest/harness.py`) emit the
// same JSON schema, so the report is reproducible either way.

import http from 'k6/http';
import { check, sleep } from 'k6';
import { Counter, Trend } from 'k6/metrics';

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8002';

const decisionDuration = new Trend('decision_duration', true);
const decisions = new Counter('decisions');

export const options = {
  scenarios: {
    W1_decision: {
      executor: 'constant-vus',
      vus: parseInt(__ENV.VUS || '10', 10),
      duration: `${__ENV.DURATION || '30'}s`,
      gracefulStop: '10s',
    },
  },
  thresholds: {
    // A threshold is not a pass/fail gate for the demo -- it is a signal.
    // The PDF wants the behaviour understood, not a green light.
    'http_req_failed': ['rate<0.05'],
    'decision_duration': ['p(95)<5000'],
  },
};

export default function () {
  // An empty body means "plan the default horizon for the whole world" --
  // the full decision, not a scoped-down one.
  const response = http.post(`${BASE_URL}/api/optimize`, JSON.stringify({}), {
    headers: { 'Content-Type': 'application/json' },
    tags: { scenario: 'W1-decision' },
  });

  const ok = check(response, {
    'status is 2xx': (r) => r.status >= 200 && r.status < 300,
    'the body is JSON': (r) => r.headers['Content-Type'] !== undefined &&
      r.headers['Content-Type'].indexOf('application/json') === 0,
  });

  decisionDuration.add(response.timings.duration);
  if (ok) {
    decisions.add(1);
  }

  // A think pause: a real operator does not fire decisions back to back
  // with no gap, and a tight loop would measure the queue rather than the
  // decision.
  sleep(0.05);
}

export function handleSummary(data) {
  const report = {
    schema_version: 1,
    tool: 'k6',
    scenarios: [],
    notes:
      'The k6 script and the Python fallback emit the same JSON schema. ' +
      'See loadtest/harness.py for the canonical shape.',
  };

  const metrics = data.metrics;
  const duration = metrics.iterations ? metrics.iterations.values.count : 0;
  const failed = metrics.http_req_failed
    ? metrics.http_req_failed.values.passes
    : 0;
  const httpDuration = metrics.http_req_duration
    ? metrics.http_req_duration.values
    : {};

  report.scenarios.push({
    name: 'W1-decision',
    description: 'POST /api/optimize -- the end-to-end decision path',
    duration_seconds: (metrics.http_req_duration
      ? metrics.http_req_duration.values.max / 1000
      : 0),
    requests: duration,
    errors: failed,
    error_rate: duration ? failed / duration : 0,
    latency_ms: {
      avg: httpDuration.avg || 0,
      p50: httpDuration['med'] || 0,
      p95: httpDuration.p(95) || 0,
      p99: httpDuration.p(99) || 0,
      min: httpDuration.min || 0,
      max: httpDuration.max || 0,
    },
    throughput_rps: 0,
    resource: { available: false, reason: 'k6 does not sample docker stats' },
  });

  return {
    stdout: JSON.stringify(report, null, 2) + '\n',
  };
}
