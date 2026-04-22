"""Unit tests for the flow-native Context dataclass."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.tools import ToolDefinition

from ccproxy.pipeline.context import Context
from ccproxy.pipeline.types import CachedSystemPromptPart

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
        assert len(ctx.messages) == 1
        assert isinstance(ctx.messages[0], ModelRequest)
        part = ctx.messages[0].parts[0]
        assert isinstance(part, UserPromptPart)
        assert part.content == "hi"

    def test_parses_metadata_from_body(self):
        flow = _make_flow(body={"model": "m", "messages": [], "metadata": {"key": "val"}})
        ctx = Context.from_flow(flow)
        assert ctx.metadata["key"] == "val"

    def test_parses_system_from_body(self):
        flow = _make_flow(body={"model": "m", "messages": [], "system": "Be helpful."})
        ctx = Context.from_flow(flow)
        assert len(ctx.system) == 1
        assert ctx.system[0].content == "Be helpful."

    def test_missing_body_fields_use_defaults(self):
        flow = _make_flow(body={"model": "", "messages": [], "metadata": {}})
        ctx = Context.from_flow(flow)
        assert ctx.model == ""
        assert ctx.messages == []
        assert ctx.metadata == {}
        assert ctx.system == []

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
        msgs = [ModelRequest(parts=[UserPromptPart(content="hello")])]
        ctx.messages = msgs
        assert len(ctx.messages) == 1
        assert isinstance(ctx.messages[0], ModelRequest)

    def test_messages_setter_writes_to_body(self):
        ctx = Context.from_flow(_make_flow())
        ctx.messages = [ModelRequest(parts=[UserPromptPart(content="test")])]
        assert isinstance(ctx._body["messages"], list)
        assert ctx._body["messages"][0]["role"] == "user"

    def test_system_setter(self):
        ctx = Context.from_flow(_make_flow())
        ctx.system = [SystemPromptPart(content="You are helpful.")]
        assert len(ctx.system) == 1
        assert ctx.system[0].content == "You are helpful."

    def test_system_setter_writes_to_body(self):
        ctx = Context.from_flow(_make_flow())
        ctx.system = [SystemPromptPart(content="Be helpful.")]
        assert ctx._body["system"] == "Be helpful."

    def test_system_cached_writes_cache_control(self):
        ctx = Context.from_flow(_make_flow())
        ctx.system = [CachedSystemPromptPart(content="cached", cache_control={"type": "ephemeral"})]
        system_body = ctx._body["system"]
        assert isinstance(system_body, list)
        assert system_body[0]["cache_control"] == {"type": "ephemeral"}

    def test_system_empty_list(self):
        flow = _make_flow(body={"model": "m", "messages": []})
        ctx = Context.from_flow(flow)
        assert ctx.system == []

    def test_tools_getter_and_setter(self):
        ctx = Context.from_flow(_make_flow(body={"model": "m", "messages": [], "tools": [
            {"name": "read_file", "description": "Read", "input_schema": {"type": "object"}},
        ]}))
        assert len(ctx.tools) == 1
        assert ctx.tools[0].name == "read_file"

    def test_tools_setter_writes_to_body(self):
        ctx = Context.from_flow(_make_flow())
        ctx.tools = [ToolDefinition(name="test", description="Test tool")]
        assert ctx._body["tools"][0]["name"] == "test"

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
        ctx.system = [SystemPromptPart(content="Be helpful.")]
        ctx.commit()
        written = json.loads(flow.request.content)
        assert written["system"] == "Be helpful."

    def test_commit_round_trips_messages(self):
        flow = _make_flow(body={"model": "m", "messages": [
            {"role": "user", "content": [{"type": "text", "text": "hello"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
        ]})
        ctx = Context.from_flow(flow)
        # Access typed messages (triggers parse)
        msgs = ctx.messages
        assert len(msgs) == 2
        # Commit (triggers serialize back)
        ctx.messages = msgs
        ctx.commit()
        written = json.loads(flow.request.content)
        assert len(written["messages"]) == 2
        assert written["messages"][0]["role"] == "user"
        assert written["messages"][1]["role"] == "assistant"

    def test_header_mutations_do_not_require_commit(self):
        flow = _make_flow(headers={"x-orig": "a"})
        ctx = Context.from_flow(flow)
        ctx.set_header("x-new", "b")
        assert flow.request.headers["x-new"] == "b"


class TestFromRequest:
    def test_from_request_wraps_bare_request(self):
        req = MagicMock()
        req.content = json.dumps({"model": "test", "messages": [{"role": "user", "content": "hi"}]}).encode()
        req.headers = {}
        ctx = Context.from_request(req)
        assert ctx.flow is None
        assert ctx.model == "test"
        assert len(ctx.messages) == 1

    def test_from_request_commit_writes_to_request(self):
        req = MagicMock()
        req.content = json.dumps({"model": "old", "messages": []}).encode()
        req.headers = {}
        ctx = Context.from_request(req)
        ctx.model = "new"
        ctx.commit()
        written = json.loads(req.content)
        assert written["model"] == "new"

    def test_flow_id_empty_for_request_context(self):
        req = MagicMock()
        req.content = b"{}"
        req.headers = {}
        ctx = Context.from_request(req)
        assert ctx.flow_id == ""
