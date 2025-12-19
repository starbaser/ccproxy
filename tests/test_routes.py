"""Tests for ccproxy FastAPI routes."""

import pytest
from fastapi.testclient import TestClient

from ccproxy.handler import CCProxyHandler
from ccproxy.routes import router


@pytest.fixture
def client():
    """Create test client for FastAPI router."""
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_get_status_no_requests(client, cleanup):
    """Test status endpoint when no requests have been processed."""
    response = client.get("/ccproxy/status")
    assert response.status_code == 404
    assert response.json() == {"error": "no requests yet"}


def test_get_status_with_request(client, cleanup):
    """Test status endpoint after a request has been processed."""
    # Simulate a routing decision by setting the handler's status
    CCProxyHandler._last_status = {
        "rule": "thinking_model",
        "model": "openai/o3-mini",
        "original_model": "claude-sonnet-4-5-20250929",
        "is_passthrough": False,
        "timestamp": "2025-12-12T10:30:45.123456",
    }

    response = client.get("/ccproxy/status")
    assert response.status_code == 200
    data = response.json()
    assert data["rule"] == "thinking_model"
    assert data["model"] == "openai/o3-mini"
    assert data["original_model"] == "claude-sonnet-4-5-20250929"
    assert data["is_passthrough"] is False
    assert "timestamp" in data


def test_get_status_passthrough(client, cleanup):
    """Test status endpoint for passthrough requests."""
    CCProxyHandler._last_status = {
        "rule": None,
        "model": "claude-sonnet-4-5-20250929",
        "original_model": "claude-sonnet-4-5-20250929",
        "is_passthrough": True,
        "timestamp": "2025-12-12T10:30:45.123456",
    }

    response = client.get("/ccproxy/status")
    assert response.status_code == 200
    data = response.json()
    assert data["is_passthrough"] is True
    assert data["rule"] is None
