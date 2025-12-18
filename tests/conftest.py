"""Shared test fixtures and helpers."""

from unittest.mock import MagicMock, patch

import pytest

from ccproxy.config import clear_config_instance
from ccproxy.router import clear_router


@pytest.fixture(autouse=True)
def cleanup():
    """Ensure clean state between tests."""
    yield
    # Clean up singleton instances
    clear_config_instance()
    clear_router()

    # Clear handler status
    from ccproxy.handler import CCProxyHandler

    CCProxyHandler._last_status = None


@pytest.fixture
def mock_proxy_server():
    """Create a mock proxy_server with configurable model list."""

    def _create_mock(model_list=None):
        if model_list is None:
            model_list = []

        mock_proxy_server = MagicMock()
        mock_proxy_server.llm_router = MagicMock()
        mock_proxy_server.llm_router.model_list = model_list

        # Create a mock module that contains proxy_server
        mock_module = MagicMock()
        mock_module.proxy_server = mock_proxy_server

        return mock_module

    return _create_mock


@pytest.fixture
def patch_litellm_proxy(mock_proxy_server):
    """Patch litellm.proxy module to use mock proxy_server."""

    def _patch(model_list=None):
        mock_module = mock_proxy_server(model_list)
        return patch.dict("sys.modules", {"litellm.proxy": mock_module})

    return _patch
