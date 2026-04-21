"""Tests for default prepare functions in ccproxy.shaping.prepare."""

from __future__ import annotations

import json
from typing import Any

from mitmproxy import http

from ccproxy.shaping.prepare import (
    strip_auth_headers,
    strip_request_content,
    strip_system_blocks,
    strip_transport_headers,
)


def _req(headers: dict[str, str] | None = None, body: dict[str, Any] | None = None) -> http.Request:
    content = json.dumps(body or {}).encode() if body is not None else b""
    return http.Request.make("POST", "https://seed.example/v1", content, headers or {})


def _body(req: http.Request) -> dict[str, Any]:
    return json.loads(req.content or b"{}")


class TestStripRequestContent:
    def test_strips_known_fields(self) -> None:
        req = _req(
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
        strip_request_content(req)
        body = _body(req)
        for key in ("model", "messages", "tools", "toolConfig", "tool_choice",
                    "contents", "prompt", "input", "stream"):
            assert key not in body
        assert body["other_field"] == "keep"

    def test_empty_body_is_safe(self) -> None:
        req = _req(body={})
        strip_request_content(req)
        assert _body(req) == {}

    def test_missing_keys_are_safe(self) -> None:
        req = _req(body={"extra": 1})
        strip_request_content(req)
        assert _body(req) == {"extra": 1}


class TestStripAuthHeaders:
    def test_removes_all_auth_headers(self) -> None:
        req = _req(
            headers={
                "authorization": "Bearer x",
                "x-api-key": "y",
                "x-goog-api-key": "z",
                "x-other": "keep",
            }
        )
        strip_auth_headers(req)
        assert "authorization" not in req.headers
        assert "x-api-key" not in req.headers
        assert "x-goog-api-key" not in req.headers
        assert req.headers["x-other"] == "keep"

    def test_missing_auth_headers_are_safe(self) -> None:
        req = _req(headers={"x-other": "keep"})
        strip_auth_headers(req)
        assert req.headers["x-other"] == "keep"


class TestStripTransportHeaders:
    def test_removes_transport_headers(self) -> None:
        req = _req(
            headers={
                "content-length": "10",
                "host": "example.com",
                "transfer-encoding": "chunked",
                "connection": "keep-alive",
                "x-custom": "keep",
            }
        )
        strip_transport_headers(req)
        for name in ("content-length", "host", "transfer-encoding", "connection"):
            assert name not in req.headers
        assert req.headers["x-custom"] == "keep"


class TestStripSystemBlocks:
    def test_removes_all_by_default(self) -> None:
        req = _req(body={"system": [{"text": "a"}, {"text": "b"}], "other": 1})
        strip_system_blocks(req)
        body = _body(req)
        assert "system" not in body
        assert body["other"] == 1

    def test_keep_first(self) -> None:
        req = _req(body={"system": [{"text": "a"}, {"text": "b"}, {"text": "c"}]})
        strip_system_blocks(req, keep=":1")
        assert _body(req)["system"] == [{"text": "a"}]

    def test_keep_last_two(self) -> None:
        req = _req(body={"system": [{"text": "a"}, {"text": "b"}, {"text": "c"}]})
        strip_system_blocks(req, keep="-2:")
        assert _body(req)["system"] == [{"text": "b"}, {"text": "c"}]

    def test_keep_single_index(self) -> None:
        req = _req(body={"system": [{"text": "a"}, {"text": "b"}, {"text": "c"}]})
        strip_system_blocks(req, keep="1")
        assert _body(req)["system"] == [{"text": "b"}]

    def test_missing_system_is_safe(self) -> None:
        req = _req(body={"foo": "bar"})
        strip_system_blocks(req)
        assert _body(req) == {"foo": "bar"}

    def test_string_system_is_unchanged(self) -> None:
        req = _req(body={"system": "just a string"})
        strip_system_blocks(req, keep=":1")
        assert _body(req)["system"] == "just a string"

    def test_empty_list_with_keep(self) -> None:
        req = _req(body={"system": []})
        strip_system_blocks(req, keep=":1")
        assert _body(req)["system"] == []
