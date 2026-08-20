import http from 'k6/http';
import { check, sleep } from 'k6';

const BASE = 'http://localhost:8900';
const API_KEY = __ENV.ROMA_API_KEY;

export const options = {
  stages: [
    { duration: '2m', target: 200 },
    { duration: '28m', target: 200 },
    { duration: '2m', target: 0 },
  ],
  thresholds: {
    http_req_duration: ['p(95)<500'],
    http_req_failed: ['rate<0.05'],
  },
};

export default function () {
  const res1 = http.get(BASE + '/health');
  check(res1, { 'health 200': (r) => r.status === 200 });

  const res2 = http.get(BASE + '/metrics');
  check(res2, { 'metrics 200': (r) => r.status === 200 });

  if (Math.random() < 0.3) {
    const res3 = http.get(BASE + '/workers', { headers: { 'X-API-Key': API_KEY } });
    check(res3, { 'workers ok': (r) => r.status === 200 });
  }
}
