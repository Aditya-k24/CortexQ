import http from 'k6/http';
import { check, sleep } from 'k6';

const BASE_URL = __ENV.ROUTER_URL || 'http://localhost:8080';

export const options = {
  vus: 1,
  iterations: 20,
  thresholds: {
    http_req_failed: ['rate<0.01'],
    http_req_duration: ['p(99)<1000'],
  },
};

export default function () {
  // Health check
  let res = http.get(`${BASE_URL}/health`);
  check(res, {
    'health 200': (r) => r.status === 200,
    'health redis connected': (r) => {
      try { return JSON.parse(r.body).redis === 'connected'; } catch { return false; }
    },
  });

  // Models endpoint
  res = http.get(`${BASE_URL}/models`);
  check(res, {
    'models 200': (r) => r.status === 200,
    'models list non-empty': (r) => {
      try { return JSON.parse(r.body).models.length > 0; } catch { return false; }
    },
  });

  // Inference — specific model
  res = http.post(
    `${BASE_URL}/infer`,
    JSON.stringify({ prompt: 'Hello from smoke test', model: 'claude' }),
    { headers: { 'Content-Type': 'application/json' } },
  );
  check(res, {
    'infer 200': (r) => r.status === 200,
    'infer queued': (r) => {
      try { return JSON.parse(r.body).status === 'queued'; } catch { return false; }
    },
    'infer job_id present': (r) => {
      try { return typeof JSON.parse(r.body).job_id === 'string'; } catch { return false; }
    },
  });

  // Bad model
  res = http.post(
    `${BASE_URL}/infer`,
    JSON.stringify({ prompt: 'test', model: 'nonexistent' }),
    { headers: { 'Content-Type': 'application/json' } },
  );
  check(res, { 'unknown model 400': (r) => r.status === 400 });

  // Missing prompt
  res = http.post(
    `${BASE_URL}/infer`,
    JSON.stringify({ model: 'claude' }),
    { headers: { 'Content-Type': 'application/json' } },
  );
  check(res, { 'missing prompt 422': (r) => r.status === 422 });

  // Metrics
  res = http.get(`${BASE_URL}/metrics`);
  check(res, {
    'metrics 200': (r) => r.status === 200,
    'metrics prometheus format': (r) => r.body.includes('infer_requests_total'),
  });

  sleep(0.5);
}
