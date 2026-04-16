"""Tests for ccproxy.pipeline.guards."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from ccproxy.pipeline.context import Context
from ccproxy.pipeline.guards import is_anthropic_destination, is_oauth_request


def _make_ctx(headers: dict[str, str] | None = None) -> Context:
    flow = MagicMock()
    flow.id = "test-flow"
    flow.request.content = json.dumps({"model": "m", "messages": []}).encode()
    flow.request.headers = dict(headers or {})
    flow.metadata = {}
    return Context.from_flow(flow)


class TestIsOauthRequest:
    def test_true_for_bearer_token(self) -> None:
        ctx = _make_ctx({"authorization": "Bearer token-123"})
        assert is_oauth_request(ctx) is True

    def test_true_for_lowercase_bearer(self) -> None:
        ctx = _make_ctx({"authorization": "bearer lowercase-token"})
        assert is_oauth_request(ctx) is True

    def test_true_for_mixed_case_bearer(self) -> None:
        ctx = _make_ctx({"authorization": "BEARER uppercase"})
        assert is_oauth_request(ctx) is True

    def test_false_when_no_authorization(self) -> None:
        ctx = _make_ctx()
        assert is_oauth_request(ctx) is False

    def test_false_when_authorization_empty(self) -> None:
        ctx = _make_ctx({"authorization": ""})
        assert is_oauth_request(ctx) is False

    def test_false_for_basic_auth(self) -> None:
        ctx = _make_ctx({"authorization": "Basic YWxhZGRpbjpvcGVuc2VzYW1l"})
        assert is_oauth_request(ctx) is False

    def test_false_for_api_key_scheme(self) -> None:
        ctx = _make_ctx({"authorization": "ApiKey abc123"})
        assert is_oauth_request(ctx) is False

    def test_false_for_raw_token_no_scheme(self) -> None:
        ctx = _make_ctx({"authorization": "sk-ant-abc123"})
        assert is_oauth_request(ctx) is False


class TestIsAnthropicDestination:
    def test_true_when_anthropic_version_present(self) -> None:
        ctx = _make_ctx({"anthropic-version": "2023-06-01"})
        assert is_anthropic_destination(ctx) is True

    def test_false_when_anthropic_version_absent(self) -> None:
        ctx = _make_ctx()
        assert is_anthropic_destination(ctx) is False

    def test_false_when_anthropic_version_empty(self) -> None:
        # set_header with "" removes the key; get_header returns "" (default)
        ctx = _make_ctx()
        assert ctx.get_header("anthropic-version") == ""
        assert is_anthropic_destination(ctx) is False
