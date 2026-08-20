import http from 'k6/http';
import { check, sleep } from 'k6';

const BASE = 'http://localhost:8900';
const API_KEY = __ENV.ROMA_API_KEY;
const PAYLOAD = JSON.stringify({
  task: 'load-test-' + Date.now(),
  gpu_required: false,
  priority: 5,
  execution_mode: 'k8s_job',
  backend: 'local',
  instance_type: 'any'
});

export const options = {
  stages: [
    { duration: '1m', target: 100 },
    { duration: '2m', target: 100 },
    { duration: '1m', target: 0 },
  ],
  thresholds: {
    http_req_duration: ['p(95)<500'],
    http_req_failed: ['rate<1.00'],
  },
};

export default function () {
  const res = http.post(BASE + '/submit',
    JSON.stringify({
      task: 'load-test-' + Date.now(),
      gpu_required: false,
      priority: 5,
      execution_mode: 'k8s_job',
      backend: 'local',
      instance_type: 'any'
    }),
    { headers: { 'Content-Type': 'application/json', 'X-API-Key': API_KEY } }
  );
  check(res, {
    'submit 202 or 402': (r) => r.status === 202 || r.status === 402,
  });
}
