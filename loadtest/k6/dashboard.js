// W2 -- the dashboard read.
//
// `GET /api/stations` is what the console polls every five seconds, and it
// is the path that exposed the simulator's connection-pool wedge: it is a
// read against the simulator's database, bounded by the backend's 4-wide
// semaphore.
//
// The pool-saturation gauge is read alongside the requests, because the
// latency and the saturation are the same story told twice -- the number
// and the mechanism behind it.

import http from 'k6/http';
import { check, sleep } from 'k6';
import { Trend } from 'k6/metrics';

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8002';

const dashboardDuration = new Trend('dashboard_duration', true);
const saturation = new Trend('pool_saturation');

export const options = {
  scenarios: {
    W2_dashboard: {
      executor: 'constant-vus',
      vus: parseInt(__ENV.VUS || '10', 10),
      duration: `${__ENV.DURATION || '30'}s`,
      gracefulStop: '10s',
    },
  },
  thresholds: {
    'http_req_failed': ['rate<0.05'],
    'dashboard_duration': ['p(95)<2000'],
  },
};

export default function () {
  const response = http.get(`${BASE_URL}/api/stations`, {
    tags: { scenario: 'W2-dashboard' },
  });

  check(response, {
    'status is 2xx': (r) => r.status >= 200 && r.status < 300,
  });

  dashboardDuration.add(response.timings.duration);

  // One in ten workers samples the saturation gauge. Sampling it on every
  // request would add its own load to the very metric being measured.
  if (Math.random() < 0.1) {
    const observability = http.get(`${BASE_URL}/api/observability`);
    try {
      const payload = JSON.parse(observability.body);
      const metrics = payload.metrics || {};
      // The mirror groups the families by type; the gauges live under
      // "gauges". An unlabelled gauge's value is keyed by the empty string.
      const gauges = metrics.gauges || {};
      const entry = gauges.backend_simulator_pool_saturation_ratio;
      let value = null;
      if (entry && typeof entry.value === 'number') {
        value = entry.value;
      } else if (entry && typeof entry === 'object') {
        const numbers = Object.keys(entry)
          .map((k) => entry[k])
          .filter((v) => typeof v === 'number');
        if (numbers.length) {
          value = Math.max.apply(null, numbers);
        }
      }
      if (value !== null) {
        saturation.add(value);
      }
    } catch (err) {
      // A malformed observability payload is a finding, not a crash.
    }
  }

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
        name: 'W2-dashboard',
        description: "GET /api/stations -- the console's poll path",
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
