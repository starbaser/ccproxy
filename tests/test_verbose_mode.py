"""Tests for verbose_mode hook."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from ccproxy.hooks.verbose_mode import verbose_mode
from ccproxy.pipeline.context import Context


def _make_ctx(anthropic_beta: str | None = None) -> Context:
    flow = MagicMock()
    flow.id = "test-flow"
    flow.request.content = json.dumps(
        {
            "model": "claude-sonnet-4-20250514",
            "messages": [],
        }
    ).encode()
    headers: dict[str, str] = {"anthropic-version": "2023-06-01"}
    if anthropic_beta is not None:
        headers["anthropic-beta"] = anthropic_beta
    flow.request.headers = headers
    return Context.from_flow(flow)


class TestVerboseMode:
    def test_strips_redact_thinking(self) -> None:
        ctx = _make_ctx(anthropic_beta="redact-thinking-2025,other-beta")
        result = verbose_mode(ctx, {})
        beta = result.get_header("anthropic-beta")
        assert "redact-thinking" not in beta
        assert "other-beta" in beta

    def test_no_beta_header_is_noop(self) -> None:
        ctx = _make_ctx()
        result = verbose_mode(ctx, {})
        assert result.get_header("anthropic-beta") == ""

    def test_no_redact_prefix_leaves_header_unchanged(self) -> None:
        original = "claude-code-20250219,oauth-2025-04-20"
        ctx = _make_ctx(anthropic_beta=original)
        result = verbose_mode(ctx, {})
        assert result.get_header("anthropic-beta") == original

    def test_strips_multiple_redact_prefixes(self) -> None:
        ctx = _make_ctx(anthropic_beta="redact-thinking-foo,redact-thinking-bar,keep-me")
        result = verbose_mode(ctx, {})
        assert result.get_header("anthropic-beta") == "keep-me"

    def test_empty_beta_header_is_noop(self) -> None:
        ctx = _make_ctx(anthropic_beta="")
        result = verbose_mode(ctx, {})
        # Empty string means header was removed by set_header("")
        assert result.get_header("anthropic-beta") == ""

    def test_logs_when_stripped(self, caplog: object) -> None:
        import logging

        with caplog.at_level(logging.INFO, logger="ccproxy.hooks.verbose_mode"):  # type: ignore[union-attr]
            ctx = _make_ctx(anthropic_beta="redact-thinking-2025")
            verbose_mode(ctx, {})
        assert any("stripped" in rec.message.lower() for rec in caplog.records)  # type: ignore[union-attr]
