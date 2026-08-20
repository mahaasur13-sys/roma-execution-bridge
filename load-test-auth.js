import http from 'k6/http';
import { check, sleep } from 'k6';

const BASE = 'http://localhost:8900';
const API_KEY = __ENV.ROMA_API_KEY;

export const options = {
  stages: [
    { duration: '1m', target: 10 },
    { duration: '3m', target: 50 },
    { duration: '2m', target: 100 },
    { duration: '1m', target: 0 },
  ],
  thresholds: {
    http_req_duration: ['p(95)<500'],
    http_req_failed: ['rate<0.05'],
  },
};

export default function () {
  const headers = { 'X-API-Key': API_KEY };

  const res1 = http.get(BASE + '/health');
  check(res1, { 'health 200': (r) => r.status === 200 });

  const res2 = http.get(BASE + '/workers', { headers });
  check(res2, { 'workers 200': (r) => r.status === 200 });

  const res3 = http.get(BASE + '/stats/daily', { headers });
  check(res3, { 'daily 401': (r) => r.status === 401 });

  const res4 = http.get(BASE + '/admin/analytics', { headers });
  check(res4, { 'analytics 200': (r) => r.status === 200 });

  const res5 = http.post(BASE + '/submit',
    JSON.stringify({task:'test-' + Math.random().toString(36).slice(2, 8)}),
    { headers: Object.assign({'Content-Type': 'application/json'}, headers) }
  );
  check(res5, { 'submit 202': (r) => r.status === 202 });
}
