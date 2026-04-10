"""Tests for ccproxy.inspector.routes.transform — lightllm transform routes."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ccproxy.config import InspectorConfig, TransformRoute, set_config_instance
from ccproxy.inspector.flow_store import InspectorMeta
from ccproxy.inspector.router import InspectorRouter
from ccproxy.inspector.routes.transform import (
    _resolve_api_key,
    _resolve_transform_target,
    register_transform_routes,
)


def _make_flow(
    host: str = "api.openai.com",
    path: str = "/v1/chat/completions",
    body: dict[str, Any] | None = None,
    direction: str = "inbound",
) -> MagicMock:
    """Build a mock HTTPFlow for testing transform routes."""
    flow = MagicMock()
    flow.request.pretty_host = host
    flow.request.host = host
    flow.request.path = path
    flow.request.port = 443
    flow.request.scheme = "https"
    flow.request.headers = {}
    flow.request.content = json.dumps(body or {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "hello"}],
    }).encode()
    flow.metadata = {InspectorMeta.DIRECTION: direction}
    flow.server_conn = MagicMock()
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
        _make_config_with_transforms([{
            "match_host": "api.openai.com",
            "match_path": "/v1/chat/completions",
            "dest_provider": "anthropic",
            "dest_model": "claude-3-5-sonnet-20241022",
        }])
        flow = _make_flow(host="api.openai.com", path="/v1/chat/completions")
        target = _resolve_transform_target(flow)
        assert target is not None
        assert target.dest_provider == "anthropic"

    def test_no_match_different_host(self, cleanup: None) -> None:
        _make_config_with_transforms([{
            "match_host": "api.openai.com",
            "match_path": "/v1/chat/completions",
            "dest_provider": "anthropic",
            "dest_model": "claude-3-5-sonnet-20241022",
        }])
        flow = _make_flow(host="api.anthropic.com", path="/v1/messages")
        assert _resolve_transform_target(flow) is None

    def test_no_match_different_path(self, cleanup: None) -> None:
        _make_config_with_transforms([{
            "match_host": "api.openai.com",
            "match_path": "/v1/chat/completions",
            "dest_provider": "anthropic",
            "dest_model": "claude-3-5-sonnet-20241022",
        }])
        flow = _make_flow(host="api.openai.com", path="/v1/embeddings")
        assert _resolve_transform_target(flow) is None

    def test_empty_transforms(self, cleanup: None) -> None:
        _make_config_with_transforms([])
        flow = _make_flow()
        assert _resolve_transform_target(flow) is None

    def test_first_match_wins(self, cleanup: None) -> None:
        _make_config_with_transforms([
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
        ])
        flow = _make_flow()
        target = _resolve_transform_target(flow)
        assert target is not None
        assert target.dest_model == "claude-first"

    def test_path_prefix_match(self, cleanup: None) -> None:
        _make_config_with_transforms([{
            "match_host": "api.openai.com",
            "match_path": "/v1/",
            "dest_provider": "anthropic",
            "dest_model": "claude-3-5-sonnet-20241022",
        }])
        flow = _make_flow(host="api.openai.com", path="/v1/chat/completions")
        target = _resolve_transform_target(flow)
        assert target is not None


class TestResolveApiKey:
    def test_none_ref(self) -> None:
        target = TransformRoute(
            match_host="x", dest_provider="anthropic",
            dest_model="m", dest_api_key_ref=None,
        )
        assert _resolve_api_key(target) is None

    def test_env_var_fallback(self, monkeypatch: pytest.MonkeyPatch, cleanup: None) -> None:
        monkeypatch.setenv("MY_API_KEY", "env-key-value")
        from ccproxy.config import CCProxyConfig
        config = CCProxyConfig()
        set_config_instance(config)

        target = TransformRoute(
            match_host="x", dest_provider="anthropic",
            dest_model="m", dest_api_key_ref="MY_API_KEY",
        )
        result = _resolve_api_key(target)
        assert result == "env-key-value"


class TestHandleTransform:
    def test_skips_outbound_flows(self, cleanup: None) -> None:
        _make_config_with_transforms([{
            "match_host": "api.openai.com",
            "match_path": "/",
            "dest_provider": "anthropic",
            "dest_model": "claude-3-5-sonnet-20241022",
        }])
        router = InspectorRouter(
            name="test_transform", request_passthrough=True, response_passthrough=True,
        )
        register_transform_routes(router)

        flow = _make_flow(direction="outbound")
        original_content = flow.request.content
        router.request(flow)
        assert flow.request.content == original_content

    def test_skips_unmatched_flows(self, cleanup: None) -> None:
        _make_config_with_transforms([{
            "match_host": "api.openai.com",
            "match_path": "/v1/chat/completions",
            "dest_provider": "anthropic",
            "dest_model": "claude-3-5-sonnet-20241022",
        }])
        router = InspectorRouter(
            name="test_transform", request_passthrough=True, response_passthrough=True,
        )
        register_transform_routes(router)

        flow = _make_flow(host="api.other.com")
        original_content = flow.request.content
        router.request(flow)
        assert flow.request.content == original_content

    @patch("ccproxy.lightllm.transform_to_provider")
    def test_rewrites_matched_flow(self, mock_transform: MagicMock, cleanup: None) -> None:
        _make_config_with_transforms([{
            "match_host": "api.openai.com",
            "match_path": "/v1/chat/completions",
            "dest_provider": "anthropic",
            "dest_model": "claude-3-5-sonnet-20241022",
        }])
        mock_transform.return_value = (
            "https://api.anthropic.com/v1/messages",
            {"x-api-key": "test-key", "anthropic-version": "2023-06-01"},
            b'{"model": "claude-3-5-sonnet-20241022", "messages": []}',
        )

        router = InspectorRouter(
            name="test_transform", request_passthrough=True, response_passthrough=True,
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
        _make_config_with_transforms([{
            "match_host": "api.openai.com",
            "match_path": "/",
            "dest_provider": "anthropic",
            "dest_model": "claude-3-5-sonnet-20241022",
            "dest_api_key_ref": None,
        }])
        mock_transform.return_value = ("https://api.anthropic.com/v1/messages", {}, b"{}")

        flow = _make_flow(body={
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0.7,
            "stream": True,
        })

        router = InspectorRouter(
            name="test_transform", request_passthrough=True, response_passthrough=True,
        )
        register_transform_routes(router)
        router.request(flow)

        mock_transform.assert_called_once()
        call_kwargs = mock_transform.call_args
        assert call_kwargs.kwargs.get("model") or call_kwargs[1].get("model") or call_kwargs[0][0] == "claude-3-5-sonnet-20241022"
