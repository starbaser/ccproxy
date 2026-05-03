"""Tests for ccproxy.specs.model_catalog (static + live merge)."""

from __future__ import annotations

import json
from dataclasses import dataclass

import httpx
import pytest

from ccproxy.config import CCProxyConfig, set_config_instance
from ccproxy.specs.model_catalog import (
    STATIC_MODEL_CATALOG,
    build_catalog,
)


def test_static_floor_returns_openai_shape() -> None:
    """Default (no refresh) returns the OpenAI-shaped floor list."""
    catalog = build_catalog()
    assert catalog["object"] == "list"
    assert isinstance(catalog["data"], list)
    assert len(catalog["data"]) > 0
    for entry in catalog["data"]:
        assert entry["object"] == "model"
        assert isinstance(entry["id"], str)
        assert isinstance(entry["owned_by"], str)
        assert isinstance(entry["created"], int)


def test_static_floor_contains_known_anthropic_models() -> None:
    """The floor includes known production Claude IDs."""
    catalog = build_catalog()
    ids = {entry["id"] for entry in catalog["data"]}
    assert "claude-opus-4-7" in ids
    assert "claude-haiku-4-5-20251001" in ids


def test_static_floor_contains_known_gemini_models() -> None:
    catalog = build_catalog()
    ids = {entry["id"] for entry in catalog["data"]}
    assert "gemini-3-pro-preview" in ids
    assert "gemini-2.5-flash" in ids


def test_owned_by_matches_provider_keys() -> None:
    """Each entry's ``owned_by`` is one of the provider keys in STATIC_MODEL_CATALOG."""
    catalog = build_catalog()
    valid_owners = set(STATIC_MODEL_CATALOG.keys())
    for entry in catalog["data"]:
        assert entry["owned_by"] in valid_owners


def test_no_refresh_does_not_call_http() -> None:
    """Without ``refresh=True``, no HTTP calls are made."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Unexpected HTTP call: {request.url}")

    catalog = build_catalog(refresh=False, transport=httpx.MockTransport(handler))
    assert len(catalog["data"]) > 0


def test_refresh_merges_live_anthropic_models() -> None:
    """``refresh=True`` unions live anthropic models with the static floor (deduped)."""
    set_config_instance(CCProxyConfig())

    def handler(request: httpx.Request) -> httpx.Response:
        if "anthropic.com" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "data": [
                        # one new model not in the floor
                        {"id": "claude-future-9-1", "type": "model", "created": 1700000000},
                        # one duplicate of a floor entry
                        {"id": "claude-opus-4-7", "type": "model"},
                    ],
                },
            )
        return httpx.Response(404)

    catalog = build_catalog(refresh=True, transport=httpx.MockTransport(handler))
    ids = [entry["id"] for entry in catalog["data"]]
    assert "claude-future-9-1" in ids
    # No duplicates of the floor entry — the live anthropic block runs first
    # so the floor copy is skipped via the (owned_by, id) dedup set.
    assert ids.count("claude-opus-4-7") == 1


def test_refresh_provider_failure_falls_back_to_floor() -> None:
    """A provider HTTP failure does not remove its floor entries from the result."""
    set_config_instance(CCProxyConfig())

    def handler(request: httpx.Request) -> httpx.Response:
        if "anthropic.com" in str(request.url):
            return httpx.Response(503, text="upstream broken")
        return httpx.Response(404)

    catalog = build_catalog(refresh=True, transport=httpx.MockTransport(handler))
    ids = {entry["id"] for entry in catalog["data"]}
    assert "claude-opus-4-7" in ids


def test_refresh_network_error_falls_back_to_floor() -> None:
    """Connection errors don't propagate out of build_catalog."""
    set_config_instance(CCProxyConfig())

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns down")

    catalog = build_catalog(refresh=True, transport=httpx.MockTransport(handler))
    ids = {entry["id"] for entry in catalog["data"]}
    assert "claude-opus-4-7" in ids


@dataclass
class CatalogShapeCase:
    name: str
    """Descriptive name for the test scenario."""

    refresh: bool
    """Whether to enable live merge."""

    expected_min_data_count: int
    """Lower bound on the number of returned entries."""


CATALOG_SHAPE_CASES: list[CatalogShapeCase] = [
    CatalogShapeCase(name="static_floor_only", refresh=False, expected_min_data_count=8),
    CatalogShapeCase(name="refresh_returns_at_least_floor", refresh=True, expected_min_data_count=8),
]


@pytest.mark.parametrize(
    "case",
    [pytest.param(c, id=c.name) for c in CATALOG_SHAPE_CASES],
)
def test_catalog_shape_invariants(case: CatalogShapeCase) -> None:
    """Refresh and non-refresh both return at least the floor count."""
    if case.refresh:
        set_config_instance(CCProxyConfig())

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": []})

        catalog = build_catalog(refresh=True, transport=httpx.MockTransport(handler))
    else:
        catalog = build_catalog()
    assert len(catalog["data"]) >= case.expected_min_data_count


def test_models_route_handler_returns_openai_shape() -> None:
    """The xepor route handler crafts a 200 JSON response with the OpenAI shape."""
    from unittest.mock import MagicMock

    from ccproxy.inspector.router import InspectorRouter
    from ccproxy.inspector.routes.models import register_models_routes

    set_config_instance(CCProxyConfig())
    router = InspectorRouter(name="test_models", request_passthrough=True, response_passthrough=True)
    register_models_routes(router)

    flow = MagicMock()
    flow.request.method = "GET"
    flow.request.path = "/v1/models"
    flow.request.query = {}
    flow.response = None

    assert len(router.request_routes) == 1
    handler = router.request_routes[0][2]
    handler(flow)

    assert flow.response is not None
    assert flow.response.status_code == 200
    assert flow.response.headers["Content-Type"] == "application/json"
    payload = json.loads(flow.response.content)
    assert payload["object"] == "list"
    assert isinstance(payload["data"], list)


def test_models_route_handler_skips_non_get() -> None:
    """POST/PUT to /v1/models is a no-op (lets the rest of the chain handle it)."""
    from unittest.mock import MagicMock

    from ccproxy.inspector.router import InspectorRouter
    from ccproxy.inspector.routes.models import register_models_routes

    router = InspectorRouter(name="test_models_post", request_passthrough=True, response_passthrough=True)
    register_models_routes(router)

    flow = MagicMock()
    flow.request.method = "POST"
    flow.request.query = {}
    flow.response = None

    handler = router.request_routes[0][2]
    handler(flow)
    assert flow.response is None


def test_models_route_handler_honors_refresh_query() -> None:
    """``?refresh=true`` triggers a live merge."""
    from unittest.mock import MagicMock, patch

    from ccproxy.inspector.router import InspectorRouter
    from ccproxy.inspector.routes.models import register_models_routes

    router = InspectorRouter(name="test_models_refresh", request_passthrough=True, response_passthrough=True)
    register_models_routes(router)

    flow = MagicMock()
    flow.request.method = "GET"
    flow.request.query = {"refresh": "true"}
    flow.response = None

    with patch("ccproxy.inspector.routes.models.build_catalog") as build:
        build.return_value = {"object": "list", "data": []}
        handler = router.request_routes[0][2]
        handler(flow)
        build.assert_called_once_with(refresh=True)
