"""Tests for ccproxy.utils.extract_first_user_text and Gemini-shape helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from ccproxy.utils import (
    extract_first_user_text,
    extract_first_user_text_gemini,
    gemini_contents,
)


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


@dataclass(frozen=True)
class GeminiContentsCase:
    name: str
    body: dict[str, Any]
    expected: list[dict[str, Any]] | None


GEMINI_CONTENTS_CASES: list[GeminiContentsCase] = [
    GeminiContentsCase(
        name="native_shape_top_level_contents",
        body={"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
        expected=[{"role": "user", "parts": [{"text": "hi"}]}],
    ),
    GeminiContentsCase(
        name="v1internal_wrapped_request_contents",
        body={"model": "x", "request": {"contents": [{"role": "user", "parts": [{"text": "wrapped"}]}]}},
        expected=[{"role": "user", "parts": [{"text": "wrapped"}]}],
    ),
    GeminiContentsCase(
        name="anthropic_shape_returns_none",
        body={"messages": [{"role": "user", "content": "x"}]},
        expected=None,
    ),
    GeminiContentsCase(
        name="empty_body_returns_none",
        body={},
        expected=None,
    ),
    GeminiContentsCase(
        name="non_dict_request_returns_none",
        body={"request": "not-a-dict"},
        expected=None,
    ),
    GeminiContentsCase(
        name="non_list_contents_returns_none",
        body={"contents": "not-a-list"},
        expected=None,
    ),
]


@pytest.mark.parametrize(
    "test_case",
    [pytest.param(tc, id=tc.name) for tc in GEMINI_CONTENTS_CASES],
)
def test_gemini_contents(test_case: GeminiContentsCase) -> None:
    """Verify gemini_contents picks up native and wrapped Gemini bodies."""
    assert gemini_contents(body=test_case.body) == test_case.expected


@dataclass(frozen=True)
class GeminiTextCase:
    name: str
    contents: list[dict[str, Any]]
    expected: str


GEMINI_TEXT_CASES: list[GeminiTextCase] = [
    GeminiTextCase(
        name="single_user_text_part",
        contents=[{"role": "user", "parts": [{"text": "hi"}]}],
        expected="hi",
    ),
    GeminiTextCase(
        name="user_skips_non_text_parts",
        contents=[
            {
                "role": "user",
                "parts": [
                    {"functionResponse": {"name": "f", "response": {}}},
                    {"text": "actual"},
                ],
            }
        ],
        expected="actual",
    ),
    GeminiTextCase(
        name="model_then_user_returns_user",
        contents=[
            {"role": "model", "parts": [{"text": "model speaks"}]},
            {"role": "user", "parts": [{"text": "user speaks"}]},
        ],
        expected="user speaks",
    ),
    GeminiTextCase(
        name="multiple_users_returns_first",
        contents=[
            {"role": "user", "parts": [{"text": "first"}]},
            {"role": "user", "parts": [{"text": "second"}]},
        ],
        expected="first",
    ),
    GeminiTextCase(
        name="no_user_role_returns_empty",
        contents=[{"role": "model", "parts": [{"text": "hi"}]}],
        expected="",
    ),
    GeminiTextCase(
        name="user_without_parts_returns_empty",
        contents=[{"role": "user", "parts": "not-a-list"}],
        expected="",
    ),
    GeminiTextCase(
        name="user_with_empty_text_returns_empty",
        contents=[{"role": "user", "parts": [{"text": ""}]}],
        expected="",
    ),
    GeminiTextCase(
        name="empty_contents_returns_empty",
        contents=[],
        expected="",
    ),
]


@pytest.mark.parametrize(
    "test_case",
    [pytest.param(tc, id=tc.name) for tc in GEMINI_TEXT_CASES],
)
def test_extract_first_user_text_gemini(test_case: GeminiTextCase) -> None:
    """Verify Gemini-shape first-user-text extraction."""
    assert extract_first_user_text_gemini(contents=test_case.contents) == test_case.expected
