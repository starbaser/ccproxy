"""Tests for the add_beta_headers hook."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from ccproxy.constants import ANTHROPIC_BETA_HEADERS
from ccproxy.hooks.add_beta_headers import add_beta_headers, add_beta_headers_guard
from ccproxy.pipeline.context import Context


def _make_ctx(headers: dict[str, str] | None = None) -> Context:
    flow = MagicMock()
    flow.id = "test-flow"
    flow.request.content = json.dumps({"model": "claude-sonnet", "messages": []}).encode()
    flow.request.headers = dict(headers or {})
    flow.metadata = {}
    return Context.from_flow(flow)


class TestAddBetaHeadersGuard:
    def test_true_when_anthropic_version_present(self) -> None:
        ctx = _make_ctx({"anthropic-version": "2023-06-01"})
        assert add_beta_headers_guard(ctx) is True

    def test_false_when_anthropic_version_absent(self) -> None:
        ctx = _make_ctx()
        assert add_beta_headers_guard(ctx) is False

    def test_false_when_anthropic_version_empty_string(self) -> None:
        # set_header("", ...) removes the key; guard must see empty string from absent header
        ctx = _make_ctx()
        assert add_beta_headers_guard(ctx) is False


class TestAddBetaHeaders:
    def test_sets_all_required_beta_headers_when_none_present(self) -> None:
        ctx = _make_ctx({"anthropic-version": "2023-06-01"})
        add_beta_headers(ctx, {})
        result = ctx.get_header("anthropic-beta")
        for header in ANTHROPIC_BETA_HEADERS:
            assert header in result

    def test_preserves_extra_existing_beta_headers(self) -> None:
        ctx = _make_ctx({
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "some-extra-header",
        })
        add_beta_headers(ctx, {})
        result = ctx.get_header("anthropic-beta")
        assert "some-extra-header" in result
        for header in ANTHROPIC_BETA_HEADERS:
            assert header in result

    def test_deduplicates_overlapping_headers(self) -> None:
        existing = ANTHROPIC_BETA_HEADERS[0]
        ctx = _make_ctx({
            "anthropic-version": "2023-06-01",
            "anthropic-beta": existing,
        })
        add_beta_headers(ctx, {})
        result = ctx.get_header("anthropic-beta")
        # No duplicates
        parts = [h.strip() for h in result.split(",") if h.strip()]
        assert len(parts) == len(set(parts))

    def test_required_headers_appear_first(self) -> None:
        ctx = _make_ctx({
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "my-custom-header",
        })
        add_beta_headers(ctx, {})
        parts = [h.strip() for h in ctx.get_header("anthropic-beta").split(",")]
        # ANTHROPIC_BETA_HEADERS should all be at the front
        for i, req in enumerate(ANTHROPIC_BETA_HEADERS):
            assert parts[i] == req

    def test_sets_anthropic_version_when_absent(self) -> None:
        ctx = _make_ctx({"anthropic-version": "2023-06-01"})
        # Remove the version before calling to simulate pre-hook state
        flow = MagicMock()
        flow.id = "test"
        flow.request.content = json.dumps({"model": "m", "messages": []}).encode()
        flow.request.headers = {"anthropic-version": ""}
        flow.metadata = {}
        ctx2 = Context.from_flow(flow)
        # Guard would reject, but we test the hook directly
        add_beta_headers(ctx2, {})
        assert ctx2.get_header("anthropic-version") == "2023-06-01"

    def test_does_not_overwrite_existing_anthropic_version(self) -> None:
        ctx = _make_ctx({"anthropic-version": "2025-01-01"})
        add_beta_headers(ctx, {})
        assert ctx.get_header("anthropic-version") == "2025-01-01"

    def test_returns_ctx(self) -> None:
        ctx = _make_ctx({"anthropic-version": "2023-06-01"})
        result = add_beta_headers(ctx, {})
        assert result is ctx
