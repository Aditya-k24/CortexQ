<div align="center">

# CortexQ

**Kubernetes-native LLM inference routing with event-driven autoscaling**

[![Tests](https://img.shields.io/badge/tests-88%20passing-brightgreen)](#testing)
[![Python](https://img.shields.io/badge/python-3.12-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Kubernetes](https://img.shields.io/badge/kubernetes-1.30%2B-326CE5?logo=kubernetes&logoColor=white)](https://kubernetes.io/)
[![KEDA](https://img.shields.io/badge/KEDA-2.x-purple)](https://keda.sh/)

Queue inference requests across multiple LLM backends. Scale pods from zero based on Redis queue depth. One custom resource deploys everything.

</div>

---

## How it works

```
  POST /infer
      │
      ▼
┌─────────────────────────────────────────────────────┐
│  FastAPI Router                                      │
│  ├─ Round-robin or latency-aware load balancer       │
│  ├─ Per-model circuit breaker (closed/open/half-open)│
│  └─ Prometheus metrics + OpenTelemetry traces        │
└──────────────────────────┬──────────────────────────┘
                           │  lpush  (returns job_id in <5ms)
                           ▼
              ┌────────────────────────┐
              │  Redis                 │
              │  claude-queue          │◄── KEDA watches LLEN
              │  gpt4-queue            │
              │  gemini-queue          │
              └──┬─────────┬──────────┘
          brpop  │         │  brpop
                 ▼         ▼
          [claude pod] [gpt4 pod]  ← scaled 0→N by KEDA
                 │         │
                 └────┬────┘
                      │  hset results
                      ▼
              Redis results hash
                      │
                      ▼
              GET /result/{job_id}
```

A **Kopf operator** watches `LLMDeployment` custom resources and reconciles each one into a `Deployment` + `Service` + KEDA `ScaledObject`. Delete the CR and Kubernetes garbage-collects everything via owner references.

---

## Stack

| Layer | Technology |
|---|---|
| Router | Python · FastAPI · redis-py async |
| Operator | Python · Kopf · kubernetes-client |
| Autoscaling | KEDA — Redis list length trigger |
| Observability | Prometheus · Grafana · OpenTelemetry |
| Packaging | Helm · ArgoCD |
| Local dev | Docker Compose |
| Load testing | k6 |

---

## Quick Start

> **Requires:** Docker

```bash
git clone https://github.com/Aditya-k24/CortexQ
cd CortexQ
docker compose up -d redis router backend-claude backend-gpt4 backend-gemini
```

```bash
# Health check
curl http://localhost:8080/health
# {"status":"healthy","redis":"connected"}

# Queue an inference job
curl -s -X POST http://localhost:8080/infer \
  -H "Content-Type: application/json" \
  -d '{"prompt": "What is Kubernetes?", "model": "claude"}'
# {"status":"queued","job_id":"<uuid>","model":"claude","queue":"claude-queue"}

# Poll for result
curl http://localhost:8080/result/<job_id>

# List models, circuit states, EWMA latencies
curl http://localhost:8080/models
```

**With monitoring:**

```bash
docker compose --profile monitoring up -d
# Prometheus → http://localhost:9090
# Grafana    → http://localhost:3000
```

---

## Kubernetes Deploy

> **Requires:** `kubectl` · `helm` · a running cluster

**1. Install KEDA**
```bash
helm repo add kedacore https://kedacore.github.io/charts
helm install keda kedacore/keda --namespace keda --create-namespace
```

**2. Apply the CRD and deploy**
```bash
kubectl apply -f manifests/crd-llmdeployment.yaml

helm install cortexq helm/kubeserve-chart/ \
  --namespace cortexq --create-namespace \
  --set monitoring.enabled=false
```

**3. Create model deployments**
```bash
kubectl apply -f manifests/example-llmdeployment.yaml
```

**4. Watch KEDA autoscale**
```bash
kubectl get hpa -n cortexq -w
kubectl get pods -n cortexq -w
```

---

## LLMDeployment CRD

Apply one resource. The operator handles the rest.

```yaml
apiVersion: kubeserve.io/v1alpha1
kind: LLMDeployment
metadata:
  name: claude
  namespace: cortexq
spec:
  model: claude
  provider: anthropic
  minReplicas: 0       # scale-to-zero when idle
  maxReplicas: 20
  queueThreshold: 5    # 1 pod per 5 queued jobs
```

The operator creates:
- `Deployment` — Redis-polling backend container
- `Service` — ClusterIP (port 80, metrics on 9090)
- `ScaledObject` — KEDA trigger watching `{model}-queue` list length

---

## API Reference

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/infer` | Queue a job. Body: `{prompt, model?, params?}` |
| `GET` | `/result/{job_id}` | `202` while pending · `200` when complete |
| `GET` | `/models` | Models, circuit states, EWMA latencies |
| `GET` | `/health` | Redis connectivity check |
| `GET` | `/metrics` | Prometheus metrics |

---

## Testing

```bash
make test                        # 88 tests
cd router && pytest tests/ -v    # 57 — router, circuit breaker, load balancer
cd operator && pytest tests/ -v  # 31 — operator handlers, manifest builders
```

Coverage includes circuit breaker state machine transitions, EWMA latency convergence, circuit fallback routing, and operator idempotency under 409 conflicts.

---

## Configuration

| Variable | Default | Description |
|---|---|---|
| `MODELS` | `claude,gpt4,gemini` | Comma-separated model names |
| `LB_STRATEGY` | `round_robin` | `round_robin` or `latency_aware` |
| `REDIS_HOST` | `localhost` | Redis hostname |
| `REDIS_PORT` | `6379` | Redis port |
| `CB_FAILURE_THRESHOLD` | `5` | Failures before circuit opens |
| `CB_SUCCESS_THRESHOLD` | `2` | Successes to close from half-open |
| `CB_TIMEOUT` | `30` | Seconds before retrying an open circuit |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | — | OTLP gRPC endpoint (tracing disabled if unset) |

---

## Project Structure

```
CortexQ/
├── router/             FastAPI router — load balancer, circuit breaker, metrics
│   └── tests/          57 pytest tests
├── operator/           Kopf operator — CRD handlers, manifest builders
│   └── tests/          31 pytest tests
├── mock-backend/       Async Redis queue worker (simulates LLM inference)
├── helm/               Helm chart — CRD, RBAC, operator, router, ServiceMonitors
├── manifests/          Raw YAML for kubectl apply
├── k6/                 Smoke test + load test (ramps to 500 VUs)
├── monitoring/         Prometheus config, Grafana provisioning
└── docker-compose.yml  Local dev stack
```

---

<div align="center">

Built with Python · FastAPI · Kopf · KEDA · Helm

</div>
