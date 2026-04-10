"""Tests for hook coverage gaps."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from ccproxy.pipeline.context import Context


def _make_ctx(
    model: str = "anthropic/claude-sonnet-4-5-20250929",
    metadata: dict | None = None,
    headers: dict | None = None,
    api_base: str = "https://api.anthropic.com",
    api_key: str | None = None,
) -> Context:
    litellm_params: dict = {"model": model, "api_base": api_base}
    if api_key:
        litellm_params["api_key"] = api_key
    data: dict = {
        "model": model,
        "messages": [{"role": "user", "content": "hello"}],
        "metadata": {
            "ccproxy_litellm_model": model,
            "ccproxy_model_config": {"litellm_params": litellm_params},
            "ccproxy_oauth_provider": "anthropic",
            **(metadata or {}),
        },
        "provider_specific_header": {"extra_headers": {}},
        "proxy_server_request": {"headers": headers or {"user-agent": "claude-cli/1.0"}},
    }
    return Context.from_litellm_data(data)


# ---------------------------------------------------------------------------
# inject_claude_code_identity
# ---------------------------------------------------------------------------


class TestInjectClaudeCodeIdentityHook:
    def _make_ctx_with_system(self, system=None, api_key=None, api_base="https://api.anthropic.com"):
        litellm_params: dict = {"model": "test-model", "api_base": api_base}
        if api_key:
            litellm_params["api_key"] = api_key
        data: dict = {
            "model": "test-model",
            "messages": [{"role": "user", "content": "hello"}],
            "metadata": {
                "ccproxy_litellm_model": "test-model",
                "ccproxy_model_config": {"litellm_params": litellm_params},
                "ccproxy_oauth_provider": "anthropic",
            },
            "provider_specific_header": {"extra_headers": {}},
            "proxy_server_request": {"headers": {"user-agent": "claude-cli/1.0"}},
        }
        if system is not None:
            data["system"] = system
        return Context.from_litellm_data(data)

    def test_skips_when_model_has_api_key(self):
        from ccproxy.hooks.inject_claude_code_identity import inject_claude_code_identity

        ctx = self._make_ctx_with_system(system="Original system", api_key="sk-my-own-key")
        result = inject_claude_code_identity(ctx, {})
        assert result.system == "Original system"

    def test_skips_for_non_anthropic_api_base(self):
        from ccproxy.hooks.inject_claude_code_identity import inject_claude_code_identity

        ctx = self._make_ctx_with_system(system="My system", api_base="https://other-provider.com")
        result = inject_claude_code_identity(ctx, {})
        assert result.system == "My system"

    def test_prepends_to_string_system(self):
        from ccproxy.constants import CLAUDE_CODE_SYSTEM_PREFIX
        from ccproxy.hooks.inject_claude_code_identity import inject_claude_code_identity

        ctx = self._make_ctx_with_system(system="You are a helpful assistant.")
        result = inject_claude_code_identity(ctx, {})
        assert isinstance(result.system, str)
        assert result.system.startswith(CLAUDE_CODE_SYSTEM_PREFIX)

    def test_prepends_block_to_list_system(self):
        from ccproxy.constants import CLAUDE_CODE_SYSTEM_PREFIX
        from ccproxy.hooks.inject_claude_code_identity import inject_claude_code_identity

        ctx = self._make_ctx_with_system(system=[{"type": "text", "text": "You are helpful."}])
        result = inject_claude_code_identity(ctx, {})
        assert isinstance(result.system, list)
        assert result.system[0]["text"] == CLAUDE_CODE_SYSTEM_PREFIX

    def test_no_double_prefix_on_string(self):
        from ccproxy.constants import CLAUDE_CODE_SYSTEM_PREFIX
        from ccproxy.hooks.inject_claude_code_identity import inject_claude_code_identity

        ctx = self._make_ctx_with_system(system=f"{CLAUDE_CODE_SYSTEM_PREFIX}\n\nAlready prefixed.")
        result = inject_claude_code_identity(ctx, {})
        assert isinstance(result.system, str)
        assert result.system.count(CLAUDE_CODE_SYSTEM_PREFIX) == 1

    def test_no_double_prefix_on_list(self):
        from ccproxy.constants import CLAUDE_CODE_SYSTEM_PREFIX
        from ccproxy.hooks.inject_claude_code_identity import inject_claude_code_identity

        ctx = self._make_ctx_with_system(system=[{"type": "text", "text": CLAUDE_CODE_SYSTEM_PREFIX}])
        result = inject_claude_code_identity(ctx, {})
        assert isinstance(result.system, list)
        count = sum(1 for b in result.system if isinstance(b, dict) and b.get("text") == CLAUDE_CODE_SYSTEM_PREFIX)
        assert count == 1

    def test_no_system_message_adds_one(self):
        from ccproxy.constants import CLAUDE_CODE_SYSTEM_PREFIX
        from ccproxy.hooks.inject_claude_code_identity import inject_claude_code_identity

        ctx = self._make_ctx_with_system()
        result = inject_claude_code_identity(ctx, {})
        assert result.system == CLAUDE_CODE_SYSTEM_PREFIX


# ---------------------------------------------------------------------------
# forward_apikey
# ---------------------------------------------------------------------------


class TestForwardApikeyHook:
    def test_forwards_api_key_to_extra_headers(self):
        from ccproxy.hooks.forward_apikey import forward_apikey

        data: dict = {
            "model": "test",
            "messages": [],
            "metadata": {},
            "provider_specific_header": {"extra_headers": {}},
            "proxy_server_request": {
                "headers": {"x-api-key": "mykey123"},
            },
            "secret_fields": {"raw_headers": {"x-api-key": "mykey123"}},
        }
        ctx = Context.from_litellm_data(data)
        result = forward_apikey(ctx, {})
        assert result.provider_headers.get("extra_headers", {}).get("x-api-key") == "mykey123"

    def test_creates_extra_headers_if_missing(self):
        from ccproxy.hooks.forward_apikey import forward_apikey

        data: dict = {
            "model": "test",
            "messages": [],
            "metadata": {},
            "provider_specific_header": {},
            "proxy_server_request": {
                "headers": {"x-api-key": "mykey123"},
            },
            "secret_fields": {"raw_headers": {"x-api-key": "mykey123"}},
        }
        ctx = Context.from_litellm_data(data)
        result = forward_apikey(ctx, {})
        assert result.provider_headers.get("extra_headers", {}).get("x-api-key") == "mykey123"

    def test_guard_false_when_no_api_key(self):
        from ccproxy.hooks.forward_apikey import forward_apikey_guard

        data: dict = {
            "model": "test",
            "messages": [],
            "metadata": {},
            "proxy_server_request": {"headers": {}},
        }
        ctx = Context.from_litellm_data(data)
        assert forward_apikey_guard(ctx) is False

    def test_guard_true_when_api_key_present(self):
        from ccproxy.hooks.forward_apikey import forward_apikey_guard

        data: dict = {
            "model": "test",
            "messages": [],
            "metadata": {},
            "proxy_server_request": {"headers": {}},
            "secret_fields": {"raw_headers": {"x-api-key": "mykey"}},
        }
        ctx = Context.from_litellm_data(data)
        assert forward_apikey_guard(ctx) is True

    def test_returns_ctx_when_no_api_key(self):
        """When api_key is empty, returns ctx unchanged."""
        from ccproxy.hooks.forward_apikey import forward_apikey

        data: dict = {
            "model": "test",
            "messages": [],
            "metadata": {},
            "provider_specific_header": {"extra_headers": {}},
            "proxy_server_request": {"headers": {}},
        }
        ctx = Context.from_litellm_data(data)
        result = forward_apikey(ctx, {})
        assert result.provider_headers.get("extra_headers", {}).get("x-api-key") is None


# ---------------------------------------------------------------------------
# capture_headers
# ---------------------------------------------------------------------------


class TestCaptureHeadersHook:
    def test_captures_headers_to_trace_metadata(self):
        from ccproxy.hooks.capture_headers import capture_headers

        data: dict = {
            "model": "test",
            "messages": [],
            "metadata": {},
            "proxy_server_request": {
                "headers": {"user-agent": "my-agent"},
            },
        }
        ctx = Context.from_litellm_data(data)
        result = capture_headers(ctx, {})
        assert "header_user-agent" in result.metadata.get("trace_metadata", {})

    def test_headers_filter_applied(self):
        from ccproxy.hooks.capture_headers import capture_headers

        data: dict = {
            "model": "test",
            "messages": [],
            "metadata": {},
            "proxy_server_request": {
                "headers": {"user-agent": "my-agent", "x-custom": "val"},
            },
        }
        ctx = Context.from_litellm_data(data)
        result = capture_headers(ctx, {"headers": ["user-agent"]})
        tm = result.metadata.get("trace_metadata", {})
        assert "header_user-agent" in tm
        assert "header_x-custom" not in tm

    def test_captures_http_method(self):
        from ccproxy.hooks.capture_headers import capture_headers

        data: dict = {
            "model": "test",
            "messages": [],
            "metadata": {},
            "proxy_server_request": {
                "headers": {},
                "method": "POST",
            },
        }
        ctx = Context.from_litellm_data(data)
        result = capture_headers(ctx, {})
        assert result.metadata["trace_metadata"]["http_method"] == "POST"

    def test_captures_http_path(self):
        from ccproxy.hooks.capture_headers import capture_headers

        data: dict = {
            "model": "test",
            "messages": [],
            "metadata": {},
            "proxy_server_request": {
                "headers": {},
                "url": "http://localhost:4000/v1/messages",
            },
        }
        ctx = Context.from_litellm_data(data)
        result = capture_headers(ctx, {})
        assert result.metadata["trace_metadata"]["http_path"] == "/v1/messages"

    def test_assigns_litellm_call_id_when_missing(self):
        from ccproxy.hooks.capture_headers import capture_headers

        data: dict = {
            "model": "test",
            "messages": [],
            "metadata": {},
            "proxy_server_request": {"headers": {}},
        }
        ctx = Context.from_litellm_data(data)
        assert not ctx.litellm_call_id
        result = capture_headers(ctx, {})
        assert result.litellm_call_id

    def test_guard_false_when_no_proxy_request(self):
        from ccproxy.hooks.capture_headers import capture_headers_guard

        data: dict = {"model": "test", "messages": [], "metadata": {}}
        ctx = Context.from_litellm_data(data)
        assert capture_headers_guard(ctx) is False

    def test_skips_empty_header_values(self):
        from ccproxy.hooks.capture_headers import capture_headers

        data: dict = {
            "model": "test",
            "messages": [],
            "metadata": {},
            "proxy_server_request": {
                "headers": {"empty-header": "", "real-header": "value"},
            },
        }
        ctx = Context.from_litellm_data(data)
        result = capture_headers(ctx, {})
        tm = result.metadata["trace_metadata"]
        assert "header_empty-header" not in tm
        assert "header_real-header" in tm


# ---------------------------------------------------------------------------
# model_router
# ---------------------------------------------------------------------------


class TestModelRouterHook:
    def test_router_none_returns_ctx(self):
        from ccproxy.config import CCProxyConfig, set_config_instance
        from ccproxy.hooks.model_router import model_router

        config = CCProxyConfig()
        set_config_instance(config)

        data: dict = {
            "model": "test",
            "messages": [],
            "metadata": {"ccproxy_model_name": "test"},
        }
        ctx = Context.from_litellm_data(data)
        result = model_router(ctx, {})
        assert result is ctx

    def test_routes_to_model_on_reload(self):
        """When router doesn't have model initially but finds it after reload."""
        from ccproxy.config import CCProxyConfig, set_config_instance
        from ccproxy.hooks.model_router import model_router

        config = CCProxyConfig(default_model_passthrough=False)
        set_config_instance(config)

        mock_router = MagicMock()
        # First call returns None, second (after reload) returns config
        mock_router.get_model_for_label.side_effect = [
            None,
            {"litellm_params": {"model": "claude-sonnet-4-5-20250929", "api_base": "https://api.anthropic.com"}},
        ]

        data: dict = {
            "model": "test",
            "messages": [],
            "metadata": {"ccproxy_model_name": "special"},
        }
        ctx = Context.from_litellm_data(data)
        result = model_router(ctx, {"router": mock_router})
        assert result.ccproxy_litellm_model == "claude-sonnet-4-5-20250929"
        mock_router.reload_models.assert_called_once()

    def test_raises_when_no_model_after_reload(self):
        """When even after reload no model found, raises ValueError."""
        from ccproxy.config import CCProxyConfig, set_config_instance
        from ccproxy.hooks.model_router import model_router

        config = CCProxyConfig(default_model_passthrough=False)
        set_config_instance(config)

        mock_router = MagicMock()
        mock_router.get_model_for_label.return_value = None

        data: dict = {
            "model": "test",
            "messages": [],
            "metadata": {"ccproxy_model_name": "unknown_model"},
        }
        ctx = Context.from_litellm_data(data)
        with pytest.raises(ValueError, match="No model configured"):
            model_router(ctx, {"router": mock_router})

    def test_no_model_name_in_litellm_params_logs_warning(self):
        """Model config without 'model' in litellm_params logs a warning."""

        from ccproxy.config import CCProxyConfig, set_config_instance
        from ccproxy.hooks.model_router import model_router

        config = CCProxyConfig(default_model_passthrough=False)
        set_config_instance(config)

        mock_router = MagicMock()
        mock_router.get_model_for_label.return_value = {"litellm_params": {}}

        data: dict = {
            "model": "test",
            "messages": [],
            "metadata": {"ccproxy_model_name": "somemodel"},
        }
        ctx = Context.from_litellm_data(data)
        result = model_router(ctx, {"router": mock_router})
        assert result.ccproxy_litellm_model == ""
