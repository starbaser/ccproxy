"""Test pipeline as source of truth for outgoing headers.

Verifies that header mutations made by hooks are applied live to
flow.request.headers and that beta header merging works correctly.
"""

import json
from unittest.mock import MagicMock

from ccproxy.constants import ANTHROPIC_BETA_HEADERS
from ccproxy.hooks.add_beta_headers import add_beta_headers
from ccproxy.pipeline.context import Context


def _make_ctx(headers: dict | None = None, body: dict | None = None) -> Context:
    flow = MagicMock()
    flow.id = "test-id"
    flow.request.content = json.dumps(
        body or {"model": "test-model", "messages": [], "metadata": {}}
    ).encode()
    flow.request.headers = dict(headers or {})
    return Context.from_flow(flow)


class TestHeaderMutationsAreLive:
    """Hook header mutations are applied directly to flow.request.headers."""

    def test_set_header_visible_on_ctx(self):
        ctx = _make_ctx(headers={"x-api-key": "original"})
        ctx.set_header("x-api-key", "")
        ctx.set_header("authorization", "Bearer new-token")
        assert ctx.get_header("x-api-key") == ""
        assert ctx.get_header("authorization") == "Bearer new-token"

    def test_set_header_removes_when_empty_value(self):
        ctx = _make_ctx(headers={"x-api-key": "to-remove"})
        ctx.set_header("x-api-key", "")
        assert ctx.get_header("x-api-key") == ""

    def test_custom_headers_pass_through_unchanged(self):
        ctx = _make_ctx(headers={"x-custom-trace": "abc-123"})
        ctx.set_header("authorization", "Bearer token")
        assert ctx.get_header("x-custom-trace") == "abc-123"

    def test_commit_flushes_body_mutations(self):
        flow = MagicMock()
        flow.id = "test-id"
        flow.request.content = json.dumps({"model": "test", "messages": [], "metadata": {}}).encode()
        flow.request.headers = {}
        ctx = Context.from_flow(flow)
        ctx.model = "updated-model"
        ctx.commit()
        body = json.loads(flow.request.content)
        assert body["model"] == "updated-model"


class TestClientBetaMerge:
    """Verify client anthropic-beta headers merge into add_beta_headers hook."""

    def test_existing_beta_merged_with_required(self):
        ctx = _make_ctx(headers={
            "anthropic-beta": "client-feature-2025",
            "anthropic-version": "2023-06-01",
        })
        result = add_beta_headers(ctx, {})
        beta_values = [b.strip() for b in result.get_header("anthropic-beta").split(",")]
        for expected in ANTHROPIC_BETA_HEADERS:
            assert expected in beta_values
        assert "client-feature-2025" in beta_values

    def test_client_beta_deduplicates(self):
        ctx = _make_ctx(headers={
            "anthropic-beta": "oauth-2025-04-20",
            "anthropic-version": "2023-06-01",
        })
        result = add_beta_headers(ctx, {})
        beta_values = [b.strip() for b in result.get_header("anthropic-beta").split(",")]
        assert beta_values.count("oauth-2025-04-20") == 1

    def test_no_prior_beta_sets_all_required(self):
        ctx = _make_ctx(headers={"anthropic-version": "2023-06-01"})
        result = add_beta_headers(ctx, {})
        beta_values = [b.strip() for b in result.get_header("anthropic-beta").split(",") if b.strip()]
        for expected in ANTHROPIC_BETA_HEADERS:
            assert expected in beta_values
