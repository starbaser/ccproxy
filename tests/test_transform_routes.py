"""Tests for ccproxy.inspector.routes.transform — lightllm transform routes."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

from mitmproxy.proxy.mode_specs import ProxyMode

from ccproxy.config import (
    CCProxyConfig,
    InspectorConfig,
    Provider,
    TransformOverride,
    set_config_instance,
)
from ccproxy.flows.store import FlowRecord, InspectorMeta
from ccproxy.inspector.router import InspectorRouter
from ccproxy.inspector.routes.transform import (
    _resolve_transform_target,
    register_transform_routes,
)
from ccproxy.oauth.sources import CommandAuthSource


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
    """Set up a CCProxyConfig with transform override rules."""
    overrides = [TransformOverride(**t) for t in transforms]
    inspector = InspectorConfig(transforms=overrides)
    config = CCProxyConfig(inspector=inspector)
    set_config_instance(config)


def _make_config_with_providers(providers: dict[str, Provider]) -> CCProxyConfig:
    """Set up a CCProxyConfig with sentinel-keyed Provider entries."""
    config = CCProxyConfig(providers=providers, inspector=InspectorConfig())
    set_config_instance(config)
    return config


def _make_provider(
    *,
    command: str = "echo tok",
    header: str | None = None,
    host: str = "api.anthropic.com",
    path: str = "/v1/messages",
    provider: str = "anthropic",
) -> Provider:
    """Build a Provider with a CommandAuthSource for tests."""
    return Provider(
        auth=CommandAuthSource(command=command, header=header) if command else None,
        host=host,
        path=path,
        provider=provider,
    )


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


class TestSentinelResolvedProvider:
    """Resolve target via flow.metadata['ccproxy.oauth_provider'] when no override matches."""

    def test_returns_provider_for_known_sentinel(self, cleanup: None) -> None:
        provider = _make_provider(host="api.anthropic.com", path="/v1/messages", provider="anthropic")
        _make_config_with_providers({"anthropic": provider})

        flow = _make_flow(host="proxy.local", path="/v1/chat/completions")
        flow.metadata["ccproxy.oauth_provider"] = "anthropic"

        target = _resolve_transform_target(flow)
        assert isinstance(target, Provider)
        assert target is provider

    def test_returns_none_when_no_override_and_no_sentinel(self, cleanup: None) -> None:
        _make_config_with_providers({})
        flow = _make_flow(host="proxy.local", path="/v1/chat/completions")
        assert _resolve_transform_target(flow) is None

    def test_returns_none_when_sentinel_provider_not_registered(self, cleanup: None) -> None:
        _make_config_with_providers({})
        flow = _make_flow(host="proxy.local", path="/v1/chat/completions")
        flow.metadata["ccproxy.oauth_provider"] = "anthropic"
        assert _resolve_transform_target(flow) is None

    def test_override_wins_over_sentinel(self, cleanup: None) -> None:
        """First-match override beats the sentinel-resolved Provider fallback."""
        from ccproxy.config import CCProxyConfig

        sentinel_provider = _make_provider(host="api.anthropic.com", provider="anthropic")
        override = TransformOverride(
            match_host="proxy.local",
            match_path="/v1/chat/completions",
            dest_provider="anthropic",
            dest_model="claude-3-5-sonnet-20241022",
        )
        config = CCProxyConfig(
            inspector=InspectorConfig(transforms=[override]),
            providers={"anthropic": sentinel_provider},
        )
        set_config_instance(config)

        flow = _make_flow(host="proxy.local", path="/v1/chat/completions")
        flow.metadata["ccproxy.oauth_provider"] = "anthropic"

        target = _resolve_transform_target(flow)
        assert isinstance(target, TransformOverride)
        assert target is override


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
        # transform action with an override requires a registered Provider entry
        # for dest_provider so the handler can resolve the LiteLLM format.
        config = CCProxyConfig(
            inspector=InspectorConfig(
                transforms=[
                    TransformOverride(
                        action="transform",
                        match_host="api.openai.com",
                        match_path="/v1/chat/completions",
                        dest_provider="anthropic",
                        dest_model="claude-3-5-sonnet-20241022",
                    )
                ]
            ),
            providers={
                "anthropic": _make_provider(host="api.anthropic.com", provider="anthropic"),
            },
        )
        set_config_instance(config)
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
        config = CCProxyConfig(
            inspector=InspectorConfig(
                transforms=[
                    TransformOverride(
                        action="transform",
                        match_host="api.openai.com",
                        match_path="/",
                        dest_provider="anthropic",
                        dest_model="claude-3-5-sonnet-20241022",
                    )
                ]
            ),
            providers={
                "anthropic": _make_provider(host="api.anthropic.com", provider="anthropic"),
            },
        )
        set_config_instance(config)
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
        body = json.loads(flow.response.content)
        assert body["error"]["type"] == "not_implemented_error"

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
                    "action": "passthrough",
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


class TestSafetyNet:
    """Tests for the localhost:1 safety net in handle_transform."""

    def test_catches_unrewritten_reverse_proxy_destination(self, cleanup: None) -> None:
        """Reverse proxy flow still targeting localhost:1 after transform gets 502."""
        _make_config_with_transforms(
            [
                {
                    "action": "redirect",
                    "match_host": "proxy.local",
                    "match_path": "/v1/",
                    "dest_provider": "anthropic",
                    # dest_host intentionally missing — _handle_redirect falls back
                }
            ]
        )
        router = InspectorRouter(
            name="test_safety",
            request_passthrough=True,
            response_passthrough=True,
        )
        register_transform_routes(router)

        flow = _make_flow(
            host="proxy.local",
            path="/v1/messages",
            proxy_mode=ProxyMode.parse("reverse:http://localhost:1@4001"),
        )
        flow.request.host = "localhost"
        flow.request.port = 1
        flow.response = None
        router.request(flow)

        assert flow.response is not None
        assert flow.response.status_code == 502
        body = json.loads(flow.response.content)
        assert body["error"]["type"] == "api_error"
        assert "transform failed" in body["error"]["message"]


class TestHandleRedirect:
    """Tests for redirect mode — host rewriting, path override, auth injection."""

    def _make_redirect_config(self, overrides: dict[str, Any] | None = None) -> None:
        base = {
            "action": "redirect",
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

    def test_redirect_missing_dest_host_passthrough(self, cleanup: None) -> None:
        # No dest_host AND no providers entry for "anthropic" → handler returns
        # without rewriting; flow.request.host stays at the inbound value.
        _make_config_with_transforms(
            [
                {
                    "action": "redirect",
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
        """Override-driven redirect injects Authorization from the bound Provider."""
        config = CCProxyConfig(
            inspector=InspectorConfig(
                transforms=[
                    TransformOverride(
                        action="redirect",
                        match_host="proxy.local",
                        match_path="/v1/",
                        dest_provider="anthropic",
                        dest_host="api.anthropic.com",
                    )
                ]
            ),
            providers={
                "anthropic": Provider(
                    auth=CommandAuthSource(command="echo tok"),
                    host="api.anthropic.com",
                    path="/v1/messages",
                    provider="anthropic",
                ),
            },
        )
        config._cached_auth_tokens["anthropic"] = "injected-token"
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
        config = CCProxyConfig(
            inspector=InspectorConfig(
                transforms=[
                    TransformOverride(
                        action="transform",
                        match_host="api.openai.com",
                        match_path="/",
                        dest_provider="gemini",
                        dest_model="gemini-2.0-flash",
                    )
                ]
            ),
            providers={
                "gemini": _make_provider(
                    host="generativelanguage.googleapis.com",
                    path="/v1beta",
                    provider="gemini",
                ),
            },
        )
        set_config_instance(config)

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
        config = CCProxyConfig(
            inspector=InspectorConfig(
                transforms=[
                    TransformOverride(
                        action="transform",
                        match_host="api.openai.com",
                        match_path="/",
                        dest_provider="gemini",
                        dest_model="gemini-2.0-flash",
                    )
                ]
            ),
            providers={
                "gemini": _make_provider(
                    host="generativelanguage.googleapis.com",
                    path="/v1beta",
                    provider="gemini",
                ),
            },
        )
        set_config_instance(config)

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
        config = CCProxyConfig(
            inspector=InspectorConfig(
                transforms=[
                    TransformOverride(
                        action="transform",
                        match_host="api.openai.com",
                        match_path="/",
                        dest_provider="anthropic",
                        dest_model="claude-3",
                    )
                ]
            ),
            providers={
                "anthropic": _make_provider(host="api.anthropic.com", provider="anthropic"),
            },
        )
        set_config_instance(config)

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
        config = CCProxyConfig()
        set_config_instance(config)

        from ccproxy.flows.store import TransformMeta

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
