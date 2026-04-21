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


def _husk(body: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> http.Request:
    return http.Request.make(
        "POST",
        "https://seed.example/",
        json.dumps(body or {}).encode(),
        headers or {},
    )


def _body(req: http.Request) -> dict[str, Any]:
    return json.loads(req.content or b"{}")


class TestFillModel:
    def test_copies_model_into_husk(self) -> None:
        ctx = _ctx({"model": "claude"})
        husk = _husk({"other": "v"})
        fill_model(husk, ctx)
        assert _body(husk)["model"] == "claude"

    def test_missing_model_leaves_husk_alone(self) -> None:
        ctx = _ctx({})
        husk = _husk({"model": "seed"})
        fill_model(husk, ctx)
        assert _body(husk)["model"] == "seed"


class TestFillMessages:
    def test_copies_messages_into_husk(self) -> None:
        msgs = [{"role": "user", "content": "hi"}]
        ctx = _ctx({"messages": msgs})
        husk = _husk({})
        fill_messages(husk, ctx)
        assert _body(husk)["messages"] == msgs

    def test_empty_messages_skipped(self) -> None:
        ctx = _ctx({})
        husk = _husk({})
        fill_messages(husk, ctx)
        assert "messages" not in _body(husk)


class TestFillTools:
    def test_copies_tools_and_choice(self) -> None:
        ctx = _ctx({"tools": [{"name": "t"}], "tool_choice": "auto"})
        husk = _husk({})
        fill_tools(husk, ctx)
        body = _body(husk)
        assert body["tools"] == [{"name": "t"}]
        assert body["tool_choice"] == "auto"

    def test_missing_tools_is_noop(self) -> None:
        ctx = _ctx({})
        husk = _husk({"unrelated": "v"})
        fill_tools(husk, ctx)
        assert "tools" not in _body(husk)


class TestFillSystemAppend:
    def test_appends_to_existing_husk_list(self) -> None:
        ctx = _ctx({"system": [{"type": "text", "text": "new"}]})
        husk = _husk({"system": [{"type": "text", "text": "seed"}]})
        fill_system_append(husk, ctx)
        blocks = _body(husk)["system"]
        assert [b["text"] for b in blocks] == ["seed", "new"]

    def test_wraps_string_system_from_ctx(self) -> None:
        ctx = _ctx({"system": "incoming"})
        husk = _husk({"system": [{"type": "text", "text": "seed"}]})
        fill_system_append(husk, ctx)
        blocks = _body(husk)["system"]
        assert blocks[-1] == {"type": "text", "text": "incoming"}

    def test_no_ctx_system_is_noop(self) -> None:
        ctx = _ctx({})
        husk = _husk({"system": [{"type": "text", "text": "seed"}]})
        fill_system_append(husk, ctx)
        assert _body(husk)["system"] == [{"type": "text", "text": "seed"}]

    def test_no_husk_system_starts_fresh(self) -> None:
        ctx = _ctx({"system": [{"type": "text", "text": "incoming"}]})
        husk = _husk({})
        fill_system_append(husk, ctx)
        assert _body(husk)["system"] == [{"type": "text", "text": "incoming"}]


class TestFillStreamPassthrough:
    def test_copies_stream_true(self) -> None:
        ctx = _ctx({"stream": True})
        husk = _husk({})
        fill_stream_passthrough(husk, ctx)
        assert _body(husk)["stream"] is True

    def test_copies_stream_false_overwriting_husk(self) -> None:
        ctx = _ctx({"stream": False})
        husk = _husk({"stream": True})
        fill_stream_passthrough(husk, ctx)
        assert _body(husk)["stream"] is False

    def test_missing_stream_is_noop(self) -> None:
        ctx = _ctx({})
        husk = _husk({})
        fill_stream_passthrough(husk, ctx)
        assert "stream" not in _body(husk)


class TestRegenerateUserPromptId:
    def test_regenerates_when_present(self) -> None:
        ctx = _ctx({})
        husk = _husk({"user_prompt_id": "old-id"})
        regenerate_user_prompt_id(husk, ctx)
        new_id = _body(husk)["user_prompt_id"]
        assert new_id != "old-id"
        assert len(new_id) == 13

    def test_absent_key_untouched(self) -> None:
        ctx = _ctx({})
        husk = _husk({"other": "v"})
        regenerate_user_prompt_id(husk, ctx)
        assert "user_prompt_id" not in _body(husk)


class TestRegenerateSessionId:
    def test_regenerates_session_id(self) -> None:
        identity = json.dumps({"device_id": "dev", "session_id": "old"})
        ctx = _ctx({})
        husk = _husk({"metadata": {"user_id": identity}})
        regenerate_session_id(husk, ctx)
        new_identity = json.loads(_body(husk)["metadata"]["user_id"])
        assert new_identity["device_id"] == "dev"
        assert new_identity["session_id"] != "old"
        uuid.UUID(new_identity["session_id"])

    def test_no_identity_untouched(self) -> None:
        ctx = _ctx({})
        husk = _husk({"metadata": {"other": "v"}})
        regenerate_session_id(husk, ctx)
        assert _body(husk)["metadata"] == {"other": "v"}

    def test_no_metadata_untouched(self) -> None:
        ctx = _ctx({})
        husk = _husk({"model": "x"})
        regenerate_session_id(husk, ctx)
        assert _body(husk) == {"model": "x"}

    def test_non_json_user_id_untouched(self) -> None:
        ctx = _ctx({})
        husk = _husk({"metadata": {"user_id": "not-json"}})
        regenerate_session_id(husk, ctx)
        assert _body(husk)["metadata"]["user_id"] == "not-json"

    def test_skips_when_no_identity_fields(self) -> None:
        identity = json.dumps({"other": "value"})
        ctx = _ctx({})
        husk = _husk({"metadata": {"user_id": identity}})
        regenerate_session_id(husk, ctx)
        result_identity = json.loads(_body(husk)["metadata"]["user_id"])
        assert "session_id" not in result_identity

    def test_non_dict_identity_untouched(self) -> None:
        identity = json.dumps([1, 2, 3])
        ctx = _ctx({})
        husk = _husk({"metadata": {"user_id": identity}})
        regenerate_session_id(husk, ctx)
        assert _body(husk)["metadata"]["user_id"] == identity

    def test_non_string_user_id_untouched(self) -> None:
        ctx = _ctx({})
        husk = _husk({"metadata": {"user_id": 1234}})
        regenerate_session_id(husk, ctx)
        assert _body(husk)["metadata"]["user_id"] == 1234
