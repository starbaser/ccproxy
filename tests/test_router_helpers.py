"""Helper functions for router tests."""

from typing import Any
from unittest.mock import MagicMock, patch


def create_mock_proxy_server(model_list: list[dict[str, Any]]) -> MagicMock:
    """Create a mock proxy_server with the given model list."""
    mock_proxy_server = MagicMock()
    mock_proxy_server.llm_router = MagicMock()
    mock_proxy_server.llm_router.model_list = model_list
    mock_proxy_server.llm_router.get_model_list.return_value = model_list
    return mock_proxy_server


def patch_proxy_server(model_list: list[dict[str, Any]]):
    """Context manager to patch proxy_server with the given model list."""
    mock_proxy_server = create_mock_proxy_server(model_list)
    # Patch at the point where it's imported inside the method
    return patch("litellm.proxy.proxy_server", mock_proxy_server)
