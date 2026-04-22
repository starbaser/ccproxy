"""Tests for default prepare functions in ccproxy.shaping.prepare."""

from __future__ import annotations

import json
from typing import Any

from mitmproxy import http

from ccproxy.pipeline.context import Context
from ccproxy.shaping.prepare import (
    strip_auth_headers,
    strip_request_content,
    strip_system_blocks,
    strip_transport_headers,
)


def _ctx(headers: dict[str, str] | None = None, body: dict[str, Any] | None = None) -> Context:
    content = json.dumps(body or {}).encode() if body is not None else b""
    req = http.Request.make("POST", "https://seed.example/v1", content, headers or {})
    return Context.from_request(req)


class TestStripRequestContent:
    def test_strips_known_fields(self) -> None:
        ctx = _ctx(
            body={
                "model": "x",
                "messages": [{}],
                "tools": [{}],
                "toolConfig": {},
                "tool_choice": "auto",
                "contents": [{}],
                "prompt": "p",
                "input": "i",
                "stream": True,
                "other_field": "keep",
            }
        )
        strip_request_content(ctx)
        assert ctx._body.get("model") is None
        assert ctx.messages == []
        assert ctx.tools == []
        for key in ("toolConfig", "tool_choice", "contents", "prompt", "input", "stream"):
            assert key not in ctx._body
        assert ctx._body["other_field"] == "keep"

    def test_empty_body_is_safe(self) -> None:
        ctx = _ctx(body={})
        strip_request_content(ctx)
        assert ctx.messages == []
        assert ctx.tools == []

    def test_missing_keys_are_safe(self) -> None:
        ctx = _ctx(body={"extra": 1})
        strip_request_content(ctx)
        assert ctx.messages == []
        assert ctx.tools == []
        assert ctx._body["extra"] == 1


class TestStripAuthHeaders:
    def test_removes_all_auth_headers(self) -> None:
        ctx = _ctx(
            headers={
                "authorization": "Bearer x",
                "x-api-key": "y",
                "x-goog-api-key": "z",
                "x-other": "keep",
            }
        )
        strip_auth_headers(ctx)
        req = ctx._resolve_request()
        assert req is not None
        assert "authorization" not in req.headers
        assert "x-api-key" not in req.headers
        assert "x-goog-api-key" not in req.headers
        assert req.headers["x-other"] == "keep"

    def test_missing_auth_headers_are_safe(self) -> None:
        ctx = _ctx(headers={"x-other": "keep"})
        strip_auth_headers(ctx)
        req = ctx._resolve_request()
        assert req is not None
        assert req.headers["x-other"] == "keep"


class TestStripTransportHeaders:
    def test_removes_transport_headers(self) -> None:
        ctx = _ctx(
            headers={
                "content-length": "10",
                "host": "example.com",
                "transfer-encoding": "chunked",
                "connection": "keep-alive",
                "x-custom": "keep",
            }
        )
        strip_transport_headers(ctx)
        req = ctx._resolve_request()
        assert req is not None
        for name in ("content-length", "host", "transfer-encoding", "connection"):
            assert name not in req.headers
        assert req.headers["x-custom"] == "keep"


class TestStripSystemBlocks:
    def test_removes_all_by_default(self) -> None:
        ctx = _ctx(body={"system": [{"text": "a"}, {"text": "b"}], "other": 1})
        strip_system_blocks(ctx)
        assert ctx.system == []
        assert ctx._body["other"] == 1

    def test_keep_first(self) -> None:
        ctx = _ctx(body={"system": [{"text": "a"}, {"text": "b"}, {"text": "c"}]})
        strip_system_blocks(ctx, keep=":1")
        assert len(ctx.system) == 1
        assert ctx.system[0].content == "a"

    def test_keep_last_two(self) -> None:
        ctx = _ctx(body={"system": [{"text": "a"}, {"text": "b"}, {"text": "c"}]})
        strip_system_blocks(ctx, keep="-2:")
        assert len(ctx.system) == 2
        assert ctx.system[0].content == "b"
        assert ctx.system[1].content == "c"

    def test_keep_single_index(self) -> None:
        ctx = _ctx(body={"system": [{"text": "a"}, {"text": "b"}, {"text": "c"}]})
        strip_system_blocks(ctx, keep="1")
        assert len(ctx.system) == 1
        assert ctx.system[0].content == "b"

    def test_missing_system_is_safe(self) -> None:
        ctx = _ctx(body={"foo": "bar"})
        strip_system_blocks(ctx)
        assert ctx._body == {"foo": "bar"}

    def test_string_system_is_unchanged(self) -> None:
        ctx = _ctx(body={"system": "just a string"})
        strip_system_blocks(ctx, keep=":1")
        assert len(ctx.system) == 1
        assert ctx.system[0].content == "just a string"

    def test_empty_list_with_keep(self) -> None:
        ctx = _ctx(body={"system": []})
        strip_system_blocks(ctx, keep=":1")
        assert ctx.system == []
