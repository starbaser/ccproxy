"""Tests for ccproxy.shaping.caching strip and insert hooks."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

import pytest

from ccproxy.pipeline.context import Context
from ccproxy.pipeline.hook import get_registry
from ccproxy.shaping.caching.insert import InsertParams
from ccproxy.shaping.caching.strip import StripParams


def _make_ctx(body: dict[str, Any]) -> Context:
    """Build a bare Context from a body dict (no flow)."""
    return Context(flow=None, _body=copy.deepcopy(body))


def test_strip_params_validates() -> None:
    """StripParams validates paths as list of strings."""
    params = StripParams(paths=["system.*.cache_control"])
    assert params.paths == ["system.*.cache_control"]


def test_insert_params_defaults() -> None:
    """InsertParams provides default value."""
    params = InsertParams(path="system.-1.cache_control")
    assert params.value == {"type": "ephemeral"}


SYSTEM_WITH_CACHE = [
    {"type": "text", "text": "shape-0", "cache_control": {"type": "ephemeral"}},
    {"type": "text", "text": "shape-1", "cache_control": {"type": "ephemeral"}},
    {"type": "text", "text": "app-0"},
    {"type": "text", "text": "app-1", "cache_control": {"type": "ephemeral"}},
]

TOOLS_WITH_CACHE = [
    {"name": "tool_a", "input_schema": {}, "cache_control": {"type": "ephemeral"}},
    {"name": "tool_b", "input_schema": {}},
]


# ---------------------------------------------------------------------------
# Strip tests
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StripTestCase:
    name: str
    """Descriptive test name."""

    body: dict[str, Any]
    """Input body."""

    paths: list[str]
    """Glom paths to strip."""

    expected_cache_control_count: int
    """How many cache_control keys should remain after strip."""


STRIP_TEST_CASES: list[StripTestCase] = [
    StripTestCase(
        name="strip_all_system_cache_control",
        body={"system": copy.deepcopy(SYSTEM_WITH_CACHE)},
        paths=["system.*.cache_control"],
        expected_cache_control_count=0,
    ),
    StripTestCase(
        name="strip_system_and_tools",
        body={
            "system": copy.deepcopy(SYSTEM_WITH_CACHE),
            "tools": copy.deepcopy(TOOLS_WITH_CACHE),
        },
        paths=["system.*.cache_control", "tools.*.cache_control"],
        expected_cache_control_count=0,
    ),
    StripTestCase(
        name="strip_first_system_block_only",
        body={"system": copy.deepcopy(SYSTEM_WITH_CACHE)},
        paths=["system.0.cache_control"],
        expected_cache_control_count=2,
    ),
    StripTestCase(
        name="empty_paths_noop",
        body={"system": copy.deepcopy(SYSTEM_WITH_CACHE)},
        paths=[],
        expected_cache_control_count=3,
    ),
    StripTestCase(
        name="nonexistent_field_no_error",
        body={"system": copy.deepcopy(SYSTEM_WITH_CACHE)},
        paths=["nonexistent.*.cache_control"],
        expected_cache_control_count=3,
    ),
    StripTestCase(
        name="no_system_in_body",
        body={"messages": []},
        paths=["system.*.cache_control"],
        expected_cache_control_count=0,
    ),
]


def _count_cache_control(body: dict[str, Any]) -> int:
    """Count total cache_control keys across system and tools."""
    count = 0
    for field in ("system", "tools"):
        for block in body.get(field, []):
            if isinstance(block, dict) and "cache_control" in block:
                count += 1
    return count


@pytest.mark.parametrize(
    "test_case",
    [pytest.param(tc, id=tc.name) for tc in STRIP_TEST_CASES],
)
def test_strip(test_case: StripTestCase) -> None:
    """Test strip hook removes cache_control at targeted paths."""
    spec = get_registry().get_spec("strip")
    assert spec is not None

    ctx = _make_ctx(test_case.body)
    spec.execute(ctx, extra_params={"paths": test_case.paths})

    assert _count_cache_control(ctx._body) == test_case.expected_cache_control_count


def test_strip_invalid_path_no_crash() -> None:
    """Malformed glom path logs debug, doesn't crash."""
    body = {"system": [{"type": "text", "text": "a", "cache_control": {"type": "ephemeral"}}]}
    ctx = _make_ctx(body)
    spec = get_registry().get_spec("strip")
    assert spec is not None
    spec.execute(ctx, extra_params={"paths": [""]})
    assert ctx._body["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_strip_preserves_other_keys() -> None:
    """Strip removes cache_control but leaves type and text intact."""
    body = {
        "system": [
            {"type": "text", "text": "hello", "cache_control": {"type": "ephemeral"}},
        ]
    }
    ctx = _make_ctx(body)
    spec = get_registry().get_spec("strip")
    assert spec is not None
    spec.execute(ctx, extra_params={"paths": ["system.*.cache_control"]})

    block = ctx._body["system"][0]
    assert block == {"type": "text", "text": "hello"}


# ---------------------------------------------------------------------------
# Insert tests
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InsertTestCase:
    name: str
    """Descriptive test name."""

    body: dict[str, Any]
    """Input body."""

    path: str
    """Glom path for insertion."""

    value: Any
    """Value to insert."""

    check_path: tuple[str, int]
    """(field, index) to verify the inserted value."""


INSERT_TEST_CASES: list[InsertTestCase] = [
    InsertTestCase(
        name="insert_last_system_block",
        body={
            "system": [
                {"type": "text", "text": "a"},
                {"type": "text", "text": "b"},
            ]
        },
        path="system.-1.cache_control",
        value={"type": "ephemeral"},
        check_path=("system", -1),
    ),
    InsertTestCase(
        name="insert_last_tool",
        body={
            "tools": [
                {"name": "t1", "input_schema": {}},
                {"name": "t2", "input_schema": {}},
            ]
        },
        path="tools.-1.cache_control",
        value={"type": "ephemeral"},
        check_path=("tools", -1),
    ),
    InsertTestCase(
        name="insert_first_system_block",
        body={
            "system": [
                {"type": "text", "text": "a"},
                {"type": "text", "text": "b"},
            ]
        },
        path="system.0.cache_control",
        value={"type": "ephemeral"},
        check_path=("system", 0),
    ),
    InsertTestCase(
        name="insert_with_custom_ttl",
        body={
            "system": [
                {"type": "text", "text": "a"},
            ]
        },
        path="system.-1.cache_control",
        value={"type": "ephemeral", "ttl": "1h"},
        check_path=("system", -1),
    ),
]


@pytest.mark.parametrize(
    "test_case",
    [pytest.param(tc, id=tc.name) for tc in INSERT_TEST_CASES],
)
def test_insert(test_case: InsertTestCase) -> None:
    """Test insert hook sets cache_control at targeted path."""
    spec = get_registry().get_spec("insert")
    assert spec is not None

    ctx = _make_ctx(test_case.body)
    spec.execute(ctx, extra_params={"path": test_case.path, "value": test_case.value})

    field, idx = test_case.check_path
    block = ctx._body[field][idx]
    assert block["cache_control"] == test_case.value


def test_insert_empty_list_no_error() -> None:
    """Insert into empty system list logs debug, no crash."""
    ctx = _make_ctx({"system": []})
    spec = get_registry().get_spec("insert")
    assert spec is not None
    spec.execute(ctx, extra_params={"path": "system.-1.cache_control", "value": {"type": "ephemeral"}})
    assert ctx._body["system"] == []


def test_insert_missing_field_no_error() -> None:
    """Insert when field is absent logs debug, no crash."""
    ctx = _make_ctx({})
    spec = get_registry().get_spec("insert")
    assert spec is not None
    spec.execute(ctx, extra_params={"path": "system.-1.cache_control", "value": {"type": "ephemeral"}})
    assert "system" not in ctx._body


# ---------------------------------------------------------------------------
# Integration: strip then insert
# ---------------------------------------------------------------------------


def test_strip_then_insert_normalizes_breakpoints() -> None:
    """After strip + insert, only the last system block has cache_control."""
    body = {
        "system": copy.deepcopy(SYSTEM_WITH_CACHE),
        "tools": copy.deepcopy(TOOLS_WITH_CACHE),
    }
    ctx = _make_ctx(body)

    strip_spec = get_registry().get_spec("strip")
    insert_spec = get_registry().get_spec("insert")
    assert strip_spec is not None
    assert insert_spec is not None

    strip_spec.execute(ctx, extra_params={"paths": ["system.*.cache_control"]})
    insert_spec.execute(
        ctx,
        extra_params={
            "path": "system.-1.cache_control",
            "value": {"type": "ephemeral"},
        },
    )

    system = ctx._body["system"]
    for i, block in enumerate(system[:-1]):
        assert "cache_control" not in block, f"system[{i}] should not have cache_control"
    assert system[-1]["cache_control"] == {"type": "ephemeral"}

    # tools untouched
    assert ctx._body["tools"][0]["cache_control"] == {"type": "ephemeral"}
