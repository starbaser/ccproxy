"""Tests for ccproxy.utils.extract_first_user_text."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from ccproxy.utils import extract_first_user_text


@dataclass(frozen=True)
class ExtractTextTestCase:
    name: str
    """Descriptive name for the test scenario."""

    messages: list[dict[str, Any]]
    """Input messages list."""

    expected: str
    """Expected return value."""


EXTRACT_TEXT_TEST_CASES: list[ExtractTextTestCase] = [
    ExtractTextTestCase(
        name="string_content",
        messages=[{"role": "user", "content": "hello world"}],
        expected="hello world",
    ),
    ExtractTextTestCase(
        name="text_block_content",
        messages=[{"role": "user", "content": [{"type": "text", "text": "hello"}]}],
        expected="hello",
    ),
    ExtractTextTestCase(
        name="no_user_message",
        messages=[{"role": "assistant", "content": "hi"}],
        expected="",
    ),
    ExtractTextTestCase(
        name="tool_result_then_text",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "x", "content": "out"},
                    {"type": "text", "text": "after tool"},
                ],
            }
        ],
        expected="after tool",
    ),
    ExtractTextTestCase(
        name="only_tool_result_returns_empty",
        messages=[
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "x", "content": "out"}],
            }
        ],
        expected="",
    ),
    ExtractTextTestCase(
        name="empty_messages",
        messages=[],
        expected="",
    ),
    ExtractTextTestCase(
        name="none_content",
        messages=[{"role": "user", "content": None}],
        expected="",
    ),
    ExtractTextTestCase(
        name="empty_string_content",
        messages=[{"role": "user", "content": ""}],
        expected="",
    ),
    ExtractTextTestCase(
        name="empty_first_text_block_returns_empty_per_signing_ts",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": ""},
                    {"type": "text", "text": "non-empty"},
                ],
            }
        ],
        expected="",
    ),
    ExtractTextTestCase(
        name="multiple_users_returns_first",
        messages=[
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "..."},
            {"role": "user", "content": "second"},
        ],
        expected="first",
    ),
    ExtractTextTestCase(
        name="empty_content_list",
        messages=[{"role": "user", "content": []}],
        expected="",
    ),
    ExtractTextTestCase(
        name="assistant_then_user",
        messages=[
            {"role": "assistant", "content": "hi"},
            {"role": "user", "content": "actual question"},
        ],
        expected="actual question",
    ),
]


@pytest.mark.parametrize(
    "test_case",
    [pytest.param(tc, id=tc.name) for tc in EXTRACT_TEXT_TEST_CASES],
)
def test_extract_first_user_text(test_case: ExtractTextTestCase) -> None:
    """Verify extract_first_user_text matches the K19 helper semantics."""
    result = extract_first_user_text(messages=test_case.messages)
    assert result == test_case.expected
