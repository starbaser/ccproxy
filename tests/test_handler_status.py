"""Tests for CCProxyHandler status tracking for statusline widget."""

from datetime import datetime

import pytest

from ccproxy.config import clear_config_instance
from ccproxy.handler import CCProxyHandler
from ccproxy.router import clear_router


@pytest.fixture
def cleanup():
    """Clear handler status and singleton instances between tests."""
    CCProxyHandler._last_status = None
    clear_config_instance()
    clear_router()
    yield
    CCProxyHandler._last_status = None
    clear_config_instance()
    clear_router()


class TestHandlerStatusTracking:
    """Test status tracking for statusline widget."""

    def test_get_status_returns_none_initially(self, cleanup):
        """Test that get_status returns None when no request processed."""
        status = CCProxyHandler.get_status()
        assert status is None

    def test_class_level_variable_exists(self, cleanup):
        """Test that _last_status class variable is properly defined."""
        assert hasattr(CCProxyHandler, "_last_status")
        assert CCProxyHandler._last_status is None

    def test_get_status_method_is_classmethod(self, cleanup):
        """Test that get_status is a class method."""
        assert isinstance(CCProxyHandler.__dict__["get_status"], classmethod)

    def test_status_structure(self, cleanup):
        """Test that status dict has correct structure when manually set."""
        # Manually set status to verify structure
        test_status = {
            "rule": "test_rule",
            "model": "test_model",
            "original_model": "original",
            "is_passthrough": False,
            "timestamp": datetime.now().isoformat(),
        }
        CCProxyHandler._last_status = test_status

        # Verify retrieval
        status = CCProxyHandler.get_status()
        assert status == test_status
        assert "rule" in status
        assert "model" in status
        assert "original_model" in status
        assert "is_passthrough" in status
        assert "timestamp" in status

    def test_timestamp_format(self, cleanup):
        """Test that timestamp can be in ISO format."""
        timestamp = datetime.now().isoformat()
        CCProxyHandler._last_status = {
            "rule": "test",
            "model": "test",
            "original_model": "test",
            "is_passthrough": False,
            "timestamp": timestamp,
        }

        status = CCProxyHandler.get_status()
        # Should be parseable as ISO format
        parsed = datetime.fromisoformat(status["timestamp"])
        assert isinstance(parsed, datetime)

    def test_status_shared_across_instances(self, cleanup):
        """Test that status is class-level (shared across instances)."""
        handler1 = CCProxyHandler()
        handler2 = CCProxyHandler()

        # Set via class
        CCProxyHandler._last_status = {"rule": "shared"}

        # Both instances should see the same value
        assert handler1.get_status() == {"rule": "shared"}
        assert handler2.get_status() == {"rule": "shared"}
        assert handler1.get_status() is handler2.get_status()
