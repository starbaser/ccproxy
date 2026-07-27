"""Funnel: why a turn ended, from upstream wire to listener terminator.

The streaming pipeline used to hand its render terminators only ``usage`` and
``raw_extras``. Every SSE terminator therefore announced a fixed outcome — the
OpenAI Chat finish chunk said ``stop`` (or ``tool_calls``), the Anthropic
``message_delta`` said ``end_turn``, the Responses postlude said
``response.completed`` — no matter what the upstream reported. A turn truncated
at the token ceiling, stopped by a content filter, or killed by an upstream
error was byte-for-byte indistinguishable from a clean completion for every
provider's cross-format streaming transform.

These tests cross that threshold. Each streaming case drives a real
:class:`SSEPipeline` with an upstream stream carrying a non-``stop`` reason and
asserts the client-visible terminator carries it:

* upstream Anthropic ``max_tokens`` → OpenAI Chat ``finish_reason: "length"``
* upstream OpenAI ``length`` / ``tool_calls`` → Anthropic ``stop_reason:
  "max_tokens"`` / ``"tool_use"``
* upstream OpenAI ``content_filter`` → ``response.incomplete`` with
  ``incomplete_details.reason: "content_filter"``
* an idle-timed-out ``openai_conversations`` turn → ``"error"`` on all three

plus the intake-side capture that feeds them (Anthropic ``stop_reason``, Google
``finishReason``), and the invariant that the streaming terminator and the
buffered body agree for the same intake state.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest
from pydantic_ai.messages import FinishReason
from pydantic_ai.models import ModelRequestParameters

from ccproxy.lightllm.graph import _finish_reason, dispatch_intake, dispatch_render
from ccproxy.lightllm.graph.anthropic_intake import AnthropicResponseIntakeFSM
from ccproxy.lightllm.graph.buffered import render_parts_to_listener, transform_buffered_response_sync
from ccproxy.lightllm.graph.google_intake import GoogleResponseIntakeFSM
from ccproxy.lightllm.graph.sse_pipeline import SSEPipeline
from ccproxy.lightllm.parsed import InboundFormat

# ── SSE frame builders ─────────────────────────────────────────────────────


def _anthropic_stream(*, stop_reason: str, text: str = "partial") -> list[bytes]:
    """A complete Anthropic Messages SSE stream ending on ``stop_reason``."""
    events: list[dict[str, Any]] = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_finish",
                "type": "message",
                "role": "assistant",
                "model": "claude-haiku-4-5",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 12, "output_tokens": 1},
            },
        },
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": 7},
        },
        {"type": "message_stop"},
    ]
    return [f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode() for event in events]


def _anthropic_body(*, stop_reason: str, text: str = "partial") -> bytes:
    return json.dumps(
        {
            "id": "msg_finish",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "model": "claude-haiku-4-5",
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 12, "output_tokens": 7},
        }
    ).encode()


def _openai_chat_stream(*, finish_reason: str, tool_call: bool = False) -> list[bytes]:
    """An OpenAI Chat Completions SSE stream ending on ``finish_reason``."""
    delta: dict[str, Any] = {"role": "assistant"}
    if tool_call:
        delta["tool_calls"] = [
            {
                "index": 0,
                "id": "call_1",
                "type": "function",
                "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'},
            }
        ]
    else:
        delta["content"] = "partial"
    chunks: list[dict[str, Any]] = [
        {
            "id": "chatcmpl-finish",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "gpt-4o",
            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
        },
        {
            "id": "chatcmpl-finish",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "gpt-4o",
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
        },
    ]
    return [f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks] + [b"data: [DONE]\n\n"]


def _google_chunk(*, finish_reason: str, parts: list[dict[str, Any]] | None = None) -> bytes:
    chunk = {
        "candidates": [
            {
                "content": {"parts": parts if parts is not None else [{"text": "partial"}], "role": "model"},
                "finishReason": finish_reason,
                "index": 0,
            }
        ],
        "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 7},
    }
    return f"data: {json.dumps(chunk)}\n\n".encode()


def _conversations_idle_timeout_stream() -> list[bytes]:
    """An ``openai_conversations`` turn that produced content, then idled out.

    The synthetic ``ccproxy_idle_timeout`` side event is what the session_ws /
    ws_handoff bridges inject when their idle backstop gives up; the intake maps
    it to ``finish_reason = "error"``.
    """
    add_frame = {
        "p": "",
        "o": "add",
        "v": {
            "message": {
                "id": "asst",
                "author": {"role": "assistant"},
                "content": {"content_type": "text", "parts": [""]},
                "status": "in_progress",
                "metadata": {"model_slug": "gpt-5", "is_visually_hidden_from_conversation": False},
            },
            "conversation_id": "conv-1",
        },
        "c": 0,
    }
    patch_frame = {"v": [{"p": "/message/content/parts/0", "o": "append", "v": "partial answer"}]}
    timeout_frame = {"type": "ccproxy_idle_timeout", "idle_seconds": 120.0}
    return [f"data: {json.dumps(frame)}\n\n".encode() for frame in (add_frame, patch_frame, timeout_frame)]


# ── Pipeline drivers ───────────────────────────────────────────────────────


def _run_pipeline(*, provider_type: str, inbound_format: InboundFormat, frames: list[bytes], model: str = "m") -> str:
    """Drive a streaming :class:`SSEPipeline` and return the listener SSE text."""
    intake = dispatch_intake(provider_type=provider_type, model=model, request_params=ModelRequestParameters())
    render = dispatch_render(inbound_format=inbound_format, model=model)
    pipeline = SSEPipeline(intake=intake, render=render)
    out = bytearray()
    try:
        for frame in frames:
            chunk = pipeline(frame)
            if isinstance(chunk, bytes):
                out += chunk
        tail = pipeline(b"")
        if isinstance(tail, bytes):
            out += tail
    finally:
        pipeline.close()
    return out.decode()


def _run_collect(*, provider_type: str, inbound_format: InboundFormat, frames: list[bytes]) -> dict[str, Any]:
    """Drive a collect-mode :class:`SSEPipeline` and return the buffered JSON object."""
    intake = dispatch_intake(provider_type=provider_type, model="m", request_params=ModelRequestParameters())

    def _buffered_render(parts: list[Any]) -> bytes:
        return render_parts_to_listener(
            parts=parts,
            inbound_format=inbound_format,
            model="m",
            provider_response_id=intake.provider_response_id,
            finish_reason=intake.finish_reason,
            usage=intake.usage,
        )

    pipeline = SSEPipeline(intake=intake, buffered_render=_buffered_render)
    out = bytearray()
    try:
        for frame in frames:
            chunk = pipeline(frame)
            if isinstance(chunk, bytes):
                out += chunk
        tail = pipeline(b"")
        if isinstance(tail, bytes):
            out += tail
    finally:
        pipeline.close()
    parsed: dict[str, Any] = json.loads(bytes(out))
    return parsed


def _sse_events(stream: str) -> list[dict[str, Any]]:
    """Every ``data:`` payload in the stream, excluding the ``[DONE]`` sentinel."""
    objects: list[dict[str, Any]] = []
    for line in stream.splitlines():
        if line.startswith("data:"):
            payload = line[len("data:") :].strip()
            if payload and payload != "[DONE]":
                objects.append(json.loads(payload))
    return objects


def _chat_terminator(stream: str) -> dict[str, Any]:
    """The final Chat Completions chunk carrying a non-null ``finish_reason``."""
    chunks = [
        obj for obj in _sse_events(stream) if obj.get("choices") and obj["choices"][0].get("finish_reason") is not None
    ]
    assert len(chunks) == 1, f"expected exactly one finish chunk, got {len(chunks)}"
    choice: dict[str, Any] = chunks[0]["choices"][0]
    return choice


def _anthropic_message_delta(stream: str) -> dict[str, Any]:
    deltas = [obj for obj in _sse_events(stream) if obj.get("type") == "message_delta"]
    assert len(deltas) == 1, f"expected exactly one message_delta, got {len(deltas)}"
    payload: dict[str, Any] = deltas[0]
    return payload


def _responses_terminator(stream: str) -> dict[str, Any]:
    terminal = [
        obj
        for obj in _sse_events(stream)
        if obj.get("type") in {"response.completed", "response.incomplete", "response.failed"}
    ]
    assert len(terminal) == 1, f"expected exactly one terminal envelope event, got {len(terminal)}"
    payload: dict[str, Any] = terminal[0]
    return payload


# ── Projection table ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProjectionCase:
    name: str
    reason: FinishReason | None
    openai_chat: str
    anthropic: str
    responses_status: str
    responses_incomplete_reason: str | None


PROJECTION_CASES: list[ProjectionCase] = [
    ProjectionCase(
        name="unreported",
        reason=None,
        openai_chat="stop",
        anthropic="end_turn",
        responses_status="completed",
        responses_incomplete_reason=None,
    ),
    ProjectionCase(
        name="stop",
        reason="stop",
        openai_chat="stop",
        anthropic="end_turn",
        responses_status="completed",
        responses_incomplete_reason=None,
    ),
    ProjectionCase(
        name="length",
        reason="length",
        openai_chat="length",
        anthropic="max_tokens",
        responses_status="incomplete",
        responses_incomplete_reason="max_output_tokens",
    ),
    ProjectionCase(
        name="content_filter",
        reason="content_filter",
        openai_chat="content_filter",
        anthropic="refusal",
        responses_status="incomplete",
        responses_incomplete_reason="content_filter",
    ),
    ProjectionCase(
        name="tool_call",
        reason="tool_call",
        openai_chat="tool_calls",
        anthropic="tool_use",
        responses_status="completed",
        responses_incomplete_reason=None,
    ),
    ProjectionCase(
        name="error",
        reason="error",
        openai_chat="error",
        anthropic="error",
        responses_status="failed",
        responses_incomplete_reason=None,
    ),
]


@pytest.mark.parametrize("case", [pytest.param(c, id=c.name) for c in PROJECTION_CASES])
def test_projection_covers_every_listener(case: ProjectionCase) -> None:
    assert _finish_reason.to_openai_chat(case.reason) == case.openai_chat
    assert _finish_reason.to_anthropic(case.reason) == case.anthropic
    assert _finish_reason.to_openai_responses_status(case.reason) == case.responses_status
    assert _finish_reason.to_openai_responses_incomplete_reason(case.reason) == case.responses_incomplete_reason


def test_rendered_tool_call_supplies_a_missing_reason() -> None:
    """An upstream that reports nothing (or a plain completion) for a tool-calling
    turn must not tell the client there is nothing to run."""
    assert _finish_reason.to_openai_chat(None, tool_calls=True) == "tool_calls"
    assert _finish_reason.to_openai_chat("stop", tool_calls=True) == "tool_calls"
    assert _finish_reason.to_anthropic(None, tool_calls=True) == "tool_use"
    assert _finish_reason.to_anthropic("stop", tool_calls=True) == "tool_use"


def test_substantive_reason_outranks_a_rendered_tool_call() -> None:
    """A truncated tool-calling turn is truncated first — ``length`` survives."""
    assert _finish_reason.to_openai_chat("length", tool_calls=True) == "length"
    assert _finish_reason.to_anthropic("length", tool_calls=True) == "max_tokens"


# ── Intake capture ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("stop_reason", "expected"),
    [
        ("end_turn", "stop"),
        ("stop_sequence", "stop"),
        ("max_tokens", "length"),
        ("model_context_window_exceeded", "length"),
        ("tool_use", "tool_call"),
        ("refusal", "content_filter"),
    ],
)
async def test_anthropic_intake_captures_stop_reason(stop_reason: str, expected: FinishReason) -> None:
    fsm = AnthropicResponseIntakeFSM(model="claude-haiku-4-5", request_params=ModelRequestParameters())
    for frame in _anthropic_stream(stop_reason=stop_reason):
        await fsm.feed(frame)
    await fsm.close()
    assert fsm.finish_reason == expected
    # The raw wire value rides through too — nothing about the ending is dropped.
    assert fsm.raw_extras["stop_reason"] == stop_reason


async def test_anthropic_intake_carries_unmappable_stop_reason_through_raw_extras() -> None:
    """``pause_turn`` describes a resumable turn the IR cannot express: no
    fabricated finish reason, but the raw value is still carried."""
    fsm = AnthropicResponseIntakeFSM(model="claude-haiku-4-5", request_params=ModelRequestParameters())
    for frame in _anthropic_stream(stop_reason="pause_turn"):
        await fsm.feed(frame)
    await fsm.close()
    assert fsm.finish_reason is None
    assert fsm.raw_extras["stop_reason"] == "pause_turn"


@pytest.mark.parametrize(
    ("wire_reason", "expected"),
    [
        ("STOP", "stop"),
        ("MAX_TOKENS", "length"),
        ("SAFETY", "content_filter"),
        ("PROHIBITED_CONTENT", "content_filter"),
        ("MALFORMED_FUNCTION_CALL", "error"),
    ],
)
async def test_google_intake_captures_finish_reason(wire_reason: str, expected: FinishReason) -> None:
    fsm = GoogleResponseIntakeFSM(model="gemini-2.0-flash", request_params=ModelRequestParameters())
    await fsm.feed(_google_chunk(finish_reason=wire_reason))
    await fsm.close()
    assert fsm.finish_reason == expected


async def test_google_intake_captures_finish_reason_on_a_contentless_candidate() -> None:
    """A MAX_TOKENS cut often arrives on a candidate with no parts at all — the
    frame the parts walk skips. The reason must survive that short-circuit."""
    chunk = {"candidates": [{"finishReason": "MAX_TOKENS", "index": 0}]}
    fsm = GoogleResponseIntakeFSM(model="gemini-2.0-flash", request_params=ModelRequestParameters())
    await fsm.feed(f"data: {json.dumps(chunk)}\n\n".encode())
    await fsm.close()
    assert fsm.finish_reason == "length"


# ── Streaming terminators: the three renderers ─────────────────────────────


def test_streaming_openai_chat_terminator_carries_length() -> None:
    """Upstream Anthropic truncated at ``max_tokens``; the OpenAI Chat client
    used to receive ``finish_reason: "stop"`` — a completed turn."""
    stream = _run_pipeline(
        provider_type="anthropic",
        inbound_format=InboundFormat.OPENAI_CHAT,
        frames=_anthropic_stream(stop_reason="max_tokens"),
    )
    assert _chat_terminator(stream)["finish_reason"] == "length"
    assert stream.rstrip().endswith("data: [DONE]")


def test_streaming_openai_chat_terminator_carries_content_filter() -> None:
    stream = _run_pipeline(
        provider_type="anthropic",
        inbound_format=InboundFormat.OPENAI_CHAT,
        frames=_anthropic_stream(stop_reason="refusal"),
    )
    assert _chat_terminator(stream)["finish_reason"] == "content_filter"


def test_streaming_openai_chat_terminator_still_says_stop_for_a_clean_turn() -> None:
    stream = _run_pipeline(
        provider_type="anthropic",
        inbound_format=InboundFormat.OPENAI_CHAT,
        frames=_anthropic_stream(stop_reason="end_turn"),
    )
    assert _chat_terminator(stream)["finish_reason"] == "stop"


def test_streaming_anthropic_terminator_carries_max_tokens() -> None:
    """Upstream OpenAI truncated at ``length``; the Anthropic client used to
    receive ``stop_reason: "end_turn"``."""
    stream = _run_pipeline(
        provider_type="openai",
        inbound_format=InboundFormat.ANTHROPIC_MESSAGES,
        frames=_openai_chat_stream(finish_reason="length"),
    )
    assert _anthropic_message_delta(stream)["delta"]["stop_reason"] == "max_tokens"


def test_streaming_anthropic_terminator_carries_tool_use() -> None:
    """A tool-calling turn ending as ``end_turn`` tells the Anthropic client
    there is nothing to run."""
    stream = _run_pipeline(
        provider_type="openai",
        inbound_format=InboundFormat.ANTHROPIC_MESSAGES,
        frames=_openai_chat_stream(finish_reason="tool_calls", tool_call=True),
    )
    assert _anthropic_message_delta(stream)["delta"]["stop_reason"] == "tool_use"


def test_streaming_anthropic_terminator_derives_tool_use_when_upstream_says_stop() -> None:
    """Gemini answers ``STOP`` even for a function call — the rendered tool block
    is what makes the turn actionable."""
    stream = _run_pipeline(
        provider_type="google",
        inbound_format=InboundFormat.ANTHROPIC_MESSAGES,
        frames=[
            _google_chunk(
                finish_reason="STOP",
                parts=[{"functionCall": {"name": "get_weather", "args": {"city": "Paris"}}}],
            )
        ],
    )
    assert _anthropic_message_delta(stream)["delta"]["stop_reason"] == "tool_use"


def test_streaming_anthropic_terminator_still_says_end_turn_for_a_clean_turn() -> None:
    stream = _run_pipeline(
        provider_type="openai",
        inbound_format=InboundFormat.ANTHROPIC_MESSAGES,
        frames=_openai_chat_stream(finish_reason="stop"),
    )
    assert _anthropic_message_delta(stream)["delta"]["stop_reason"] == "end_turn"


def test_streaming_openai_responses_terminator_is_incomplete_on_content_filter() -> None:
    """The Responses postlude used to be ``response.completed`` unconditionally."""
    stream = _run_pipeline(
        provider_type="openai",
        inbound_format=InboundFormat.OPENAI_RESPONSES,
        frames=_openai_chat_stream(finish_reason="content_filter"),
    )
    terminator = _responses_terminator(stream)
    assert terminator["type"] == "response.incomplete"
    assert terminator["response"]["status"] == "incomplete"
    assert terminator["response"]["incomplete_details"] == {"reason": "content_filter"}


def test_streaming_openai_responses_terminator_is_incomplete_on_length() -> None:
    stream = _run_pipeline(
        provider_type="anthropic",
        inbound_format=InboundFormat.OPENAI_RESPONSES,
        frames=_anthropic_stream(stop_reason="max_tokens"),
    )
    terminator = _responses_terminator(stream)
    assert terminator["type"] == "response.incomplete"
    assert terminator["response"]["incomplete_details"] == {"reason": "max_output_tokens"}


def test_streaming_openai_responses_terminator_still_completes_a_clean_turn() -> None:
    stream = _run_pipeline(
        provider_type="anthropic",
        inbound_format=InboundFormat.OPENAI_RESPONSES,
        frames=_anthropic_stream(stop_reason="end_turn"),
    )
    terminator = _responses_terminator(stream)
    assert terminator["type"] == "response.completed"
    assert terminator["response"]["status"] == "completed"
    assert "incomplete_details" not in terminator["response"]


# ── The idle-terminated turn, on the streaming path ────────────────────────


def test_streaming_idle_timeout_reaches_every_listener_terminator() -> None:
    """The turn that started this: an ``openai_conversations`` turn whose idle
    backstop gave up. The intake classifies it ``"error"`` and the buffered body
    already reported it — the SSE terminator a ``stream: true`` client actually
    receives did not."""
    frames = _conversations_idle_timeout_stream()

    chat = _run_pipeline(
        provider_type="openai_conversations",
        inbound_format=InboundFormat.OPENAI_CHAT,
        frames=frames,
    )
    assert _chat_terminator(chat)["finish_reason"] == "error"

    anthropic = _run_pipeline(
        provider_type="openai_conversations",
        inbound_format=InboundFormat.ANTHROPIC_MESSAGES,
        frames=frames,
    )
    assert _anthropic_message_delta(anthropic)["delta"]["stop_reason"] == "error"

    responses = _run_pipeline(
        provider_type="openai_conversations",
        inbound_format=InboundFormat.OPENAI_RESPONSES,
        frames=frames,
    )
    assert _responses_terminator(responses)["type"] == "response.failed"


def test_streaming_idle_timeout_preserves_the_partial_content() -> None:
    """The classification never costs the content the turn did produce."""
    stream = _run_pipeline(
        provider_type="openai_conversations",
        inbound_format=InboundFormat.OPENAI_CHAT,
        frames=_conversations_idle_timeout_stream(),
    )
    text = "".join(
        obj["choices"][0]["delta"].get("content", "")
        for obj in _sse_events(stream)
        if obj.get("choices") and obj["choices"][0].get("delta")
    )
    assert text == "partial answer"


def test_streaming_completed_turn_is_distinguishable_from_the_idle_one() -> None:
    """The whole point: the two endings differ on the wire."""
    timed_out = _run_pipeline(
        provider_type="openai_conversations",
        inbound_format=InboundFormat.OPENAI_CHAT,
        frames=_conversations_idle_timeout_stream(),
    )
    completed_frames = [*_conversations_idle_timeout_stream()[:-1], b"data: [DONE]\n\n"]
    completed = _run_pipeline(
        provider_type="openai_conversations",
        inbound_format=InboundFormat.OPENAI_CHAT,
        frames=completed_frames,
    )
    assert _chat_terminator(timed_out)["finish_reason"] == "error"
    assert _chat_terminator(completed)["finish_reason"] == "stop"


# ── Streaming and buffered agree ───────────────────────────────────────────


@pytest.mark.parametrize("stop_reason", ["end_turn", "max_tokens", "refusal", "tool_use"])
def test_streaming_terminator_agrees_with_the_buffered_body(stop_reason: str) -> None:
    """One projection, two paths: what a ``stream: true`` client is told about
    the ending must match what a ``stream: false`` client is told."""
    stream = _run_pipeline(
        provider_type="anthropic",
        inbound_format=InboundFormat.ANTHROPIC_MESSAGES,
        frames=_anthropic_stream(stop_reason=stop_reason),
    )
    buffered = json.loads(
        transform_buffered_response_sync(
            raw_bytes=_anthropic_body(stop_reason=stop_reason),
            provider_type="anthropic",
            inbound_format=InboundFormat.ANTHROPIC_MESSAGES,
            model="m",
            request_params=ModelRequestParameters(),
        )
    )
    assert _anthropic_message_delta(stream)["delta"]["stop_reason"] == buffered["stop_reason"]


def test_collect_mode_and_streaming_mode_agree_on_the_idle_turn() -> None:
    frames = _conversations_idle_timeout_stream()
    collected = _run_collect(
        provider_type="openai_conversations",
        inbound_format=InboundFormat.OPENAI_CHAT,
        frames=frames,
    )
    stream = _run_pipeline(
        provider_type="openai_conversations",
        inbound_format=InboundFormat.OPENAI_CHAT,
        frames=frames,
    )
    assert collected["choices"][0]["finish_reason"] == _chat_terminator(stream)["finish_reason"] == "error"
