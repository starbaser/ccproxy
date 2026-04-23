"""Tests for prepare functions in ccproxy.shaping.prepare."""

from __future__ import annotations

import json
from typing import Any

from mitmproxy import http

from ccproxy.pipeline.context import Context
from ccproxy.shaping.prepare import strip_headers


def _ctx(headers: dict[str, str] | None = None, body: dict[str, Any] | None = None) -> Context:
    content = json.dumps(body or {}).encode() if body is not None else b""
    req = http.Request.make("POST", "https://seed.example/v1", content, headers or {})
    return Context.from_request(req)


_AUTH = ["authorization", "x-api-key", "x-goog-api-key"]
_TRANSPORT = ["content-length", "host", "transfer-encoding", "connection"]


class TestStripHeaders:
    def test_removes_auth_headers(self) -> None:
        ctx = _ctx(
            headers={
                "authorization": "Bearer x",
                "x-api-key": "y",
                "x-goog-api-key": "z",
                "x-other": "keep",
            }
        )
        strip_headers(ctx, _AUTH)
        req = ctx._resolve_request()
        assert req is not None
        assert "authorization" not in req.headers
        assert "x-api-key" not in req.headers
        assert "x-goog-api-key" not in req.headers
        assert req.headers["x-other"] == "keep"

    def test_missing_headers_are_safe(self) -> None:
        ctx = _ctx(headers={"x-other": "keep"})
        strip_headers(ctx, _AUTH)
        req = ctx._resolve_request()
        assert req is not None
        assert req.headers["x-other"] == "keep"

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
        strip_headers(ctx, _TRANSPORT)
        req = ctx._resolve_request()
        assert req is not None
        for name in _TRANSPORT:
            assert name not in req.headers
        assert req.headers["x-custom"] == "keep"

    def test_custom_header_list(self) -> None:
        ctx = _ctx(headers={"x-custom-auth": "secret", "x-keep": "yes"})
        strip_headers(ctx, ["x-custom-auth"])
        req = ctx._resolve_request()
        assert req is not None
        assert "x-custom-auth" not in req.headers
        assert req.headers["x-keep"] == "yes"
