import os
import sys
import pytest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("REDIS_HOST", "redis-master")
os.environ.setdefault("REDIS_PORT", "6379")


@pytest.fixture
def mock_apps_api():
    with patch("main._apps_api") as mock:
        api = MagicMock()
        mock.return_value = api
        yield api


@pytest.fixture
def mock_core_api():
    with patch("main._core_api") as mock:
        api = MagicMock()
        mock.return_value = api
        yield api


@pytest.fixture
def mock_custom_api():
    with patch("main._custom_api") as mock:
        api = MagicMock()
        mock.return_value = api
        yield api


@pytest.fixture
def base_spec():
    return {
        "model": "claude",
        "provider": "anthropic",
        "minReplicas": 1,
        "maxReplicas": 10,
        "queueThreshold": 5,
    }
