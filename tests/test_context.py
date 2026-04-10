"""Unit tests for the flow-native Context dataclass."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from ccproxy.pipeline.context import Context


_DEFAULT_BODY = {"model": "test", "messages": [], "metadata": {}}


def _make_flow(body: dict | None = None, headers: dict | None = None) -> MagicMock:
    flow = MagicMock()
    flow.id = "test-id"
    flow.request.content = json.dumps(_DEFAULT_BODY if body is None else body).encode()
    flow.request.headers = dict(headers or {})
    return flow


class TestContextFromFlow:
    def test_parses_model_from_body(self):
        flow = _make_flow(body={"model": "claude-3", "messages": []})
        ctx = Context.from_flow(flow)
        assert ctx.model == "claude-3"

    def test_parses_messages_from_body(self):
        msgs = [{"role": "user", "content": "hi"}]
        flow = _make_flow(body={"model": "m", "messages": msgs})
        ctx = Context.from_flow(flow)
        assert ctx.messages == msgs

    def test_parses_metadata_from_body(self):
        flow = _make_flow(body={"model": "m", "messages": [], "metadata": {"key": "val"}})
        ctx = Context.from_flow(flow)
        assert ctx.metadata["key"] == "val"

    def test_parses_system_from_body(self):
        flow = _make_flow(body={"model": "m", "messages": [], "system": "Be helpful."})
        ctx = Context.from_flow(flow)
        assert ctx.system == "Be helpful."

    def test_missing_body_fields_use_defaults(self):
        flow = _make_flow(body={"model": "", "messages": [], "metadata": {}})
        ctx = Context.from_flow(flow)
        assert ctx.model == ""
        assert ctx.messages == []
        assert ctx.metadata == {}
        assert ctx.system is None

    def test_invalid_json_body_uses_empty_body(self):
        flow = MagicMock()
        flow.id = "test-id"
        flow.request.content = b"not-json"
        flow.request.headers = {}
        ctx = Context.from_flow(flow)
        assert ctx.model == ""
        assert ctx.messages == []

    def test_empty_body_uses_defaults(self):
        flow = MagicMock()
        flow.id = "test-id"
        flow.request.content = b""
        flow.request.headers = {}
        ctx = Context.from_flow(flow)
        assert ctx.model == ""

    def test_flow_id_from_flow(self):
        flow = _make_flow()
        flow.id = "unique-flow-id-123"
        ctx = Context.from_flow(flow)
        assert ctx.flow_id == "unique-flow-id-123"


class TestBodyProperties:
    def test_model_getter_and_setter(self):
        ctx = Context.from_flow(_make_flow())
        ctx.model = "gpt-4"
        assert ctx.model == "gpt-4"

    def test_messages_getter_and_setter(self):
        ctx = Context.from_flow(_make_flow())
        msgs = [{"role": "user", "content": "hello"}]
        ctx.messages = msgs
        assert ctx.messages == msgs

    def test_system_string_setter(self):
        ctx = Context.from_flow(_make_flow())
        ctx.system = "You are helpful."
        assert ctx.system == "You are helpful."

    def test_system_list_setter(self):
        ctx = Context.from_flow(_make_flow())
        blocks = [{"type": "text", "text": "Be helpful."}]
        ctx.system = blocks
        assert ctx.system == blocks

    def test_system_none_removes_key(self):
        flow = _make_flow(body={"model": "m", "messages": [], "system": "existing"})
        ctx = Context.from_flow(flow)
        ctx.system = None
        assert ctx.system is None
        assert "system" not in ctx._body

    def test_metadata_getter_and_setter(self):
        ctx = Context.from_flow(_make_flow())
        ctx.metadata = {"trace_id": "abc"}
        assert ctx.metadata["trace_id"] == "abc"

    def test_metadata_setdefault_behavior(self):
        ctx = Context.from_flow(_make_flow())
        ctx.metadata["new_key"] = "new_val"
        assert ctx.metadata["new_key"] == "new_val"


class TestHeaderMethods:
    def test_get_header_returns_value(self):
        ctx = Context.from_flow(_make_flow(headers={"authorization": "Bearer tok"}))
        assert ctx.get_header("authorization") == "Bearer tok"

    def test_get_header_exact_key_match(self):
        ctx = Context.from_flow(_make_flow(headers={"authorization": "Bearer tok"}))
        assert ctx.get_header("authorization") == "Bearer tok"

    def test_get_header_returns_default_when_missing(self):
        ctx = Context.from_flow(_make_flow(headers={}))
        assert ctx.get_header("authorization") == ""
        assert ctx.get_header("x-missing", "fallback") == "fallback"

    def test_set_header_adds_value(self):
        ctx = Context.from_flow(_make_flow(headers={}))
        ctx.set_header("x-custom", "myval")
        assert ctx.get_header("x-custom") == "myval"

    def test_set_header_empty_string_removes(self):
        ctx = Context.from_flow(_make_flow(headers={"x-api-key": "old"}))
        ctx.set_header("x-api-key", "")
        assert ctx.get_header("x-api-key") == ""

    def test_authorization_convenience_property(self):
        ctx = Context.from_flow(_make_flow(headers={"authorization": "Bearer xyz"}))
        assert ctx.authorization == "Bearer xyz"

    def test_x_api_key_convenience_property(self):
        ctx = Context.from_flow(_make_flow(headers={"x-api-key": "sk-123"}))
        assert ctx.x_api_key == "sk-123"

    def test_headers_snapshot_lowercased(self):
        ctx = Context.from_flow(_make_flow(headers={"X-Custom": "val", "Content-Type": "json"}))
        snap = ctx.headers
        assert snap["x-custom"] == "val"
        assert snap["content-type"] == "json"


class TestMetadataConvenienceProperties:
    def test_ccproxy_oauth_provider_getter(self):
        flow = _make_flow(body={"model": "m", "messages": [], "metadata": {"ccproxy_oauth_provider": "anthropic"}})
        ctx = Context.from_flow(flow)
        assert ctx.ccproxy_oauth_provider == "anthropic"

    def test_ccproxy_oauth_provider_setter(self):
        ctx = Context.from_flow(_make_flow())
        ctx.ccproxy_oauth_provider = "google"
        assert ctx.metadata["ccproxy_oauth_provider"] == "google"

    def test_session_id_getter(self):
        flow = _make_flow(body={"model": "m", "messages": [], "metadata": {"session_id": "sess-xyz"}})
        ctx = Context.from_flow(flow)
        assert ctx.session_id == "sess-xyz"

    def test_session_id_setter(self):
        ctx = Context.from_flow(_make_flow())
        ctx.session_id = "sess-abc"
        assert ctx.metadata["session_id"] == "sess-abc"


class TestCommit:
    def test_commit_writes_body_to_flow(self):
        flow = _make_flow(body={"model": "original", "messages": []})
        ctx = Context.from_flow(flow)
        ctx.model = "updated"
        ctx.commit()
        written = json.loads(flow.request.content)
        assert written["model"] == "updated"

    def test_commit_includes_metadata_changes(self):
        flow = _make_flow()
        ctx = Context.from_flow(flow)
        ctx.metadata["trace_id"] = "t123"
        ctx.commit()
        written = json.loads(flow.request.content)
        assert written["metadata"]["trace_id"] == "t123"

    def test_commit_includes_system_when_set(self):
        flow = _make_flow()
        ctx = Context.from_flow(flow)
        ctx.system = "Be helpful."
        ctx.commit()
        written = json.loads(flow.request.content)
        assert written["system"] == "Be helpful."

    def test_commit_excludes_system_when_none(self):
        flow = _make_flow(body={"model": "m", "messages": [], "system": "original"})
        ctx = Context.from_flow(flow)
        ctx.system = None
        ctx.commit()
        written = json.loads(flow.request.content)
        assert "system" not in written

    def test_header_mutations_do_not_require_commit(self):
        flow = _make_flow(headers={"x-orig": "a"})
        ctx = Context.from_flow(flow)
        ctx.set_header("x-new", "b")
        assert flow.request.headers["x-new"] == "b"
