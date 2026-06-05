# KubeServe: Deep Concept Reference

Every concept in the codebase explained from first principles, mapped to exact file locations.

---

## Table of Contents

1. [The Big Picture — What Problem This Solves](#1-the-big-picture)
2. [Kubernetes Fundamentals](#2-kubernetes-fundamentals)
3. [Custom Resource Definitions (CRDs)](#3-custom-resource-definitions)
4. [The Kopf Operator Pattern](#4-the-kopf-operator-pattern)
5. [KEDA — Kubernetes Event-Driven Autoscaling](#5-keda)
6. [FastAPI + Async Python](#6-fastapi--async-python)
7. [Redis as a Message Queue](#7-redis-as-a-message-queue)
8. [Circuit Breaker Pattern](#8-circuit-breaker-pattern)
9. [Load Balancing — Round Robin and EWMA](#9-load-balancing)
10. [Prometheus Metrics](#10-prometheus-metrics)
11. [OpenTelemetry Distributed Tracing](#11-opentelemetry-distributed-tracing)
12. [Helm — Kubernetes Package Manager](#12-helm)
13. [ArgoCD — GitOps](#13-argocd--gitops)
14. [Docker Compose — Local Dev Stack](#14-docker-compose)
15. [k6 — Load Testing](#15-k6--load-testing)
16. [Full Request Lifecycle (end-to-end walkthrough)](#16-full-request-lifecycle)

---

## 1. The Big Picture

### The Problem

Calling an LLM API is slow (seconds, not milliseconds). If you call it synchronously inside a web request:

```
User → POST /infer → [wait 3 seconds for GPT-4] → response
```

Your web server is blocked for 3 seconds. With 100 concurrent users, you need 100 threads all sitting idle waiting. That doesn't scale.

### The Solution: Async Queue-Based Architecture

Instead:

```
User → POST /infer → [push job to Redis queue] → immediately return job_id
User → GET /result/{job_id} → [poll until backend finishes] → get response
```

The router returns in <5ms. Backend workers consume from the queue at their own pace. This decouples request acceptance from request processing — the core idea behind every high-throughput inference system.

### Why Kubernetes?

LLM inference has spiky demand. At 3am nobody calls Claude. At 9am everyone does. Running 20 GPU pods at 3am wastes thousands of dollars. KEDA watches the Redis queue length and scales pods from 0 to 20 based on actual demand. Kubernetes handles scheduling those pods onto machines, health checking them, restarting failures.

---

## 2. Kubernetes Fundamentals

### What Kubernetes Actually Is

Kubernetes (k8s) is a control loop system. Every component watches some state and reconciles reality toward desired state. You declare "I want 3 pods running image X" and Kubernetes makes that true, keeps it true, and repairs it when something breaks.

### Core Objects Used in KubeServe

**Deployment** (`operator/main.py:32-84`)  
Declares a desired number of Pods running a container image. Built by `build_deployment()`:
```python
"kind": "Deployment",
"spec": {
    "replicas": min_replicas,          # how many pods
    "selector": {"matchLabels": ...},  # which pods belong to this deployment
    "template": { ... }                # pod spec (image, env vars, resources)
}
```
When you apply this, Kubernetes creates a **ReplicaSet** which creates actual **Pods**. If a pod crashes, ReplicaSet creates a replacement.

**Service** (`operator/main.py:87-105`)  
A stable DNS name + IP address for a set of pods. Pods come and go (new IPs each time), but the Service address stays constant. Built by `build_service()`:
```python
"kind": "Service",
"spec": {
    "selector": {"app": f"llm-{name}"},   # routes to pods with this label
    "ports": [{"port": 80, "targetPort": 8000}],
    "type": "ClusterIP",                  # only accessible inside cluster
}
```
`ClusterIP` means internal-only. Other types: `NodePort` (expose on node IP), `LoadBalancer` (cloud LB).

**Namespace**  
Logical isolation within a cluster. All KubeServe resources live in the `kubeserve` namespace. Prevents naming collisions with other apps.

**ConfigMap / Secret**  
Key-value stores injected as env vars or files. Redis host, model names, etc. come in as env vars in the Deployment template.

**RBAC (Role-Based Access Control)**  
The operator needs permission to create Deployments, Services, etc. The Helm chart creates a `ServiceAccount`, `ClusterRole` (list of allowed verbs + resources), and `ClusterRoleBinding` (grants that role to the ServiceAccount). Without RBAC, the operator's API calls return 403.

### The Kubernetes API

Everything in Kubernetes is an HTTP API call. `kubectl apply -f foo.yaml` is just a `PUT` to the API server. The operator uses the Python `kubernetes` client to make these same calls:
```python
apps.create_namespaced_deployment(namespace, deployment)  # POST /apis/apps/v1/namespaces/{ns}/deployments
core.create_namespaced_service(namespace, service)         # POST /api/v1/namespaces/{ns}/services
```

---

## 3. Custom Resource Definitions

### What a CRD Is

Kubernetes ships with built-in resource types (Deployment, Service, Pod...). CRDs let you **invent new resource types**. You define a schema, Kubernetes stores and validates instances of it, and your operator reacts to create/update/delete events.

### LLMDeployment CRD (`manifests/crd-llmdeployment.yaml`)

```yaml
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: llmdeployments.kubeserve.io
spec:
  group: kubeserve.io
  versions:
    - name: v1alpha1
      schema:
        openAPIV3Schema:
          properties:
            spec:
              properties:
                model:          { type: string }
                provider:       { type: string }
                minReplicas:    { type: integer, minimum: 0 }
                maxReplicas:    { type: integer, minimum: 1 }
                queueThreshold: { type: integer, minimum: 1 }
```

Once this CRD is applied to the cluster, you can do:
```bash
kubectl apply -f manifests/example-llmdeployment.yaml
kubectl get llmdeployments
kubectl describe llmdeployment claude
```

Kubernetes validates the spec against the OpenAPIV3 schema and stores it in etcd (its distributed key-value store). Your operator gets an event notification.

### Why This Design

Without CRDs, deploying a new LLM backend means manually creating a Deployment + Service + ScaledObject — three separate YAML files, easy to get out of sync. With the CRD, a single `LLMDeployment` resource is the source of truth. The operator handles all the downstream reconciliation automatically.

---

## 4. The Kopf Operator Pattern

### What an Operator Is

An operator is a program that runs inside Kubernetes and extends its control loop behavior. It watches your custom resources and takes actions — the same way the built-in Deployment controller watches Deployment resources and creates ReplicaSets.

**Kopf** (Kubernetes Operator Pythonic Framework) is a Python library that handles the plumbing: connecting to the Kubernetes watch API, receiving events, calling your handler functions, managing retries and finalizers.

### Handler Registration (`operator/main.py:164-263`)

```python
@kopf.on.create("kubeserve.io", "v1alpha1", "llmdeployments")
def create_llm_deployment(spec, name, namespace, logger, **kwargs):
    ...

@kopf.on.update("kubeserve.io", "v1alpha1", "llmdeployments")
def update_llm_deployment(spec, name, namespace, logger, **kwargs):
    ...

@kopf.on.delete("kubeserve.io", "v1alpha1", "llmdeployments")
def delete_llm_deployment(name, namespace, logger, **kwargs):
    ...

@kopf.on.field("kubeserve.io", "v1alpha1", "llmdeployments", field="spec.minReplicas")
def scale_min_replicas(spec, name, namespace, logger, **kwargs):
    ...
```

Kopf uses Kubernetes **watch streams** — a long-lived HTTP connection that streams events as they happen. When you `kubectl apply` a new `LLMDeployment`, the API server emits a `ADDED` event, Kopf receives it, calls `create_llm_deployment`.

### What the Create Handler Does (`operator/main.py:165-214`)

1. Builds three manifest dicts (Deployment, Service, ScaledObject) from the CRD spec
2. Calls `kopf.adopt()` on each — this sets `ownerReferences` in the manifest metadata, linking them to the parent `LLMDeployment`
3. Creates each resource via the Kubernetes API
4. Handles 409 Conflict (resource already exists) by patching instead of creating

**ownerReferences** is crucial. When the `LLMDeployment` is deleted, Kubernetes garbage-collects all resources with `ownerReferences` pointing to it. The operator's delete handler is essentially empty because Kubernetes handles cleanup automatically:
```python
@kopf.on.delete(...)
def delete_llm_deployment(name, namespace, logger, **kwargs):
    # Kubernetes GC handles owned resources via ownerReferences set by kopf.adopt()
    pass
```

### Idempotency and 409 Handling

Network partitions, pod restarts, and operator crashes mean handlers can run multiple times for the same event. The operator must be **idempotent** — running twice has the same result as running once:
```python
try:
    apps.create_namespaced_deployment(namespace, deployment)
except ApiException as e:
    if e.status == 409:           # already exists from a previous run
        apps.patch_namespaced_deployment(...)   # update it instead
    else:
        raise kopf.PermanentError(...)  # unrecoverable — don't retry
```

`kopf.PermanentError` tells Kopf "don't retry this." Regular exceptions trigger automatic retry with backoff.

### Field-Level Handlers

```python
@kopf.on.field(..., field="spec.minReplicas")
def scale_min_replicas(spec, name, namespace, **kwargs):
    patch = {"spec": {"replicas": spec.get("minReplicas", 1)}}
    apps.patch_namespaced_deployment(f"llm-{name}-deployment", namespace, patch)
```

This fires only when `spec.minReplicas` changes, not on every update. Fine-grained reaction to specific field changes.

### Operator Startup (`operator/main.py:153-161`)

```python
@kopf.on.startup()
def configure(settings, **kwargs):
    settings.persistence.finalizer = "kubeserve.io/finalizer"
    try:
        kubernetes.config.load_incluster_config()   # running inside k8s pod
    except kubernetes.config.ConfigException:
        kubernetes.config.load_kube_config()         # running locally (uses ~/.kube/config)
```

In-cluster config reads the service account token mounted at `/var/run/secrets/kubernetes.io/serviceaccount/`. Local config reads `~/.kube/config` (same file `kubectl` uses).

---

## 5. KEDA

### The Problem KEDA Solves

Kubernetes' built-in **HorizontalPodAutoscaler (HPA)** scales on CPU/memory. But LLM inference throughput is driven by queue depth, not CPU. A pod sitting idle waiting for jobs uses 0% CPU — HPA would scale it down even if 1000 jobs are waiting.

KEDA (Kubernetes Event-Driven Autoscaler) extends HPA with external trigger sources: Redis list length, Kafka lag, SQS depth, Prometheus queries, etc.

### ScaledObject (`operator/main.py:108-138`)

```python
{
    "apiVersion": "keda.sh/v1alpha1",
    "kind": "ScaledObject",
    "spec": {
        "scaleTargetRef": {"name": f"llm-{name}-deployment"},  # what to scale
        "minReplicaCount": 0,        # scale to zero when idle
        "maxReplicaCount": 20,       # hard cap
        "cooldownPeriod": 300,       # wait 5 min before scaling down
        "triggers": [{
            "type": "redis",
            "metadata": {
                "address": "redis-master:6379",
                "listName": "claude-queue",   # watch this Redis list
                "listLength": "5",            # target: 5 jobs per pod
            }
        }]
    }
}
```

**How KEDA computes replica count:**  
`desired_replicas = ceil(queue_length / listLength)`  
Queue has 50 jobs, listLength=5 → 10 pods. Queue empties → KEDA waits `cooldownPeriod` (300s) then scales to `minReplicaCount` (0).

**Scale to zero:** `minReplicaCount: 0` means when queue is empty, KEDA scales the deployment to 0 pods. Zero cost at idle. When a job arrives, KEDA detects queue_length > 0 and scales back up. Cold start latency (pod boot time) is the tradeoff.

### How KEDA Works Internally

KEDA installs two components in the cluster:
- **keda-operator**: watches ScaledObjects, drives scaling decisions
- **keda-metrics-apiserver**: exposes external metrics to the Kubernetes metrics API

The HPA (which KEDA manages on your behalf) polls the metrics API. KEDA's metrics server queries Redis (`LLEN claude-queue`) and returns the current length. HPA computes desired replicas and updates the Deployment.

---

## 6. FastAPI + Async Python

### Why FastAPI

FastAPI is built on **Starlette** (ASGI framework) and uses Python's `async`/`await` for non-blocking I/O. For an inference router that spends most of its time waiting on Redis, this matters enormously.

**Sync model:** Each request needs a thread. Thread pool size limits concurrency (~100-1000).  
**Async model:** Single thread handles thousands of concurrent requests by yielding while waiting for I/O.

### Lifespan Context Manager (`router/main.py:56-75`)

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # STARTUP: runs once when server starts
    app_state["redis"] = await aioredis.from_url(redis_url, decode_responses=True)
    app_state["load_balancer"] = LoadBalancer(models, strategy=LB_STRATEGY)
    app_state["circuit_breakers"] = {m: CircuitBreaker(m, ...) for m in models}
    ACTIVE_MODELS.set(len(models))
    
    yield  # <-- server runs here, handling requests
    
    # SHUTDOWN: runs once when server stops
    await app_state["redis"].close()
```

`yield` inside `@asynccontextmanager` splits the function into startup (before yield) and shutdown (after yield). This replaces the older `@app.on_event("startup")` pattern. Ensures clean resource cleanup on graceful shutdown.

`app_state` is a plain dict used as global state. FastAPI has no built-in state container, so this pattern (module-level dict) is idiomatic for sharing connections across requests.

### Background Tasks (`router/main.py:181-203`)

```python
@app.post("/infer")
async def infer(body: InferRequest, background_tasks: BackgroundTasks):
    ...
    background_tasks.add_task(_enqueue, queue_name, payload, model, cb, start)
    return InferResponse(status="queued", ...)   # returns IMMEDIATELY
```

`BackgroundTasks` schedules `_enqueue` to run after the response is sent to the client. The client gets the response in <5ms. `_enqueue` runs asynchronously, pushing to Redis, recording latency, updating metrics. If enqueue fails, the client already got "queued" — this is an eventual consistency tradeoff by design.

### Pydantic Validation (`router/main.py:40-50`)

```python
class InferRequest(BaseModel):
    prompt: str = Field(..., min_length=1)       # required, non-empty
    model: Optional[str] = Field(None)           # optional
    params: Dict[str, Any] = Field(default_factory=dict)
```

FastAPI automatically validates the request body against `InferRequest`. Wrong type → 422 Unprocessable Entity, no handler code needed. `Field(..., min_length=1)` means required (`...` = Ellipsis = no default) with minimum length 1.

### Async Redis Client (`router/main.py:9,60`)

```python
import redis.asyncio as aioredis
app_state["redis"] = await aioredis.from_url(redis_url, decode_responses=True)
```

`redis.asyncio` is the async version of the redis-py client. All operations (`lpush`, `brpop`, `hget`, `ping`) are coroutines — they yield to the event loop while waiting for network I/O. `decode_responses=True` means Redis bytes are automatically decoded to Python strings.

---

## 7. Redis as a Message Queue

### Redis Data Structures Used

**List** (the queue):  
```
claude-queue: [job3, job2, job1]  ← left end (head)
```
- `LPUSH claude-queue <json>` — push to left (head). Router does this.
- `BRPOP claude-queue 2` — blocking pop from right (tail). Backend does this.
- `LLEN claude-queue` — current length. KEDA watches this.

LPUSH + BRPOP = FIFO queue (first in, first out). Items pushed to left, popped from right.

**Hash** (results store):  
```
results: { "job-uuid-1": "{...json...}", "job-uuid-2": "{...json...}" }
```
- `HSET results <job_id> <json>` — backend stores completed result
- `HGET results <job_id>` — router returns to polling client

### The Enqueue Flow (`router/main.py:187-203`)

```python
async def _enqueue(queue_name, payload, model, cb, start):
    await redis_client.lpush(queue_name, json.dumps(payload))
    elapsed = time.time() - start
    cb.record_success()
    lb.record_latency(model, elapsed)
    depth = await redis_client.llen(queue_name)
    QUEUE_DEPTH.labels(model=model).set(depth)
```

`payload` is a JSON-serialized dict:
```json
{
    "job_id": "uuid",
    "model": "claude",
    "prompt": "What is Kubernetes?",
    "params": {},
    "enqueued_at": 1717200000.0
}
```

### The Worker Loop (`mock-backend/main.py:64-111`)

```python
async def worker_loop():
    while state.get("running", False):
        item = await redis_client.brpop(QUEUE_NAME, timeout=2)
        if item is None:    # timeout expired, no jobs
            continue
        _, raw = item       # BRPOP returns (list_name, value)
        job = json.loads(raw)
        
        output = await simulate_inference(job["prompt"])
        result = {"job_id": ..., "output": output, "status": "completed"}
        
        await redis_client.hset("results", job_id, json.dumps(result))
```

`BRPOP` with `timeout=2` blocks for up to 2 seconds. If no item arrives, returns `None` and the loop continues. This is efficient — the worker doesn't spin-poll. When an item arrives, Redis wakes the blocked client immediately.

`simulate_inference` uses `asyncio.sleep(random.uniform(0.05, 0.5))` — simulates 50-500ms LLM latency without blocking the event loop. A real backend would call `openai.ChatCompletion.create(...)` here.

### Why Not HTTP Between Router and Backend?

Direct HTTP: router calls `POST backend/infer`, waits for response. Problems:
- Backend overload → slow responses → router accumulates waiting requests
- Backend crash → router requests fail immediately
- No buffering — peak demand hits backend directly

Queue-based: router pushes to Redis, returns immediately. Backend consumes at its own pace. Benefits:
- Natural rate limiting — backend only processes what it can handle
- Buffering — peak traffic fills queue rather than crashing backend
- Durability — Redis persists queue if backend crashes (jobs not lost)
- KEDA scaling — queue depth drives pod count automatically

---

## 8. Circuit Breaker Pattern

### The Problem

If a backend is down and the router keeps sending it requests, those requests fail immediately. Without protection, cascading failure: upstream service floods failing backend with retries, making it harder to recover.

### Three States (`router/circuit_breaker.py`)

```
                    failures >= threshold
    CLOSED ──────────────────────────────────► OPEN
      ▲                                          │
      │  successes >= success_threshold          │ timeout elapsed
      │                                          ▼
      └──────────────────────────────────── HALF_OPEN
```

**CLOSED** (normal operation):  
- All requests pass through
- Track failure count
- On failure: increment counter
- If failures >= threshold → transition to OPEN

**OPEN** (circuit tripped):  
- All requests rejected immediately (`CircuitOpenError`)
- No requests reach the failing backend
- After `timeout` seconds → transition to HALF_OPEN

**HALF_OPEN** (probing recovery):  
- Let limited requests through
- If success → increment success counter
- If successes >= success_threshold → transition to CLOSED (recovered)
- If failure → immediately back to OPEN (not recovered yet)

### The `check()` Method (`router/circuit_breaker.py:44-50`)

```python
def check(self) -> None:
    if self._state == CircuitState.OPEN:
        if self._last_failure_time and (time.time() - self._last_failure_time) > self.timeout:
            self._state = CircuitState.HALF_OPEN
            self._success_count = 0
        else:
            raise CircuitOpenError(...)   # reject request
```

`check()` is called before routing each request. If CLOSED or HALF_OPEN: returns normally (request proceeds). If OPEN and timeout not elapsed: raises `CircuitOpenError` (request rejected). If OPEN and timeout elapsed: transitions to HALF_OPEN and returns normally (probe request gets through).

The state transition happens inside `check()` itself — lazy evaluation. No background timer thread needed.

### Fallback Routing (`router/main.py:149-168`)

```python
candidates = [model] + [m for m in lb.models if m != model]
selected = None
for candidate in candidates:
    try:
        cbs[candidate].check()   # raises if OPEN
        selected = candidate
        break
    except CircuitOpenError:
        continue

if selected is None:
    raise HTTPException(503, "All model backends are unavailable")
```

If the requested model's circuit is OPEN, the router tries other models in order. Client asked for `claude` but claude is down → try `gpt4` → if that's also down, try `gemini`. Only 503 when all circuits are open simultaneously.

### No Locks Needed

The circuit breaker has no mutexes. This is safe because Python's async code runs on a **single OS thread**. There's no true parallelism — coroutines interleave at `await` points. `check()` and `record_failure()` are synchronous, so they run atomically relative to each other. Two requests can't race on the same state.

---

## 9. Load Balancing

### Round Robin (`router/load_balancer.py:38-41`)

```python
def _round_robin(self) -> str:
    model = self._queue[0]
    self._queue.rotate(-1)   # [a,b,c] → [b,c,a]
    return model
```

`collections.deque` stores model names. `rotate(-1)` moves front element to back. Sequence: `claude → gpt4 → gemini → claude → gpt4 → gemini → ...`

Each model gets exactly equal share of requests. Good when all backends have similar performance.

### Latency-Aware Selection (`router/load_balancer.py:43-44`)

```python
def _latency_aware(self) -> str:
    return min(self.models, key=lambda m: self._latencies[m])
```

Always pick the model with lowest EWMA latency. If `gpt4` is consistently faster than `claude`, it gets more traffic.

### EWMA — Exponential Weighted Moving Average (`router/load_balancer.py:46-51`)

```python
def record_latency(self, model: str, latency_seconds: float) -> None:
    current = self._latencies[model]
    self._latencies[model] = self._alpha * latency_seconds + (1 - self._alpha) * current
```

With `alpha=0.2`:
```
new_ewma = 0.2 * latest_sample + 0.8 * previous_ewma
```

Properties:
- **Smooth**: single outlier (network hiccup) barely moves the average
- **Responsive**: sustained latency increase shifts estimate within ~5-10 samples
- **Recency-weighted**: recent samples matter more than old ones
- **No history needed**: just one float per model, O(1) update

If `alpha=1.0`: always use latest sample (no smoothing, jittery)  
If `alpha=0.0`: never update (completely static, useless)  
`alpha=0.2` is a standard choice for slowly-changing metrics.

Concrete example:
```
Model starts at 0.0ms EWMA
Sample 1: 100ms → EWMA = 0.2*100 + 0.8*0   = 20ms
Sample 2: 100ms → EWMA = 0.2*100 + 0.8*20  = 36ms
Sample 3: 100ms → EWMA = 0.2*100 + 0.8*36  = 48.8ms
...converges toward 100ms over time
```

---

## 10. Prometheus Metrics

### Pull vs Push Model

Prometheus uses a **pull model**: it scrapes your `/metrics` endpoint on a schedule (every 15s by default). You expose metrics; Prometheus comes and reads them. This is opposite to most logging/monitoring systems where you push data to a central sink.

Advantage: Prometheus controls scrape rate. Your service doesn't need to know about Prometheus. Scaling scrape targets is Prometheus's problem.

### Metric Types (`router/metrics.py`)

**Counter** — only goes up, never down:
```python
REQUEST_COUNT = Counter(
    "infer_requests_total",
    "Total inference requests",
    ["model", "status"],     # labels
    registry=REGISTRY
)
REQUEST_COUNT.labels(model="claude", status="queued").inc()
```
Use for: requests served, errors, jobs processed. Query: `rate(infer_requests_total[5m])` = requests/second.

**Histogram** — samples values into buckets, computes percentiles:
```python
REQUEST_LATENCY = Histogram(
    "infer_request_duration_seconds",
    "Request latency",
    ["model"],
    registry=REGISTRY
)
REQUEST_LATENCY.labels(model="claude").observe(0.094)  # record one sample
```
Default buckets: 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0 seconds.  
Query: `histogram_quantile(0.95, rate(infer_request_duration_seconds_bucket[5m]))` = p95 latency.

**Gauge** — can go up or down:
```python
QUEUE_DEPTH = Gauge("llm_queue_length", "Queue depth", ["model"], registry=REGISTRY)
QUEUE_DEPTH.labels(model="claude").set(42)
```
Use for: current values — queue depth, active connections, memory usage.

### Labels

Labels turn one metric into many time series:
- `infer_requests_total{model="claude", status="queued"}` 
- `infer_requests_total{model="gpt4", status="queued"}`
- `infer_requests_total{model="claude", status="circuit_open"}`

Each unique label combination is a separate time series in Prometheus. Allows filtering and grouping in queries.

### Custom Registry (`router/metrics.py`)

```python
REGISTRY = CollectorRegistry()   # isolated registry, not the default global one
```

The default registry is a global singleton. In tests, multiple test runs would double-register metrics and crash. Custom registry = isolated, safe to create/destroy per test.

### The `/metrics` Endpoint (`router/main.py:113-123`)

```python
@app.get("/metrics")
async def metrics():
    for model, cb in circuit_breakers.items():
        depth = await redis_client.llen(f"{model}-queue")
        QUEUE_DEPTH.labels(model=model).set(depth)
        CIRCUIT_STATE.labels(model=model).set(cb.state_numeric())
    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)
```

Queue depth and circuit state are read fresh on every scrape. Latency metrics are recorded in the background task. `generate_latest(REGISTRY)` serializes all metrics to Prometheus text format.

---

## 11. OpenTelemetry Distributed Tracing

### Traces vs Metrics vs Logs

- **Logs**: discrete events. "Request received at 14:02:01"
- **Metrics**: aggregated numbers over time. "p95 latency = 120ms"
- **Traces**: end-to-end request journey. "This specific request spent 5ms in routing, 3ms in Redis lpush, 200ms in backend processing"

### Trace Structure

A **trace** is a tree of **spans**. Each span has: name, start time, duration, attributes, parent span ID.

```
Trace: POST /infer (total: 8ms)
├── router: validate request (1ms)
├── router: circuit breaker check (0.1ms)
└── router: redis lpush (6ms)
    └── redis: network roundtrip
```

### Instrumentation (`router/main.py:85-100`)

```python
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
FastAPIInstrumentor.instrument_app(app)
```

This automatically creates a span for every HTTP request — no manual span creation needed. Request attributes (method, path, status code) added automatically.

```python
if otlp_endpoint:
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    _provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint)))
```

`BatchSpanProcessor` buffers spans in memory and sends them to a collector (Jaeger, Tempo, Honeycomb) in batches via gRPC. If `OTEL_EXPORTER_OTLP_ENDPOINT` is not set, tracing is silently disabled (graceful degradation).

### W3C Trace Context Propagation

When router calls backend (in a real system, not the queue-based mock), trace context is propagated via HTTP headers:
```
traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01
```
This allows backend spans to be children of router spans — one unified trace across service boundaries.

---

## 12. Helm

### What Helm Is

Helm is Kubernetes' package manager. Instead of maintaining 15 separate YAML files for a deployment, you have a **chart** with templated YAML and a `values.yaml` for configuration. One `helm install` deploys everything.

### Chart Structure (`helm/kubeserve-chart/`)

```
Chart.yaml          # chart metadata (name, version, description)
values.yaml         # default configuration values
templates/
  namespace.yaml
  crd.yaml          # LLMDeployment CRD
  rbac.yaml         # ServiceAccount, ClusterRole, ClusterRoleBinding
  operator.yaml     # Operator Deployment
  router.yaml       # Router Deployment + Service
  servicemonitors.yaml  # Prometheus scraping config
  argocd-app.yaml   # ArgoCD Application resource
```

### Templates

Helm templates use Go template syntax:
```yaml
# templates/router.yaml
env:
  - name: MODELS
    value: {{ .Values.router.models | join "," | quote }}
  - name: LB_STRATEGY
    value: {{ .Values.router.lbStrategy | quote }}
replicas: {{ .Values.router.replicas }}
```

`{{ .Values.router.models }}` pulls from `values.yaml`:
```yaml
router:
  replicas: 2
  models: ["claude", "gpt4", "gemini"]
  lbStrategy: round_robin
```

### Why Helm Over Plain YAML

Plain YAML files are copy-pasted and drift. Helm:
- Single source of config truth (`values.yaml`)
- Versioned releases (`helm history kubeserve`)
- Rollback (`helm rollback kubeserve 1`)
- Override for different environments: `helm install ... --set redis.host=prod-redis`

---

## 13. ArgoCD / GitOps

### GitOps Principle

Git is the single source of truth for cluster state. To deploy a change:
1. Push to git
2. ArgoCD detects the diff between git and cluster
3. ArgoCD applies the diff

No `kubectl apply` in CI pipelines. No SSH into servers. Audit trail = git history.

### ArgoCD Application Resource (`helm/kubeserve-chart/templates/argocd-app.yaml`)

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: kubeserve
  namespace: argocd
spec:
  source:
    repoURL: https://github.com/Aditya-k24/KubeServe
    targetRevision: HEAD
    path: helm/kubeserve-chart
  destination:
    server: https://kubernetes.default.svc
    namespace: kubeserve
  syncPolicy:
    automated:
      prune: true        # delete resources removed from git
      selfHeal: true     # revert manual kubectl changes
```

ArgoCD polls the repo every 3 minutes (or uses webhooks). When it detects `helm/kubeserve-chart/` changed relative to what's deployed, it runs `helm upgrade` automatically.

`selfHeal: true` means if someone does `kubectl edit deployment router` manually, ArgoCD reverts it back to git state within minutes. Enforces GitOps discipline.

---

## 14. Docker Compose

### The Local Dev Problem

Running KubeServe locally needs: Redis, router, 3 model backends. Without Docker Compose, you'd start each manually, manage ports, set env vars. Docker Compose declares the full multi-container topology in one file.

### Service Definition (`docker-compose.yml`)

```yaml
services:
  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      retries: 5

  router:
    build: ./router
    ports: ["8080:8080"]
    depends_on:
      redis: { condition: service_healthy }  # wait for Redis healthcheck to pass
    environment:
      REDIS_HOST: redis         # Docker DNS: service name resolves to container IP
      MODELS: claude,gpt4,gemini

  backend-claude:
    build: ./mock-backend
    environment:
      MODEL_NAME: claude
      QUEUE_NAME: claude-queue
      REDIS_HOST: redis
```

**Docker DNS**: inside Compose network, `redis` resolves to the Redis container's IP. No hardcoded IPs needed.

**healthcheck + depends_on**: Router won't start until `redis-cli ping` returns PONG. Prevents "connection refused" errors on startup.

**Profiles**: monitoring services (Prometheus, Grafana) use `profiles: [monitoring]`. They only start when explicitly requested: `docker compose --profile monitoring up`.

---

## 15. k6 — Load Testing

### What k6 Measures

k6 runs virtual users (VUs) in parallel, each executing a test script in a loop. Measures: throughput, latency percentiles, error rate. Validates your system under realistic load.

### Load Test Structure (`k6/load-test.js`)

```javascript
export const options = {
    stages: [
        { duration: "30s", target: 10 },   // ramp up to 10 VUs
        { duration: "1m",  target: 50 },   // hold at 50
        { duration: "2m",  target: 100 },  // ramp to 100
        { duration: "2m",  target: 500 },  // ramp to 500 (peak load)
        { duration: "1m",  target: 50 },   // ramp down
        { duration: "30s", target: 0 },    // cool down
    ],
    thresholds: {
        http_req_duration: ["p(95)<2000"],  // 95% of requests < 2s
        http_req_failed: ["rate<0.05"],     // error rate < 5%
    },
};
```

At 500 VUs, 500 concurrent `/infer` requests are firing. This fills Redis queues rapidly → KEDA detects depth → scales up pods. The test validates that the autoscaling loop actually works under load.

### KEDA Validation During Load Test

```bash
# Watch in real time during k6 run:
kubectl get hpa -n kubeserve -w
kubectl top pods -n kubeserve
```

Expected sequence:
1. k6 ramps to 500 VUs
2. Redis `claude-queue` grows (500 requests/s >> backend capacity)
3. KEDA sees depth > threshold, requests scale-up
4. HPA updates Deployment replica count
5. New pods start, connect to Redis, consume from queue
6. Queue drains, latency drops
7. k6 ramps down, queue empties, KEDA scales back to 0 after cooldown

---

## 16. Full Request Lifecycle

Step-by-step trace of `POST /infer {"prompt": "What is Kubernetes?", "model": "claude"}`:

### Step 1: HTTP Request Arrives at Router

FastAPI receives the request. Pydantic validates the body against `InferRequest`. `model="claude"` is set.

### Step 2: Load Balancer Selection (`router/main.py:143-147`)

```python
model = body.model or lb.next()
```
`body.model = "claude"` → skip LB, use claude directly.  
If `body.model` was null → LB picks next in round-robin sequence.

### Step 3: Circuit Breaker Check (`router/main.py:149-167`)

```python
candidates = ["claude", "gpt4", "gemini"]
for candidate in candidates:
    cbs[candidate].check()   # returns normally if not OPEN
    selected = candidate
    break
```
`cbs["claude"].check()` — state is CLOSED → returns normally. `selected = "claude"`.

### Step 4: Job Creation (`router/main.py:170-178`)

```python
job_id = str(uuid.uuid4())    # "6e02093a-1d04-4483-874e-31804fdd1361"
queue_name = "claude-queue"
payload = {
    "job_id": job_id,
    "model": "claude",
    "prompt": "What is Kubernetes?",
    "params": {},
    "enqueued_at": 1717200000.0
}
```

### Step 5: Background Task Scheduled, Response Sent (`router/main.py:181-184`)

```python
background_tasks.add_task(_enqueue, ...)
return InferResponse(status="queued", job_id=job_id, model="claude", queue="claude-queue")
```
Client receives `{"status": "queued", "job_id": "6e02093a..."}` in ~2ms. `_enqueue` hasn't run yet.

### Step 6: Background Enqueue (`router/main.py:187-204`)

After response is sent:
```python
await redis_client.lpush("claude-queue", '{"job_id": "6e02093a...", "prompt": "What is Kubernetes?", ...}')
```
Redis list `claude-queue` now has 1 item. `LLEN` returns 1.

### Step 7: KEDA Detects Queue Depth

KEDA's metrics server polls Redis every few seconds: `LLEN claude-queue` = 1. If `listLength=5`, desired_replicas = `ceil(1/5)` = 1. If already 1 pod running, no scale event. Queue depth needs to exceed threshold for scaling to trigger.

### Step 8: Backend Consumes Job (`mock-backend/main.py:70-75`)

Backend's `worker_loop()` is blocked on `BRPOP claude-queue 2`. Redis wakes it:
```python
item = await redis_client.brpop("claude-queue", timeout=2)
# item = ("claude-queue", '{"job_id": "6e02093a...", ...}')
_, raw = item
job = json.loads(raw)
```

### Step 9: Simulated Inference (`mock-backend/main.py:56-61`)

```python
latency = random.uniform(0.05, 0.5)   # 50-500ms
await asyncio.sleep(latency)           # simulate LLM call
return f"[claude] Response to: What is Kubernetes?..."
```

### Step 10: Result Stored (`mock-backend/main.py:82-88`)

```python
result = {
    "job_id": "6e02093a...",
    "model": "claude",
    "output": "[claude] Response to: What is Kubernetes?...",
    "latency_seconds": 0.094,
    "status": "completed"
}
await redis_client.hset("results", "6e02093a...", json.dumps(result))
```

### Step 11: Client Polls for Result (`router/main.py:207-213`)

```python
@app.get("/result/{job_id}")
async def get_result(job_id: str):
    result = await redis_client.hget("results", job_id)
    if result is None:
        return JSONResponse(status_code=202, content={"status": "pending"})
    return json.loads(result)
```

While backend processes: `GET /result/6e02093a` → `202 {"status": "pending"}`.  
After backend stores result: `GET /result/6e02093a` → `200 {"status": "completed", "output": "..."}`.

### Step 12: Metrics Updated

Throughout this flow:
- `REQUEST_COUNT{model="claude", status="queued"}.inc()` — in background task
- `REQUEST_LATENCY{model="claude"}.observe(0.094)` — in background task  
- `QUEUE_DEPTH{model="claude"}.set(0)` — after backend consumes
- `CIRCUIT_STATE{model="claude"}.set(0)` — still closed (success)
- `backend_jobs_processed_total{model="claude", status="success"}.inc()` — in backend

Prometheus scrapes `/metrics` every 15s and these counters/gauges are returned.

---

## Summary: Design Principles

| Principle | Where Applied |
|-----------|--------------|
| **Async I/O** | FastAPI + aioredis — thousands of concurrent requests on single thread |
| **Queue-based decoupling** | Redis list — router and backend independent, naturally rate-limited |
| **Event-driven scaling** | KEDA — pod count tracks actual queue depth, not CPU |
| **Declarative infra** | CRD + Operator — desired state declared, controller reconciles |
| **Resilience** | Circuit breaker — failing backend isolated, fallback to others |
| **GitOps** | ArgoCD — git is truth, no manual kubectl in production |
| **Observability** | Prometheus + OTel — metrics for aggregates, traces for individual requests |
| **Idempotency** | Operator 409 handling — safe to run handlers multiple times |
