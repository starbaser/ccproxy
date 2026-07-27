"""Buffered (non-streaming) cross-provider response transform via FSM.

Reuses the per-upstream intake FSMs (Anthropic / OpenAI / OpenAI Responses /
Google / Perplexity) shipped under :mod:`ccproxy.lightllm.graph`.

Two structural cases per upstream:

1. **Provider-streaming body, client-buffered listener** — the upstream
   always emits SSE (Perplexity Pro, some Gemini OAuth flows). The body is
   concatenated SSE chunks. The intake FSM handles it natively; feed the
   whole body + close().

2. **Provider-buffered body, client-buffered listener** — Anthropic
   ``stream: false`` (``BetaMessage`` JSON), OpenAI ``stream: false``
   (``ChatCompletion`` JSON), Google ``:generateContent``
   (``GenerateContentResponse`` JSON). The JSON shape differs from the
   streaming-event shape so the intake can't parse it directly — we
   synthesize a sequence of streaming events that the intake WILL accept
   and feed those synthetic SSE frames through.

Per-provider conversion strategy:

* **Anthropic** (anthropic / deepseek / zai): parse ``BetaMessage`` JSON,
  synthesize an event stream the existing :class:`AnthropicResponseIntakeFSM`
  would emit — one ``message_start`` + (per content block) a
  ``content_block_start`` + a single ``content_block_delta`` covering the
  block's full content + a ``content_block_stop``, then ``message_delta``
  + ``message_stop``. Encode each synthesized event as an SSE frame and
  feed the whole batch.
* **OpenAI Chat**: parse ``ChatCompletion`` JSON, build a single
  ``ChatCompletionChunk``-shaped frame whose ``delta`` carries the entire
  ``message.content`` + ``tool_calls`` + ``finish_reason``. Single SSE frame.
* **OpenAI Responses**: pass concatenated Responses SSE through directly, or
  synthesize a Responses event stream from a buffered ``Response`` JSON body.
* **Google / Gemini / Vertex AI**: the buffered body is already a
  ``GenerateContentResponse`` — the same shape the streaming intake parses
  (``cloudcode-pa`` envelope unwrap is folded into the intake). Wrap as
  one SSE frame; the FSM handles the rest.
* **Perplexity Pro**: the buffered body IS concatenated SSE — feed
  directly without synthesis.

Output assembly:

Unlike the streaming pipeline (which drives an SSE render FSM and emits
listener SSE), buffered transforms must emit a single JSON object — the
buffered shape the listener client expects. The function pulls the final
assembled :class:`ModelResponsePartsManager.get_parts()` list after the
intake drains, then serializes those parts into the listener's buffered
JSON shape:

* :data:`InboundFormat.OPENAI_CHAT` → OpenAI ``ChatCompletion`` JSON.
* :data:`InboundFormat.ANTHROPIC_MESSAGES` → Anthropic ``BetaMessage``
  JSON.

The function is sync. For one-shot per-response use the simpler per-call
asyncio-loop pattern; the streaming side's persistent-loop pattern is
unjustified overhead here.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import time
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from pydantic_ai.messages import TextPart, ThinkingPart, ToolCallPart

from ccproxy.lightllm.graph import (
    _ANTHROPIC_COMPATIBLE,
    _GOOGLE_COMPATIBLE,
    UnsupportedListenerError,
    UnsupportedUpstreamError,
    _finish_reason,
    _usage,
    dispatch_intake,
)
from ccproxy.lightllm.parsed import InboundFormat

if TYPE_CHECKING:
    from pydantic_ai.messages import FinishReason, ModelResponsePart
    from pydantic_ai.models import ModelRequestParameters
    from pydantic_ai.usage import RequestUsage

    from ccproxy.lightllm.graph import AnyAsyncIntakeFSM

logger = logging.getLogger(__name__)


type _WireObject = dict[str, object]
type _NextSequence = Callable[[], int]


# ── SSE frame encoding helper ──────────────────────────────────────────────


def _frame(event_dict: dict[str, Any], *, event_name: str | None = None) -> bytes:
    """Encode one event dict as an SSE frame.

    Anthropic frames are conventionally ``event: <name>\\ndata: <json>\\n\\n``;
    OpenAI / Gemini / Perplexity frames are ``data: <json>\\n\\n``. The intake
    parsers accept both, but we honor the convention per provider so
    inspection of the synthesized bytes is unsurprising.
    """
    payload = json.dumps(event_dict, separators=(",", ":"))
    if event_name is not None:
        return f"event: {event_name}\ndata: {payload}\n\n".encode()
    return f"data: {payload}\n\n".encode()


# ── Anthropic: BetaMessage → synthetic event stream ────────────────────────


def _synthesize_anthropic_sse(body: dict[str, Any]) -> bytes:
    """Convert a buffered ``BetaMessage`` JSON dict into the synthetic SSE bytes
    the :class:`AnthropicResponseIntakeFSM` would consume.

    Mirrors what Anthropic itself would emit for ``stream: true``. Per content
    block we emit one ``content_block_start`` (carrying the *empty* block
    descriptor — matches the wire spec) + one ``content_block_delta`` (full
    content as the single delta) + one ``content_block_stop``. For
    ``redacted_thinking`` we attach the opaque ``data`` directly on the start
    event since there's no streaming delta variant for it.
    """
    message_obj: dict[str, Any] = {
        "id": body.get("id", "msg_buffered"),
        "type": "message",
        "role": body.get("role", "assistant"),
        "content": [],
        "model": body.get("model", "unknown"),
        "stop_reason": None,
        "stop_sequence": None,
        "usage": body.get("usage", {"input_tokens": 0, "output_tokens": 0}),
    }
    frames: list[bytes] = [
        _frame(
            {"type": "message_start", "message": message_obj},
            event_name="message_start",
        )
    ]

    for idx, block in enumerate(body.get("content") or []):
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            start_block: dict[str, Any] = {"type": "text", "text": ""}
            delta_event: dict[str, Any] | None = {
                "type": "text_delta",
                "text": block.get("text", ""),
            }
        elif btype == "thinking":
            # Emit content + signature deltas separately so the intake walks
            # both BetaThinkingDelta and BetaSignatureDelta branches.
            start_block = {"type": "thinking", "thinking": "", "signature": ""}
            content_text = block.get("thinking", "")
            signature = block.get("signature", "")
            frames.append(
                _frame(
                    {
                        "type": "content_block_start",
                        "index": idx,
                        "content_block": start_block,
                    },
                    event_name="content_block_start",
                )
            )
            if content_text:
                frames.append(
                    _frame(
                        {
                            "type": "content_block_delta",
                            "index": idx,
                            "delta": {
                                "type": "thinking_delta",
                                "thinking": content_text,
                            },
                        },
                        event_name="content_block_delta",
                    )
                )
            if signature:
                frames.append(
                    _frame(
                        {
                            "type": "content_block_delta",
                            "index": idx,
                            "delta": {
                                "type": "signature_delta",
                                "signature": signature,
                            },
                        },
                        event_name="content_block_delta",
                    )
                )
            frames.append(
                _frame(
                    {"type": "content_block_stop", "index": idx},
                    event_name="content_block_stop",
                )
            )
            continue
        elif btype == "redacted_thinking":
            # No streaming delta variant — pass the opaque ``data`` on start.
            start_block = {
                "type": "redacted_thinking",
                "data": block.get("data", ""),
            }
            frames.append(
                _frame(
                    {
                        "type": "content_block_start",
                        "index": idx,
                        "content_block": start_block,
                    },
                    event_name="content_block_start",
                )
            )
            frames.append(
                _frame(
                    {"type": "content_block_stop", "index": idx},
                    event_name="content_block_stop",
                )
            )
            continue
        elif btype == "tool_use":
            start_block = {
                "type": "tool_use",
                "id": block.get("id", ""),
                "name": block.get("name", ""),
                "input": {},
            }
            # Wire deltas carry the JSON-serialized args as ``partial_json``.
            input_obj = block.get("input") or {}
            input_json = json.dumps(input_obj, separators=(",", ":"))
            delta_event = {"type": "input_json_delta", "partial_json": input_json} if input_obj else None
        else:
            # Unknown block — pass through as a content_block_start with the
            # original payload; the intake's discriminated TypeAdapter will
            # skip what it can't parse.
            frames.append(
                _frame(
                    {
                        "type": "content_block_start",
                        "index": idx,
                        "content_block": block,
                    },
                    event_name="content_block_start",
                )
            )
            frames.append(
                _frame(
                    {"type": "content_block_stop", "index": idx},
                    event_name="content_block_stop",
                )
            )
            continue

        frames.append(
            _frame(
                {
                    "type": "content_block_start",
                    "index": idx,
                    "content_block": start_block,
                },
                event_name="content_block_start",
            )
        )
        if delta_event is not None:
            frames.append(
                _frame(
                    {
                        "type": "content_block_delta",
                        "index": idx,
                        "delta": delta_event,
                    },
                    event_name="content_block_delta",
                )
            )
        frames.append(
            _frame(
                {"type": "content_block_stop", "index": idx},
                event_name="content_block_stop",
            )
        )

    frames.append(
        _frame(
            {
                "type": "message_delta",
                "delta": {
                    "stop_reason": body.get("stop_reason"),
                    "stop_sequence": body.get("stop_sequence"),
                },
                "usage": body.get("usage", {"output_tokens": 0}),
            },
            event_name="message_delta",
        )
    )
    frames.append(_frame({"type": "message_stop"}, event_name="message_stop"))
    return b"".join(frames)


# ── OpenAI: ChatCompletion → synthetic ChatCompletionChunk ─────────────────


def _synthesize_openai_sse(body: dict[str, Any]) -> bytes:
    """Convert a buffered ``ChatCompletion`` JSON dict into a single synthetic
    ``ChatCompletionChunk`` SSE frame.

    The chunk's ``delta`` carries the entire ``message.content`` and any
    ``tool_calls``; ``finish_reason`` rides on the same chunk. The intake
    drains it via ``handle_text_delta`` / ``handle_tool_call_delta`` exactly
    like a single-event streaming response.
    """
    choices = body.get("choices") or []
    if not choices:
        logger.debug("buffered transform: ChatCompletion body has no choices; no synthetic SSE")
        return b""
    choice = choices[0]
    message = choice.get("message") or {}

    delta: dict[str, Any] = {"role": message.get("role", "assistant")}
    content = message.get("content")
    if content:
        delta["content"] = content
    refusal = message.get("refusal")
    if refusal:
        delta["refusal"] = refusal

    raw_tool_calls = message.get("tool_calls") or []
    if raw_tool_calls:
        out_tool_calls: list[dict[str, Any]] = []
        for tc_idx, tc in enumerate(raw_tool_calls):
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            args = fn.get("arguments", "")
            if not isinstance(args, str):
                args = json.dumps(args, separators=(",", ":"))
            out_tool_calls.append(
                {
                    "index": tc_idx,
                    "id": tc.get("id"),
                    "type": tc.get("type", "function"),
                    "function": {
                        "name": fn.get("name", ""),
                        "arguments": args,
                    },
                }
            )
        delta["tool_calls"] = out_tool_calls

    chunk_dict: dict[str, Any] = {
        "id": body.get("id", "chatcmpl-buffered"),
        "object": "chat.completion.chunk",
        "created": body.get("created", 0),
        "model": body.get("model", "unknown"),
        "choices": [
            {
                "index": choice.get("index", 0),
                "delta": delta,
                "finish_reason": choice.get("finish_reason"),
                "logprobs": choice.get("logprobs"),
            }
        ],
    }
    # Forward the buffered body's usage so the intake's funnel accumulator
    # captures it (the synthetic chunk stands in for the terminal usage chunk).
    if isinstance(body.get("usage"), dict):
        chunk_dict["usage"] = body["usage"]
    return _frame(chunk_dict) + b"data: [DONE]\n\n"


# ── OpenAI Responses: Response → synthetic Responses event stream ──────────


def _synthesize_openai_responses_sse(body: _WireObject) -> bytes:
    """Convert a buffered ``Response`` JSON dict into Responses SSE bytes."""
    sequence_number = 0

    def next_seq() -> int:
        nonlocal sequence_number
        seq = sequence_number
        sequence_number += 1
        return seq

    response_id = body.get("id", "resp_buffered")
    created_at = body.get("created_at", int(time.time()))
    model = body.get("model", "unknown")
    raw_output = body.get("output")
    output_items = (
        [cast(_WireObject, item) for item in raw_output if isinstance(item, dict)]
        if isinstance(raw_output, list)
        else []
    )

    response_base = {
        **body,
        "id": response_id,
        "object": body.get("object", "response"),
        "created_at": created_at,
        "model": model,
        "status": "in_progress",
        "output": [],
    }
    frames: list[bytes] = [
        _frame(
            {
                "type": "response.created",
                "response": response_base,
                "sequence_number": next_seq(),
            },
            event_name="response.created",
        )
    ]

    for output_index, item in enumerate(output_items):
        item_type = item.get("type")
        raw_item_id = item.get("id")
        item_id = raw_item_id if isinstance(raw_item_id, str) else f"item_{output_index}"

        if item_type == "message":
            added_item = {
                "id": item_id,
                "type": "message",
                "status": "in_progress",
                "content": [],
                "role": item.get("role", "assistant"),
            }
        elif item_type == "function_call":
            added_item = {**item, "arguments": "", "status": "in_progress"}
        elif item_type == "reasoning":
            added_item = {
                "id": item_id,
                "type": "reasoning",
                "status": "in_progress",
                "summary": [],
                "content": [],
            }
        else:
            added_item = item

        frames.append(
            _frame(
                {
                    "type": "response.output_item.added",
                    "output_index": output_index,
                    "item": added_item,
                    "sequence_number": next_seq(),
                },
                event_name="response.output_item.added",
            )
        )

        if item_type == "message":
            frames.extend(
                _synthesize_openai_responses_message_content(
                    item=item,
                    item_id=item_id,
                    output_index=output_index,
                    next_seq=next_seq,
                )
            )
        elif item_type == "function_call":
            raw_args = item.get("arguments")
            args = raw_args if isinstance(raw_args, str) else json.dumps(raw_args or {}, separators=(",", ":"))
            if args:
                frames.append(
                    _frame(
                        {
                            "type": "response.function_call_arguments.delta",
                            "item_id": item_id,
                            "output_index": output_index,
                            "delta": args,
                            "sequence_number": next_seq(),
                        },
                        event_name="response.function_call_arguments.delta",
                    )
                )
            frames.append(
                _frame(
                    {
                        "type": "response.function_call_arguments.done",
                        "item_id": item_id,
                        "output_index": output_index,
                        "name": item.get("name", ""),
                        "arguments": args,
                        "sequence_number": next_seq(),
                    },
                    event_name="response.function_call_arguments.done",
                )
            )
        elif item_type == "reasoning":
            frames.extend(
                _synthesize_openai_responses_reasoning_content(
                    item=item,
                    item_id=item_id,
                    output_index=output_index,
                    next_seq=next_seq,
                )
            )

        frames.append(
            _frame(
                {
                    "type": "response.output_item.done",
                    "output_index": output_index,
                    "item": item,
                    "sequence_number": next_seq(),
                },
                event_name="response.output_item.done",
            )
        )

    response_done = {
        **body,
        "id": response_id,
        "object": body.get("object", "response"),
        "created_at": created_at,
        "model": model,
        "status": body.get("status", "completed"),
        "output": output_items,
    }
    frames.append(
        _frame(
            {
                "type": "response.completed",
                "response": response_done,
                "sequence_number": next_seq(),
            },
            event_name="response.completed",
        )
    )
    return b"".join(frames)


def _synthesize_openai_responses_message_content(
    *,
    item: _WireObject,
    item_id: str,
    output_index: int,
    next_seq: _NextSequence,
) -> list[bytes]:
    frames: list[bytes] = []
    raw_content = item.get("content")
    content_parts = raw_content if isinstance(raw_content, list) else []
    for content_index, raw_part in enumerate(content_parts):
        if not isinstance(raw_part, dict):
            continue
        part = cast(_WireObject, raw_part)
        part_type = part.get("type")
        if part_type == "output_text":
            raw_text = part.get("text")
            text = raw_text if isinstance(raw_text, str) else ""
            empty_part = {**part, "text": ""}
            frames.append(
                _frame(
                    {
                        "type": "response.content_part.added",
                        "item_id": item_id,
                        "output_index": output_index,
                        "content_index": content_index,
                        "part": empty_part,
                        "sequence_number": next_seq(),
                    },
                    event_name="response.content_part.added",
                )
            )
            if text:
                frames.append(
                    _frame(
                        {
                            "type": "response.output_text.delta",
                            "item_id": item_id,
                            "output_index": output_index,
                            "content_index": content_index,
                            "delta": text,
                            "logprobs": [],
                            "sequence_number": next_seq(),
                        },
                        event_name="response.output_text.delta",
                    )
                )
            frames.append(
                _frame(
                    {
                        "type": "response.output_text.done",
                        "item_id": item_id,
                        "output_index": output_index,
                        "content_index": content_index,
                        "text": text,
                        "logprobs": part["logprobs"] if isinstance(part.get("logprobs"), list) else [],
                        "sequence_number": next_seq(),
                    },
                    event_name="response.output_text.done",
                )
            )
            frames.append(
                _frame(
                    {
                        "type": "response.content_part.done",
                        "item_id": item_id,
                        "output_index": output_index,
                        "content_index": content_index,
                        "part": part,
                        "sequence_number": next_seq(),
                    },
                    event_name="response.content_part.done",
                )
            )
        elif part_type == "refusal":
            raw_refusal = part.get("refusal")
            refusal = raw_refusal if isinstance(raw_refusal, str) else ""
            if refusal:
                frames.append(
                    _frame(
                        {
                            "type": "response.refusal.delta",
                            "item_id": item_id,
                            "output_index": output_index,
                            "content_index": content_index,
                            "delta": refusal,
                            "sequence_number": next_seq(),
                        },
                        event_name="response.refusal.delta",
                    )
                )
            frames.append(
                _frame(
                    {
                        "type": "response.refusal.done",
                        "item_id": item_id,
                        "output_index": output_index,
                        "content_index": content_index,
                        "refusal": refusal,
                        "sequence_number": next_seq(),
                    },
                    event_name="response.refusal.done",
                )
            )
    return frames


def _synthesize_openai_responses_reasoning_content(
    *,
    item: _WireObject,
    item_id: str,
    output_index: int,
    next_seq: _NextSequence,
) -> list[bytes]:
    frames: list[bytes] = []
    raw_summary = item.get("summary")
    summaries = raw_summary if isinstance(raw_summary, list) else []
    for summary_index, raw_summary_item in enumerate(summaries):
        if not isinstance(raw_summary_item, dict):
            continue
        summary = cast(_WireObject, raw_summary_item)
        raw_text = summary.get("text")
        text = raw_text if isinstance(raw_text, str) else ""
        if text:
            frames.append(
                _frame(
                    {
                        "type": "response.reasoning_summary_text.delta",
                        "item_id": item_id,
                        "output_index": output_index,
                        "summary_index": summary_index,
                        "delta": text,
                        "sequence_number": next_seq(),
                    },
                    event_name="response.reasoning_summary_text.delta",
                )
            )
        frames.append(
            _frame(
                {
                    "type": "response.reasoning_summary_text.done",
                    "item_id": item_id,
                    "output_index": output_index,
                    "summary_index": summary_index,
                    "text": text,
                    "sequence_number": next_seq(),
                },
                event_name="response.reasoning_summary_text.done",
            )
        )

    raw_content = item.get("content")
    content_parts = raw_content if isinstance(raw_content, list) else []
    for content_index, raw_content_part in enumerate(content_parts):
        if not isinstance(raw_content_part, dict):
            continue
        content = cast(_WireObject, raw_content_part)
        if content.get("type") != "reasoning_text":
            continue
        raw_text = content.get("text")
        text = raw_text if isinstance(raw_text, str) else ""
        if text:
            frames.append(
                _frame(
                    {
                        "type": "response.reasoning_text.delta",
                        "item_id": item_id,
                        "output_index": output_index,
                        "content_index": content_index,
                        "delta": text,
                        "sequence_number": next_seq(),
                    },
                    event_name="response.reasoning_text.delta",
                )
            )
        frames.append(
            _frame(
                {
                    "type": "response.reasoning_text.done",
                    "item_id": item_id,
                    "output_index": output_index,
                    "content_index": content_index,
                    "text": text,
                    "sequence_number": next_seq(),
                },
                event_name="response.reasoning_text.done",
            )
        )
    return frames


# ── Google: GenerateContentResponse → single SSE frame ─────────────────────


def _synthesize_google_sse(body: dict[str, Any]) -> bytes:
    """Wrap a buffered ``GenerateContentResponse`` JSON dict as one SSE frame.

    Standard ``generateContent`` and streaming ``streamGenerateContent`` emit
    structurally identical per-chunk payloads — both are
    ``GenerateContentResponse``. The intake's parser doesn't care whether
    there's one chunk or many. The intake also folds the cloudcode-pa
    ``{response: {...}}`` envelope unwrap, so passing either shape is safe.
    """
    return _frame(body)


# ── IR parts → listener-buffered JSON ──────────────────────────────────────


def _parts_to_openai_chat_completion(
    *,
    parts: list[ModelResponsePart],
    model: str,
    provider_response_id: str | None = None,
    finish_reason: FinishReason | None = None,
    usage: RequestUsage | None = None,
) -> dict[str, Any]:
    """Serialize IR parts into an OpenAI ``ChatCompletion`` JSON dict.

    One ``choice`` with a ``message`` carrying assembled text + tool_calls
    + finish_reason. When usage was captured off the upstream, it is projected
    into the OpenAI ``usage`` block; when absent it is omitted (never zeroed).
    """
    content_chunks: list[str] = []
    out_tool_calls: list[dict[str, Any]] = []
    for part in parts:
        if isinstance(part, TextPart):
            if part.content:
                content_chunks.append(part.content)
        elif isinstance(part, ToolCallPart):
            args = part.args
            args_str = args if isinstance(args, str) else json.dumps(args or {}, separators=(",", ":"))
            out_tool_calls.append(
                {
                    "id": part.tool_call_id,
                    "type": "function",
                    "function": {
                        "name": part.tool_name,
                        "arguments": args_str,
                    },
                }
            )

    content_str = "".join(content_chunks) if content_chunks else None
    resolved_finish = _finish_reason.to_openai_chat(finish_reason, tool_calls=bool(out_tool_calls))
    message: dict[str, Any] = {
        "role": "assistant",
        "content": content_str,
    }
    if out_tool_calls:
        message["tool_calls"] = out_tool_calls

    out: dict[str, Any] = {
        "id": provider_response_id or f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": resolved_finish,
                "logprobs": None,
            }
        ],
    }
    if not _usage.usage_is_empty(usage):
        out["usage"] = _usage.to_openai_chat(cast("RequestUsage", usage))
    return out


def _parts_to_openai_responses(
    *,
    parts: list[ModelResponsePart],
    model: str,
    provider_response_id: str | None = None,
    finish_reason: FinishReason | None = None,
    usage: RequestUsage | None = None,
) -> dict[str, Any]:
    """Serialize IR parts into an OpenAI ``/v1/responses`` buffered JSON dict.

    Produces the ``Response`` envelope: ``output[]`` is a list of
    items derived from the IR parts. :class:`TextPart` chunks coalesce
    into one ``message`` item with ``content=[{type: "output_text",
    text: ...}]``. :class:`ToolCallPart` becomes a ``function_call``
    item. :class:`ThinkingPart` becomes a ``reasoning`` item with its
    text under ``content=[{type: "reasoning_text", text: ...}]``.

    ``finish_reason`` is captured in the envelope's ``status``: ``"completed"``
    normally, ``"incomplete"`` (with ``incomplete_details.reason``) for a turn
    cut short by the token ceiling or a content filter, ``"failed"`` for one
    killed by an upstream error — mirroring the OpenAI Response spec and the
    streaming renderer's terminal event.
    """
    text_chunks: list[str] = []
    output_items: list[_WireObject] = []

    def flush_text() -> None:
        if text_chunks:
            output_items.append(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "".join(text_chunks)}],
                }
            )
            text_chunks.clear()

    for part in parts:
        if isinstance(part, TextPart):
            if part.content:
                text_chunks.append(part.content)
        elif isinstance(part, ToolCallPart):
            flush_text()
            args = part.args
            if isinstance(args, dict):
                args_str = json.dumps(args, separators=(",", ":"))
            elif isinstance(args, str):
                args_str = args
            else:
                args_str = json.dumps(args or {}, separators=(",", ":"))
            output_items.append(
                {
                    "type": "function_call",
                    "call_id": part.tool_call_id,
                    "name": part.tool_name,
                    "arguments": args_str,
                }
            )
        elif isinstance(part, ThinkingPart):
            flush_text()
            output_items.append(
                {
                    "type": "reasoning",
                    "summary": [],
                    "content": [{"type": "reasoning_text", "text": part.content or ""}],
                }
            )
    flush_text()

    usage_block = None if _usage.usage_is_empty(usage) else _usage.to_openai_responses(cast("RequestUsage", usage))
    out: dict[str, Any] = {
        "id": provider_response_id or f"resp_{uuid.uuid4().hex[:24]}",
        "object": "response",
        "created_at": int(time.time()),
        "model": model,
        "status": _finish_reason.to_openai_responses_status(finish_reason),
        "output": output_items,
        "usage": usage_block,
    }
    if (incomplete_reason := _finish_reason.to_openai_responses_incomplete_reason(finish_reason)) is not None:
        out["incomplete_details"] = {"reason": incomplete_reason}
    return out


def _parts_to_anthropic_message(
    *,
    parts: list[ModelResponsePart],
    model: str,
    provider_response_id: str | None = None,
    stop_reason: str,
    usage: RequestUsage | None = None,
) -> dict[str, Any]:
    """Serialize IR parts into an Anthropic ``BetaMessage`` JSON dict.

    ``stop_reason`` is the already-projected Anthropic wire value (see
    :func:`ccproxy.lightllm.graph._finish_reason.to_anthropic`), so the buffered
    body and the streaming ``message_delta`` agree for the same intake state.
    """
    blocks: list[dict[str, Any]] = []
    for part in parts:
        if isinstance(part, TextPart):
            if part.content:
                blocks.append({"type": "text", "text": part.content})
        elif isinstance(part, ThinkingPart):
            if part.id == "redacted_thinking":
                blocks.append({"type": "redacted_thinking", "data": part.signature or ""})
            else:
                blocks.append(
                    {
                        "type": "thinking",
                        "thinking": part.content or "",
                        "signature": part.signature or "",
                    }
                )
        elif isinstance(part, ToolCallPart):
            args = part.args
            input_obj = args if isinstance(args, dict) else (json.loads(args) if isinstance(args, str) and args else {})
            blocks.append(
                {
                    "type": "tool_use",
                    "id": part.tool_call_id,
                    "name": part.tool_name,
                    "input": input_obj,
                }
            )

    usage_block = (
        {"input_tokens": 0, "output_tokens": 0}
        if _usage.usage_is_empty(usage)
        else _usage.to_anthropic(cast("RequestUsage", usage))
    )
    return {
        "id": provider_response_id or f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "content": blocks,
        "model": model,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": usage_block,
    }


def render_parts_to_listener(
    *,
    parts: list[ModelResponsePart],
    inbound_format: InboundFormat,
    model: str,
    provider_response_id: str | None = None,
    finish_reason: FinishReason | None = None,
    usage: RequestUsage | None = None,
) -> bytes:
    """Serialize IR parts into the listener's buffered JSON bytes by inbound format.

    Shared by :func:`transform_buffered_response_sync` (the provider-buffered
    path) and the streaming :class:`~ccproxy.lightllm.graph.sse_pipeline.SSEPipeline`'s
    collect mode (force-streamed providers such as ``openai_conversations``
    whose client asked for a single buffered object). ``provider_response_id``
    is honored by the OpenAI Chat / Responses renderers. ``finish_reason``
    projects into every listener's native spelling — OpenAI Chat
    ``finish_reason``, Anthropic ``stop_reason``, Responses envelope ``status``
    — through the same helpers the streaming terminators use, so both paths
    answer identically for a given intake state. ``usage`` (captured off the
    upstream by the intake) is projected into each listener's native usage
    block; when absent it is omitted rather than zeroed.
    """
    if not parts:
        logger.warning(
            "buffered render: assembling a CONTENTLESS %s object — the intake produced "
            "no IR parts; the client receives an empty response",
            inbound_format,
        )
    if inbound_format is InboundFormat.OPENAI_CHAT:
        out_dict = _parts_to_openai_chat_completion(
            parts=parts,
            model=model,
            provider_response_id=provider_response_id,
            finish_reason=finish_reason,
            usage=usage,
        )
    elif inbound_format is InboundFormat.ANTHROPIC_MESSAGES:
        out_dict = _parts_to_anthropic_message(
            parts=parts,
            model=model,
            stop_reason=_finish_reason.to_anthropic(
                finish_reason,
                tool_calls=any(isinstance(part, ToolCallPart) for part in parts),
            ),
            usage=usage,
        )
    elif inbound_format is InboundFormat.OPENAI_RESPONSES:
        out_dict = _parts_to_openai_responses(
            parts=parts,
            model=model,
            provider_response_id=provider_response_id,
            finish_reason=finish_reason,
            usage=usage,
        )
    else:
        raise UnsupportedListenerError(f"no buffered renderer for inbound_format={inbound_format}")
    return json.dumps(out_dict, separators=(",", ":")).encode()


# ── Public sync entry point ────────────────────────────────────────────────


def transform_buffered_response_sync(
    *,
    raw_bytes: bytes,
    provider_type: str,
    inbound_format: InboundFormat,
    model: str,
    request_params: ModelRequestParameters,
) -> bytes:
    """Transform a buffered upstream response into listener-buffered JSON bytes.

    Provider routing:

    * Anthropic-compatible (anthropic / deepseek / zai) → parse
      ``BetaMessage`` JSON → synthesize SSE → feed Anthropic intake FSM.
    * OpenAI → parse ``ChatCompletion`` JSON → synthesize one
      ``ChatCompletionChunk`` SSE frame → feed OpenAI intake FSM.
    * OpenAI Responses → pass SSE through or synthesize one Responses stream
      from buffered ``Response`` JSON.
    * Google family (google / gemini / vertex_ai / vertex_ai_beta) → parse
      ``GenerateContentResponse`` JSON → wrap as one SSE frame → feed
      Google intake FSM (folds cloudcode-pa envelope unwrap internally).
    * Perplexity Pro → body is already concatenated SSE → feed directly.

    Output assembly: pull ``parts_manager.get_parts()`` from the intake
    after the synthetic SSE drains, then serialize those parts into the
    listener's buffered JSON shape (OpenAI ``ChatCompletion`` or Anthropic
    ``BetaMessage``).
    """
    if provider_type in _ANTHROPIC_COMPATIBLE:
        body = _parse_json_body(raw_bytes)
        synthetic_sse = _synthesize_anthropic_sse(body) if isinstance(body, dict) else b""
    elif provider_type == "openai":
        body = _parse_json_body(raw_bytes)
        synthetic_sse = _synthesize_openai_sse(body) if isinstance(body, dict) else b""
    elif provider_type == "openai_responses":
        if _looks_like_sse(raw_bytes):
            synthetic_sse = raw_bytes
        else:
            body = _parse_json_body(raw_bytes)
            synthetic_sse = _synthesize_openai_responses_sse(body) if isinstance(body, dict) else b""
    elif provider_type in _GOOGLE_COMPATIBLE:
        body = _parse_json_body(raw_bytes)
        synthetic_sse = _synthesize_google_sse(body) if isinstance(body, dict) else b""
    elif provider_type == "perplexity_pro":
        synthetic_sse = raw_bytes
    else:
        # openai_conversations is intentionally absent: it is always force-streamed
        # through SSEPipeline (the egress sidecar reconstructs inline OR WS-bridged
        # content), so it never reaches the buffered transform.
        raise UnsupportedUpstreamError(f"no buffered transform for provider_type={provider_type!r}")

    if not synthetic_sse:
        logger.warning(
            "buffered transform: provider_type=%s produced EMPTY synthetic SSE from %d "
            "upstream byte(s) — the buffered body was not parseable; client gets an empty response",
            provider_type,
            len(raw_bytes),
        )

    intake = dispatch_intake(
        provider_type=provider_type,
        model=model,
        request_params=request_params,
    )
    parts = _run_intake_one_shot(intake=intake, raw=synthetic_sse)

    return render_parts_to_listener(
        parts=parts,
        inbound_format=inbound_format,
        model=model,
        provider_response_id=intake.provider_response_id,
        finish_reason=intake.finish_reason,
        usage=intake.usage,
    )


# ── Helpers ────────────────────────────────────────────────────────────────


def _parse_json_body(raw_bytes: bytes) -> Any:
    if not raw_bytes:
        return {}
    try:
        return json.loads(raw_bytes)
    except (ValueError, TypeError):
        logger.debug("buffered transform: unparseable upstream body; treating as empty")
        return {}


def _looks_like_sse(raw_bytes: bytes) -> bool:
    stripped = raw_bytes.lstrip()
    return stripped.startswith(b"data:") or stripped.startswith(b"event:")


# ── Sync driver — one-shot asyncio loop ────────────────────────────────────


def _run_intake_one_shot(
    *,
    intake: AnyAsyncIntakeFSM,
    raw: bytes,
) -> list[ModelResponsePart]:
    """Drive ``intake.feed(raw)`` then ``intake.close()`` synchronously and
    return the final assembled parts list.

    Mirrors the worker-thread bridge used by :func:`dispatch_dump_sync` —
    a private asyncio loop on this thread if no loop is running, otherwise
    a worker thread that owns its own loop. One-shot per response, no
    persistent loop overhead.
    """

    async def _async() -> list[ModelResponsePart]:
        await intake.feed(raw)
        await intake.close()
        return list(intake.parts_manager.get_parts())

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_async())
        finally:
            loop.close()

    def _worker() -> list[ModelResponsePart]:
        worker_loop = asyncio.new_event_loop()
        try:
            return worker_loop.run_until_complete(_async())
        finally:
            worker_loop.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_worker).result()
