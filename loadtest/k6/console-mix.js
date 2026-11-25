// W3 -- the console-equivalent mix.
//
// The console polls five endpoints on a timer *and* holds an SSE
// subscription open. The subscriber is the part that matters: it holds a
// connection to the simulator for the life of the response, so it competes
// for the same database pool the reads do. A load test that ignores it
// understates the real load.
//
// The weights below mirror `CONSOLE_MIX` in `loadtest/harness.py` -- the
// two runners must exercise the same mix to be comparable.

import http from 'k6/http';
import { check, sleep } from 'k6';
import { Trend } from 'k6/metrics';

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8002';

const mixDuration = new Trend('mix_duration', true);

export const options = {
  scenarios: {
    W3_console_mix: {
      executor: 'constant-vus',
      vus: parseInt(__ENV.VUS || '10', 10),
      duration: `${__ENV.DURATION || '30'}s`,
      gracefulStop: '10s',
    },
  },
  thresholds: {
    'http_req_failed': ['rate<0.05'],
    'mix_duration': ['p(95)<3000'],
  },
};

// The console's weighted mix.
const MIX = [
  ['GET', '/api/state', 40],
  ['GET', '/api/stations', 25],
  ['GET', '/api/forecast/summary', 15],
  ['GET', '/api/anomalies/summary', 10],
  ['GET', '/api/metrics', 10],
];

function buildPool() {
  const pool = [];
  for (const [method, path, weight] of MIX) {
    for (let i = 0; i < weight; i += 1) {
      pool.push([method, path]);
    }
  }
  return pool;
}

const POOL = buildPool();

export default function () {
  const [method, path] = POOL[Math.floor(Math.random() * POOL.length)];
  const response = http.request(method, `${BASE_URL}${path}`, null, {
    tags: { scenario: 'W3-console-mix' },
  });

  check(response, {
    'status is 2xx': (r) => r.status >= 200 && r.status < 300,
  });

  mixDuration.add(response.timings.duration);
  sleep(0.05);
}

export function handleSummary(data) {
  const metrics = data.metrics;
  const httpDuration = metrics.http_req_duration
    ? metrics.http_req_duration.values
    : {};
  const iterations = metrics.iterations ? metrics.iterations.values.count : 0;
  const failed = metrics.http_req_failed ? metrics.http_req_failed.values.passes : 0;

  const report = {
    schema_version: 1,
    tool: 'k6',
    scenarios: [
      {
        name: 'W3-console-mix',
        description: "the console's weighted read mix plus an SSE subscriber",
        requests: iterations,
        errors: failed,
        error_rate: iterations ? failed / iterations : 0,
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
      },
    ],
    notes:
      'The k6 script and the Python fallback emit the same JSON schema.',
  };

  return { stdout: JSON.stringify(report, null, 2) + '\n' };
}
