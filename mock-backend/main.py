"""
Mock LLM backend — simulates inference by consuming from a Redis queue.
Exposes /health and /metrics; processes jobs asynchronously.
"""
import asyncio
import json
import logging
import os
import random
import time
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI
from prometheus_client import Counter, Gauge, Histogram, generate_latest, CollectorRegistry, CONTENT_TYPE_LATEST
from starlette.responses import Response

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("kubeserve.backend")

MODEL_NAME = os.getenv("MODEL_NAME", "mock-model")
PROVIDER = os.getenv("PROVIDER", "mock")
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
QUEUE_NAME = os.getenv("QUEUE_NAME", f"{MODEL_NAME}-queue")
MOCK_LATENCY_MIN = float(os.getenv("MOCK_LATENCY_MIN", "0.05"))
MOCK_LATENCY_MAX = float(os.getenv("MOCK_LATENCY_MAX", "0.5"))
MOCK_ERROR_RATE = float(os.getenv("MOCK_ERROR_RATE", "0.01"))

REGISTRY = CollectorRegistry()
JOBS_PROCESSED = Counter("backend_jobs_processed_total", "Jobs processed", ["model", "status"], registry=REGISTRY)
JOB_LATENCY = Histogram("backend_job_duration_seconds", "Job processing duration", ["model"], registry=REGISTRY)
QUEUE_DEPTH_GAUGE = Gauge("backend_queue_depth", "Current queue depth", ["model"], registry=REGISTRY)

state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    state["redis"] = await aioredis.from_url(
        f"redis://{REDIS_HOST}:{REDIS_PORT}", decode_responses=True
    )
    state["running"] = True
    state["worker_task"] = asyncio.create_task(worker_loop())
    logger.info("Backend started: model=%s queue=%s", MODEL_NAME, QUEUE_NAME)
    yield
    state["running"] = False
    state["worker_task"].cancel()
    await state["redis"].close()
    logger.info("Backend shut down")


app = FastAPI(title=f"KubeServe Mock Backend — {MODEL_NAME}", version="1.0.0", lifespan=lifespan)


async def simulate_inference(prompt: str) -> str:
    latency = random.uniform(MOCK_LATENCY_MIN, MOCK_LATENCY_MAX)
    await asyncio.sleep(latency)
    if random.random() < MOCK_ERROR_RATE:
        raise RuntimeError(f"Mock inference error (rate={MOCK_ERROR_RATE})")
    return f"[{MODEL_NAME}] Response to: {prompt[:60]}..."


async def worker_loop():
    redis_client = state["redis"]
    logger.info("Worker started, listening on %s", QUEUE_NAME)
    while state.get("running", False):
        try:
            # BRPOP blocks until an item is available (timeout=2s)
            item = await redis_client.brpop(QUEUE_NAME, timeout=2)
            if item is None:
                continue
            _, raw = item
            job = json.loads(raw)
            job_id = job.get("job_id", "unknown")
            prompt = job.get("prompt", "")
            logger.info("Processing job %s", job_id)

            start = time.time()
            try:
                output = await simulate_inference(prompt)
                elapsed = time.time() - start
                result = {
                    "job_id": job_id,
                    "model": MODEL_NAME,
                    "output": output,
                    "latency_seconds": elapsed,
                    "status": "completed",
                }
                JOBS_PROCESSED.labels(model=MODEL_NAME, status="success").inc()
                JOB_LATENCY.labels(model=MODEL_NAME).observe(elapsed)
            except Exception as exc:
                elapsed = time.time() - start
                result = {
                    "job_id": job_id,
                    "model": MODEL_NAME,
                    "error": str(exc),
                    "latency_seconds": elapsed,
                    "status": "failed",
                }
                JOBS_PROCESSED.labels(model=MODEL_NAME, status="error").inc()
                logger.warning("Job %s failed: %s", job_id, exc)

            await redis_client.hset("results", job_id, json.dumps(result))
            depth = await redis_client.llen(QUEUE_NAME)
            QUEUE_DEPTH_GAUGE.labels(model=MODEL_NAME).set(depth)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.exception("Worker error: %s", exc)
            await asyncio.sleep(1)


@app.get("/health")
async def health():
    try:
        await state["redis"].ping()
        return {"status": "healthy", "model": MODEL_NAME, "queue": QUEUE_NAME}
    except Exception as exc:
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail=str(exc))


@app.get("/metrics", response_class=Response)
async def metrics():
    try:
        depth = await state["redis"].llen(QUEUE_NAME)
        QUEUE_DEPTH_GAUGE.labels(model=MODEL_NAME).set(depth)
    except Exception:
        pass
    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)


@app.get("/status")
async def status_endpoint():
    return {
        "model": MODEL_NAME,
        "provider": PROVIDER,
        "queue": QUEUE_NAME,
        "worker_running": state.get("running", False),
    }
