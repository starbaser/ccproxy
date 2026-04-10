"""Tests for hook coverage — flow-native Context hooks."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from ccproxy.pipeline.context import Context


def _make_ctx(
    body: dict | None = None,
    headers: dict | None = None,
) -> Context:
    flow = MagicMock()
    flow.id = "test-id"
    flow.request.content = json.dumps(
        body or {"model": "test-model", "messages": [{"role": "user", "content": "hello"}], "metadata": {}}
    ).encode()
    flow.request.headers = dict(headers or {})
    return Context.from_flow(flow)


# ---------------------------------------------------------------------------
# inject_claude_code_identity
# ---------------------------------------------------------------------------


class TestInjectClaudeCodeIdentityHook:
    def _make_ctx_with_system(
        self,
        system=None,
        headers: dict | None = None,
    ) -> Context:
        body: dict = {
            "model": "test-model",
            "messages": [{"role": "user", "content": "hello"}],
            "metadata": {"ccproxy_oauth_provider": "anthropic"},
        }
        if system is not None:
            body["system"] = system
        default_headers = {
            "authorization": "Bearer oauth-token",
            "anthropic-version": "2023-06-01",
        }
        if headers:
            default_headers.update(headers)
        return _make_ctx(body=body, headers=default_headers)

    def test_guard_false_when_no_oauth(self):
        from ccproxy.hooks.inject_claude_code_identity import inject_claude_code_identity_guard

        ctx = _make_ctx(headers={})
        assert inject_claude_code_identity_guard(ctx) is False

    def test_guard_false_when_oauth_but_no_anthropic_version(self):
        from ccproxy.hooks.inject_claude_code_identity import inject_claude_code_identity_guard

        ctx = _make_ctx(
            body={"model": "t", "messages": [], "metadata": {}},
            headers={"authorization": "Bearer token"},
        )
        assert inject_claude_code_identity_guard(ctx) is False

    def test_guard_true_when_oauth_and_anthropic_version(self):
        from ccproxy.hooks.inject_claude_code_identity import inject_claude_code_identity_guard

        ctx = _make_ctx(
            body={"model": "t", "messages": [], "metadata": {}},
            headers={"authorization": "Bearer token", "anthropic-version": "2023-06-01"},
        )
        assert inject_claude_code_identity_guard(ctx) is True

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
