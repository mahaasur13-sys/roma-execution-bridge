import http from 'k6/http';
import { check, sleep } from 'k6';

const BASE = 'http://localhost:8900';

export const options = {
  stages: [
    { duration: '30s', target: 500 },
    { duration: '2m', target: 500 },
    { duration: '30s', target: 0 },
  ],
  thresholds: {
    http_req_duration: ['p(95)<2000'],
    http_req_failed: ['rate<0.25'],
  },
};

export default function () {
  const res = http.get(BASE + '/health');
  check(res, { 'health 200': (r) => r.status === 200 });
  const res2 = http.get(BASE + '/metrics');
  check(res2, { 'metrics 200': (r) => r.status === 200 });
}
