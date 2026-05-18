import os
import sys
import pytest
import fakeredis
import fakeredis.aioredis as fakeredis_async
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("REDIS_HOST", "localhost")
os.environ.setdefault("REDIS_PORT", "6379")
os.environ.setdefault("MODELS", "claude,gpt4,gemini")
os.environ.setdefault("LB_STRATEGY", "round_robin")


@pytest.fixture
def fake_server():
    return fakeredis.FakeServer()


@pytest.fixture
def sync_redis(fake_server):
    """Synchronous fakeredis client sharing the same server — for test assertions."""
    return fakeredis.FakeRedis(server=fake_server, decode_responses=True)


@pytest.fixture
def patched_app(fake_server):
    """TestClient with app lifespan connected to an in-memory fake Redis."""
    import main as router_main
    from fastapi.testclient import TestClient
    import redis.asyncio as aioredis

    async def fake_from_url(*args, **kwargs):
        return fakeredis_async.FakeRedis(server=fake_server, decode_responses=True)

    with patch.object(aioredis, "from_url", side_effect=fake_from_url):
        with TestClient(router_main.app, raise_server_exceptions=True) as client:
            yield client
