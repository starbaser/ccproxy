"""Shared test fixtures and helpers."""

import pytest

from ccproxy.compliance.store import clear_store_instance
from ccproxy.config import clear_config_instance
from ccproxy.inspector.flow_store import clear_flow_store
from ccproxy.mcp.buffer import clear_buffer


@pytest.fixture(autouse=True)
def cleanup():
    """Ensure clean state between tests."""
    yield
    clear_config_instance()
    clear_buffer()
    clear_flow_store()
    clear_store_instance()
