"""Sync facade over the async dispatch_dump (replacement for outbound_sync).

Verifies ``dispatch_dump_sync`` produces bytes byte-equal to
``asyncio.run(dispatch_dump(...))`` across every supported provider, and
that the unsupported-provider path still raises ``UnsupportedUpstreamError``.

This is the FSM-side replacement for ``test_lightllm_outbound_sync.py``.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models import ModelRequestParameters

from ccproxy.lightllm.graph import (
    UnsupportedUpstreamError,
    dispatch_dump,
    dispatch_dump_sync,
    dispatch_intake,
)
from ccproxy.lightllm.graph.openai_responses_intake import OpenAIResponsesIntakeFSM
from ccproxy.lightllm.parsed import ParsedRequest


def _make_parsed(
    *,
    model: str = "test-model",
    raw_extras: dict[str, Any] | None = None,
) -> ParsedRequest:
    return ParsedRequest(
        model=model,
        messages=[ModelRequest(parts=[UserPromptPart(content="hello")])],
        request_parameters=ModelRequestParameters(),
        settings={},
        stream=False,
        raw_extras=raw_extras or {},
    )


@pytest.mark.parametrize(
    ("provider_type", "model"),
    [
        ("anthropic", "claude-3"),
        ("deepseek", "deepseek-chat"),
        ("zai", "glm-4"),
        ("minimax", "MiniMax-M3"),
        ("openai", "gpt-4o"),
        ("openai_responses", "gpt-5"),
        ("google", "gemini-1.5-pro"),
        ("gemini", "gemini-1.5-pro"),
        ("vertex_ai", "gemini-1.5-pro"),
    ],
)
def test_dispatch_dump_sync_matches_async(provider_type: str, model: str) -> None:
    parsed = _make_parsed(model=model)
    expected = asyncio.run(dispatch_dump(parsed, provider_type=provider_type))
    actual = dispatch_dump_sync(parsed, provider_type=provider_type)
    assert actual == expected


def test_dispatch_dump_sync_matches_async_perplexity_pro() -> None:
    """Perplexity Pro mints a ``frontend_uuid`` per request. Lock it via
    patch so both async and sync paths emit identical bytes."""
    parsed = _make_parsed(
        model="perplexity/best",
        raw_extras={
            "pplx": {
                "last_backend_uuid": "11111111-1111-1111-1111-111111111111",
                "frontend_context_uuid": "22222222-2222-2222-2222-222222222222",
                "read_write_token": "tok",
            }
        },
    )

    with patch(
        "ccproxy.lightllm.pplx.uuid.uuid4",
        return_value="33333333-3333-3333-3333-333333333333",
    ):
        expected = asyncio.run(dispatch_dump(parsed, provider_type="perplexity_pro"))
    with patch(
        "ccproxy.lightllm.pplx.uuid.uuid4",
        return_value="33333333-3333-3333-3333-333333333333",
    ):
        actual = dispatch_dump_sync(parsed, provider_type="perplexity_pro")

    assert actual == expected


def test_dispatch_dump_sync_raises_for_unknown_provider() -> None:
    parsed = _make_parsed()
    with pytest.raises(UnsupportedUpstreamError, match="no outbound renderer"):
        dispatch_dump_sync(parsed, provider_type="not-a-real-provider")


def test_dispatch_intake_openai_responses() -> None:
    intake = dispatch_intake(
        provider_type="openai_responses",
        model="gpt-5",
        request_params=ModelRequestParameters(),
    )
    assert isinstance(intake, OpenAIResponsesIntakeFSM)


class TestDispatchTelemetry:
    """Never-silently-drop diagnostics for the graph dispatchers.

    Each dispatcher logs (DEBUG) the resolved FSM/adapter choice and WARNS
    before raising on an unknown/unsupported provider or listener format.
    """

    def test_dispatch_intake_logs_resolution(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level("DEBUG", logger="ccproxy.lightllm.graph"):
            dispatch_intake(provider_type="anthropic", model="claude-3", request_params=ModelRequestParameters())
        assert "AnthropicResponseIntakeFSM" in caplog.text

    def test_dispatch_intake_warns_then_raises_on_unknown(self, caplog: pytest.LogCaptureFixture) -> None:
        with (
            caplog.at_level("WARNING", logger="ccproxy.lightllm.graph"),
            pytest.raises(UnsupportedUpstreamError, match="no response intake"),
        ):
            dispatch_intake(provider_type="not-a-real-provider", model="x", request_params=ModelRequestParameters())
        assert "no response intake" in caplog.text
        assert any(r.levelname == "WARNING" for r in caplog.records)

    def test_dispatch_render_logs_resolution(self, caplog: pytest.LogCaptureFixture) -> None:
        from ccproxy.lightllm.graph import dispatch_render
        from ccproxy.lightllm.parsed import InboundFormat

        with caplog.at_level("DEBUG", logger="ccproxy.lightllm.graph"):
            dispatch_render(inbound_format=InboundFormat.OPENAI_CHAT, model="gpt-4o")
        assert "OpenAIResponseRenderFSM" in caplog.text

    def test_dispatch_render_warns_then_raises_on_unknown(self, caplog: pytest.LogCaptureFixture) -> None:
        from ccproxy.lightllm.graph import UnsupportedListenerError, dispatch_render
        from ccproxy.lightllm.parsed import InboundFormat

        with (
            caplog.at_level("WARNING", logger="ccproxy.lightllm.graph"),
            pytest.raises(UnsupportedListenerError, match="no response render"),
        ):
            dispatch_render(inbound_format=InboundFormat.UNKNOWN, model="x")
        assert "no response render" in caplog.text

    def test_dispatch_dump_sync_warns_then_raises_on_unknown(self, caplog: pytest.LogCaptureFixture) -> None:
        parsed = _make_parsed()
        with (
            caplog.at_level("WARNING", logger="ccproxy.lightllm.graph"),
            pytest.raises(UnsupportedUpstreamError, match="no outbound renderer"),
        ):
            dispatch_dump_sync(parsed, provider_type="not-a-real-provider")
        assert "no outbound renderer" in caplog.text
