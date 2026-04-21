"""Tests for shaping/body.py JSON helpers."""

from __future__ import annotations

from typing import Any

from mitmproxy import http

from ccproxy.shaping.body import get_body, mutate_body, set_body


def _req(content: bytes = b"") -> http.Request:
    return http.Request.make("POST", "https://example/", content, {})


class TestGetBody:
    def test_returns_parsed_dict(self) -> None:
        req = _req(b'{"k": "v"}')
        assert get_body(req) == {"k": "v"}

    def test_returns_empty_dict_on_empty_body(self) -> None:
        assert get_body(_req(b"")) == {}

    def test_returns_empty_dict_on_malformed_json(self) -> None:
        assert get_body(_req(b"not json {")) == {}

    def test_returns_empty_dict_on_non_object_top_level(self) -> None:
        assert get_body(_req(b"[1, 2, 3]")) == {}


class TestSetBody:
    def test_serializes_dict(self) -> None:
        req = _req()
        set_body(req, {"k": "v"})
        assert req.content == b'{"k": "v"}'


class TestMutateBody:
    def test_roundtrip_mutation(self) -> None:
        req = _req(b'{"a": 1}')
        mutate_body(req, lambda b: b.update(b=2))
        assert get_body(req) == {"a": 1, "b": 2}

    def test_mutation_on_empty_starts_from_dict(self) -> None:
        req = _req()

        def add(body: dict[str, Any]) -> None:
            body["hello"] = "world"

        mutate_body(req, add)
        assert get_body(req) == {"hello": "world"}
