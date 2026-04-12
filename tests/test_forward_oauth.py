"""Tests for the forward_oauth hook."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from ccproxy.config import CCProxyConfig, OAuthSource, set_config_instance
from ccproxy.constants import OAUTH_SENTINEL_PREFIX, OAuthConfigError
from ccproxy.hooks.forward_oauth import (
    _inject_token,
    forward_oauth,
    forward_oauth_guard,
)
from ccproxy.pipeline.context import Context


def _make_ctx(headers: dict[str, str] | None = None) -> Context:
    """Context with a plain dict for headers so mutations are observable."""
    flow = MagicMock()
    flow.id = "test-flow"
    flow.request.content = json.dumps({"model": "test-model", "messages": []}).encode()
    flow.request.headers = dict(headers or {})
    flow.metadata = {}
    return Context.from_flow(flow)


@pytest.fixture
def clean_config():
    config = CCProxyConfig()
    set_config_instance(config)
    return config


class TestForwardOAuthGuard:
    def test_true_when_x_api_key_set(self, clean_config: CCProxyConfig) -> None:
        ctx = _make_ctx({"x-api-key": "some-key"})
        assert forward_oauth_guard(ctx) is True

    def test_true_when_authorization_set(self, clean_config: CCProxyConfig) -> None:
        ctx = _make_ctx({"authorization": "Bearer token"})
        assert forward_oauth_guard(ctx) is True

    def test_true_when_x_goog_api_key_set(self, clean_config: CCProxyConfig) -> None:
        ctx = _make_ctx({"x-goog-api-key": "google-key"})
        assert forward_oauth_guard(ctx) is True

    def test_false_when_all_empty(self, clean_config: CCProxyConfig) -> None:
        ctx = _make_ctx()
        assert forward_oauth_guard(ctx) is False

    def test_true_when_multiple_headers_set(self, clean_config: CCProxyConfig) -> None:
        ctx = _make_ctx({"x-api-key": "key", "authorization": "Bearer tok"})
        assert forward_oauth_guard(ctx) is True


class TestForwardOAuthSentinelPath:
    def test_sentinel_injects_bearer_and_sets_metadata(self, clean_config: CCProxyConfig) -> None:
        clean_config._oat_values["anthropic"] = "real-token-xyz"
        ctx = _make_ctx({"x-api-key": f"{OAUTH_SENTINEL_PREFIX}anthropic"})

        result = forward_oauth(ctx, {})

        assert result is ctx
        assert ctx.get_header("authorization") == "Bearer real-token-xyz"
        assert ctx.get_header("x-ccproxy-oauth-injected") == "1"
        assert ctx.flow.metadata["ccproxy.oauth_provider"] == "anthropic"

    def test_sentinel_clears_x_api_key(self, clean_config: CCProxyConfig) -> None:
        clean_config._oat_values["anthropic"] = "real-token"
        ctx = _make_ctx({"x-api-key": f"{OAUTH_SENTINEL_PREFIX}anthropic"})

        forward_oauth(ctx, {})

        # x-api-key must be cleared since default target is authorization
        assert ctx.get_header("x-api-key") == ""

    def test_sentinel_via_goog_api_key_header(self, clean_config: CCProxyConfig) -> None:
        clean_config._oat_values["google"] = "goog-token"
        ctx = _make_ctx({"x-goog-api-key": f"{OAUTH_SENTINEL_PREFIX}google"})

        result = forward_oauth(ctx, {})

        assert result is ctx
        assert ctx.get_header("authorization") == "Bearer goog-token"
        assert ctx.flow.metadata["ccproxy.oauth_provider"] == "google"

    def test_sentinel_no_token_raises_oauth_config_error(self, clean_config: CCProxyConfig) -> None:
        ctx = _make_ctx({"x-api-key": f"{OAUTH_SENTINEL_PREFIX}missing-provider"})

        with pytest.raises(OAuthConfigError, match="missing-provider"):
            forward_oauth(ctx, {})

    def test_sentinel_get_config_exception_raises_oauth_config_error(self) -> None:
        ctx = _make_ctx({"x-api-key": f"{OAUTH_SENTINEL_PREFIX}err-provider"})

        with patch("ccproxy.hooks.forward_oauth.get_config", side_effect=RuntimeError("config exploded")):
            with pytest.raises(OAuthConfigError, match="err-provider"):
                forward_oauth(ctx, {})


class TestForwardOAuthCachedPath:
    def test_no_keys_cached_token_injects(self, clean_config: CCProxyConfig) -> None:
        clean_config.oat_sources = {"fallback": "dummy"}
        clean_config._oat_values["fallback"] = "cached-tok"
        ctx = _make_ctx()

        result = forward_oauth(ctx, {})

        assert result is ctx
        assert ctx.get_header("authorization") == "Bearer cached-tok"
        assert ctx.get_header("x-ccproxy-oauth-injected") == "1"
        assert ctx.flow.metadata["ccproxy.oauth_provider"] == "fallback"

    def test_first_provider_with_token_used(self, clean_config: CCProxyConfig) -> None:
        # oat_sources iteration order → first loaded token wins
        clean_config.oat_sources = {"p1": "d1", "p2": "d2"}
        clean_config._oat_values["p1"] = "token-p1"
        clean_config._oat_values["p2"] = "token-p2"
        ctx = _make_ctx()

        forward_oauth(ctx, {})

        assert ctx.flow.metadata["ccproxy.oauth_provider"] == "p1"

    def test_no_keys_no_cached_token_noop(self, clean_config: CCProxyConfig) -> None:
        clean_config.oat_sources = {"empty": "dummy"}
        # _oat_values intentionally empty
        ctx = _make_ctx()

        result = forward_oauth(ctx, {})

        assert result is ctx
        assert ctx.get_header("x-ccproxy-oauth-injected") == ""
        assert "ccproxy.oauth_provider" not in ctx.flow.metadata

    def test_no_oat_sources_noop(self, clean_config: CCProxyConfig) -> None:
        ctx = _make_ctx()

        result = forward_oauth(ctx, {})

        assert result is ctx
        assert ctx.get_header("x-ccproxy-oauth-injected") == ""

    def test_try_cached_token_config_exception_handled(self) -> None:
        ctx = _make_ctx()

        with patch("ccproxy.hooks.forward_oauth.get_config", side_effect=RuntimeError("oops")):
            result = forward_oauth(ctx, {})

        assert result is ctx
        assert ctx.get_header("x-ccproxy-oauth-injected") == ""


class TestForwardOAuthPassthrough:
    def test_non_sentinel_api_key_no_injection(self, clean_config: CCProxyConfig) -> None:
        ctx = _make_ctx({"x-api-key": "sk-real-key-not-a-sentinel"})

        result = forward_oauth(ctx, {})

        assert result is ctx
        assert ctx.get_header("x-ccproxy-oauth-injected") == ""
        assert "ccproxy.oauth_provider" not in ctx.flow.metadata

    def test_real_auth_header_no_cached_injection(self, clean_config: CCProxyConfig) -> None:
        # Existing Bearer token → skip cached path
        clean_config.oat_sources = {"fallback": "dummy"}
        clean_config._oat_values["fallback"] = "cached"
        ctx = _make_ctx({"authorization": "Bearer real-existing-token"})

        result = forward_oauth(ctx, {})

        assert result is ctx
        assert ctx.get_header("authorization") == "Bearer real-existing-token"
        assert ctx.get_header("x-ccproxy-oauth-injected") == ""


class TestInjectToken:
    def test_default_header_sets_authorization_bearer(self, clean_config: CCProxyConfig) -> None:
        ctx = _make_ctx()

        _inject_token(ctx, "anthropic", "my-token")

        assert ctx.get_header("authorization") == "Bearer my-token"
        assert ctx.get_header("x-ccproxy-oauth-injected") == "1"
        assert ctx.get_header("x-api-key") == ""
        assert ctx.get_header("x-goog-api-key") == ""

    def test_custom_goog_api_key_header(self, clean_config: CCProxyConfig) -> None:
        clean_config.oat_sources = {
            "google": OAuthSource(command="echo tok", auth_header="x-goog-api-key")
        }
        ctx = _make_ctx()

        _inject_token(ctx, "google", "goog-token")

        assert ctx.get_header("x-goog-api-key") == "goog-token"
        assert ctx.get_header("x-ccproxy-oauth-injected") == "1"
        # x-api-key cleared (not the target)
        assert ctx.get_header("x-api-key") == ""
        # authorization not touched
        assert ctx.get_header("authorization") == ""

    def test_custom_x_api_key_header(self, clean_config: CCProxyConfig) -> None:
        clean_config.oat_sources = {
            "prov": OAuthSource(command="echo tok", auth_header="x-api-key")
        }
        ctx = _make_ctx()

        _inject_token(ctx, "prov", "my-secret")

        assert ctx.get_header("x-api-key") == "my-secret"
        assert ctx.get_header("x-goog-api-key") == ""
        assert ctx.get_header("x-ccproxy-oauth-injected") == "1"

    def test_always_sets_injected_flag(self, clean_config: CCProxyConfig) -> None:
        ctx = _make_ctx()
        _inject_token(ctx, "any", "any-token")
        assert ctx.get_header("x-ccproxy-oauth-injected") == "1"

    def test_inject_preserves_other_headers(self, clean_config: CCProxyConfig) -> None:
        ctx = _make_ctx({"content-type": "application/json", "anthropic-version": "2023-06-01"})

        _inject_token(ctx, "prov", "tok")

        assert ctx.get_header("content-type") == "application/json"
        assert ctx.get_header("anthropic-version") == "2023-06-01"
