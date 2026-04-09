"""Tests for verbose_mode hook."""

from __future__ import annotations

import pytest

from ccproxy.hooks.verbose_mode import verbose_mode
from ccproxy.pipeline.context import Context


def _make_ctx(extra_headers: dict | None = None, provider_extra_headers: dict | None = None) -> Context:
    data: dict = {
        "model": "anthropic/claude-sonnet-4-5-20250929",
        "messages": [],
        "metadata": {
            "ccproxy_litellm_model": "anthropic/claude-sonnet-4-5-20250929",
            "ccproxy_model_config": {
                "litellm_params": {
                    "model": "anthropic/claude-sonnet-4-5-20250929",
                    "api_base": "https://api.anthropic.com",
                },
            },
        },
        "provider_specific_header": {"extra_headers": provider_extra_headers or {}},
    }
    if extra_headers is not None:
        data["extra_headers"] = extra_headers
    return Context.from_litellm_data(data)


class TestVerboseMode:
    def test_strips_redact_thinking_from_extra_headers(self):
        ctx = _make_ctx(extra_headers={"anthropic-beta": "redact-thinking-2025,other-beta"})
        result = verbose_mode(ctx, {})
        beta = result._raw_data["extra_headers"]["anthropic-beta"]
        assert "redact-thinking" not in beta
        assert "other-beta" in beta

    def test_strips_redact_thinking_from_provider_headers(self):
        ctx = _make_ctx(provider_extra_headers={"anthropic-beta": "redact-thinking-2025,other-beta"})
        result = verbose_mode(ctx, {})
        beta = result.provider_headers["extra_headers"]["anthropic-beta"]
        assert "redact-thinking" not in beta
        assert "other-beta" in beta

    def test_no_beta_header_is_noop(self):
        ctx = _make_ctx(extra_headers={"content-type": "application/json"})
        result = verbose_mode(ctx, {})
        assert result._raw_data.get("extra_headers", {}).get("anthropic-beta") is None

    def test_no_redact_prefix_leaves_header_unchanged(self):
        original = "claude-code-20250219,oauth-2025-04-20"
        ctx = _make_ctx(extra_headers={"anthropic-beta": original})
        result = verbose_mode(ctx, {})
        assert result._raw_data["extra_headers"]["anthropic-beta"] == original

    def test_strips_multiple_redact_prefixes(self):
        ctx = _make_ctx(extra_headers={"anthropic-beta": "redact-thinking-foo,redact-thinking-bar,keep-me"})
        result = verbose_mode(ctx, {})
        beta = result._raw_data["extra_headers"]["anthropic-beta"]
        assert beta == "keep-me"

    def test_empty_beta_header_is_noop(self):
        ctx = _make_ctx(extra_headers={"anthropic-beta": ""})
        result = verbose_mode(ctx, {})
        # Empty beta — function skips (not beta), no change
        assert result._raw_data["extra_headers"]["anthropic-beta"] == ""

    def test_strips_from_both_header_locations(self):
        ctx = _make_ctx(
            extra_headers={"anthropic-beta": "redact-thinking-a,keep-a"},
            provider_extra_headers={"anthropic-beta": "redact-thinking-b,keep-b"},
        )
        result = verbose_mode(ctx, {})
        raw_beta = result._raw_data["extra_headers"]["anthropic-beta"]
        provider_beta = result.provider_headers["extra_headers"]["anthropic-beta"]
        assert "redact-thinking" not in raw_beta
        assert "keep-a" in raw_beta
        assert "redact-thinking" not in provider_beta
        assert "keep-b" in provider_beta

    def test_extra_headers_not_dict_is_skipped(self):
        ctx = _make_ctx()
        # Inject non-dict extra_headers
        ctx._raw_data["extra_headers"] = "not-a-dict"
        result = verbose_mode(ctx, {})
        assert result._raw_data["extra_headers"] == "not-a-dict"

    def test_logs_when_stripped(self, caplog):
        import logging

        with caplog.at_level(logging.INFO, logger="ccproxy.hooks.verbose_mode"):
            ctx = _make_ctx(extra_headers={"anthropic-beta": "redact-thinking-2025"})
            verbose_mode(ctx, {})
        assert any("stripped" in rec.message.lower() for rec in caplog.records)
