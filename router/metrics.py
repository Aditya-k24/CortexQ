from prometheus_client import Counter, Histogram, Gauge, CollectorRegistry

REGISTRY = CollectorRegistry()

REQUEST_COUNT = Counter(
    "infer_requests_total",
    "Total inference requests by model and status",
    ["model", "status"],
    registry=REGISTRY,
)

REQUEST_LATENCY = Histogram(
    "infer_request_duration_seconds",
    "Inference request duration in seconds",
    ["model"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    registry=REGISTRY,
)

QUEUE_DEPTH = Gauge(
    "llm_queue_length",
    "Current Redis queue depth per model",
    ["model"],
    registry=REGISTRY,
)

CIRCUIT_STATE = Gauge(
    "circuit_breaker_state",
    "Circuit breaker state: 0=closed, 1=open, 2=half_open",
    ["model"],
    registry=REGISTRY,
)

ACTIVE_MODELS = Gauge(
    "llm_active_models_total",
    "Number of registered model backends",
    registry=REGISTRY,
)
