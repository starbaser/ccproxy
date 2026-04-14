"""Tests for the inject_claude_code_identity hook."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from ccproxy.constants import CLAUDE_CODE_SYSTEM_PREFIX
from ccproxy.hooks.inject_claude_code_identity import (
    inject_claude_code_identity,
    inject_claude_code_identity_guard,
)
from ccproxy.pipeline.context import Context


def _make_ctx(
    headers: dict[str, str] | None = None,
    system: str | list | None = ...,  # type: ignore[assignment]
    oauth_provider: str | None = None,
) -> Context:
    body: dict = {"model": "claude-sonnet", "messages": []}
    if system is not ... and system is not None:
        body["system"] = system
    if oauth_provider:
        body["metadata"] = {"ccproxy_oauth_provider": oauth_provider}
    flow = MagicMock()
    flow.id = "test-flow"
    flow.request.content = json.dumps(body).encode()
    flow.request.headers = dict(headers or {})
    flow.metadata = {}
    return Context.from_flow(flow)


class TestInjectClaudeCodeIdentityGuard:
    def test_false_when_no_bearer_and_no_provider(self) -> None:
        ctx = _make_ctx(headers={"anthropic-version": "2023-06-01"})
        assert inject_claude_code_identity_guard(ctx) is False

    def test_false_when_no_auth_conditions_regardless_of_version(self) -> None:
        ctx = _make_ctx()
        assert inject_claude_code_identity_guard(ctx) is False

    def test_true_when_bearer_and_anthropic_version(self) -> None:
        ctx = _make_ctx(headers={
            "authorization": "Bearer token",
            "anthropic-version": "2023-06-01",
        })
        assert inject_claude_code_identity_guard(ctx) is True

    def test_false_when_bearer_but_no_anthropic_version(self) -> None:
        ctx = _make_ctx(headers={"authorization": "Bearer token"})
        assert inject_claude_code_identity_guard(ctx) is False

    def test_true_when_body_provider_and_anthropic_version(self) -> None:
        ctx = _make_ctx(
            headers={"anthropic-version": "2023-06-01"},
            oauth_provider="anthropic",
        )
        assert inject_claude_code_identity_guard(ctx) is True

    def test_false_when_body_provider_and_no_anthropic_version(self) -> None:
        ctx = _make_ctx(oauth_provider="anthropic")
        assert inject_claude_code_identity_guard(ctx) is False


class TestInjectClaudeCodeIdentity:
    def test_none_system_set_to_prefix(self) -> None:
        ctx = _make_ctx(system=None)
        result = inject_claude_code_identity(ctx, {})
        assert result.system == CLAUDE_CODE_SYSTEM_PREFIX

    def test_string_system_without_prefix_gets_prepended(self) -> None:
        ctx = _make_ctx(system="You are a helpful assistant.")
        result = inject_claude_code_identity(ctx, {})
        assert result.system == f"{CLAUDE_CODE_SYSTEM_PREFIX}\n\nYou are a helpful assistant."

    def test_string_system_with_prefix_unchanged(self) -> None:
        original = f"{CLAUDE_CODE_SYSTEM_PREFIX} Additional instructions."
        ctx = _make_ctx(system=original)
        result = inject_claude_code_identity(ctx, {})
        assert result.system == original

    def test_empty_string_system_prepends_prefix(self) -> None:
        ctx = _make_ctx(system="")
        result = inject_claude_code_identity(ctx, {})
        assert result.system == f"{CLAUDE_CODE_SYSTEM_PREFIX}\n\n"

    def test_list_system_without_prefix_block_gets_prepended(self) -> None:
        blocks = [{"type": "text", "text": "Hello world"}]
        ctx = _make_ctx(system=list(blocks))
        result = inject_claude_code_identity(ctx, {})
        assert isinstance(result.system, list)
        assert len(result.system) == 2
        assert result.system[0] == {"type": "text", "text": CLAUDE_CODE_SYSTEM_PREFIX}
        assert result.system[1] == blocks[0]

    def test_list_system_with_prefix_block_unchanged(self) -> None:
        blocks = [
            {"type": "text", "text": f"{CLAUDE_CODE_SYSTEM_PREFIX} extended"},
            {"type": "text", "text": "Other"},
        ]
        ctx = _make_ctx(system=list(blocks))
        result = inject_claude_code_identity(ctx, {})
        assert result.system == blocks

    def test_list_system_prefix_in_non_text_block_triggers_prepend(self) -> None:
        # block has prefix in text field but type is not "text" → has_prefix = False → prepend
        blocks = [{"type": "image", "text": CLAUDE_CODE_SYSTEM_PREFIX}]
        ctx = _make_ctx(system=list(blocks))
        result = inject_claude_code_identity(ctx, {})
        assert isinstance(result.system, list)
        assert len(result.system) == 2
        assert result.system[0] == {"type": "text", "text": CLAUDE_CODE_SYSTEM_PREFIX}

    def test_list_system_empty_list_gets_prefix_block(self) -> None:
        ctx = _make_ctx(system=[])
        result = inject_claude_code_identity(ctx, {})
        assert isinstance(result.system, list)
        assert result.system == [{"type": "text", "text": CLAUDE_CODE_SYSTEM_PREFIX}]

    def test_returns_ctx(self) -> None:
        ctx = _make_ctx(system=None)
        result = inject_claude_code_identity(ctx, {})
        assert result is ctx
