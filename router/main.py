import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

import redis.asyncio as aioredis
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from pydantic import BaseModel, Field
from starlette.responses import Response

from circuit_breaker import CircuitBreaker, CircuitOpenError
from load_balancer import LoadBalancer
from metrics import (
    ACTIVE_MODELS,
    CIRCUIT_STATE,
    QUEUE_DEPTH,
    REGISTRY,
    REQUEST_COUNT,
    REQUEST_LATENCY,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("kubeserve.router")

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", None)
MODELS_ENV = os.getenv("MODELS", "claude,gpt4,gemini")
LB_STRATEGY = os.getenv("LB_STRATEGY", "round_robin")
CB_FAILURE_THRESHOLD = int(os.getenv("CB_FAILURE_THRESHOLD", "5"))
CB_SUCCESS_THRESHOLD = int(os.getenv("CB_SUCCESS_THRESHOLD", "2"))
CB_TIMEOUT = float(os.getenv("CB_TIMEOUT", "30.0"))


class InferRequest(BaseModel):
    prompt: str = Field(..., min_length=1, description="Prompt text to send to the model")
    model: Optional[str] = Field(None, description="Target model name; auto-selected if omitted")
    params: Dict[str, Any] = Field(default_factory=dict, description="Additional model parameters")


class InferResponse(BaseModel):
    status: str
    job_id: str
    model: str
    queue: str


app_state: Dict[str, Any] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    models = [m.strip() for m in MODELS_ENV.split(",") if m.strip()]
    redis_url = f"redis://{':' + REDIS_PASSWORD + '@' if REDIS_PASSWORD else ''}{REDIS_HOST}:{REDIS_PORT}"
    app_state["redis"] = await aioredis.from_url(redis_url, decode_responses=True)
    app_state["load_balancer"] = LoadBalancer(models, strategy=LB_STRATEGY)
    app_state["circuit_breakers"] = {
        m: CircuitBreaker(
            m,
            failure_threshold=CB_FAILURE_THRESHOLD,
            success_threshold=CB_SUCCESS_THRESHOLD,
            timeout=CB_TIMEOUT,
        )
        for m in models
    }
    ACTIVE_MODELS.set(len(models))
    logger.info("KubeServe router started: models=%s strategy=%s", models, LB_STRATEGY)
    yield
    await app_state["redis"].close()
    logger.info("KubeServe router shut down")


app = FastAPI(
    title="KubeServe LLM Router",
    description="Kubernetes-native LLM inference router with autoscaling support",
    version="1.0.0",
    lifespan=lifespan,
)

try:
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry import trace

    _provider = TracerProvider()
    otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    if otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        _provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint)))
    trace.set_tracer_provider(_provider)
    FastAPIInstrumentor.instrument_app(app)
    logger.info("OpenTelemetry instrumentation enabled")
except ImportError:
    logger.warning("OpenTelemetry packages not found — tracing disabled")


@app.get("/health", tags=["ops"])
async def health():
    try:
        await app_state["redis"].ping()
        return {"status": "healthy", "redis": "connected"}
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Redis unreachable: {exc}")


@app.get("/metrics", tags=["ops"], response_class=Response)
async def metrics():
    redis_client = app_state["redis"]
    circuit_breakers: Dict[str, CircuitBreaker] = app_state.get("circuit_breakers", {})
    for model, cb in circuit_breakers.items():
        try:
            depth = await redis_client.llen(f"{model}-queue")
            QUEUE_DEPTH.labels(model=model).set(depth)
        except Exception:
            pass
        CIRCUIT_STATE.labels(model=model).set(cb.state_numeric())
    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)


@app.get("/models", tags=["ops"])
async def list_models():
    lb: LoadBalancer = app_state.get("load_balancer")
    cbs: Dict[str, CircuitBreaker] = app_state.get("circuit_breakers", {})
    return {
        "models": lb.models if lb else [],
        "strategy": lb.strategy if lb else None,
        "latencies": lb.get_latencies() if lb else {},
        "circuit_states": {m: cb.state for m, cb in cbs.items()},
    }


@app.post("/infer", response_model=InferResponse, tags=["inference"])
async def infer(body: InferRequest, background_tasks: BackgroundTasks):
    lb: LoadBalancer = app_state["load_balancer"]
    cbs: Dict[str, CircuitBreaker] = app_state["circuit_breakers"]

    model = body.model or lb.next()

    if model not in lb.models:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unknown model: {model!r}")

    # Find first model with a closed (or half-open) circuit
    candidates = [model] + [m for m in lb.models if m != model]
    selected: Optional[str] = None
    for candidate in candidates:
        try:
            cbs[candidate].check()
            selected = candidate
            break
        except CircuitOpenError:
            continue

    if selected is None:
        REQUEST_COUNT.labels(model=model, status="circuit_open").inc()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="All model backends are unavailable (circuits open)",
        )
    if selected != model:
        logger.warning("Circuit open for %s — routing to fallback %s", model, selected)
    model = selected
    cb = cbs[model]

    job_id = str(uuid.uuid4())
    queue_name = f"{model}-queue"
    payload = {
        "job_id": job_id,
        "model": model,
        "prompt": body.prompt,
        "params": body.params,
        "enqueued_at": time.time(),
    }

    start = time.time()
    background_tasks.add_task(_enqueue, queue_name, payload, model, cb, start)
    REQUEST_COUNT.labels(model=model, status="queued").inc()

    return InferResponse(status="queued", job_id=job_id, model=model, queue=queue_name)


async def _enqueue(queue_name: str, payload: dict, model: str, cb: CircuitBreaker, start: float):
    redis_client = app_state["redis"]
    try:
        await redis_client.lpush(queue_name, json.dumps(payload))
        elapsed = time.time() - start
        cb.record_success()
        lb: LoadBalancer = app_state["load_balancer"]
        lb.record_latency(model, elapsed)
        REQUEST_LATENCY.labels(model=model).observe(elapsed)
        depth = await redis_client.llen(queue_name)
        QUEUE_DEPTH.labels(model=model).set(depth)
        CIRCUIT_STATE.labels(model=model).set(cb.state_numeric())
        logger.debug("Enqueued job to %s (depth=%d)", queue_name, depth)
    except Exception as exc:
        cb.record_failure()
        CIRCUIT_STATE.labels(model=model).set(cb.state_numeric())
        logger.error("Failed to enqueue to %s: %s", queue_name, exc)
        raise


@app.get("/result/{job_id}", tags=["inference"])
async def get_result(job_id: str):
    redis_client = app_state["redis"]
    result = await redis_client.hget("results", job_id)
    if result is None:
        return JSONResponse(status_code=202, content={"status": "pending", "job_id": job_id})
    return json.loads(result)


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "type": type(exc).__name__},
    )
