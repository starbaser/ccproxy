"""Tests for extract_session_id hook."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

from ccproxy.hooks.extract_session_id import extract_session_id, extract_session_id_guard
from ccproxy.pipeline.context import Context


def _make_ctx(body_metadata: dict[str, Any] | None = None) -> Context:
    metadata = body_metadata or {}
    body = {
        "model": "claude-sonnet-4-20250514",
        "messages": [],
        "metadata": metadata,
    }
    flow = MagicMock()
    flow.id = "test-flow"
    flow.request.content = json.dumps(body).encode()
    flow.request.headers = {}
    flow.metadata = {}
    return Context.from_flow(flow)


class TestExtractSessionIdHook:
    def test_json_user_id_extracts_session(self) -> None:
        user_id = json.dumps({"device_id": "dev1", "account_uuid": "acc1", "session_id": "sess-abc"})
        ctx = _make_ctx(body_metadata={"user_id": user_id})
        result = extract_session_id(ctx, {})
        assert result.flow.metadata["ccproxy.session_id"] == "sess-abc"

    def test_legacy_user_id_extracts_session(self) -> None:
        user_id = "user_hash123_account_acc456_session_sess789"
        ctx = _make_ctx(body_metadata={"user_id": user_id})
        result = extract_session_id(ctx, {})
        assert result.flow.metadata["ccproxy.session_id"] == "sess789"

    def test_no_user_id_does_not_set_session(self) -> None:
        ctx = _make_ctx(body_metadata={"other_key": "value"})
        result = extract_session_id(ctx, {})
        assert "ccproxy.session_id" not in result.flow.metadata

    def test_guard_with_user_id(self) -> None:
        ctx = _make_ctx(body_metadata={"user_id": "some-id"})
        assert extract_session_id_guard(ctx) is True

    def test_guard_without_user_id(self) -> None:
        ctx = _make_ctx(body_metadata={})
        assert extract_session_id_guard(ctx) is False

    def test_guard_empty_metadata(self) -> None:
        ctx = _make_ctx()
        assert extract_session_id_guard(ctx) is False

    def test_json_user_id_no_account_uuid(self) -> None:
        user_id = json.dumps({"device_id": "dev1", "session_id": "s1"})
        ctx = _make_ctx(body_metadata={"user_id": user_id})
        result = extract_session_id(ctx, {})
        assert result.flow.metadata["ccproxy.session_id"] == "s1"
