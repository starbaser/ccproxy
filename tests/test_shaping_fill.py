"""Tests for default fill functions in ccproxy.shaping.fill."""

from __future__ import annotations

import json
import uuid
from typing import Any

from mitmproxy import http
from mitmproxy.test import tflow

from ccproxy.shaping.fill import (
    fill_messages,
    fill_model,
    fill_stream_passthrough,
    fill_system_append,
    fill_tools,
    regenerate_session_id,
    regenerate_user_prompt_id,
)
from ccproxy.pipeline.context import Context


def _ctx(body: dict[str, Any] | None = None) -> Context:
    flow = tflow.tflow()
    flow.request = http.Request.make(
        "POST",
        "https://incoming.example/",
        json.dumps(body or {}).encode() if body is not None else b"",
        {},
    )
    return Context.from_flow(flow)


def _shape_ctx(body: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> Context:
    req = http.Request.make(
        "POST",
        "https://seed.example/",
        json.dumps(body or {}).encode(),
        headers or {},
    )
    return Context.from_request(req)


class TestFillModel:
    def test_copies_model_into_shape(self) -> None:
        ctx = _ctx({"model": "claude"})
        shape = _shape_ctx({"other": "v"})
        fill_model(shape, ctx)
        assert shape.model == "claude"

    def test_missing_model_leaves_shape_alone(self) -> None:
        ctx = _ctx({})
        shape = _shape_ctx({"model": "seed"})
        fill_model(shape, ctx)
        assert shape.model == "seed"


class TestFillMessages:
    def test_copies_messages_into_shape(self) -> None:
        msgs = [{"role": "user", "content": "hi"}]
        ctx = _ctx({"messages": msgs})
        shape = _shape_ctx({})
        fill_messages(shape, ctx)
        assert len(shape.messages) == 1
        # Round-trip through typed parse/serialize produces Anthropic block format
        assert shape._body["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]

    def test_empty_messages_skipped(self) -> None:
        ctx = _ctx({})
        shape = _shape_ctx({})
        fill_messages(shape, ctx)
        assert "messages" not in shape._body


class TestFillTools:
    def test_copies_tools_and_choice(self) -> None:
        ctx = _ctx({"tools": [{"name": "t"}], "tool_choice": "auto"})
        shape = _shape_ctx({})
        fill_tools(shape, ctx)
        assert len(shape.tools) == 1
        assert shape.tools[0].name == "t"
        assert shape._body["tool_choice"] == "auto"

    def test_missing_tools_is_noop(self) -> None:
        ctx = _ctx({})
        shape = _shape_ctx({"unrelated": "v"})
        fill_tools(shape, ctx)
        assert "tools" not in shape._body


class TestFillSystemAppend:
    def test_appends_to_existing_shape_list(self) -> None:
        ctx = _ctx({"system": [{"type": "text", "text": "new"}]})
        shape = _shape_ctx({"system": [{"type": "text", "text": "seed"}]})
        fill_system_append(shape, ctx)
        assert [p.content for p in shape.system] == ["seed", "new"]

    def test_wraps_string_system_from_ctx(self) -> None:
        ctx = _ctx({"system": "incoming"})
        shape = _shape_ctx({"system": [{"type": "text", "text": "seed"}]})
        fill_system_append(shape, ctx)
        assert shape.system[-1].content == "incoming"

    def test_no_ctx_system_is_noop(self) -> None:
        ctx = _ctx({})
        shape = _shape_ctx({"system": [{"type": "text", "text": "seed"}]})
        fill_system_append(shape, ctx)
        assert len(shape.system) == 1
        assert shape.system[0].content == "seed"

    def test_no_shape_system_starts_fresh(self) -> None:
        ctx = _ctx({"system": [{"type": "text", "text": "incoming"}]})
        shape = _shape_ctx({})
        fill_system_append(shape, ctx)
        assert len(shape.system) == 1
        assert shape.system[0].content == "incoming"


class TestFillStreamPassthrough:
    def test_copies_stream_true(self) -> None:
        ctx = _ctx({"stream": True})
        shape = _shape_ctx({})
        fill_stream_passthrough(shape, ctx)
        assert shape._body["stream"] is True

    def test_copies_stream_false_overwriting_shape(self) -> None:
        ctx = _ctx({"stream": False})
        shape = _shape_ctx({"stream": True})
        fill_stream_passthrough(shape, ctx)
        assert shape._body["stream"] is False

    def test_missing_stream_is_noop(self) -> None:
        ctx = _ctx({})
        shape = _shape_ctx({})
        fill_stream_passthrough(shape, ctx)
        assert "stream" not in shape._body


class TestRegenerateUserPromptId:
    def test_regenerates_when_present(self) -> None:
        ctx = _ctx({})
        shape = _shape_ctx({"user_prompt_id": "old-id"})
        regenerate_user_prompt_id(shape, ctx)
        new_id = shape._body["user_prompt_id"]
        assert new_id != "old-id"
        assert len(new_id) == 13

    def test_absent_key_untouched(self) -> None:
        ctx = _ctx({})
        shape = _shape_ctx({"other": "v"})
        regenerate_user_prompt_id(shape, ctx)
        assert "user_prompt_id" not in shape._body


class TestRegenerateSessionId:
    def test_regenerates_session_id(self) -> None:
        identity = json.dumps({"device_id": "dev", "session_id": "old"})
        ctx = _ctx({})
        shape = _shape_ctx({"metadata": {"user_id": identity}})
        regenerate_session_id(shape, ctx)
        new_identity = json.loads(shape._body["metadata"]["user_id"])
        assert new_identity["device_id"] == "dev"
        assert new_identity["session_id"] != "old"
        uuid.UUID(new_identity["session_id"])

    def test_no_identity_untouched(self) -> None:
        ctx = _ctx({})
        shape = _shape_ctx({"metadata": {"other": "v"}})
        regenerate_session_id(shape, ctx)
        assert shape._body["metadata"] == {"other": "v"}

    def test_no_metadata_untouched(self) -> None:
        ctx = _ctx({})
        shape = _shape_ctx({"model": "x"})
        regenerate_session_id(shape, ctx)
        assert shape._body == {"model": "x"}

    def test_non_json_user_id_untouched(self) -> None:
        ctx = _ctx({})
        shape = _shape_ctx({"metadata": {"user_id": "not-json"}})
        regenerate_session_id(shape, ctx)
        assert shape._body["metadata"]["user_id"] == "not-json"

    def test_skips_when_no_identity_fields(self) -> None:
        identity = json.dumps({"other": "value"})
        ctx = _ctx({})
        shape = _shape_ctx({"metadata": {"user_id": identity}})
        regenerate_session_id(shape, ctx)
        result_identity = json.loads(shape._body["metadata"]["user_id"])
        assert "session_id" not in result_identity

    def test_non_dict_identity_untouched(self) -> None:
        identity = json.dumps([1, 2, 3])
        ctx = _ctx({})
        shape = _shape_ctx({"metadata": {"user_id": identity}})
        regenerate_session_id(shape, ctx)
        assert shape._body["metadata"]["user_id"] == identity

    def test_non_string_user_id_untouched(self) -> None:
        ctx = _ctx({})
        shape = _shape_ctx({"metadata": {"user_id": 1234}})
        regenerate_session_id(shape, ctx)
        assert shape._body["metadata"]["user_id"] == 1234
