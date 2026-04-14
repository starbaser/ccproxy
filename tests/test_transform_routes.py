"""Tests for ccproxy.inspector.routes.transform — lightllm transform routes."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from mitmproxy.proxy.mode_specs import ProxyMode

from ccproxy.config import InspectorConfig, TransformRoute, set_config_instance
from ccproxy.inspector.flow_store import FlowRecord, InspectorMeta
from ccproxy.inspector.router import InspectorRouter
from ccproxy.inspector.routes.transform import (
    _resolve_api_key,
    _resolve_transform_target,
    _rewrite_path,
    register_transform_routes,
)


def _make_flow(
    host: str = "api.openai.com",
    path: str = "/v1/chat/completions",
    body: dict[str, Any] | None = None,
    direction: str = "inbound",
    proxy_mode: Any = None,
) -> MagicMock:
    """Build a mock HTTPFlow for testing transform routes."""
    flow = MagicMock()
    flow.request.pretty_host = host
    flow.request.host = host
    flow.request.path = path
    flow.request.port = 443
    flow.request.scheme = "https"
    flow.request.headers = {}
    flow.request.content = json.dumps(
        body
        or {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "hello"}],
        }
    ).encode()
    flow.metadata = {InspectorMeta.DIRECTION: direction}
    flow.server_conn = MagicMock()
    flow.response = None
    # Default to ReverseMode (transform/redirect only apply to reverse proxy)
    if proxy_mode is None:
        proxy_mode = ProxyMode.parse("reverse:http://localhost:1@4001")
    flow.client_conn.proxy_mode = proxy_mode
    return flow


def _make_config_with_transforms(transforms: list[dict[str, Any]]) -> None:
    """Set up a CCProxyConfig with transform rules."""
    from ccproxy.config import CCProxyConfig

    transform_routes = [TransformRoute(**t) for t in transforms]
    inspector = InspectorConfig(transforms=transform_routes)
    config = CCProxyConfig(inspector=inspector)
    set_config_instance(config)


class TestResolveTransformTarget:
    def test_matches_host_and_path(self, cleanup: None) -> None:
        _make_config_with_transforms(
            [
                {
                    "match_host": "api.openai.com",
                    "match_path": "/v1/chat/completions",
                    "dest_provider": "anthropic",
                    "dest_model": "claude-3-5-sonnet-20241022",
                }
            ]
        )
        flow = _make_flow(host="api.openai.com", path="/v1/chat/completions")
        target = _resolve_transform_target(flow)
        assert target is not None
        assert target.dest_provider == "anthropic"

    def test_no_match_different_host(self, cleanup: None) -> None:
        _make_config_with_transforms(
            [
                {
                    "match_host": "api.openai.com",
                    "match_path": "/v1/chat/completions",
                    "dest_provider": "anthropic",
                    "dest_model": "claude-3-5-sonnet-20241022",
                }
            ]
        )
        flow = _make_flow(host="api.anthropic.com", path="/v1/messages")
        assert _resolve_transform_target(flow) is None

    def test_no_match_different_path(self, cleanup: None) -> None:
        _make_config_with_transforms(
            [
                {
                    "match_host": "api.openai.com",
                    "match_path": "/v1/chat/completions",
                    "dest_provider": "anthropic",
                    "dest_model": "claude-3-5-sonnet-20241022",
                }
            ]
        )
        flow = _make_flow(host="api.openai.com", path="/v1/embeddings")
        assert _resolve_transform_target(flow) is None

    def test_empty_transforms(self, cleanup: None) -> None:
        _make_config_with_transforms([])
        flow = _make_flow()
        assert _resolve_transform_target(flow) is None

    def test_first_match_wins(self, cleanup: None) -> None:
        _make_config_with_transforms(
            [
                {
                    "match_host": "api.openai.com",
                    "match_path": "/",
                    "dest_provider": "anthropic",
                    "dest_model": "claude-first",
                },
                {
                    "match_host": "api.openai.com",
                    "match_path": "/",
                    "dest_provider": "gemini",
                    "dest_model": "gemini-second",
                },
            ]
        )
        flow = _make_flow()
        target = _resolve_transform_target(flow)
        assert target is not None
        assert target.dest_model == "claude-first"

    def test_path_prefix_match(self, cleanup: None) -> None:
        _make_config_with_transforms(
            [
                {
                    "match_host": "api.openai.com",
                    "match_path": "/v1/",
                    "dest_provider": "anthropic",
                    "dest_model": "claude-3-5-sonnet-20241022",
                }
            ]
        )
        flow = _make_flow(host="api.openai.com", path="/v1/chat/completions")
        target = _resolve_transform_target(flow)
        assert target is not None

    def test_match_model(self, cleanup: None) -> None:
        _make_config_with_transforms(
            [
                {
                    "match_path": "/v1/chat/completions",
                    "match_model": "gpt-4o",
                    "dest_provider": "anthropic",
                    "dest_model": "claude-3-5-sonnet-20241022",
                }
            ]
        )
        flow = _make_flow(body={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]})
        body = json.loads(flow.request.content)
        target = _resolve_transform_target(flow, body)
        assert target is not None
        assert target.dest_provider == "anthropic"

    def test_match_model_no_match(self, cleanup: None) -> None:
        _make_config_with_transforms(
            [
                {
                    "match_path": "/v1/chat/completions",
                    "match_model": "gpt-4o",
                    "dest_provider": "anthropic",
                    "dest_model": "claude-3-5-sonnet-20241022",
                }
            ]
        )
        flow = _make_flow(body={"model": "claude-3-haiku", "messages": [{"role": "user", "content": "hi"}]})
        body = json.loads(flow.request.content)
        assert _resolve_transform_target(flow, body) is None

    def test_null_match_host_matches_any(self, cleanup: None) -> None:
        _make_config_with_transforms(
            [
                {
                    "match_path": "/v1/chat/completions",
                    "dest_provider": "anthropic",
                    "dest_model": "claude-3-5-sonnet-20241022",
                }
            ]
        )
        flow = _make_flow(host="any-host.example.com")
        target = _resolve_transform_target(flow)
        assert target is not None


class TestResolveApiKey:
    def test_none_ref(self) -> None:
        target = TransformRoute(
            match_host="x",
            dest_provider="anthropic",
            dest_model="m",
            dest_api_key_ref=None,
        )
        assert _resolve_api_key(target) is None

    def test_env_var_fallback(self, monkeypatch: pytest.MonkeyPatch, cleanup: None) -> None:
        monkeypatch.setenv("MY_API_KEY", "env-key-value")
        from ccproxy.config import CCProxyConfig

        config = CCProxyConfig()
        set_config_instance(config)

        target = TransformRoute(
            match_host="x",
            dest_provider="anthropic",
            dest_model="m",
            dest_api_key_ref="MY_API_KEY",
        )
        result = _resolve_api_key(target)
        assert result == "env-key-value"


class TestHandleTransform:
    def test_skips_outbound_flows(self, cleanup: None) -> None:
        _make_config_with_transforms(
            [
                {
                    "match_host": "api.openai.com",
                    "match_path": "/",
                    "dest_provider": "anthropic",
                    "dest_model": "claude-3-5-sonnet-20241022",
                }
            ]
        )
        router = InspectorRouter(
            name="test_transform",
            request_passthrough=True,
            response_passthrough=True,
        )
        register_transform_routes(router)

        flow = _make_flow(direction="outbound")
        original_content = flow.request.content
        router.request(flow)
        assert flow.request.content == original_content

    def test_skips_unmatched_flows(self, cleanup: None) -> None:
        _make_config_with_transforms(
            [
                {
                    "match_host": "api.openai.com",
                    "match_path": "/v1/chat/completions",
                    "dest_provider": "anthropic",
                    "dest_model": "claude-3-5-sonnet-20241022",
                }
            ]
        )
        router = InspectorRouter(
            name="test_transform",
            request_passthrough=True,
            response_passthrough=True,
        )
        register_transform_routes(router)

        flow = _make_flow(host="api.other.com")
        original_content = flow.request.content
        router.request(flow)
        assert flow.request.content == original_content

    @patch("ccproxy.lightllm.transform_to_provider")
    def test_rewrites_matched_flow(self, mock_transform: MagicMock, cleanup: None) -> None:
        _make_config_with_transforms(
            [
                {
                    "mode": "transform",
                    "match_host": "api.openai.com",
                    "match_path": "/v1/chat/completions",
                    "dest_provider": "anthropic",
                    "dest_model": "claude-3-5-sonnet-20241022",
                }
            ]
        )
        mock_transform.return_value = (
            "https://api.anthropic.com/v1/messages",
            {"x-api-key": "test-key", "anthropic-version": "2023-06-01"},
            b'{"model": "claude-3-5-sonnet-20241022", "messages": []}',
        )

        router = InspectorRouter(
            name="test_transform",
            request_passthrough=True,
            response_passthrough=True,
        )
        register_transform_routes(router)

        flow = _make_flow()
        router.request(flow)

        assert flow.request.host == "api.anthropic.com"
        assert flow.request.port == 443
        assert flow.request.scheme == "https"
        assert flow.request.path == "/v1/messages"
        assert flow.request.headers["x-api-key"] == "test-key"
        assert flow.request.content == b'{"model": "claude-3-5-sonnet-20241022", "messages": []}'

    @patch("ccproxy.lightllm.transform_to_provider")
    def test_passes_messages_and_params(self, mock_transform: MagicMock, cleanup: None) -> None:
        _make_config_with_transforms(
            [
                {
                    "mode": "transform",
                    "match_host": "api.openai.com",
                    "match_path": "/",
                    "dest_provider": "anthropic",
                    "dest_model": "claude-3-5-sonnet-20241022",
                    "dest_api_key_ref": None,
                }
            ]
        )
        mock_transform.return_value = ("https://api.anthropic.com/v1/messages", {}, b"{}")

        flow = _make_flow(
            body={
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": "hi"}],
                "temperature": 0.7,
                "stream": True,
            }
        )

        router = InspectorRouter(
            name="test_transform",
            request_passthrough=True,
            response_passthrough=True,
        )
        register_transform_routes(router)
        router.request(flow)

        mock_transform.assert_called_once()
        call_kwargs = mock_transform.call_args
        assert (
            call_kwargs.kwargs.get("model")
            or call_kwargs[1].get("model")
            or call_kwargs[0][0] == "claude-3-5-sonnet-20241022"
        )

    def test_reverse_proxy_unmatched_returns_501(self, cleanup: None) -> None:
        _make_config_with_transforms(
            [
                {
                    "match_host": "api.openai.com",
                    "match_path": "/v1/chat/completions",
                    "dest_provider": "anthropic",
                    "dest_model": "claude-3-5-sonnet-20241022",
                }
            ]
        )
        router = InspectorRouter(
            name="test_transform",
            request_passthrough=True,
            response_passthrough=True,
        )
        register_transform_routes(router)

        flow = _make_flow(
            host="api.other.com",
            proxy_mode=ProxyMode.parse("reverse:http://localhost:1@4001"),
        )
        router.request(flow)

        assert flow.response is not None
        assert flow.response.status_code == 501

    def test_wireguard_unmatched_passes_through(self, cleanup: None) -> None:
        _make_config_with_transforms(
            [
                {
                    "match_host": "api.openai.com",
                    "match_path": "/v1/chat/completions",
                    "dest_provider": "anthropic",
                    "dest_model": "claude-3-5-sonnet-20241022",
                }
            ]
        )
        router = InspectorRouter(
            name="test_transform",
            request_passthrough=True,
            response_passthrough=True,
        )
        register_transform_routes(router)

        flow = _make_flow(
            host="api.other.com",
            proxy_mode=ProxyMode.parse("wireguard@51820"),
        )
        original_content = flow.request.content
        router.request(flow)

        assert flow.response is None
        assert flow.request.content == original_content

    def test_passthrough_mode_leaves_flow_unchanged(self, cleanup: None) -> None:
        _make_config_with_transforms(
            [
                {
                    "match_host": "api.openai.com",
                    "match_path": "/v1/chat/completions",
                    "dest_provider": "anthropic",
                    "dest_model": "claude-3-5-sonnet-20241022",
                    "mode": "passthrough",
                }
            ]
        )
        router = InspectorRouter(
            name="test_transform",
            request_passthrough=True,
            response_passthrough=True,
        )
        register_transform_routes(router)

        flow = _make_flow()
        original_host = flow.request.host
        original_path = flow.request.path
        original_content = flow.request.content
        router.request(flow)

        assert flow.request.host == original_host
        assert flow.request.path == original_path
        assert flow.request.content == original_content
        assert flow.response is None


class TestRewritePath:
    """Tests for _rewrite_path — Gemini action extraction and path rewriting."""

    def test_non_gemini_provider_returns_none(self) -> None:
        target = TransformRoute(dest_provider="anthropic", match_path="/v1/")
        assert _rewrite_path("/models/claude:chat", target) is None

    def test_gemini_generate_content(self) -> None:
        target = TransformRoute(dest_provider="gemini", match_path="/v1beta/")
        result = _rewrite_path("/models/gemini-pro:generateContent", target)
        assert result == "/v1internal:generateContent"

    def test_gemini_stream_generate_content(self) -> None:
        target = TransformRoute(dest_provider="gemini", match_path="/v1beta/")
        result = _rewrite_path("/models/gemini-pro:streamGenerateContent", target)
        assert result == "/v1internal:streamGenerateContent?alt=sse"

    def test_gemini_stream_with_query_params(self) -> None:
        target = TransformRoute(dest_provider="gemini", match_path="/v1beta/")
        result = _rewrite_path("/models/gemini-pro:streamGenerateContent?alt=sse", target)
        assert result == "/v1internal:streamGenerateContent?alt=sse"

    def test_gemini_no_action_returns_none(self) -> None:
        target = TransformRoute(dest_provider="gemini", match_path="/v1beta/")
        assert _rewrite_path("/some/path/without/action", target) is None


class TestHandleRedirect:
    """Tests for redirect mode — host rewriting, path override, auth injection."""

    def _make_redirect_config(self, overrides: dict[str, Any] | None = None) -> None:
        base = {
            "mode": "redirect",
            "match_host": "proxy.local",
            "match_path": "/v1/",
            "dest_provider": "anthropic",
            "dest_host": "api.anthropic.com",
        }
        base.update(overrides or {})
        _make_config_with_transforms([base])

    def _make_redirect_flow(self, path: str = "/v1/messages", host: str = "proxy.local") -> MagicMock:
        record = FlowRecord(direction="inbound")
        flow = _make_flow(host=host, path=path)
        flow.metadata[InspectorMeta.RECORD] = record
        return flow

    def test_redirect_rewrites_host_and_port(self, cleanup: None) -> None:
        self._make_redirect_config()
        router = InspectorRouter(name="test_redir", request_passthrough=True, response_passthrough=True)
        register_transform_routes(router)

        flow = self._make_redirect_flow()
        router.request(flow)

        assert flow.request.host == "api.anthropic.com"
        assert flow.request.port == 443
        assert flow.request.scheme == "https"

    def test_redirect_with_dest_path_override(self, cleanup: None) -> None:
        self._make_redirect_config({"dest_path": "/v2/override"})
        router = InspectorRouter(name="test_redir", request_passthrough=True, response_passthrough=True)
        register_transform_routes(router)

        flow = self._make_redirect_flow(path="/v1/messages")
        router.request(flow)

        assert flow.request.path == "/v2/override"

    def test_redirect_strips_match_prefix(self, cleanup: None) -> None:
        self._make_redirect_config({"match_path": "/gemini/"})
        router = InspectorRouter(name="test_redir", request_passthrough=True, response_passthrough=True)
        register_transform_routes(router)

        flow = self._make_redirect_flow(path="/gemini/v1beta/models/gemini-pro:generateContent")
        router.request(flow)

        # Prefix /gemini stripped, remainder preserved
        assert flow.request.path.startswith("/v1beta/")

    def test_redirect_gemini_path_rewrite(self, cleanup: None) -> None:
        self._make_redirect_config(
            {
                "match_path": "/gemini/",
                "dest_provider": "gemini",
                "dest_host": "cloudcode-pa.googleapis.com",
            }
        )
        router = InspectorRouter(name="test_redir", request_passthrough=True, response_passthrough=True)
        register_transform_routes(router)

        flow = self._make_redirect_flow(path="/gemini/models/gemini-pro:generateContent")
        router.request(flow)

        assert flow.request.path == "/v1internal:generateContent"
        assert flow.request.host == "cloudcode-pa.googleapis.com"

    def test_redirect_missing_dest_host_passthrough(self, cleanup: None) -> None:
        _make_config_with_transforms(
            [
                {
                    "mode": "redirect",
                    "match_host": "proxy.local",
                    "match_path": "/v1/",
                    "dest_provider": "anthropic",
                    # dest_host intentionally missing
                }
            ]
        )
        router = InspectorRouter(name="test_redir", request_passthrough=True, response_passthrough=True)
        register_transform_routes(router)

        flow = self._make_redirect_flow()
        original_host = flow.request.host
        router.request(flow)

        # Falls back to passthrough (host unchanged)
        assert flow.request.host == original_host

    def test_redirect_stores_transform_meta(self, cleanup: None) -> None:
        self._make_redirect_config()
        router = InspectorRouter(name="test_redir", request_passthrough=True, response_passthrough=True)
        register_transform_routes(router)

        flow = self._make_redirect_flow()
        router.request(flow)

        record = flow.metadata[InspectorMeta.RECORD]
        assert record.transform is not None
        assert record.transform.provider == "anthropic"

    def test_redirect_injects_api_key(self, cleanup: None) -> None:
        from ccproxy.config import CCProxyConfig, OAuthSource

        config = CCProxyConfig(
            inspector=InspectorConfig(
                transforms=[
                    TransformRoute(
                        mode="redirect",
                        match_host="proxy.local",
                        match_path="/v1/",
                        dest_provider="anthropic",
                        dest_host="api.anthropic.com",
                        dest_api_key_ref="anthropic",
                    )
                ]
            ),
            oat_sources={"anthropic": OAuthSource(command="echo tok")},
        )
        config._oat_values["anthropic"] = "injected-token"
        set_config_instance(config)

        router = InspectorRouter(name="test_redir", request_passthrough=True, response_passthrough=True)
        register_transform_routes(router)

        flow = self._make_redirect_flow()
        router.request(flow)

        assert flow.request.headers.get("authorization") == "Bearer injected-token"


class TestContextCacheInTransform:
    """Tests for Gemini context cache integration in _handle_transform."""

    @patch("ccproxy.lightllm.transform_to_provider")
    @patch("ccproxy.lightllm.context_cache.resolve_cached_content")
    def test_gemini_calls_resolve_cached_content(
        self,
        mock_cache: MagicMock,
        mock_transform: MagicMock,
        cleanup: None,
    ) -> None:
        _make_config_with_transforms(
            [
                {
                    "mode": "transform",
                    "match_host": "api.openai.com",
                    "match_path": "/",
                    "dest_provider": "gemini",
                    "dest_model": "gemini-2.0-flash",
                }
            ]
        )

        mock_cache.return_value = (
            [{"role": "user", "content": "filtered"}],
            {"model": "gemini-2.0-flash"},
            "cachedContents/abc123",
        )
        mock_transform.return_value = ("https://gemini.googleapis.com/v1", {}, b"{}")

        router = InspectorRouter(name="test_cache", request_passthrough=True, response_passthrough=True)
        register_transform_routes(router)

        flow = _make_flow(
            body={
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": "hello"}],
            }
        )
        router.request(flow)

        mock_cache.assert_called_once()
        mock_transform.assert_called_once()
        # cached_content should be passed to transform_to_provider
        assert mock_transform.call_args.kwargs.get("cached_content") == "cachedContents/abc123"

    @patch("ccproxy.lightllm.transform_to_provider")
    @patch("ccproxy.lightllm.context_cache.resolve_cached_content", side_effect=RuntimeError("cache boom"))
    def test_gemini_cache_failure_graceful(
        self,
        mock_cache: MagicMock,
        mock_transform: MagicMock,
        cleanup: None,
    ) -> None:
        _make_config_with_transforms(
            [
                {
                    "mode": "transform",
                    "match_host": "api.openai.com",
                    "match_path": "/",
                    "dest_provider": "gemini",
                    "dest_model": "gemini-2.0-flash",
                }
            ]
        )

        mock_transform.return_value = ("https://gemini.googleapis.com/v1", {}, b"{}")

        router = InspectorRouter(name="test_cache", request_passthrough=True, response_passthrough=True)
        register_transform_routes(router)

        flow = _make_flow(
            body={
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": "hello"}],
            }
        )
        router.request(flow)

        # Transform still proceeds despite cache failure
        mock_transform.assert_called_once()
        assert mock_transform.call_args.kwargs.get("cached_content") is None

    @patch("ccproxy.lightllm.transform_to_provider")
    def test_non_gemini_skips_context_cache(
        self,
        mock_transform: MagicMock,
        cleanup: None,
    ) -> None:
        _make_config_with_transforms(
            [
                {
                    "mode": "transform",
                    "match_host": "api.openai.com",
                    "match_path": "/",
                    "dest_provider": "anthropic",
                    "dest_model": "claude-3",
                }
            ]
        )

        mock_transform.return_value = ("https://api.anthropic.com/v1/messages", {}, b"{}")

        router = InspectorRouter(name="test_cache", request_passthrough=True, response_passthrough=True)
        register_transform_routes(router)

        flow = _make_flow()
        with patch("ccproxy.lightllm.context_cache.resolve_cached_content") as mock_cache:
            router.request(flow)
            mock_cache.assert_not_called()


class TestResponseTransformExceptionHandling:
    """Tests for response-phase exception handling."""

    @patch("ccproxy.lightllm.transform_to_openai", side_effect=RuntimeError("transform exploded"))
    def test_transform_exception_passes_through(self, mock_transform: MagicMock, cleanup: None) -> None:
        from ccproxy.config import CCProxyConfig

        config = CCProxyConfig()
        set_config_instance(config)

        from ccproxy.inspector.flow_store import TransformMeta

        router = InspectorRouter(name="test_resp", request_passthrough=True, response_passthrough=True)
        register_transform_routes(router)

        meta = TransformMeta(
            provider="anthropic",
            model="claude-3",
            request_data={"messages": [{"role": "user", "content": "hi"}], "max_tokens": 100},
            is_streaming=False,
            mode="transform",
        )
        record = FlowRecord(direction="inbound", transform=meta)

        flow = MagicMock()
        flow.request.pretty_host = "api.anthropic.com"
        flow.request.path = "/v1/messages"
        flow.request.content = b"{}"
        flow.request.headers = {}
        flow.client_conn.proxy_mode = ProxyMode.parse("reverse:http://localhost:1@4001")
        flow.response = MagicMock()
        flow.response.status_code = 200
        flow.response.content = b'{"original": true}'
        resp_headers = MagicMock()
        resp_headers.items.return_value = [("content-type", "application/json")]
        flow.response.headers = resp_headers
        flow.metadata = {InspectorMeta.DIRECTION: "inbound", InspectorMeta.RECORD: record}
        flow.server_conn = MagicMock()

        original_content = flow.response.content
        router.response(flow)

        # Response content unchanged — exception was caught
        assert flow.response.content == original_content
