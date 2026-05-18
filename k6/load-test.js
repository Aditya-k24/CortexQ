import http from 'k6/http';
import { check, sleep } from 'k6';
import { Counter, Trend } from 'k6/metrics';

const queuedRequests = new Counter('queued_requests');
const inferLatency = new Trend('infer_latency_ms');
const failedRequests = new Counter('failed_requests');

const BASE_URL = __ENV.ROUTER_URL || 'http://localhost:8080';

export const options = {
  stages: [
    { duration: '30s', target: 10 },   // ramp up to 10 VUs
    { duration: '1m', target: 50 },    // ramp to 50 VUs — should trigger KEDA scale-up
    { duration: '2m', target: 100 },   // peak load
    { duration: '2m', target: 500 },   // stress load
    { duration: '1m', target: 50 },    // ramp down
    { duration: '30s', target: 0 },    // cool down — KEDA should scale back down
  ],
  thresholds: {
    http_req_duration: ['p(95)<2000'],   // 95% of requests under 2s
    http_req_failed: ['rate<0.05'],      // <5% error rate
    queued_requests: ['count>0'],        // at least some requests queued
  },
};

const MODELS = ['claude', 'gpt4', 'gemini'];

function randomModel() {
  return MODELS[Math.floor(Math.random() * MODELS.length)];
}

function randomPrompt() {
  const prompts = [
    'Explain Kubernetes autoscaling in simple terms.',
    'What are the benefits of using KEDA?',
    'Summarize the main features of FastAPI.',
    'How does a circuit breaker pattern work?',
    'Describe the GitOps methodology.',
    'What is Redis used for in a distributed system?',
    'Compare horizontal and vertical scaling.',
    'Explain the role of Prometheus in observability.',
  ];
  return prompts[Math.floor(Math.random() * prompts.length)];
}

export default function () {
  const payload = JSON.stringify({
    prompt: randomPrompt(),
    model: randomModel(),
    params: { temperature: 0.7 },
  });

  const params = {
    headers: { 'Content-Type': 'application/json' },
    timeout: '10s',
  };

  const start = Date.now();
  const res = http.post(`${BASE_URL}/infer`, payload, params);
  inferLatency.add(Date.now() - start);

  const ok = check(res, {
    'status is 200': (r) => r.status === 200,
    'response has job_id': (r) => {
      try { return JSON.parse(r.body).job_id !== undefined; } catch { return false; }
    },
    'response has model': (r) => {
      try { return JSON.parse(r.body).model !== undefined; } catch { return false; }
    },
  });

  if (ok) {
    queuedRequests.add(1);
  } else {
    failedRequests.add(1);
  }

  sleep(0.1);
}

export function setup() {
  // Verify router is reachable before running
  const res = http.get(`${BASE_URL}/health`);
  check(res, { 'router healthy': (r) => r.status === 200 });
}
