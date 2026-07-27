"""Response-side finish-reason capture and wire-shape projection.

The response IR (``ModelResponsePart`` + ``ModelResponseStreamEvent``) has no
slot for *why* a turn ended — pydantic-ai keeps that on ``ModelResponse.
finish_reason``, side-channel state the parts manager never sees. The intake
FSMs therefore capture it off the already-parsed wire events into the shared
``IntakeState.finish_reason`` funnel slot, and the render seams project it back
into each listener's native spelling. Same two directions as :mod:`_usage`:

* ``from_*`` map a raw provider value (wire string or SDK enum) into the
  canonical :data:`pydantic_ai.messages.FinishReason`. Values the IR cannot
  express map to ``None``; the caller carries the raw string through
  ``raw_extras`` so nothing is silently dropped.
* ``to_*`` project a captured ``FinishReason`` into a listener wire value.

Why this matters: a turn that hit the token ceiling, tripped a safety filter, or
died on an upstream error is otherwise indistinguishable from a clean
completion. Every cross-format transform — buffered and streaming alike — runs
through these helpers so the two paths agree on the answer for a given intake
state.

**Vocabulary mismatch is resolved in favour of truth.** OpenAI Chat's
``finish_reason`` and Anthropic's ``stop_reason`` are closed enums with no
member for ``"error"``; both wires get the literal ``"error"`` rather than a
fabricated completion. Stainless-generated SDKs (Anthropic, OpenAI) parse
response enums permissively for forward compatibility, so an out-of-enum value
reaches the client as a plain string instead of raising. The OpenAI Responses
envelope needs no such escape — ``status: "failed"`` is spec-normative.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pydantic_ai.messages import FinishReason


def _wire_value(raw: Any) -> str | None:
    """Normalize a raw wire finish reason (string or SDK enum) to a plain string."""
    if raw is None:
        return None
    value = getattr(raw, "value", raw)
    return value if isinstance(value, str) else None


# ── Capture: provider wire value → canonical FinishReason ───────────────────


_OPENAI_CHAT_WIRE: dict[str, FinishReason] = {
    "stop": "stop",
    "length": "length",
    "tool_calls": "tool_call",
    "content_filter": "content_filter",
    "function_call": "tool_call",
}

_OPENAI_RESPONSES_WIRE: dict[str, FinishReason] = {
    # ``incomplete_details.reason`` first, then the envelope ``status``.
    "max_output_tokens": "length",
    "content_filter": "content_filter",
    "completed": "stop",
    "cancelled": "error",
    "failed": "error",
}

_ANTHROPIC_WIRE: dict[str, FinishReason] = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "model_context_window_exceeded": "length",
    "tool_use": "tool_call",
    "refusal": "content_filter",
    # ``pause_turn`` and ``compaction`` describe a turn the client is expected
    # to resume, which the IR cannot express; they stay unmapped and ride
    # through the intake's ``raw_extras`` instead.
}

_GOOGLE_WIRE: dict[str, FinishReason] = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
    "LANGUAGE": "content_filter",
    "BLOCKLIST": "content_filter",
    "PROHIBITED_CONTENT": "content_filter",
    "SPII": "content_filter",
    "IMAGE_SAFETY": "content_filter",
    "IMAGE_PROHIBITED_CONTENT": "content_filter",
    "IMAGE_RECITATION": "content_filter",
    "MALFORMED_FUNCTION_CALL": "error",
    "UNEXPECTED_TOOL_CALL": "error",
    "OTHER": "error",
    "NO_IMAGE": "error",
    "IMAGE_OTHER": "error",
    # ``FINISH_REASON_UNSPECIFIED`` carries no information — stays unmapped.
}


def from_openai_chat(raw: Any) -> FinishReason | None:
    """Map an OpenAI Chat Completions ``choice.finish_reason`` to the IR value."""
    value = _wire_value(raw)
    return _OPENAI_CHAT_WIRE.get(value) if value else None


def from_openai_responses(raw: Any) -> FinishReason | None:
    """Map an OpenAI Responses ``incomplete_details.reason`` / ``status`` to the IR value."""
    value = _wire_value(raw)
    return _OPENAI_RESPONSES_WIRE.get(value) if value else None


def from_anthropic(raw: Any) -> FinishReason | None:
    """Map an Anthropic ``stop_reason`` to the IR value."""
    value = _wire_value(raw)
    return _ANTHROPIC_WIRE.get(value) if value else None


def from_google(raw: Any) -> FinishReason | None:
    """Map a Google ``candidate.finishReason`` (wire string or SDK enum) to the IR value."""
    value = _wire_value(raw)
    return _GOOGLE_WIRE.get(value) if value else None


# ── Projection: canonical FinishReason → listener wire value ────────────────


_IR_TO_OPENAI_CHAT: dict[FinishReason, str] = {
    "stop": "stop",
    "length": "length",
    "tool_call": "tool_calls",
    "content_filter": "content_filter",
    "error": "error",
}

_IR_TO_ANTHROPIC: dict[FinishReason, str] = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_call": "tool_use",
    "content_filter": "refusal",
    "error": "error",
}

_IR_TO_RESPONSES_STATUS: dict[FinishReason, str] = {
    "stop": "completed",
    "length": "incomplete",
    "tool_call": "completed",
    "content_filter": "incomplete",
    "error": "failed",
}

_IR_TO_RESPONSES_INCOMPLETE_REASON: dict[FinishReason, str] = {
    "length": "max_output_tokens",
    "content_filter": "content_filter",
}


def to_openai_chat(reason: FinishReason | None, *, tool_calls: bool = False) -> str:
    """Project to an OpenAI Chat Completions ``choice.finish_reason``.

    ``tool_calls`` states whether the assembled turn actually carries tool
    calls. An upstream that reports a plain completion for a tool-calling turn
    (Gemini answers ``STOP``, Anthropic ``tool_use`` only sometimes survives a
    cross-format hop) would otherwise tell the client there is nothing to run,
    so a rendered tool call wins over ``stop`` — never over a substantive
    reason such as ``length``.
    """
    if reason is None or reason == "stop":
        return "tool_calls" if tool_calls else "stop"
    return _IR_TO_OPENAI_CHAT.get(reason, reason)


def to_anthropic(reason: FinishReason | None, *, tool_calls: bool = False) -> str:
    """Project to an Anthropic ``stop_reason`` (same tool-call precedence as chat)."""
    if reason is None or reason == "stop":
        return "tool_use" if tool_calls else "end_turn"
    return _IR_TO_ANTHROPIC.get(reason, reason)


def to_openai_responses_status(reason: FinishReason | None) -> str:
    """Project to an OpenAI Responses envelope ``status``.

    ``"completed"`` / ``"incomplete"`` / ``"failed"`` — the three terminal
    statuses of the Response object, each with its own terminal stream event.
    """
    if reason is None:
        return "completed"
    return _IR_TO_RESPONSES_STATUS.get(reason, "completed")


def to_openai_responses_incomplete_reason(reason: FinishReason | None) -> str | None:
    """Project to ``incomplete_details.reason``; ``None`` when the turn is not incomplete."""
    if reason is None:
        return None
    return _IR_TO_RESPONSES_INCOMPLETE_REASON.get(reason)
