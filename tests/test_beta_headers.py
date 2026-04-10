"""Test anthropic-beta header injection for Claude Code impersonation."""

import json
from unittest.mock import MagicMock

import pytest

from ccproxy.constants import ANTHROPIC_BETA_HEADERS
from ccproxy.hooks.add_beta_headers import add_beta_headers, add_beta_headers_guard
from ccproxy.pipeline.context import Context


def _make_ctx(headers: dict | None = None, body: dict | None = None) -> Context:
    flow = MagicMock()
    flow.id = "test-id"
    flow.request.content = json.dumps(
        body or {"model": "test", "messages": [], "metadata": {}}
    ).encode()
    flow.request.headers = dict(headers or {})
    return Context.from_flow(flow)


class TestAddBetaHeadersGuard:
    def test_guard_true_when_anthropic_version_present(self):
        ctx = _make_ctx(headers={"anthropic-version": "2023-06-01"})
        assert add_beta_headers_guard(ctx) is True

    def test_guard_false_when_no_anthropic_version(self):
        ctx = _make_ctx(headers={})
        assert add_beta_headers_guard(ctx) is False


class TestAddBetaHeaders:
    def test_adds_all_required_beta_headers(self):
        ctx = _make_ctx(headers={"anthropic-version": "2023-06-01"})
        result = add_beta_headers(ctx, {})
        beta = result.get_header("anthropic-beta")
        beta_values = [b.strip() for b in beta.split(",") if b.strip()]
        for expected in ANTHROPIC_BETA_HEADERS:
            assert expected in beta_values, f"Missing beta header: {expected}"

    def test_sets_anthropic_version_when_missing(self):
        ctx = _make_ctx(headers={})
        result = add_beta_headers(ctx, {})
        assert result.get_header("anthropic-version") == "2023-06-01"

    def test_preserves_existing_anthropic_version(self):
        ctx = _make_ctx(headers={"anthropic-version": "2024-01-01"})
        result = add_beta_headers(ctx, {})
        assert result.get_header("anthropic-version") == "2024-01-01"

    def test_merges_with_existing_beta_headers(self):
        existing_beta = "some-custom-beta-2025"
        ctx = _make_ctx(headers={"anthropic-beta": existing_beta})
        result = add_beta_headers(ctx, {})
        beta_values = [b.strip() for b in result.get_header("anthropic-beta").split(",")]
        for expected in ANTHROPIC_BETA_HEADERS:
            assert expected in beta_values
        assert existing_beta in beta_values

    def test_deduplicates_beta_headers(self):
        duplicate = ANTHROPIC_BETA_HEADERS[0]
        ctx = _make_ctx(headers={"anthropic-beta": duplicate})
        result = add_beta_headers(ctx, {})
        beta_values = [b.strip() for b in result.get_header("anthropic-beta").split(",")]
        assert beta_values.count(duplicate) == 1

    def test_no_existing_beta_sets_all_required(self):
        ctx = _make_ctx(headers={})
        result = add_beta_headers(ctx, {})
        beta_values = [b.strip() for b in result.get_header("anthropic-beta").split(",") if b.strip()]
        assert beta_values == list(ANTHROPIC_BETA_HEADERS)

    def test_extra_custom_beta_preserved_and_deduped(self):
        ctx = _make_ctx(headers={"anthropic-beta": "oauth-2025-04-20,my-custom-beta"})
        result = add_beta_headers(ctx, {})
        beta_values = [b.strip() for b in result.get_header("anthropic-beta").split(",")]
        assert "my-custom-beta" in beta_values
        assert beta_values.count("oauth-2025-04-20") == 1
