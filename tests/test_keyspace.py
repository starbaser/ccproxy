"""Unit tests for extract_available_keys (pipeline/keyspace.py)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from ccproxy.pipeline.context import Context
from ccproxy.pipeline.keyspace import _walk_dict, extract_available_keys


def _make_flow(body: dict, headers: dict | None = None) -> MagicMock:
    flow = MagicMock()
    flow.id = "test-id"
    flow.request.content = json.dumps(body).encode()
    flow.request.headers = dict(headers or {})
    return flow


class TestExtractAvailableKeys:
    def test_top_level_body_keys(self) -> None:
        flow = _make_flow({"model": "claude-3", "messages": [], "system": "hi"})
        ctx = Context.from_flow(flow)
        keys = extract_available_keys(ctx)
        assert "model" in keys
        assert "messages" in keys
        assert "system" in keys

    def test_nested_dict_dot_paths(self) -> None:
        flow = _make_flow(
            {
                "metadata": {"user_id": "foo", "session_id": "bar"},
                "model": "m",
            }
        )
        ctx = Context.from_flow(flow)
        keys = extract_available_keys(ctx)
        assert "metadata" in keys
        assert "metadata.user_id" in keys
        assert "metadata.session_id" in keys
        assert "model" in keys

    def test_deeply_nested_dict(self) -> None:
        flow = _make_flow(
            {
                "outer": {"middle": {"inner": "value"}},
            }
        )
        ctx = Context.from_flow(flow)
        keys = extract_available_keys(ctx)
        assert "outer" in keys
        assert "outer.middle" in keys
        assert "outer.middle.inner" in keys

    def test_lists_skipped(self) -> None:
        flow = _make_flow(
            {
                "messages": [{"role": "user", "content": "hi"}],
            }
        )
        ctx = Context.from_flow(flow)
        keys = extract_available_keys(ctx)
        # Parent dict key present
        assert "messages" in keys
        # No index-based or element-field paths
        assert "messages.0" not in keys
        assert "messages.role" not in keys

    def test_empty_body_produces_only_headers(self) -> None:
        flow = _make_flow({}, headers={"X-Test": "v"})
        ctx = Context.from_flow(flow)
        keys = extract_available_keys(ctx)
        assert keys == {"x-test"}

    def test_header_names_lowercased(self) -> None:
        flow = _make_flow(
            {"model": "m"},
            headers={"Authorization": "Bearer x", "X-API-Key": "k"},
        )
        ctx = Context.from_flow(flow)
        keys = extract_available_keys(ctx)
        assert "authorization" in keys
        assert "x-api-key" in keys

    def test_extract_session_id_pattern(self) -> None:
        """Regression: `reads=["metadata"]` must resolve when metadata dict exists."""
        flow = _make_flow(
            {
                "metadata": {"user_id": "claude_code-123_456_789"},
                "model": "m",
            }
        )
        ctx = Context.from_flow(flow)
        keys = extract_available_keys(ctx)
        # The extract_session_id hook declares `reads=["metadata"]`
        assert "metadata" in keys
        # Subpath also available if a hook wants `metadata.user_id` directly
        assert "metadata.user_id" in keys


class TestWalkDictHelper:
    def test_walks_mixed_types(self) -> None:
        out: set[str] = set()
        _walk_dict(
            {"a": 1, "b": {"c": 2, "d": [1, 2]}, "e": "str"},
            prefix="",
            out=out,
        )
        assert out == {"a", "b", "b.c", "b.d", "e"}

    def test_non_dict_input_noop(self) -> None:
        out: set[str] = set()
        _walk_dict([1, 2, 3], prefix="", out=out)  # type: ignore[arg-type]
        assert out == set()

    def test_prefix_prepended(self) -> None:
        out: set[str] = set()
        _walk_dict({"x": {"y": 1}}, prefix="root", out=out)
        assert out == {"root.x", "root.x.y"}
