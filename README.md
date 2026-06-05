# KubeServe

Kubernetes-native LLM inference routing platform. Accepts inference requests, queues them in Redis per model, and autoscales backend pods based on queue depth using KEDA. A custom Kubernetes operator reconciles `LLMDeployment` CRDs into the full stack automatically.

## How it works

  POST /infer
      │
      ▼
  FastAPI Router  ──── circuit breaker + load balancer ────►  Redis queue (per model)
      │                                                              │
      │  returns job_id immediately                                  │ brpop
      │                                                              ▼
  GET /result/{id}  ◄──────────────────────────────────  Backend worker pod
                              hset results hash              (simulates inference)

  KEDA watches LLEN(queue) ──► scales backend pods 0 → N
  Kopf operator watches LLMDeployment CRDs ──► creates Deployment + Service + ScaledObject
```

**Router** — FastAPI service that accepts `/infer`, selects a model via round-robin or latency-aware load balancing, checks a per-model circuit breaker, and pushes the job to Redis as a background task. Returns a `job_id` in under 5ms.

**Backend** — Workers that block on `BRPOP` from their model's Redis queue, simulate inference, and write the result to a Redis hash. Scaled 0→N by KEDA.

**Operator** — Kopf-based controller. Apply a single `LLMDeployment` CR and it creates the Deployment, Service, and KEDA ScaledObject for that model automatically.

**KEDA** — Watches Redis list length per model. Scales the backend deployment up when queue depth exceeds a threshold, back to zero when idle.

## Stack

| Layer | Technology |
|---|---|
| Router | Python, FastAPI, redis-py async |
| Operator | Python, Kopf, kubernetes-client |
| Autoscaling | KEDA (Redis list scaler) |
| Observability | Prometheus, Grafana, OpenTelemetry |
| Packaging | Helm chart, ArgoCD |
| Local dev | Docker Compose |
| Load testing | k6 |

## Quick start (local)

Requires Docker.

```bash
git clone https://github.com/Aditya-k24/CortexQ
cd KubeServe

docker compose up -d redis router backend-claude backend-gpt4 backend-gemini
```

```bash
# Health check
curl http://localhost:8080/health

# Queue an inference job
curl -s -X POST http://localhost:8080/infer \
  -H "Content-Type: application/json" \
  -d '{"prompt": "What is Kubernetes?", "model": "claude"}'
# → {"status":"queued","job_id":"<uuid>","model":"claude","queue":"claude-queue"}

# Poll for result
curl http://localhost:8080/result/<job_id>

# List models + circuit states
curl http://localhost:8080/models
```

Add monitoring:

```bash
docker compose --profile monitoring up -d
# Prometheus → http://localhost:9090
# Grafana    → http://localhost:3000
```

## Kubernetes deploy

```bash
# Install KEDA
helm repo add kedacore https://kedacore.github.io/charts
helm install keda kedacore/keda --namespace keda --create-namespace

# Apply the CRD
kubectl apply -f manifests/crd-llmdeployment.yaml

# Deploy
helm install kubeserve helm/kubeserve-chart/ \
  --namespace kubeserve --create-namespace \
  --set monitoring.enabled=false

# Create model deployments
kubectl apply -f manifests/example-llmdeployment.yaml

# Watch KEDA scale pods as queue fills
kubectl get hpa -n kubeserve -w
```

## LLMDeployment CRD

```yaml
apiVersion: kubeserve.io/v1alpha1
kind: LLMDeployment
metadata:
  name: claude
  namespace: kubeserve
spec:
  model: claude
  provider: anthropic
  minReplicas: 0        # scale to zero when idle
  maxReplicas: 20
  queueThreshold: 5     # one pod per 5 queued jobs
```

The operator reconciles this into a Deployment, ClusterIP Service, and KEDA ScaledObject. Deleting the CR garbage-collects all three via `ownerReferences`.

## API

| Endpoint | Description |
|---|---|
| `POST /infer` | Queue an inference job. Body: `{prompt, model?, params?}` |
| `GET /result/{job_id}` | Poll for result. `202` while pending, `200` when complete |
| `GET /models` | List models, circuit states, EWMA latencies |
| `GET /health` | Redis connectivity check |
| `GET /metrics` | Prometheus metrics |

## Tests

```bash
make test          # 88 tests total
cd router && pytest tests/ -v    # 57 tests
cd operator && pytest tests/ -v  # 31 tests
```

Covers circuit breaker state transitions, load balancer EWMA convergence, router endpoint behavior, and operator manifest generation.

## Configuration

Router is configured via environment variables:

| Variable | Default | Description |
|---|---|---|
| `MODELS` | `claude,gpt4,gemini` | Comma-separated model names |
| `LB_STRATEGY` | `round_robin` | `round_robin` or `latency_aware` |
| `REDIS_HOST` | `localhost` | Redis hostname |
| `CB_FAILURE_THRESHOLD` | `5` | Failures before circuit opens |
| `CB_TIMEOUT` | `30` | Seconds before open circuit retries |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | — | OTLP endpoint for traces (disabled if unset) |

## Project structure

```
router/          FastAPI router, circuit breaker, load balancer, metrics
operator/        Kopf operator — CRD handlers and manifest builders
mock-backend/    Redis queue worker simulating LLM inference
helm/            Helm chart (CRD, RBAC, operator, router, ServiceMonitors)
manifests/       Raw YAML for kubectl apply
k6/              Smoke test and load test scripts
monitoring/      Prometheus config, Grafana provisioning
docker-compose.yml
```
