"""Tests for health check pipeline integration.

Hybrid architecture: _inject_health_check_auth sets api_key and headers BEFORE
acompletion (required because LiteLLM validates API keys pre-hook), then pipeline
hooks reinforce/enhance during async_pre_call_hook.
"""

from unittest.mock import MagicMock, patch

import pytest

from ccproxy.handler import _inject_health_check_auth
from ccproxy.hooks import ANTHROPIC_BETA_HEADERS, CLAUDE_CODE_SYSTEM_PREFIX


def _patch_config(config):
    return patch("ccproxy.handler.get_config", return_value=config)


@pytest.fixture
def mock_config():
    """Config with anthropic and zai oat_sources."""
    config = MagicMock()
    config.oat_sources = {
        "anthropic": MagicMock(destinations=["api.anthropic.com"]),
        "zai": MagicMock(destinations=["z.ai"]),
    }
    config.get_provider_for_destination.side_effect = lambda api_base: (
        "anthropic"
        if api_base and "anthropic" in api_base.lower()
        else "zai"
        if api_base and "z.ai" in api_base.lower()
        else None
    )
    config.get_oauth_token.return_value = "test-oauth-token-123"
    return config


@pytest.fixture
def mock_config_no_oat():
    """Config with no oat_sources."""
    config = MagicMock()
    config.oat_sources = {}
    return config


# ---------------------------------------------------------------------------
# _inject_health_check_auth: OAuth credential injection + max_tokens
# ---------------------------------------------------------------------------


def test_inject_always_sets_max_tokens(mock_config_no_oat):
    """max_tokens=1 is set even when no oat_sources configured."""
    result = {"max_tokens": 100}
    with _patch_config(mock_config_no_oat):
        _inject_health_check_auth(result, {"api_base": "https://api.anthropic.com"})
    assert result["max_tokens"] == 1


def test_inject_noop_auth_when_no_oat_sources(mock_config_no_oat):
    """No auth injected when oat_sources is empty (max_tokens still set)."""
    result = {}
    with _patch_config(mock_config_no_oat):
        _inject_health_check_auth(result, {"api_base": "https://api.anthropic.com"})
    assert "api_key" not in result
    assert "extra_headers" not in result
    assert result["max_tokens"] == 1


def test_inject_noop_auth_when_no_provider_match(mock_config):
    """No auth when api_base and model prefix don't match any oat_source."""
    mock_config.get_provider_for_destination.side_effect = lambda _: None
    result = {}
    with _patch_config(mock_config):
        _inject_health_check_auth(result, {"api_base": "https://api.openai.com", "model": "gpt-4o"})
    assert "api_key" not in result
    assert result["max_tokens"] == 1


def test_inject_noop_auth_when_no_token(mock_config):
    """No auth when provider matched but token is None."""
    mock_config.get_oauth_token.return_value = None
    result = {}
    with _patch_config(mock_config):
        _inject_health_check_auth(result, {"api_base": "https://api.anthropic.com", "model": "claude"})
    assert "api_key" not in result
    assert result["max_tokens"] == 1


def test_inject_anthropic_credentials(mock_config):
    """Anthropic destination: sets api_key, extra_headers, and system message."""
    result: dict = {}
    with _patch_config(mock_config):
        _inject_health_check_auth(result, {"api_base": "https://api.anthropic.com", "model": "claude-sonnet"})

    assert result["api_key"] == "test-oauth-token-123"
    assert result["max_tokens"] == 1
    headers = result["extra_headers"]
    assert headers["authorization"] == "Bearer test-oauth-token-123"
    assert headers["x-api-key"] == ""
    assert headers["anthropic-beta"] == ",".join(ANTHROPIC_BETA_HEADERS)
    assert headers["anthropic-version"] == "2023-06-01"
    assert result["messages"][0]["content"] == CLAUDE_CODE_SYSTEM_PREFIX


def test_inject_zai_credentials(mock_config):
    """z.ai destination: same Anthropic-format headers."""
    result: dict = {}
    with _patch_config(mock_config):
        _inject_health_check_auth(result, {"api_base": "https://api.z.ai/api/anthropic", "model": "glm-4.7"})

    assert result["api_key"] == "test-oauth-token-123"
    assert result["extra_headers"]["authorization"] == "Bearer test-oauth-token-123"


def test_inject_non_anthropic_provider(mock_config):
    """Non-Anthropic OAuth provider: api_key only, no extra_headers."""
    mock_config.oat_sources["vertex"] = MagicMock(destinations=["googleapis.com"])
    mock_config.get_provider_for_destination.side_effect = lambda api_base: (
        "vertex" if api_base and "googleapis" in api_base else None
    )
    result: dict = {}
    with _patch_config(mock_config):
        _inject_health_check_auth(result, {"api_base": "https://aiplatform.googleapis.com", "model": "gemini"})

    assert result["api_key"] == "test-oauth-token-123"
    assert result["max_tokens"] == 1
    assert "extra_headers" not in result


def test_inject_provider_detection_model_prefix_fallback(mock_config):
    """When api_base is None, detects provider from model prefix."""
    mock_config.get_provider_for_destination.side_effect = lambda _: None
    result: dict = {}
    with _patch_config(mock_config):
        _inject_health_check_auth(result, {"api_base": None, "model": "anthropic/claude-sonnet-4-5"})

    assert result["api_key"] == "test-oauth-token-123"


def test_inject_system_message_prepend(mock_config):
    """Prepends prefix to existing system message."""
    result = {"messages": [{"role": "system", "content": "Be helpful."}, {"role": "user", "content": "hi"}]}
    with _patch_config(mock_config):
        _inject_health_check_auth(result, {"api_base": "https://api.anthropic.com", "model": "claude"})

    assert result["messages"][0]["content"].startswith(CLAUDE_CODE_SYSTEM_PREFIX)
    assert "Be helpful." in result["messages"][0]["content"]


def test_inject_system_message_no_duplicate(mock_config):
    """Does not duplicate prefix if already present."""
    content = CLAUDE_CODE_SYSTEM_PREFIX + "\nExisting."
    result = {"messages": [{"role": "system", "content": content}]}
    with _patch_config(mock_config):
        _inject_health_check_auth(result, {"api_base": "https://api.anthropic.com", "model": "claude"})

    assert result["messages"][0]["content"].count(CLAUDE_CODE_SYSTEM_PREFIX) == 1


# ---------------------------------------------------------------------------
# Pipeline hooks: rule_evaluator and model_router health check behavior
# ---------------------------------------------------------------------------


def test_rule_evaluator_skips_health_check():
    """Rule evaluator sets alias model but skips classification for health checks."""
    from ccproxy.pipeline.hooks.rule_evaluator import rule_evaluator

    ctx = MagicMock()
    ctx.model = "anthropic/claude-sonnet-4-5-20250929"
    ctx.metadata = {"ccproxy_is_health_check": True}
    ctx.ccproxy_alias_model = None
    ctx.ccproxy_model_name = None
    classifier = MagicMock()

    result = rule_evaluator(ctx, {"classifier": classifier})

    assert result.ccproxy_alias_model == "anthropic/claude-sonnet-4-5-20250929"
    classifier.classify.assert_not_called()
    assert result.ccproxy_model_name is None


def test_rule_evaluator_runs_normally_without_flag():
    """Rule evaluator classifies normally when not a health check."""
    from ccproxy.pipeline.hooks.rule_evaluator import rule_evaluator

    ctx = MagicMock()
    ctx.model = "claude-sonnet-4-5"
    ctx.metadata = {}
    ctx.to_litellm_data.return_value = {"model": "claude-sonnet-4-5"}
    classifier = MagicMock()
    classifier.classify.return_value = "thinking_model"

    result = rule_evaluator(ctx, {"classifier": classifier})
    classifier.classify.assert_called_once()
    assert result.ccproxy_model_name == "thinking_model"


def test_model_router_forces_passthrough_for_health_check():
    """Model router forces passthrough for health checks even when config disables it."""
    from ccproxy.pipeline.hooks.model_router import model_router

    ctx = MagicMock()
    ctx.ccproxy_model_name = None
    ctx.ccproxy_alias_model = "anthropic/claude-sonnet-4-5-20250929"
    ctx.metadata = {"ccproxy_is_health_check": True}

    router = MagicMock()
    model_config = {"litellm_params": {"model": "anthropic/claude-sonnet-4-5-20250929", "api_base": "https://api.anthropic.com"}}
    router.get_model_for_label.return_value = model_config

    mock_cfg = MagicMock()
    mock_cfg.default_model_passthrough = False

    with patch("ccproxy.pipeline.hooks.model_router.get_config", return_value=mock_cfg):
        result = model_router(ctx, {"router": router})

    assert result.ccproxy_litellm_model == "anthropic/claude-sonnet-4-5-20250929"
    assert result.ccproxy_is_passthrough is True
    assert result.ccproxy_model_config == model_config


# ---------------------------------------------------------------------------
# async_pre_call_hook: sets health check flag and runs pipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_call_hook_sets_flag_and_runs_pipeline():
    """Health check requests get metadata flag and pipeline runs (not skipped)."""
    from ccproxy.handler import CCProxyHandler

    with (
        patch.object(CCProxyHandler, "_init_pipeline"),
        patch.object(CCProxyHandler, "_register_routes"),
        patch.object(CCProxyHandler, "_patch_health_check"),
        patch.object(CCProxyHandler, "_patch_anthropic_oauth_headers"),
        patch.object(CCProxyHandler, "_start_oauth_refresh_task"),
    ):
        handler = CCProxyHandler()
        handler._pipeline = MagicMock()
        handler._pipeline.execute.side_effect = lambda data, _: data

        data = {
            "model": "anthropic/claude-sonnet-4-5-20250929",
            "messages": [{"role": "user", "content": "hi"}],
            "metadata": {"tags": ["litellm-internal-health-check"]},
        }

        result = await handler.async_pre_call_hook(data, {}, litellm_params={})

    assert result["metadata"]["ccproxy_is_health_check"] is True
    handler._pipeline.execute.assert_called_once()
