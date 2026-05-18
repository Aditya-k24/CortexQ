import json
import time
import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, patch, MagicMock


class TestHealthEndpoint:
    def test_health_returns_200(self, patched_app):
        resp = patched_app.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert data["redis"] == "connected"

    def test_health_returns_503_when_redis_down(self, patched_app, monkeypatch):
        import main as m
        broken = AsyncMock(side_effect=ConnectionError("Redis down"))
        monkeypatch.setattr(m.app_state["redis"], "ping", broken)
        resp = patched_app.get("/health")
        assert resp.status_code == 503


class TestModelsEndpoint:
    def test_models_lists_all(self, patched_app):
        resp = patched_app.get("/models")
        assert resp.status_code == 200
        data = resp.json()
        assert set(data["models"]) == {"claude", "gpt4", "gemini"}

    def test_models_circuit_states_all_closed(self, patched_app):
        resp = patched_app.get("/models")
        states = resp.json()["circuit_states"]
        for model, state in states.items():
            assert state == "closed", f"{model} circuit should be closed"

    def test_models_includes_strategy(self, patched_app):
        resp = patched_app.get("/models")
        assert resp.json()["strategy"] == "round_robin"


class TestInferEndpoint:
    def test_infer_queues_request(self, patched_app):
        resp = patched_app.post("/infer", json={"prompt": "Hello world", "model": "claude"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "queued"
        assert data["model"] == "claude"
        assert data["queue"] == "claude-queue"
        assert "job_id" in data

    def test_infer_auto_selects_model(self, patched_app):
        resp = patched_app.post("/infer", json={"prompt": "Auto select test"})
        assert resp.status_code == 200
        assert resp.json()["model"] in ("claude", "gpt4", "gemini")

    def test_infer_round_robin_rotation(self, patched_app):
        models_seen = set()
        for _ in range(9):
            resp = patched_app.post("/infer", json={"prompt": "test"})
            models_seen.add(resp.json()["model"])
        assert models_seen == {"claude", "gpt4", "gemini"}

    def test_infer_unknown_model_returns_400(self, patched_app):
        resp = patched_app.post("/infer", json={"prompt": "test", "model": "unknown_model"})
        assert resp.status_code == 400
        assert "Unknown model" in resp.json()["detail"]

    def test_infer_missing_prompt_returns_422(self, patched_app):
        resp = patched_app.post("/infer", json={"model": "claude"})
        assert resp.status_code == 422

    def test_infer_empty_prompt_returns_422(self, patched_app):
        resp = patched_app.post("/infer", json={"prompt": ""})
        assert resp.status_code == 422

    def test_infer_job_ids_are_unique(self, patched_app):
        job_ids = set()
        for _ in range(10):
            resp = patched_app.post("/infer", json={"prompt": "test"})
            job_ids.add(resp.json()["job_id"])
        assert len(job_ids) == 10

    def test_infer_with_params(self, patched_app):
        resp = patched_app.post(
            "/infer",
            json={"prompt": "test", "model": "claude", "params": {"temperature": 0.7, "max_tokens": 100}},
        )
        assert resp.status_code == 200

    def test_infer_payload_in_redis(self, patched_app, sync_redis):
        resp = patched_app.post("/infer", json={"prompt": "check redis", "model": "gpt4"})
        assert resp.status_code == 200
        job_id = resp.json()["job_id"]

        # Background task runs synchronously in TestClient — read from shared fake server
        raw = sync_redis.rpop("gpt4-queue")
        payload = json.loads(raw) if raw else None
        assert payload is not None
        assert payload["job_id"] == job_id
        assert payload["model"] == "gpt4"
        assert payload["prompt"] == "check redis"


class TestCircuitBreakerIntegration:
    def test_open_circuit_triggers_fallback(self, patched_app):
        import main as m

        # Force claude circuit open via app_state (lifespan seeded it)
        claude_cb = m.app_state["circuit_breakers"]["claude"]
        for _ in range(10):
            claude_cb.record_failure()
        assert claude_cb.state == "open"

        resp = patched_app.post("/infer", json={"prompt": "fallback test", "model": "claude"})
        assert resp.status_code == 200
        assert resp.json()["model"] != "claude"

    def test_all_circuits_open_returns_503(self, patched_app):
        import main as m

        for cb in m.app_state["circuit_breakers"].values():
            for _ in range(10):
                cb.record_failure()

        resp = patched_app.post("/infer", json={"prompt": "all down"})
        assert resp.status_code == 503


class TestMetricsEndpoint:
    def test_metrics_returns_200(self, patched_app):
        resp = patched_app.get("/metrics")
        assert resp.status_code == 200
        assert "text/plain" in resp.headers["content-type"]

    def test_metrics_contains_infer_requests(self, patched_app):
        patched_app.post("/infer", json={"prompt": "metrics test", "model": "claude"})
        resp = patched_app.get("/metrics")
        assert "infer_requests_total" in resp.text

    def test_metrics_contains_queue_length(self, patched_app):
        patched_app.post("/infer", json={"prompt": "queue metrics test", "model": "gemini"})
        resp = patched_app.get("/metrics")
        assert "llm_queue_length" in resp.text

    def test_metrics_contains_circuit_state(self, patched_app):
        resp = patched_app.get("/metrics")
        assert "circuit_breaker_state" in resp.text


class TestResultEndpoint:
    def test_pending_result_returns_202(self, patched_app):
        resp = patched_app.get("/result/nonexistent-job-id")
        assert resp.status_code == 202
        assert resp.json()["status"] == "pending"

    def test_completed_result_returns_200(self, patched_app, sync_redis):
        sync_redis.hset("results", "test-job-123", json.dumps({"output": "Hello!"}))
        resp = patched_app.get("/result/test-job-123")
        assert resp.status_code == 200
        assert resp.json()["output"] == "Hello!"
