# KubeServe

Kubernetes-native LLM inference autoscaling platform. Routes inference requests across multiple model backends, autoscales pods based on Redis queue depth via KEDA, and exposes full OpenTelemetry/Prometheus/Grafana observability.

## Architecture

```
                        ┌─────────────────────────────────────────┐
  HTTP /infer  ──────►  │           FastAPI Router                │
                        │  • Round-robin / latency-aware LB       │
                        │  • Circuit breaker per backend          │
                        │  • Prometheus metrics + OTel traces     │
                        └───────────┬─────────────────────────────┘
                                    │ lpush
                     ┌──────────────▼──────────────────┐
                     │              Redis              │
                     │  claude-queue / gpt4-queue / …  │
                     └──┬──────────┬──────────┬────────┘
                        │          │          │ brpop
               ┌────────▼──┐  ┌────▼────┐  ┌─▼───────┐
               │  Backend  │  │ Backend │  │ Backend │
               │  (claude) │  │ (gpt4)  │  │(gemini) │
               └────────┬──┘  └────┬────┘  └───┬─────┘
                        │          │           │
                        └──────────┼───────────┘
                                   │ hset results
                              Redis results hash

  KEDA watches queue depth ──► scales Backend Deployments 0 → N
  Kopf Operator watches LLMDeployment CRDs ──► creates Deployments + ScaledObjects
```

## Components

| Component | Tech | Description |
|-----------|------|-------------|
| **Router** | FastAPI + Redis | Accepts `/infer`, routes to model queue, circuit-breaks on failure |
| **Operator** | Python + Kopf | Watches `LLMDeployment` CRDs, reconciles Deployments/Services/ScaledObjects |
| **Mock Backend** | FastAPI + Redis | Simulates LLM inference; polls queue, stores results |
| **Autoscaling** | KEDA Redis trigger | Scales model pods 0→N based on queue depth |
| **Observability** | OTel + Prometheus + Grafana | Traces, metrics, dashboards for latency/throughput/queue depth |
| **GitOps** | Helm + ArgoCD | Single `helm install` deploys everything; ArgoCD auto-syncs on git push |
| **Load Testing** | k6 | Smoke test + ramp-up load test to 500 VUs |

## Quick Start

### Prerequisites

- Docker + Docker Compose
- Python 3.11+ (for running tests locally)
- k6 (optional, for load testing)
- kubectl + helm (optional, for Kubernetes deploy)

### Local Development

```bash
# Clone and enter the repo
git clone https://github.com/Aditya-k24/KubeServe
cd KubeServe

# Start the full stack (Redis + Router + 3 model backends)
docker compose up -d redis router backend-claude backend-gpt4 backend-gemini

# Check router health
curl http://localhost:8080/health
# {"status":"healthy","redis":"connected"}

# Send an inference request
curl -s -X POST http://localhost:8080/infer \
  -H "Content-Type: application/json" \
  -d '{"prompt": "What is Kubernetes?", "model": "claude"}' | jq
# {"status":"queued","job_id":"...","model":"claude","queue":"claude-queue"}

# Poll for result
curl http://localhost:8080/result/<job_id>

# List models and circuit states
curl http://localhost:8080/models | jq

# Scrape Prometheus metrics
curl http://localhost:8080/metrics
```

### With Monitoring (Prometheus + Grafana)

```bash
docker compose --profile monitoring up -d
# Prometheus: http://localhost:9090
# Grafana:    http://localhost:3000  (anonymous access, no login)
```

## API Reference

### `POST /infer`
Queue an inference request.

**Request body:**
```json
{
  "prompt": "string (required)",
  "model": "claude | gpt4 | gemini  (optional, auto-selected if omitted)",
  "params": {}
}
```

**Response:**
```json
{
  "status": "queued",
  "job_id": "uuid",
  "model": "claude",
  "queue": "claude-queue"
}
```

### `GET /result/{job_id}`
Poll for job result. Returns `202 Pending` until the backend processes it.

### `GET /health`
Redis connectivity check.

### `GET /metrics`
Prometheus metrics endpoint. Key metrics:

| Metric | Type | Description |
|--------|------|-------------|
| `infer_requests_total` | Counter | Requests by model + status |
| `infer_request_duration_seconds` | Histogram | End-to-end latency |
| `llm_queue_length` | Gauge | Current Redis queue depth per model |
| `circuit_breaker_state` | Gauge | 0=closed, 1=open, 2=half_open |

### `GET /models`
List registered models, circuit states, and EWMA latencies.

## Testing

```bash
# Router unit tests (57 tests)
cd router && pytest tests/ -v

# Operator unit tests (31 tests)
cd operator && pytest tests/ -v

# Both via Makefile
make test
```

Test coverage:
- **Circuit breaker:** all state transitions (closed→open→half_open→closed, fallback routing, all-open 503)
- **Load balancer:** round-robin cycling, latency-aware selection, model add/remove, EWMA convergence
- **Router endpoints:** /infer routing, queue payload verification, metrics registration, result polling
- **Operator:** Deployment/Service/ScaledObject manifest generation, create/update/delete handlers, conflict resolution

## Kubernetes Deploy

### Prerequisites

```bash
# Install KEDA
helm repo add kedacore https://kedacore.github.io/charts
helm install keda kedacore/keda --namespace keda --create-namespace

# Install Prometheus Operator (optional, for ServiceMonitors)
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm install kube-prometheus prometheus-community/kube-prometheus-stack -n monitoring --create-namespace
```

### Deploy KubeServe

```bash
# Apply CRD first
kubectl apply -f manifests/crd-llmdeployment.yaml

# Deploy via Helm
helm install kubeserve helm/kubeserve-chart/ \
  --namespace kubeserve \
  --create-namespace \
  --set redis.host=redis-master

# Create LLM model deployments
kubectl apply -f manifests/example-llmdeployment.yaml

# Watch pods scale up
kubectl get pods -n kubeserve -w
```

### GitOps with ArgoCD

```bash
# Install ArgoCD
kubectl create namespace argocd
kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml

# Enable ArgoCD app in Helm values
helm upgrade kubeserve helm/kubeserve-chart/ \
  --set argocd.enabled=true \
  --set argocd.repoURL=https://github.com/Aditya-k24/KubeServe
```

ArgoCD will auto-sync on every `git push` to `main`.

## Custom Resource: LLMDeployment

```yaml
apiVersion: kubeserve.io/v1alpha1
kind: LLMDeployment
metadata:
  name: claude
  namespace: kubeserve
spec:
  model: claude          # model name (used as Redis queue key)
  provider: anthropic    # provider (determines container image mapping)
  minReplicas: 0         # 0 = scale-to-zero when idle
  maxReplicas: 20        # KEDA upper bound
  queueThreshold: 5      # scale up when queue length exceeds this
```

The Operator reconciles this into:
- `Deployment` with Redis-polling backend container
- `Service` (ClusterIP on port 80, metrics on 9090)
- KEDA `ScaledObject` targeting the Deployment with Redis list trigger

## Load Testing

```bash
# Smoke test (20 iterations, 1 VU)
k6 run --env ROUTER_URL=http://localhost:8080 k6/smoke-test.js

# Full load test (ramps 10 → 50 → 100 → 500 VUs over ~7 minutes)
k6 run --env ROUTER_URL=http://localhost:8080 k6/load-test.js
```

The load test verifies KEDA scale-up: as queue depth grows, KEDA triggers new pod replicas. Watch with:

```bash
kubectl get hpa -n kubeserve -w
kubectl top pods -n kubeserve
```

## Project Structure

```
KubeServe/
├── router/
│   ├── main.py              FastAPI app (lifespan, endpoints, background enqueue)
│   ├── circuit_breaker.py   3-state circuit breaker (closed/open/half_open)
│   ├── load_balancer.py     Round-robin + EWMA latency-aware selection
│   ├── metrics.py           Prometheus collector registry
│   ├── Dockerfile
│   └── tests/               57 pytest tests
├── operator/
│   ├── main.py              Kopf operator — CRD handlers + manifest builders
│   ├── Dockerfile
│   └── tests/               31 pytest tests
├── mock-backend/
│   ├── main.py              Async Redis worker + FastAPI health/metrics
│   └── Dockerfile
├── helm/kubeserve-chart/
│   ├── Chart.yaml
│   ├── values.yaml
│   └── templates/           Namespace, CRD, RBAC, Operator, Router, ServiceMonitors, ArgoCD app
├── manifests/               Raw YAML for direct kubectl apply
├── k6/                      smoke-test.js, load-test.js
├── monitoring/              Prometheus config, Grafana provisioning
└── docker-compose.yml       Local dev stack
```

## Configuration

All router settings are environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `REDIS_HOST` | `localhost` | Redis hostname |
| `REDIS_PORT` | `6379` | Redis port |
| `MODELS` | `claude,gpt4,gemini` | Comma-separated model names |
| `LB_STRATEGY` | `round_robin` | `round_robin` or `latency_aware` |
| `CB_FAILURE_THRESHOLD` | `5` | Failures before circuit opens |
| `CB_SUCCESS_THRESHOLD` | `2` | Successes before circuit closes from half-open |
| `CB_TIMEOUT` | `30` | Seconds before open circuit retries |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | _(disabled)_ | OTLP gRPC endpoint for traces |

## Grafana Dashboard Queries

```promql
# p95 request latency
histogram_quantile(0.95, sum by (le, model) (rate(infer_request_duration_seconds_bucket[5m])))

# Throughput (req/s per model)
rate(infer_requests_total[1m])

# Queue depth
llm_queue_length

# Circuit breaker state
circuit_breaker_state
```
