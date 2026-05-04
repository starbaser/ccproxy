"""Tests for ccproxy.inspector.routes.health — Portkey-style alive endpoint."""

from __future__ import annotations

from unittest.mock import MagicMock

from mitmproxy.proxy.mode_specs import ProxyMode

from ccproxy.inspector.router import InspectorRouter
from ccproxy.inspector.routes.health import register_health_routes


def _make_flow(method: str = "GET", path: str = "/health", reverse: bool = True) -> MagicMock:
    """Build a mock HTTPFlow for testing the health route handler."""
    flow = MagicMock()
    flow.request.method = method
    flow.request.path = path
    flow.response = None
    if reverse:
        flow.client_conn.proxy_mode = ProxyMode.parse("reverse:http://localhost:1@4001")
    else:
        flow.client_conn.proxy_mode = ProxyMode.parse("wireguard@51820")
    return flow


def _registered_paths(router: InspectorRouter) -> set[str]:
    """Return the literal route patterns currently registered on the router."""
    return {parser._format for _, parser, _ in router.request_routes}


def test_register_health_routes_registers_root_and_health() -> None:
    """register_health_routes adds two REQUEST routes on the same handler."""
    router = InspectorRouter(name="test_health_paths", request_passthrough=True, response_passthrough=True)
    register_health_routes(router)

    assert _registered_paths(router) == {"/", "/health"}
    handlers = {handler for _, _, handler in router.request_routes}
    assert len(handlers) == 1


def test_health_route_handler_returns_greeting() -> None:
    """GET /health on the reverse-proxy listener returns the Portkey-style text greeting."""
    router = InspectorRouter(name="test_health_get", request_passthrough=True, response_passthrough=True)
    register_health_routes(router)

    flow = _make_flow(method="GET", path="/health")

    handler = next(h for _, parser, h in router.request_routes if parser._format == "/health")
    handler(flow)

    assert flow.response is not None
    assert flow.response.status_code == 200
    assert flow.response.headers["Content-Type"] == "text/plain"
    assert flow.response.content == b"ccproxy says hey!"


def test_root_route_handler_returns_greeting() -> None:
    """GET / on the reverse-proxy listener also returns the greeting (Portkey-faithful)."""
    router = InspectorRouter(name="test_root_get", request_passthrough=True, response_passthrough=True)
    register_health_routes(router)

    flow = _make_flow(method="GET", path="/")

    handler = next(h for _, parser, h in router.request_routes if parser._format == "/")
    handler(flow)

    assert flow.response is not None
    assert flow.response.status_code == 200
    assert flow.response.headers["Content-Type"] == "text/plain"
    assert flow.response.content == b"ccproxy says hey!"


def test_health_route_handler_skips_non_get() -> None:
    """POST /health is a no-op so the rest of the chain can handle it."""
    router = InspectorRouter(name="test_health_post", request_passthrough=True, response_passthrough=True)
    register_health_routes(router)

    flow = _make_flow(method="POST", path="/health")

    handler = next(h for _, parser, h in router.request_routes if parser._format == "/health")
    handler(flow)

    assert flow.response is None


def test_health_route_handler_skips_wireguard_flows() -> None:
    """WireGuard flows hitting an upstream's /health continue to forward unchanged."""
    router = InspectorRouter(name="test_health_wg", request_passthrough=True, response_passthrough=True)
    register_health_routes(router)

    flow = _make_flow(method="GET", path="/health", reverse=False)

    handler = next(h for _, parser, h in router.request_routes if parser._format == "/health")
    handler(flow)

    assert flow.response is None
