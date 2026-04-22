"""Tests for bidirectional wire format <-> Pydantic AI type conversion."""

from __future__ import annotations

import json

from pydantic_ai.messages import (
    CachePoint,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.tools import ToolDefinition

from ccproxy.pipeline.types import CachedSystemPromptPart, CachedToolDefinition
from ccproxy.pipeline.wire import (
    parse_messages,
    parse_system,
    parse_tools,
    serialize_messages,
    serialize_system,
    serialize_tools,
)


# ---------------------------------------------------------------------------
# parse_system
# ---------------------------------------------------------------------------


class TestParseSystem:
    def test_none(self):
        assert parse_system(None) == []

    def test_empty_string(self):
        assert parse_system("") == []

    def test_string(self):
        parts = parse_system("Be helpful.")
        assert len(parts) == 1
        assert parts[0].content == "Be helpful."
        assert isinstance(parts[0], SystemPromptPart)

    def test_list_blocks(self):
        blocks = [
            {"type": "text", "text": "First"},
            {"type": "text", "text": "Second"},
        ]
        parts = parse_system(blocks)
        assert len(parts) == 2
        assert parts[0].content == "First"
        assert parts[1].content == "Second"

    def test_list_with_cache_control(self):
        blocks = [
            {"type": "text", "text": "cached", "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "not cached"},
        ]
        parts = parse_system(blocks)
        assert isinstance(parts[0], CachedSystemPromptPart)
        assert parts[0].cache_control == {"type": "ephemeral"}
        assert not isinstance(parts[1], CachedSystemPromptPart)


# ---------------------------------------------------------------------------
# serialize_system
# ---------------------------------------------------------------------------


class TestSerializeSystem:
    def test_empty(self):
        assert serialize_system([]) == []

    def test_single_part_returns_string(self):
        result = serialize_system([SystemPromptPart(content="hello")])
        assert result == "hello"

    def test_single_cached_part_returns_list(self):
        result = serialize_system([CachedSystemPromptPart(content="hello", cache_control={"type": "ephemeral"})])
        assert isinstance(result, list)
        assert result[0]["cache_control"] == {"type": "ephemeral"}

    def test_multiple_parts_returns_list(self):
        parts = [SystemPromptPart(content="a"), SystemPromptPart(content="b")]
        result = serialize_system(parts)
        assert isinstance(result, list)
        assert len(result) == 2

    def test_round_trip_with_cache(self):
        blocks = [
            {"type": "text", "text": "cached", "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "plain"},
        ]
        parsed = parse_system(blocks)
        serialized = serialize_system(parsed)
        assert isinstance(serialized, list)
        assert serialized[0]["cache_control"] == {"type": "ephemeral"}
        assert "cache_control" not in serialized[1]


# ---------------------------------------------------------------------------
# parse_tools
# ---------------------------------------------------------------------------


class TestParseTools:
    def test_anthropic_format(self):
        tools = [{"name": "read", "description": "Read file", "input_schema": {"type": "object"}}]
        result = parse_tools(tools)
        assert len(result) == 1
        assert result[0].name == "read"
        assert result[0].description == "Read file"
        assert result[0].parameters_json_schema == {"type": "object"}

    def test_openai_format(self):
        tools = [{"type": "function", "function": {"name": "search", "description": "Search", "parameters": {"type": "object"}}}]
        result = parse_tools(tools)
        assert result[0].name == "search"
        assert result[0].parameters_json_schema == {"type": "object"}

    def test_with_cache_control(self):
        tools = [{"name": "t", "input_schema": {}, "cache_control": {"type": "ephemeral"}}]
        result = parse_tools(tools)
        assert isinstance(result[0], CachedToolDefinition)
        assert result[0].cache_control == {"type": "ephemeral"}

    def test_without_cache_control(self):
        tools = [{"name": "t", "input_schema": {}}]
        result = parse_tools(tools)
        assert isinstance(result[0], ToolDefinition)
        assert not isinstance(result[0], CachedToolDefinition)


# ---------------------------------------------------------------------------
# serialize_tools
# ---------------------------------------------------------------------------


class TestSerializeTools:
    def test_basic(self):
        tools = [ToolDefinition(name="test", description="Test", parameters_json_schema={"type": "object"})]
        result = serialize_tools(tools)
        assert result[0]["name"] == "test"
        assert result[0]["description"] == "Test"
        assert result[0]["input_schema"] == {"type": "object"}

    def test_cached(self):
        tools = [CachedToolDefinition(name="t", cache_control={"type": "ephemeral"})]
        result = serialize_tools(tools)
        assert result[0]["cache_control"] == {"type": "ephemeral"}

    def test_round_trip(self):
        original = [
            {"name": "a", "description": "A", "input_schema": {"type": "object"}},
            {"name": "b", "input_schema": {}, "cache_control": {"type": "ephemeral"}},
        ]
        parsed = parse_tools(original)
        serialized = serialize_tools(parsed)
        assert serialized[0]["name"] == "a"
        assert "cache_control" not in serialized[0]
        assert serialized[1]["cache_control"] == {"type": "ephemeral"}


# ---------------------------------------------------------------------------
# parse_messages
# ---------------------------------------------------------------------------


class TestParseMessages:
    def test_simple_user_string(self):
        msgs = [{"role": "user", "content": "hello"}]
        result = parse_messages(msgs)
        assert len(result) == 1
        assert isinstance(result[0], ModelRequest)
        assert isinstance(result[0].parts[0], UserPromptPart)
        assert result[0].parts[0].content == "hello"

    def test_user_content_blocks(self):
        msgs = [{"role": "user", "content": [
            {"type": "text", "text": "one"},
            {"type": "text", "text": "two"},
        ]}]
        result = parse_messages(msgs)
        req = result[0]
        assert isinstance(req, ModelRequest)
        up = req.parts[0]
        assert isinstance(up, UserPromptPart)
        assert isinstance(up.content, list)
        assert up.content[0] == "one"
        assert up.content[1] == "two"

    def test_cache_control_on_text_block(self):
        msgs = [{"role": "user", "content": [
            {"type": "text", "text": "cached", "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "plain"},
        ]}]
        result = parse_messages(msgs)
        up = result[0].parts[0]
        assert isinstance(up, UserPromptPart)
        assert isinstance(up.content, list)
        assert up.content[0] == "cached"
        assert isinstance(up.content[1], CachePoint)
        assert up.content[2] == "plain"

    def test_assistant_text(self):
        msgs = [{"role": "assistant", "content": [{"type": "text", "text": "hi"}]}]
        result = parse_messages(msgs)
        assert isinstance(result[0], ModelResponse)
        assert isinstance(result[0].parts[0], TextPart)
        assert result[0].parts[0].content == "hi"

    def test_assistant_string_content(self):
        msgs = [{"role": "assistant", "content": "hi"}]
        result = parse_messages(msgs)
        assert isinstance(result[0], ModelResponse)
        assert result[0].parts[0].content == "hi"

    def test_tool_use(self):
        msgs = [{"role": "assistant", "content": [
            {"type": "tool_use", "id": "call_1", "name": "read_file", "input": {"path": "/tmp"}},
        ]}]
        result = parse_messages(msgs)
        tc = result[0].parts[0]
        assert isinstance(tc, ToolCallPart)
        assert tc.tool_name == "read_file"
        assert tc.args == {"path": "/tmp"}
        assert tc.tool_call_id == "call_1"

    def test_thinking(self):
        msgs = [{"role": "assistant", "content": [
            {"type": "thinking", "thinking": "Let me think...", "signature": "sig"},
        ]}]
        result = parse_messages(msgs)
        tp = result[0].parts[0]
        assert isinstance(tp, ThinkingPart)
        assert tp.content == "Let me think..."
        assert tp.signature == "sig"

    def test_redacted_thinking(self):
        msgs = [{"role": "assistant", "content": [
            {"type": "redacted_thinking", "data": "encrypted"},
        ]}]
        result = parse_messages(msgs)
        tp = result[0].parts[0]
        assert isinstance(tp, ThinkingPart)
        assert tp.id == "redacted_thinking"
        assert tp.content == ""
        assert tp.signature == "encrypted"

    def test_tool_result(self):
        msgs = [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "call_1", "content": "file contents"},
        ]}]
        result = parse_messages(msgs)
        tr = result[0].parts[0]
        assert isinstance(tr, ToolReturnPart)
        assert tr.tool_call_id == "call_1"
        assert tr.content == "file contents"

    def test_system_role_message(self):
        msgs = [{"role": "system", "content": "You are helpful"}]
        result = parse_messages(msgs)
        assert isinstance(result[0], ModelRequest)
        assert isinstance(result[0].parts[0], SystemPromptPart)

    def test_empty_list(self):
        assert parse_messages([]) == []

    def test_full_conversation(self):
        msgs = [
            {"role": "user", "content": [{"type": "text", "text": "hello"}]},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "hmm", "signature": "s"},
                {"type": "text", "text": "hi"},
                {"type": "tool_use", "id": "c1", "name": "read", "input": {}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "c1", "content": "data"},
            ]},
            {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
        ]
        result = parse_messages(msgs)
        assert len(result) == 4
        assert isinstance(result[0], ModelRequest)
        assert isinstance(result[1], ModelResponse)
        assert isinstance(result[2], ModelRequest)
        assert isinstance(result[3], ModelResponse)


# ---------------------------------------------------------------------------
# serialize_messages
# ---------------------------------------------------------------------------


class TestSerializeMessages:
    def test_simple_user(self):
        msgs = [ModelRequest(parts=[UserPromptPart(content="hello")])]
        result = serialize_messages(msgs)
        assert len(result) == 1
        assert result[0]["role"] == "user"
        assert result[0]["content"] == [{"type": "text", "text": "hello"}]

    def test_assistant_text(self):
        msgs = [ModelResponse(parts=[TextPart(content="hi")])]
        result = serialize_messages(msgs)
        assert result[0]["role"] == "assistant"
        assert result[0]["content"][0] == {"type": "text", "text": "hi"}

    def test_tool_call(self):
        msgs = [ModelResponse(parts=[ToolCallPart(tool_name="read", args={"p": 1}, tool_call_id="c1")])]
        result = serialize_messages(msgs)
        block = result[0]["content"][0]
        assert block["type"] == "tool_use"
        assert block["name"] == "read"
        assert block["input"] == {"p": 1}
        assert block["id"] == "c1"

    def test_thinking(self):
        msgs = [ModelResponse(parts=[ThinkingPart(content="hmm", signature="sig")])]
        result = serialize_messages(msgs)
        block = result[0]["content"][0]
        assert block["type"] == "thinking"
        assert block["thinking"] == "hmm"
        assert block["signature"] == "sig"

    def test_redacted_thinking(self):
        msgs = [ModelResponse(parts=[ThinkingPart(content="", id="redacted_thinking", signature="enc")])]
        result = serialize_messages(msgs)
        block = result[0]["content"][0]
        assert block["type"] == "redacted_thinking"
        assert block["data"] == "enc"

    def test_tool_return(self):
        msgs = [ModelRequest(parts=[ToolReturnPart(tool_name="read", content="data", tool_call_id="c1")])]
        result = serialize_messages(msgs)
        block = result[0]["content"][0]
        assert block["type"] == "tool_result"
        assert block["tool_use_id"] == "c1"

    def test_cache_point_in_user_content(self):
        msgs = [ModelRequest(parts=[UserPromptPart(content=["hello", CachePoint(), "world"])])]
        result = serialize_messages(msgs)
        blocks = result[0]["content"]
        assert blocks[0] == {"type": "text", "text": "hello", "cache_control": {"type": "ephemeral"}}
        assert blocks[1] == {"type": "text", "text": "world"}

    def test_cache_point_with_1h_ttl(self):
        msgs = [ModelRequest(parts=[UserPromptPart(content=["hello", CachePoint(ttl="1h")])])]
        result = serialize_messages(msgs)
        cc = result[0]["content"][0]["cache_control"]
        assert cc == {"type": "ephemeral", "ttl": "1h"}


# ---------------------------------------------------------------------------
# Round-trip tests
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_non_list_content_returns_empty_request(self):
        msgs = [{"role": "user", "content": 42}]
        result = parse_messages(msgs)
        assert isinstance(result[0], ModelRequest)
        assert result[0].parts == []

    def test_image_block(self):
        msgs = [{"role": "user", "content": [
            {"type": "image", "source": {"data": "base64data"}},
        ]}]
        result = parse_messages(msgs)
        up = result[0].parts[0]
        assert isinstance(up, UserPromptPart)

    def test_image_block_with_cache_control(self):
        msgs = [{"role": "user", "content": [
            {"type": "image", "source": {"data": "img"}, "cache_control": {"type": "ephemeral"}},
        ]}]
        result = parse_messages(msgs)
        up = result[0].parts[0]
        assert isinstance(up, UserPromptPart)
        assert isinstance(up.content, list)
        assert isinstance(up.content[1], CachePoint)

    def test_unknown_block_type(self):
        msgs = [{"role": "user", "content": [
            {"type": "custom_block", "data": "something"},
        ]}]
        result = parse_messages(msgs)
        up = result[0].parts[0]
        assert isinstance(up, UserPromptPart)

    def test_tool_result_with_list_content(self):
        msgs = [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "c1", "content": [
                {"type": "text", "text": "line 1"},
                {"type": "text", "text": "line 2"},
            ]},
        ]}]
        result = parse_messages(msgs)
        tr = result[0].parts[0]
        assert isinstance(tr, ToolReturnPart)
        assert tr.content == "line 1\nline 2"

    def test_tool_result_flushed_after_text(self):
        msgs = [{"role": "user", "content": [
            {"type": "text", "text": "before"},
            {"type": "tool_result", "tool_use_id": "c1", "content": "result"},
        ]}]
        result = parse_messages(msgs)
        req = result[0]
        assert len(req.parts) == 2
        assert isinstance(req.parts[0], UserPromptPart)
        assert isinstance(req.parts[1], ToolReturnPart)

    def test_unknown_assistant_block(self):
        msgs = [{"role": "assistant", "content": [
            {"type": "custom", "data": "x"},
        ]}]
        result = parse_messages(msgs)
        assert isinstance(result[0].parts[0], TextPart)

    def test_empty_assistant_content(self):
        msgs = [{"role": "assistant", "content": []}]
        result = parse_messages(msgs)
        resp = result[0]
        assert isinstance(resp, ModelResponse)
        assert resp.parts[0].content == ""

    def test_invalid_ttl_defaults_to_5m(self):
        from ccproxy.pipeline.wire import _cache_control_to_cache_point
        cp = _cache_control_to_cache_point({"type": "ephemeral", "ttl": "99h"})
        assert cp.ttl == "5m"

    def test_serialize_system_prompt_in_model_request(self):
        msgs = [ModelRequest(parts=[SystemPromptPart(content="sys")])]
        result = serialize_messages(msgs)
        assert result[0]["role"] == "user"
        assert result[0]["content"][0]["text"] == "sys"

    def test_serialize_tool_return_standalone(self):
        msgs = [ModelRequest(parts=[ToolReturnPart(tool_name="t", content="r", tool_call_id="c1")])]
        result = serialize_messages(msgs)
        assert result[0]["role"] == "user"
        assert result[0]["content"][0]["type"] == "tool_result"

    def test_serialize_tool_return_appended_to_user(self):
        from pydantic_ai.messages import TextContent
        msgs = [ModelRequest(parts=[
            UserPromptPart(content="hi"),
            ToolReturnPart(tool_name="t", content="r", tool_call_id="c1"),
        ])]
        result = serialize_messages(msgs)
        assert len(result) == 1
        assert result[0]["role"] == "user"
        assert len(result[0]["content"]) == 2

    def test_serialize_text_content_object(self):
        from pydantic_ai.messages import TextContent
        msgs = [ModelRequest(parts=[UserPromptPart(content=[TextContent(content="tagged")])])]
        result = serialize_messages(msgs)
        assert result[0]["content"][0]["text"] == "tagged"

    def test_serialize_tool_return_non_string_content(self):
        msgs = [ModelRequest(parts=[ToolReturnPart(tool_name="t", content={"key": "val"}, tool_call_id="c1")])]
        result = serialize_messages(msgs)
        assert result[0]["content"][0]["content"] == "{'key': 'val'}"

    def test_serialize_unknown_response_part(self):
        from pydantic_ai.messages import CompactionPart
        msgs = [ModelResponse(parts=[CompactionPart(content="compacted")])]
        result = serialize_messages(msgs)
        assert result[0]["content"][0]["type"] == "text"

    def test_thinking_without_signature(self):
        msgs = [ModelResponse(parts=[ThinkingPart(content="thought")])]
        result = serialize_messages(msgs)
        block = result[0]["content"][0]
        assert block["type"] == "thinking"
        assert "signature" not in block

    def test_tool_call_string_args(self):
        msgs = [ModelResponse(parts=[ToolCallPart(tool_name="t", args='{"x":1}', tool_call_id="c1")])]
        result = serialize_messages(msgs)
        assert result[0]["content"][0]["input"] == {}


class TestRoundTrip:
    def test_simple_conversation(self):
        original = [
            {"role": "user", "content": [{"type": "text", "text": "hello"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
        ]
        parsed = parse_messages(original)
        serialized = serialize_messages(parsed)
        assert len(serialized) == 2
        assert serialized[0]["role"] == "user"
        assert serialized[0]["content"][0]["text"] == "hello"
        assert serialized[1]["role"] == "assistant"
        assert serialized[1]["content"][0]["text"] == "hi"

    def test_tool_use_round_trip(self):
        original = [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "c1", "name": "read_file", "input": {"path": "/tmp/test"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "c1", "content": "file data"},
            ]},
        ]
        parsed = parse_messages(original)
        serialized = serialize_messages(parsed)
        assert serialized[0]["content"][0]["name"] == "read_file"
        assert serialized[0]["content"][0]["id"] == "c1"
        assert serialized[1]["content"][0]["tool_use_id"] == "c1"

    def test_cache_control_round_trip(self):
        original = [{"role": "user", "content": [
            {"type": "text", "text": "cached", "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "plain"},
        ]}]
        parsed = parse_messages(original)
        serialized = serialize_messages(parsed)
        assert serialized[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
        assert "cache_control" not in serialized[0]["content"][1]

    def test_thinking_round_trip(self):
        original = [{"role": "assistant", "content": [
            {"type": "thinking", "thinking": "Let me think", "signature": "sig123"},
            {"type": "text", "text": "answer"},
        ]}]
        parsed = parse_messages(original)
        serialized = serialize_messages(parsed)
        assert serialized[0]["content"][0]["type"] == "thinking"
        assert serialized[0]["content"][0]["thinking"] == "Let me think"
        assert serialized[0]["content"][0]["signature"] == "sig123"
        assert serialized[0]["content"][1]["text"] == "answer"

    def test_system_round_trip_with_cache(self):
        original = [
            {"type": "text", "text": "System prompt", "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "More instructions"},
        ]
        parsed = parse_system(original)
        serialized = serialize_system(parsed)
        assert isinstance(serialized, list)
        assert serialized[0]["text"] == "System prompt"
        assert serialized[0]["cache_control"] == {"type": "ephemeral"}
        assert serialized[1]["text"] == "More instructions"
        assert "cache_control" not in serialized[1]

    def test_tools_round_trip_with_cache(self):
        original = [
            {"name": "read", "description": "Read", "input_schema": {"type": "object"}},
            {"name": "write", "description": "Write", "input_schema": {}, "cache_control": {"type": "ephemeral"}},
        ]
        parsed = parse_tools(original)
        serialized = serialize_tools(parsed)
        assert serialized[0]["name"] == "read"
        assert "cache_control" not in serialized[0]
        assert serialized[1]["cache_control"] == {"type": "ephemeral"}
