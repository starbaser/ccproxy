"""Tests for inbound route handlers (OAuth sentinel key handling)."""

from unittest.mock import MagicMock, patch

import pytest

from ccproxy.constants import OAUTH_SENTINEL_PREFIX
from ccproxy.inspector.flow_store import FlowRecord, InspectorMeta, create_flow_record
from ccproxy.inspector.router import InspectorRouter


def _make_inbound_flow(
    api_key: str = "",
    mode: str = "wireguard@51820",
    with_record: bool = False,
) -> MagicMock:
    from mitmproxy.proxy.mode_specs import ProxyMode

    flow = MagicMock()
    flow.request.headers = {"x-api-key": api_key} if api_key else {}
    flow.request.pretty_url = "https://api.anthropic.com/v1/messages"
    flow.request.method = "POST"
    flow.request.path = "/v1/messages"
    flow.request.scheme = "https"
    flow.request.host = "api.anthropic.com"
    flow.request.port = 443
    flow.request.pretty_host = "api.anthropic.com"
    flow.metadata = {}
    flow.client_conn.proxy_mode = ProxyMode.parse(mode)
    flow.id = "test-flow-1"

    if with_record:
        flow_id, record = create_flow_record("inbound")
        flow.metadata[InspectorMeta.RECORD] = record
        flow.metadata[InspectorMeta.DIRECTION] = "inbound"
        flow.request.headers["x-ccproxy-flow-id"] = flow_id

    return flow


def _setup_router() -> InspectorRouter:
    router = InspectorRouter(name="test_inbound", request_passthrough=True)
    from ccproxy.inspector.routes.inbound import register_inbound_routes

    register_inbound_routes(router)
    return router


class TestOAuthSentinelKey:
    def test_sentinel_key_substitutes_token(self) -> None:
        router = _setup_router()
        flow = _make_inbound_flow(api_key=f"{OAUTH_SENTINEL_PREFIX}anthropic", with_record=True)

        with (
            patch("ccproxy.inspector.routes.inbound._get_oauth_token", return_value="real-token-123"),
            patch("ccproxy.inspector.routes.inbound._get_oauth_auth_header", return_value=None),
        ):
            router.request(flow)

        assert flow.request.headers["authorization"] == "Bearer real-token-123"
        assert flow.request.headers["x-api-key"] == ""
        assert flow.request.headers["x-ccproxy-oauth-injected"] == "1"

        record: FlowRecord = flow.metadata[InspectorMeta.RECORD]
        assert record.auth is not None
        assert record.auth.provider == "anthropic"
        assert record.auth.credential == "real-token-123"
        assert record.auth.auth_header == "authorization"
        assert record.auth.injected is True
        assert record.auth.original_key == f"{OAUTH_SENTINEL_PREFIX}anthropic"

    def test_sentinel_key_with_custom_auth_header(self) -> None:
        router = _setup_router()
        flow = _make_inbound_flow(api_key=f"{OAUTH_SENTINEL_PREFIX}zai", with_record=True)

        with (
            patch("ccproxy.inspector.routes.inbound._get_oauth_token", return_value="zai-token"),
            patch("ccproxy.inspector.routes.inbound._get_oauth_auth_header", return_value="x-api-key"),
        ):
            router.request(flow)

        assert flow.request.headers["x-api-key"] == "zai-token"

        record: FlowRecord = flow.metadata[InspectorMeta.RECORD]
        assert record.auth is not None
        assert record.auth.auth_header == "x-api-key"
        assert record.auth.injected is True

    def test_missing_oat_sources_logs_error(self, caplog: pytest.LogCaptureFixture) -> None:
        router = _setup_router()
        flow = _make_inbound_flow(api_key=f"{OAUTH_SENTINEL_PREFIX}unknown")

        with patch("ccproxy.inspector.routes.inbound._get_oauth_token", return_value=None):
            router.request(flow)

        assert "unknown" in caplog.text
        assert "oat_sources" in caplog.text

    def test_non_sentinel_key_passes_through(self) -> None:
        router = _setup_router()
        flow = _make_inbound_flow(api_key="sk-ant-real-key-123")
        router.request(flow)
        assert flow.request.headers["x-api-key"] == "sk-ant-real-key-123"

    def test_empty_api_key_passes_through(self) -> None:
        router = _setup_router()
        flow = _make_inbound_flow(api_key="")
        router.request(flow)
        assert "x-ccproxy-oauth-injected" not in flow.request.headers

    def test_no_api_key_header_passes_through(self) -> None:
        router = _setup_router()
        flow = _make_inbound_flow()
        flow.request.headers = {}
        router.request(flow)
        assert "x-ccproxy-oauth-injected" not in flow.request.headers

    def test_regular_mode_flow_skipped(self) -> None:
        router = _setup_router()
        flow = _make_inbound_flow(api_key=f"{OAUTH_SENTINEL_PREFIX}anthropic", mode="regular@4003")
        with (
            patch("ccproxy.inspector.routes.inbound._get_oauth_token", return_value="token"),
            patch("ccproxy.inspector.routes.inbound._get_oauth_auth_header", return_value=None),
        ):
            router.request(flow)
        assert "x-ccproxy-oauth-injected" not in flow.request.headers

    def test_works_without_flow_record(self) -> None:
        """OAuth injection works even without FlowRecord (graceful degradation)."""
        router = _setup_router()
        flow = _make_inbound_flow(api_key=f"{OAUTH_SENTINEL_PREFIX}anthropic")

        with (
            patch("ccproxy.inspector.routes.inbound._get_oauth_token", return_value="token-123"),
            patch("ccproxy.inspector.routes.inbound._get_oauth_auth_header", return_value=None),
        ):
            router.request(flow)

        assert flow.request.headers["authorization"] == "Bearer token-123"
        assert flow.request.headers["x-ccproxy-oauth-injected"] == "1"


class TestGetOauthHelpers:
    """Direct tests for the private helper functions."""

    def test_get_oauth_token_returns_token(self) -> None:
        import time

        from ccproxy.config import CCProxyConfig, set_config_instance
        from ccproxy.inspector.routes.inbound import _get_oauth_token

        config = CCProxyConfig()
        config._oat_values["anthropic"] = ("my-token-abc", time.time())
        set_config_instance(config)

        try:
            result = _get_oauth_token("anthropic")
            assert result == "my-token-abc"
        finally:
            from ccproxy.config import clear_config_instance
            clear_config_instance()

    def test_get_oauth_token_returns_none_when_no_token(self) -> None:
        from ccproxy.config import CCProxyConfig, set_config_instance
        from ccproxy.inspector.routes.inbound import _get_oauth_token

        config = CCProxyConfig()
        set_config_instance(config)

        try:
            result = _get_oauth_token("unknown_provider")
            assert result is None
        finally:
            from ccproxy.config import clear_config_instance
            clear_config_instance()

    def test_get_oauth_token_handles_exception(self) -> None:
        from ccproxy.inspector.routes.inbound import _get_oauth_token
        with patch("ccproxy.config.get_config", side_effect=RuntimeError("error")):
            result = _get_oauth_token("anthropic")
            assert result is None

    def test_get_oauth_auth_header_returns_header(self) -> None:
        from ccproxy.config import CCProxyConfig, OAuthSource, set_config_instance
        from ccproxy.inspector.routes.inbound import _get_oauth_auth_header

        config = CCProxyConfig(
            oat_sources={"zai": OAuthSource(command="echo token", auth_header="x-api-key")}
        )
        set_config_instance(config)

        try:
            result = _get_oauth_auth_header("zai")
            assert result == "x-api-key"
        finally:
            from ccproxy.config import clear_config_instance
            clear_config_instance()

    def test_get_oauth_auth_header_handles_exception(self) -> None:
        from ccproxy.inspector.routes.inbound import _get_oauth_auth_header
        with patch("ccproxy.config.get_config", side_effect=RuntimeError("error")):
            result = _get_oauth_auth_header("anthropic")
            assert result is None
