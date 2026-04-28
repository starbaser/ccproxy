"""Tests for Gemini v1internal shape hook — inject_gemini_content."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from mitmproxy import http

from ccproxy.pipeline.context import Context
from ccproxy.shaping.gemini import inject_gemini_content


def _make_ctx(body: dict[str, Any]) -> Context:
    """Build a Context from a body dict via a synthetic mitmproxy Request."""
    req = http.Request.make(
        "POST",
        "https://cloudcode-pa.googleapis.com/v1internal:generateContent",
        content=b"{}",
        headers={"content-type": "application/json"},
    )
    ctx = Context.from_request(req)
    ctx._body = body
    return ctx


@dataclass(frozen=True)
class InjectTestCase:
    name: str
    """Descriptive name for the test scenario."""

    shape_body: dict[str, Any]
    """Shape context body (the captured template)."""

    incoming_body: dict[str, Any]
    """Incoming context body (the client request)."""

    expected_request: dict[str, Any]
    """Expected request field in shape body after injection."""


INJECT_TEST_CASES: list[InjectTestCase] = [
    InjectTestCase(
        name="contents_replaced_from_incoming",
        shape_body={
            "model": "gemini-3.1-pro-preview",
            "request": {
                "session_id": "shape-session-123",
                "contents": [{"role": "user", "parts": [{"text": "shape prompt"}]}],
                "generationConfig": {"topP": 0.95, "topK": 64},
            },
        },
        incoming_body={
            "model": "gemini-3.1-pro-preview",
            "request": {
                "contents": [{"role": "user", "parts": [{"text": "real user prompt"}]}],
                "generationConfig": {"maxOutputTokens": 8192, "temperature": 1.0},
            },
        },
        expected_request={
            "session_id": "shape-session-123",
            "contents": [{"role": "user", "parts": [{"text": "real user prompt"}]}],
            "generationConfig": {
                "topP": 0.95,
                "topK": 64,
                "maxOutputTokens": 8192,
                "temperature": 1.0,
            },
        },
    ),
    InjectTestCase(
        name="generation_config_incoming_overrides_shape",
        shape_body={
            "request": {
                "contents": [{"role": "user", "parts": [{"text": "shape"}]}],
                "generationConfig": {
                    "maxOutputTokens": 4096,
                    "temperature": 0.5,
                    "topP": 0.95,
                    "thinkingConfig": {"includeThoughts": True},
                },
            },
        },
        incoming_body={
            "request": {
                "contents": [{"role": "user", "parts": [{"text": "incoming"}]}],
                "generationConfig": {"maxOutputTokens": 16384, "temperature": 0.8},
            },
        },
        expected_request={
            "contents": [{"role": "user", "parts": [{"text": "incoming"}]}],
            "generationConfig": {
                "maxOutputTokens": 16384,
                "temperature": 0.8,
                "topP": 0.95,
                "thinkingConfig": {"includeThoughts": True},
            },
        },
    ),
    InjectTestCase(
        name="system_instruction_from_incoming",
        shape_body={
            "request": {
                "contents": [{"role": "user", "parts": [{"text": "shape"}]}],
                "generationConfig": {},
            },
        },
        incoming_body={
            "request": {
                "contents": [{"role": "user", "parts": [{"text": "incoming"}]}],
                "generationConfig": {},
                "systemInstruction": {"parts": [{"text": "You are helpful."}]},
            },
        },
        expected_request={
            "contents": [{"role": "user", "parts": [{"text": "incoming"}]}],
            "generationConfig": {},
            "systemInstruction": {"parts": [{"text": "You are helpful."}]},
        },
    ),
    InjectTestCase(
        name="no_incoming_contents_preserves_shape",
        shape_body={
            "request": {
                "session_id": "abc",
                "contents": [{"role": "user", "parts": [{"text": "shape only"}]}],
                "generationConfig": {"topP": 0.95},
            },
        },
        incoming_body={
            "request": {
                "generationConfig": {"maxOutputTokens": 8192},
            },
        },
        expected_request={
            "session_id": "abc",
            "contents": [{"role": "user", "parts": [{"text": "shape only"}]}],
            "generationConfig": {"topP": 0.95, "maxOutputTokens": 8192},
        },
    ),
]


@pytest.mark.parametrize(
    "test_case",
    [pytest.param(tc, id=tc.name) for tc in INJECT_TEST_CASES],
)
def test_inject_gemini_content(test_case: InjectTestCase) -> None:
    shape_ctx = _make_ctx(test_case.shape_body)
    incoming_ctx = _make_ctx(test_case.incoming_body)

    result = inject_gemini_content(shape_ctx, {"incoming_ctx": incoming_ctx})

    assert result._body["request"] == test_case.expected_request


def test_missing_incoming_ctx_returns_unchanged() -> None:
    body = {"request": {"contents": [{"text": "original"}]}}
    ctx = _make_ctx(body)

    result = inject_gemini_content(ctx, {})

    assert result._body["request"]["contents"] == [{"text": "original"}]


def test_non_dict_shape_request_returns_unchanged() -> None:
    ctx = _make_ctx({"request": "not-a-dict"})
    incoming = _make_ctx({"request": {"contents": [{"text": "hi"}]}})

    result = inject_gemini_content(ctx, {"incoming_ctx": incoming})

    assert result._body["request"] == "not-a-dict"


def test_non_dict_incoming_request_returns_unchanged() -> None:
    body = {"request": {"contents": [{"text": "original"}]}}
    ctx = _make_ctx(body)
    incoming = _make_ctx({"request": "not-a-dict"})

    result = inject_gemini_content(ctx, {"incoming_ctx": incoming})

    assert result._body["request"]["contents"] == [{"text": "original"}]
