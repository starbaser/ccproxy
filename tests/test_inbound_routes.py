"""Tests for inbound route handlers (OAuth sentinel key handling)."""

from unittest.mock import MagicMock, patch

import pytest

from ccproxy.constants import OAUTH_SENTINEL_PREFIX, OAuthConfigError
from ccproxy.inspector.routing import InspectorRouter


def _make_inbound_flow(
    api_key: str = "",
    mode: str = "wireguard@51820",
) -> MagicMock:
    from mitmproxy.proxy.mode_specs import ProxyMode

    flow = MagicMock()
    flow.request.headers = {"x-api-key": api_key} if api_key else {}
    flow.request.pretty_url = "https://api.anthropic.com/v1/messages"
    flow.request.method = "POST"
    flow.request.path = "/v1/messages"
    flow.request.pretty_host = "api.anthropic.com"
    flow.metadata = {}
    flow.client_conn.proxy_mode = ProxyMode.parse(mode)
    flow.id = "test-flow-1"
    return flow


def _setup_router() -> InspectorRouter:
    router = InspectorRouter(name="test_inbound", request_passthrough=True)
    from ccproxy.inspector.routes.inbound import register_inbound_routes

    register_inbound_routes(router)
    return router


class TestInboundDirectionTag:
    def test_tags_wireguard_flow_as_inbound(self) -> None:
        router = _setup_router()
        flow = _make_inbound_flow()
        router.request(flow)
        assert flow.metadata.get("ccproxy.direction") == "inbound"

    def test_tags_reverse_flow_as_inbound(self) -> None:
        router = _setup_router()
        flow = _make_inbound_flow(mode="reverse:http://localhost:4001@4000")
        router.request(flow)
        assert flow.metadata.get("ccproxy.direction") == "inbound"

    def test_skips_regular_mode_flow(self) -> None:
        router = _setup_router()
        flow = _make_inbound_flow(mode="regular@4003")
        router.request(flow)
        assert "ccproxy.direction" not in flow.metadata


class TestOAuthSentinelKey:
    def test_sentinel_key_substitutes_token(self) -> None:
        router = _setup_router()
        flow = _make_inbound_flow(api_key=f"{OAUTH_SENTINEL_PREFIX}anthropic")

        with patch("ccproxy.inspector.routes.inbound._get_oauth_token", return_value="real-token-123"):
            with patch("ccproxy.inspector.routes.inbound._get_oauth_auth_header", return_value=None):
                router.request(flow)

        assert flow.request.headers["authorization"] == "Bearer real-token-123"
        assert flow.request.headers["x-api-key"] == ""
        assert flow.metadata["ccproxy.oauth_injected"] is True
        assert flow.metadata["ccproxy.oauth_provider"] == "anthropic"
        assert flow.request.headers["x-ccproxy-oauth-injected"] == "1"

    def test_sentinel_key_with_custom_auth_header(self) -> None:
        router = _setup_router()
        flow = _make_inbound_flow(api_key=f"{OAUTH_SENTINEL_PREFIX}zai")

        with patch("ccproxy.inspector.routes.inbound._get_oauth_token", return_value="zai-token"):
            with patch("ccproxy.inspector.routes.inbound._get_oauth_auth_header", return_value="x-api-key"):
                router.request(flow)

        assert flow.request.headers["x-api-key"] == "zai-token"
        assert flow.metadata["ccproxy.oauth_injected"] is True

    def test_missing_oat_sources_logs_error(self, caplog: pytest.LogCaptureFixture) -> None:
        router = _setup_router()
        flow = _make_inbound_flow(api_key=f"{OAUTH_SENTINEL_PREFIX}unknown")

        with patch("ccproxy.inspector.routes.inbound._get_oauth_token", return_value=None):
            # xepor's catch_error=True catches the OAuthConfigError
            router.request(flow)

        assert "unknown" in caplog.text
        assert "oat_sources" in caplog.text

    def test_non_sentinel_key_passes_through(self) -> None:
        router = _setup_router()
        flow = _make_inbound_flow(api_key="sk-ant-real-key-123")
        router.request(flow)
        assert flow.request.headers["x-api-key"] == "sk-ant-real-key-123"
        assert "ccproxy.oauth_injected" not in flow.metadata

    def test_empty_api_key_passes_through(self) -> None:
        router = _setup_router()
        flow = _make_inbound_flow(api_key="")
        router.request(flow)
        assert "ccproxy.oauth_injected" not in flow.metadata

    def test_no_api_key_header_passes_through(self) -> None:
        router = _setup_router()
        flow = _make_inbound_flow()
        flow.request.headers = {}  # No x-api-key at all
        router.request(flow)
        assert "ccproxy.oauth_injected" not in flow.metadata
