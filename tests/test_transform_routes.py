"""Tests for ccproxy.inspector.routes.transform — lightllm transform routes."""

from __future__ import annotations

import json
from typing import Any, cast
from unittest.mock import MagicMock, patch

from mitmproxy.proxy.mode_specs import ProxyMode

from ccproxy.auth.sources import CommandAuthSource, EnvironmentAuthSource
from ccproxy.config import (
    CCProxyConfig,
    LightllmConfig,
    ModelBinding,
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


def _make_flow(
    host: str = "api.openai.com",
    path: str = "/v1/chat/completions",
    body: dict[str, Any] | None = None,
    direction: str = "inbound",
    proxy_mode: Any = None,
) -> Any:
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
    lightllm = LightllmConfig(transforms=overrides)
    config = CCProxyConfig(lightllm=lightllm)
    set_config_instance(config)


def _make_config_with_providers(providers: dict[str, Provider]) -> CCProxyConfig:
    """Set up a CCProxyConfig with sentinel-keyed Provider entries."""
    config = CCProxyConfig(providers=providers)
    set_config_instance(config)
    return config


def _make_provider(
    *,
    command: str = "echo tok",
    header: str | None = None,
    host: str = "api.anthropic.com",
    path: str = "/v1/messages",
    type: str = "anthropic",
) -> Provider:
    """Build a Provider with a CommandAuthSource for tests."""
    return Provider(
        auth=CommandAuthSource(command=command, header=header) if command else None,
        base_url=f"https://{host}",
        path=path,
        type=type,
    )


class TestResolveTransformTarget:
    def test_matches_host_and_path(self) -> None:
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
        assert isinstance(target, TransformOverride)
        assert target.dest_provider == "anthropic"

    def test_no_match_different_host(self) -> None:
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

    def test_no_match_different_path(self) -> None:
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

    def test_empty_transforms(self) -> None:
        _make_config_with_transforms([])
        flow = _make_flow()
        assert _resolve_transform_target(flow) is None

    def test_first_match_wins(self) -> None:
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
        assert isinstance(target, TransformOverride)
        assert target.dest_model == "claude-first"

    def test_path_prefix_match(self) -> None:
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

    def test_match_model(self) -> None:
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
        assert isinstance(target, TransformOverride)
        assert target.dest_provider == "anthropic"

    def test_match_model_no_match(self) -> None:
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

    def test_null_match_host_matches_any(self) -> None:
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
    """Resolve target via flow.metadata['ccproxy.auth_provider'] when no override matches."""

    def test_returns_provider_for_known_sentinel(self) -> None:
        provider = _make_provider(host="api.anthropic.com", path="/v1/messages", type="anthropic")
        _make_config_with_providers({"anthropic": provider})

        flow = _make_flow(host="proxy.local", path="/v1/chat/completions")
        flow.metadata["ccproxy.auth_provider"] = "anthropic"

        target = _resolve_transform_target(flow)
        assert isinstance(target, Provider)
        assert target is provider

    def test_returns_none_when_no_override_and_no_sentinel(self) -> None:
        _make_config_with_providers({})
        flow = _make_flow(host="proxy.local", path="/v1/chat/completions")
        assert _resolve_transform_target(flow) is None

    def test_returns_none_when_sentinel_provider_not_registered(self) -> None:
        _make_config_with_providers({})
        flow = _make_flow(host="proxy.local", path="/v1/chat/completions")
        flow.metadata["ccproxy.auth_provider"] = "anthropic"
        assert _resolve_transform_target(flow) is None

    def test_override_wins_over_sentinel(self) -> None:
        """First-match override beats the sentinel-resolved Provider fallback."""
        from ccproxy.config import CCProxyConfig

        sentinel_provider = _make_provider(host="api.anthropic.com", type="anthropic")
        override = TransformOverride(
            match_host="proxy.local",
            match_path="/v1/chat/completions",
            dest_provider="anthropic",
            dest_model="claude-3-5-sonnet-20241022",
        )
        config = CCProxyConfig(
            lightllm=LightllmConfig(transforms=[override]),
            providers={"anthropic": sentinel_provider},
        )
        set_config_instance(config)

        flow = _make_flow(host="proxy.local", path="/v1/chat/completions")
        flow.metadata["ccproxy.auth_provider"] = "anthropic"

        target = _resolve_transform_target(flow)
        assert isinstance(target, TransformOverride)
        assert target is override


class TestHandleTransform:
    def test_skips_outbound_flows(self) -> None:
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

    def test_skips_unmatched_flows(self) -> None:
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

    @patch("ccproxy.lightllm.graph.dispatch_dump_sync")
    def test_rewrites_matched_flow(
        self,
        mock_render: MagicMock,
    ) -> None:
        # transform action with an override requires a registered Provider entry
        # for dest_provider so the handler can resolve the destination format.
        config = CCProxyConfig(
            lightllm=LightllmConfig(
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
                "anthropic": _make_provider(host="api.anthropic.com", type="anthropic"),
            },
        )
        set_config_instance(config)
        mock_render.return_value = b'{"model": "claude-3-5-sonnet-20241022", "messages": []}'

        router = InspectorRouter(
            name="test_transform",
            request_passthrough=True,
            response_passthrough=True,
        )
        register_transform_routes(router)

        flow = _make_flow()
        router.request(flow)

        # URL came from the bound Provider's host + path (no {action} for /v1/messages).
        assert flow.request.host == "api.anthropic.com"
        assert flow.request.port == 443
        assert flow.request.scheme == "https"
        assert flow.request.path == "/v1/messages"
        # Anthropic-compatible upstream gets the anthropic-version floor.
        assert flow.request.headers.get("anthropic-version") == "2023-06-01"
        assert flow.request.content == b'{"model": "claude-3-5-sonnet-20241022", "messages": []}'

    @patch("ccproxy.lightllm.graph.dispatch_dump_sync")
    def test_passes_messages_and_params(
        self,
        mock_render: MagicMock,
    ) -> None:
        config = CCProxyConfig(
            lightllm=LightllmConfig(
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
                "anthropic": _make_provider(host="api.anthropic.com", type="anthropic"),
            },
        )
        set_config_instance(config)
        mock_render.return_value = b"{}"

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

        # dispatch_dump_sync gets the parsed IR with the overridden model.
        mock_render.assert_called_once()
        call = mock_render.call_args
        parsed_arg = call.args[0]
        assert parsed_arg.model == "claude-3-5-sonnet-20241022"
        assert call.kwargs.get("provider_type") == "anthropic"

    def test_reverse_proxy_unmatched_returns_501(self) -> None:
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

    def test_wireguard_unmatched_passes_through(self) -> None:
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

    def test_passthrough_mode_leaves_flow_unchanged(self) -> None:
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

    def test_catches_unrewritten_reverse_proxy_destination(self) -> None:
        """Reverse proxy flow still targeting localhost:1 after transform gets 502."""
        _make_config_with_transforms(
            [
                {
                    "action": "redirect",
                    "match_host": "proxy.local",
                    "match_path": "/v1/",
                    "dest_provider": "anthropic",
                    # dest_base_url intentionally missing — _handle_redirect falls back
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
            "dest_base_url": "https://api.anthropic.com",
        }
        base.update(overrides or {})
        _make_config_with_transforms([base])

    def _make_redirect_flow(self, path: str = "/v1/messages", host: str = "proxy.local") -> MagicMock:
        record = FlowRecord(direction="inbound")
        flow = _make_flow(host=host, path=path)
        flow.metadata[InspectorMeta.RECORD] = record
        return cast(MagicMock, flow)

    def test_redirect_rewrites_host_and_port(self) -> None:
        self._make_redirect_config()
        router = InspectorRouter(name="test_redir", request_passthrough=True, response_passthrough=True)
        register_transform_routes(router)

        flow = self._make_redirect_flow()
        router.request(flow)

        assert flow.request.host == "api.anthropic.com"
        assert flow.request.port == 443
        assert flow.request.scheme == "https"

    def test_redirect_with_dest_path_override(self) -> None:
        self._make_redirect_config({"dest_path": "/v2/override"})
        router = InspectorRouter(name="test_redir", request_passthrough=True, response_passthrough=True)
        register_transform_routes(router)

        flow = self._make_redirect_flow(path="/v1/messages")
        router.request(flow)

        assert flow.request.path == "/v2/override"

    def test_redirect_missing_dest_base_url_passthrough(self) -> None:
        # No dest_base_url AND no providers entry for "anthropic" → handler returns
        # without rewriting; flow.request.host stays at the inbound value.
        _make_config_with_transforms(
            [
                {
                    "action": "redirect",
                    "match_host": "proxy.local",
                    "match_path": "/v1/",
                    "dest_provider": "anthropic",
                    # dest_base_url intentionally missing
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

    def test_redirect_stores_transform_meta(self) -> None:
        self._make_redirect_config()
        router = InspectorRouter(name="test_redir", request_passthrough=True, response_passthrough=True)
        register_transform_routes(router)

        flow = self._make_redirect_flow()
        router.request(flow)

        record = flow.metadata[InspectorMeta.RECORD]
        assert record.transform is not None
        assert record.transform.provider_type == "anthropic"

    def test_redirect_injects_api_key(self) -> None:
        """Override-driven redirect injects Authorization from the bound Provider."""
        config = CCProxyConfig(
            lightllm=LightllmConfig(
                transforms=[
                    TransformOverride(
                        action="redirect",
                        match_host="proxy.local",
                        match_path="/v1/",
                        dest_provider="anthropic",
                        dest_base_url="https://api.anthropic.com",
                    )
                ]
            ),
            providers={
                "anthropic": _make_provider(
                    command="printf '%s' injected-token",
                    host="api.anthropic.com",
                    path="/v1/messages",
                    type="anthropic",
                ),
            },
        )
        set_config_instance(config)

        router = InspectorRouter(name="test_redir", request_passthrough=True, response_passthrough=True)
        register_transform_routes(router)

        flow = self._make_redirect_flow()
        router.request(flow)

        assert flow.request.headers.get("authorization") == "Bearer injected-token"


class TestLiteLLMModelBindings:
    def test_exact_binding_precedes_earlier_wildcard(self) -> None:
        wildcard_provider = Provider(base_url="https://wildcard.example/v1", path="/chat/completions", type="openai")
        exact_provider = Provider(base_url="https://exact.example/v1", path="/chat/completions", type="openai")
        wildcard = ModelBinding.create(
            model_name="*",
            upstream_model="*",
            owned_by="openai",
            provider_name="wildcard",
            provider=wildcard_provider,
            request_defaults={},
            model_info={},
            source_index=0,
        )
        exact = ModelBinding.create(
            model_name="special",
            upstream_model="actual-special",
            owned_by="openai",
            provider_name="exact",
            provider=exact_provider,
            request_defaults={},
            model_info={},
            source_index=1,
        )
        set_config_instance(CCProxyConfig(model_bindings=[wildcard, exact]))

        target = _resolve_transform_target(_make_flow(body={"model": "special"}), {"model": "special"})

        assert target is exact

    def test_gemini_path_model_selects_binding(self) -> None:
        provider = Provider(
            base_url="https://generativelanguage.googleapis.com/v1beta",
            path="/models/{model}:{action}",
            type="gemini",
        )
        binding = ModelBinding.create(
            model_name="gemini-alias",
            upstream_model="gemini-2.5-pro",
            owned_by="gemini",
            provider_name="gemini",
            provider=provider,
            request_defaults={},
            model_info={},
            source_index=0,
        )
        set_config_instance(CCProxyConfig(model_bindings=[binding]))
        flow = _make_flow(
            host="proxy.local",
            path="/v1beta/models/gemini-alias:generateContent",
            body={"contents": [{"role": "user", "parts": [{"text": "hello"}]}]},
        )

        target = _resolve_transform_target(flow, json.loads(flow.request.content))

        assert target is binding

    def test_wildcard_binding_routes_http_port_rewrites_model_and_applies_defaults(
        self,
        monkeypatch: Any,
    ) -> None:
        monkeypatch.setenv("LOCAL_LLM_KEY", "local-token")
        provider = Provider(
            auth=EnvironmentAuthSource(variable="LOCAL_LLM_KEY"),
            base_url="http://127.0.0.1:18000/v1",
            path="/chat/completions",
            type="openai",
        )
        binding = ModelBinding.create(
            model_name="local/*",
            upstream_model="*",
            owned_by="openai",
            provider_name="litellm:0:local/*",
            provider=provider,
            request_defaults={"temperature": 0.25, "top_p": 0.9},
            model_info={},
            source_index=0,
        )
        config = CCProxyConfig(
            model_bindings=[binding],
            deployment_providers={binding.provider_name: provider},
        )
        set_config_instance(config)
        router = InspectorRouter(name="test_binding", request_passthrough=True, response_passthrough=True)
        register_transform_routes(router)
        flow = _make_flow(
            host="proxy.local",
            body={
                "model": "local/qwen3",
                "messages": [{"role": "user", "content": "hello"}],
                "temperature": 0.8,
            },
        )
        flow.request.headers = {"authorization": "Bearer client-key"}

        router.request(flow)

        assert flow.request.scheme == "http"
        assert flow.request.host == "127.0.0.1"
        assert flow.request.port == 18000
        assert flow.request.path == "/v1/chat/completions"
        assert flow.request.headers["authorization"] == "Bearer local-token"
        body = json.loads(flow.request.content)
        assert body["model"] == "qwen3"
        assert body["temperature"] == 0.8
        assert body["top_p"] == 0.9
        assert flow.metadata["ccproxy.auth_provider"] == binding.provider_name

    @patch("ccproxy.lightllm.graph.dispatch_dump_sync")
    def test_cross_format_binding_uses_native_provider_and_custom_auth_header(
        self,
        mock_render: MagicMock,
    ) -> None:
        provider = _make_provider(
            command="printf '%s' anthropic-token",
            header="x-api-key",
            host="api.anthropic.com",
            path="/v1/messages",
            type="anthropic",
        )
        binding = ModelBinding.create(
            model_name="claude",
            upstream_model="claude-sonnet-4-6",
            owned_by="anthropic",
            provider_name="anthropic",
            provider=provider,
            request_defaults={"max_tokens": 2048},
            model_info={},
            source_index=0,
        )
        set_config_instance(CCProxyConfig(providers={"anthropic": provider}, model_bindings=[binding]))
        mock_render.return_value = b'{"model":"claude-sonnet-4-6","messages":[]}'
        router = InspectorRouter(name="test_binding_transform", request_passthrough=True, response_passthrough=True)
        register_transform_routes(router)
        flow = _make_flow(
            host="proxy.local",
            body={"model": "claude", "messages": [{"role": "user", "content": "hello"}]},
        )

        router.request(flow)

        assert flow.request.host == "api.anthropic.com"
        assert flow.request.path == "/v1/messages"
        assert flow.request.headers["x-api-key"] == "anthropic-token"
        assert "authorization" not in flow.request.headers
        render_ctx = mock_render.call_args.args[0]
        assert render_ctx.model == "claude-sonnet-4-6"
        assert render_ctx.settings["max_tokens"] == 2048

    @patch("ccproxy.lightllm.graph.dispatch_dump_sync")
    def test_packaged_minimax_binding_routes_anthropic_endpoint(
        self,
        mock_render: MagicMock,
        monkeypatch: Any,
    ) -> None:
        from importlib.resources import as_file, files
        from pathlib import Path

        monkeypatch.setenv("MINIMAX_API_KEY", "test-key")
        with as_file(files("ccproxy.templates").joinpath("ccproxy.yaml")) as template_path:
            config = CCProxyConfig.from_yaml(Path(template_path))
        set_config_instance(config)
        mock_render.return_value = b'{"model":"MiniMax-M3","messages":[]}'
        router = InspectorRouter(name="test_minimax_binding", request_passthrough=True, response_passthrough=True)
        register_transform_routes(router)
        flow = _make_flow(
            path="/v1/chat/completions",
            body={"model": "MiniMax-M3", "messages": [{"role": "user", "content": "hello"}]},
        )

        router.request(flow)

        assert flow.request.scheme == "https"
        assert flow.request.host == "api.minimax.io"
        assert flow.request.path == "/anthropic/v1/messages"
        assert flow.request.headers["x-api-key"] == "test-key"
        assert "authorization" not in flow.request.headers

    def test_minimax_binding_routes_documented_openai_endpoints(
        self,
        monkeypatch: Any,
        tmp_path: Any,
    ) -> None:
        monkeypatch.setenv("MINIMAX_API_KEY", "test-key")
        for region, base_url, expected_host in (
            ("global", "https://api.minimax.io/v1", "api.minimax.io"),
            ("china", "https://api.minimaxi.com/v1", "api.minimaxi.com"),
        ):
            ccproxy_path = tmp_path / f"ccproxy-{region}.yaml"
            litellm_path = tmp_path / f"config-{region}.yaml"
            ccproxy_path.write_text(
                f"""
ccproxy:
  providers:
    minimax:
      auth:
        command: printenv MINIMAX_API_KEY
        type: command
      base_url: {base_url}
      path: /chat/completions
      type: openai
"""
            )
            litellm_path.write_text(
                """
model_list:
  - model_name: MiniMax-M3
    litellm_params:
      model: minimax/MiniMax-M3
"""
            )
            config = CCProxyConfig.from_yaml(ccproxy_path, litellm_path=litellm_path)
            set_config_instance(config)
            router = InspectorRouter(
                name=f"test_minimax_{region}",
                request_passthrough=True,
                response_passthrough=True,
            )
            register_transform_routes(router)
            flow = _make_flow(
                path="/v1/chat/completions",
                body={"model": "MiniMax-M3", "messages": [{"role": "user", "content": "hello"}]},
            )

            router.request(flow)

            assert flow.request.scheme == "https"
            assert flow.request.host == expected_host
            assert flow.request.path == "/v1/chat/completions"
            assert flow.request.headers["authorization"] == "Bearer test-key"
            assert "x-api-key" not in flow.request.headers


class TestGeminiTransform:
    """Tests for the unified Gemini transform path via dispatch_dump_sync."""

    @patch("ccproxy.lightllm.graph.dispatch_dump_sync")
    def test_gemini_streaming_action(
        self,
        mock_render: MagicMock,
    ) -> None:
        """A streaming Gemini transform produces ``:streamGenerateContent`` in the URL."""
        config = CCProxyConfig(
            lightllm=LightllmConfig(
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
                    host="cloudcode-pa.googleapis.com",
                    path="/v1internal:{action}",
                    type="gemini",
                ),
            },
        )
        set_config_instance(config)
        mock_render.return_value = b'{"contents": []}'

        router = InspectorRouter(name="test_gemini", request_passthrough=True, response_passthrough=True)
        register_transform_routes(router)

        flow = _make_flow(
            body={
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            }
        )
        router.request(flow)

        assert flow.request.host == "cloudcode-pa.googleapis.com"
        assert flow.request.path == "/v1internal:streamGenerateContent"
        # Non-Anthropic upstream: no anthropic-version floor.
        assert "anthropic-version" not in flow.request.headers
        mock_render.assert_called_once()
        assert mock_render.call_args.kwargs.get("provider_type") == "gemini"

    @patch("ccproxy.lightllm.graph.dispatch_dump_sync")
    def test_gemini_non_streaming_action(
        self,
        mock_render: MagicMock,
    ) -> None:
        """A non-streaming Gemini transform produces ``:generateContent``."""
        config = CCProxyConfig(
            lightllm=LightllmConfig(
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
                    host="cloudcode-pa.googleapis.com",
                    path="/v1internal:{action}",
                    type="gemini",
                ),
            },
        )
        set_config_instance(config)
        mock_render.return_value = b'{"contents": []}'

        router = InspectorRouter(name="test_gemini", request_passthrough=True, response_passthrough=True)
        register_transform_routes(router)

        flow = _make_flow(
            body={
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": "hello"}],
            }
        )
        router.request(flow)

        assert flow.request.path == "/v1internal:generateContent"


class TestResponseTransformExceptionHandling:
    """Tests for response-phase exception handling."""

    @patch(
        "ccproxy.lightllm.graph.buffered.transform_buffered_response_sync",
        side_effect=RuntimeError("transform exploded"),
    )
    def test_transform_exception_returns_500_error(self, _mock_transform: MagicMock) -> None:
        config = CCProxyConfig()
        set_config_instance(config)

        from ccproxy.flows.store import TransformMeta

        router = InspectorRouter(name="test_resp", request_passthrough=True, response_passthrough=True)
        register_transform_routes(router)

        meta = TransformMeta(
            provider_type="anthropic",
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

        router.response(flow)

        # The untransformed provider body would be the wrong wire format for
        # the client — the route replaces it with an OpenAI-shape 500 error.
        assert flow.response.status_code == 500
        error = json.loads(flow.response.content)["error"]
        assert error["type"] == "api_error"
        assert error["code"] == 500
        assert "transform exploded" in error["message"]
