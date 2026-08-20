import http from 'k6/http';
import { check, sleep } from 'k6';

export const options = {
  stages: [
    { duration: '30s', target: 10 },
    { duration: '2m',  target: 50 },
    { duration: '1m',  target: 100 },
    { duration: '30s', target: 0 },
  ],
  thresholds: {
    'http_req_duration': ['p(95)<500'],
    'http_req_failed':    ['rate<0.05'],
  },
  summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(90)', 'p(95)', 'p(99)'],
};

const BASE = 'http://localhost:8900';

export default function () {
  // ── Public endpoints ──────────────────────────────
  let r;

  r = http.get(`${BASE}/health`);
  check(r, { 'health 200': (x) => x.status === 200 });

  r = http.get(`${BASE}/metrics`);
  check(r, { 'metrics 200': (x) => x.status === 200 });

  r = http.get(`${BASE}/stats/daily`);
  check(r, { 'stats/daily 200': (x) => x.status === 200 });

  r = http.get(`${BASE}/dashboard`);
  check(r, { 'dashboard 200': (x) => x.status === 200 });

  r = http.get(`${BASE}/beta`);
  check(r, { 'beta 200': (x) => x.status === 200 });

  // ── Public POST ────────────────────────────────────
  r = http.post(`${BASE}/feedback`, JSON.stringify({
    rating: 4,
    comment: 'load-test',
  }), { headers: { 'Content-Type': 'application/json' } });
  check(r, { 'feedback 200/422': (x) => x.status === 200 || x.status === 422 });

  // ── Protected (expect 401 without API key) ─────────
  r = http.get(`${BASE}/workers`);
  check(r, { 'workers 401': (x) => x.status === 401 });

  r = http.get(`${BASE}/jobs`);
  check(r, { 'jobs 401': (x) => x.status === 401 });

  r = http.get(`${BASE}/admin/analytics`);
  check(r, { 'admin 401': (x) => x.status === 401 });

  // ── POST with payload ──────────────────────────────
  r = http.post(`${BASE}/submit`, JSON.stringify({
    image: 'ubuntu:22.04',
    command: 'echo hello',
    cpu: 1,
    memory: 512,
  }), { headers: { 'Content-Type': 'application/json' } });
  check(r, { 'submit 401': (x) => x.status === 401 });

  sleep(0.05);
}
