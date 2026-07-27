"""IR events → OpenAI Responses API SSE wire bytes via pydantic-graph FSM.

Listener-side render FSM for ``InboundFormat.OPENAI_RESPONSES``.
Consumes pydantic-ai :class:`ModelResponseStreamEvent` instances and
emits the OpenAI Responses streaming wire format — the per-item +
per-content-part lifecycle the Codex CLI expects.

The Responses streaming protocol is structurally richer than Chat
Completions. Each item in ``output[]`` brackets with
``response.output_item.added`` / ``response.output_item.done``;
message items further bracket their content parts with
``response.content_part.added`` / ``response.content_part.done``. Text
chunks stream via ``response.output_text.delta`` and conclude with
``response.output_text.done`` carrying the accumulated text. Function
calls stream their JSON arguments via
``response.function_call_arguments.delta``; reasoning items stream via
``response.reasoning_text.delta``. The stream prelude is a single
``response.created`` event with a Response envelope snapshot; the
postlude is the terminal envelope event for the turn's outcome —
``response.completed``, ``response.incomplete``, or ``response.failed`` —
carrying the final usage.

Mirrors :mod:`ccproxy.lightllm.graph.openai_render` in shape: state is
held across :meth:`render` calls, the graph dispatches one IR event
per run, and :meth:`close` emits the imperative terminator (no FSM
dispatch — the postlude is a fixed two-event sequence).

The 56-event upstream intake FSM lives separately in
``openai_responses_intake.py`` — this module is render-only.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from pydantic_ai.messages import (
    FinalResultEvent,
    ModelResponsePart,
    ModelResponsePartDelta,
    ModelResponseStreamEvent,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
    ToolCallPart,
    ToolCallPartDelta,
)
from pydantic_graph import GraphBuilder, StepContext, TypeExpression

import ccproxy.lightllm.graph._subgraph_patch  # noqa: F401  -- installs GraphBuilder.add_subgraph
from ccproxy.lightllm.graph import _finish_reason, _usage
from ccproxy.lightllm.graph._base import RenderState, ResponseRenderFSM

if TYPE_CHECKING:
    from pydantic_ai.messages import FinishReason
    from pydantic_ai.usage import RequestUsage

logger = logging.getLogger(__name__)


# ── Wire emission helpers ──────────────────────────────────────────────────


type _ToolCallArgs = str | Mapping[str, object] | None
type _WirePayload = Mapping[str, object]


def _args_to_str(args: _ToolCallArgs) -> str:
    """Coerce IR tool-call args (string fragment | dict | None) to a JSON string."""
    if args is None:
        return ""
    if isinstance(args, str):
        return args
    return json.dumps(args, separators=(",", ":"))


_TERMINAL_EVENT_BY_STATUS: dict[str, str] = {
    "completed": "response.completed",
    "incomplete": "response.incomplete",
    "failed": "response.failed",
}
"""The Responses stream's three terminal events, one per terminal envelope status."""


def _emit_event(event_name: str, payload: _WirePayload) -> bytes:
    """Encode one event as a Responses SSE frame.

    Responses uses the named-event SSE form
    (``event: <name>\\ndata: <json>\\n\\n``) — same convention as
    Anthropic, distinct from OpenAI Chat Completion's data-only form.
    """
    data = json.dumps(payload, separators=(",", ":"))
    return f"event: {event_name}\ndata: {data}\n\n".encode()


# ── State ──────────────────────────────────────────────────────────────────


@dataclass
class _OpenItemState:
    """Per-item state for an open output item (message / function_call / reasoning).

    ``output_index`` is the position in ``output[]``. ``content_index`` is
    the current content part within a message item (always 0 in this
    implementation — we don't open multiple content parts per message).
    ``text_buffer`` accumulates the streamed text for the ``.done``
    event payload; ``args_buffer`` does the same for function_call
    arguments.
    """

    item_type: str
    """``"message"`` / ``"function_call"`` / ``"reasoning"``."""

    item_id: str
    """The item id emitted on ``output_item.added``."""

    output_index: int
    """Position in the response's ``output[]`` array."""

    text_buffer: str = ""
    """Accumulated text (message: output_text; reasoning: reasoning_text)."""

    args_buffer: str = ""
    """Accumulated JSON argument string for function_call items."""

    tool_name: str = ""
    """Function tool name for function_call ``.done`` events."""

    tool_call_id: str = ""
    """Function call id for function_call ``output_item.done`` events."""

    content_part_opened: bool = False
    """True after ``response.content_part.added`` was emitted (message items only)."""


@dataclass
class _OpenAIResponsesRenderState(RenderState):
    """FSM state for one Responses render graph run.

    Shared queue/output/telemetry slots come from :class:`RenderState`. The
    remaining fields persist across :meth:`render` calls so the stream-level
    lifecycle (sequence_number monotonicity, item open/close state,
    response_id) stays consistent. ``current_ir_index`` is a transient
    scratch field written by the inner-subgraph open steps.
    """

    response_id: str
    """``resp_<24-hex>`` — stamped on every event's response envelope (and prelude)."""

    created_at: int
    """Unix seconds — stamped in the prelude snapshot."""

    sequence_number: int = 0
    """Monotonic per-event counter, reset to 0 on construction."""

    response_created_emitted: bool = False
    """Lazily emitted on the first :meth:`render` call so we know the model."""

    next_output_index: int = 0
    """Allocator for ``output_index`` on each new item."""

    part_to_output_index: dict[int, int] = field(default_factory=dict)
    """Map IR part index → output_index so deltas can address the right open item."""

    open_items: dict[int, _OpenItemState] = field(default_factory=dict)
    """Indexed by ``output_index`` so each delta/end can find its open item."""

    current_ir_index: int = 0
    """Transient: the IR event index, stashed by the inner-subgraph open step."""


class _RenderDone:
    """Marker returned by the router when the events queue is exhausted."""


class _NoDelta:
    """Marker returned by ``open_responses_delta`` when the delta type is unroutable."""


# ── Prelude helper ─────────────────────────────────────────────────────────


def _ensure_response_created(state: _OpenAIResponsesRenderState) -> None:
    """Emit ``response.created`` lazily before the first item event.

    The Responses prelude is a single ``response.created`` event
    carrying an in-progress envelope snapshot (id, object, model,
    status, empty output[], usage:None). Codex CLI expects this to
    arrive before any per-item events.
    """
    if state.response_created_emitted:
        return
    state.response_created_emitted = True

    snapshot = _response_envelope_snapshot(state, status="in_progress")
    state.out += _emit_event(
        "response.created",
        {
            "type": "response.created",
            "response": snapshot,
            "sequence_number": state.sequence_number,
        },
    )
    state.sequence_number += 1


def _response_envelope_snapshot(
    state: _OpenAIResponsesRenderState,
    *,
    status: str,
    usage: Mapping[str, object] | None = None,
    incomplete_reason: str | None = None,
) -> dict[str, object]:
    """Build the Response envelope snapshot stamped in prelude/postlude.

    ``incomplete_reason`` fills the spec's ``incomplete_details`` block, which
    is the only place a Responses client learns *why* a turn came back
    ``incomplete`` (``max_output_tokens`` vs ``content_filter``).
    """
    snapshot: dict[str, object] = {
        "id": state.response_id,
        "object": "response",
        "created_at": state.created_at,
        "status": status,
        "model": state.model,
        "output": [],
        "usage": usage,
    }
    if incomplete_reason is not None:
        snapshot["incomplete_details"] = {"reason": incomplete_reason}
    return snapshot


def _bump_seq(state: _OpenAIResponsesRenderState) -> int:
    """Allocate the next sequence_number and advance the counter."""
    seq = state.sequence_number
    state.sequence_number += 1
    return seq


# ── Item lifecycle helpers ─────────────────────────────────────────────────


def _open_message_item(
    state: _OpenAIResponsesRenderState,
    *,
    ir_index: int,
) -> _OpenItemState:
    """Emit ``response.output_item.added`` + ``response.content_part.added`` for a new message item.

    Codex's Codex-mode responses always carry assistant role for
    streamed text — we hardcode it here. If we ever need to render
    cross-format streams where the assistant emits as a different
    role, parametrize from the IR.
    """
    output_index = state.next_output_index
    state.next_output_index += 1
    item_id = f"msg_{uuid.uuid4().hex[:24]}"

    item = _OpenItemState(
        item_type="message",
        item_id=item_id,
        output_index=output_index,
    )
    state.open_items[output_index] = item
    state.part_to_output_index[ir_index] = output_index

    state.out += _emit_event(
        "response.output_item.added",
        {
            "type": "response.output_item.added",
            "output_index": output_index,
            "item": {
                "id": item_id,
                "type": "message",
                "status": "in_progress",
                "content": [],
                "role": "assistant",
            },
            "sequence_number": _bump_seq(state),
        },
    )
    state.out += _emit_event(
        "response.content_part.added",
        {
            "type": "response.content_part.added",
            "item_id": item_id,
            "output_index": output_index,
            "content_index": 0,
            "part": {
                "type": "output_text",
                "annotations": [],
                "logprobs": [],
                "text": "",
            },
            "sequence_number": _bump_seq(state),
        },
    )
    item.content_part_opened = True
    return item


def _open_function_call_item(
    state: _OpenAIResponsesRenderState,
    *,
    ir_index: int,
    part: ToolCallPart,
) -> _OpenItemState:
    """Emit ``response.output_item.added`` for a new function_call item."""
    output_index = state.next_output_index
    state.next_output_index += 1
    item_id = f"fc_{uuid.uuid4().hex[:24]}"

    item = _OpenItemState(
        item_type="function_call",
        item_id=item_id,
        output_index=output_index,
        tool_name=part.tool_name,
        tool_call_id=part.tool_call_id or "",
    )
    state.open_items[output_index] = item
    state.part_to_output_index[ir_index] = output_index

    state.out += _emit_event(
        "response.output_item.added",
        {
            "type": "response.output_item.added",
            "output_index": output_index,
            "item": {
                "id": item_id,
                "type": "function_call",
                "status": "in_progress",
                "call_id": part.tool_call_id,
                "name": part.tool_name,
                "arguments": "",
            },
            "sequence_number": _bump_seq(state),
        },
    )
    return item


def _open_reasoning_item(
    state: _OpenAIResponsesRenderState,
    *,
    ir_index: int,
) -> _OpenItemState:
    """Emit ``response.output_item.added`` for a new reasoning item."""
    output_index = state.next_output_index
    state.next_output_index += 1
    item_id = f"rs_{uuid.uuid4().hex[:24]}"

    item = _OpenItemState(
        item_type="reasoning",
        item_id=item_id,
        output_index=output_index,
    )
    state.open_items[output_index] = item
    state.part_to_output_index[ir_index] = output_index

    state.out += _emit_event(
        "response.output_item.added",
        {
            "type": "response.output_item.added",
            "output_index": output_index,
            "item": {
                "id": item_id,
                "type": "reasoning",
                "status": "in_progress",
                "summary": [],
                "content": [],
            },
            "sequence_number": _bump_seq(state),
        },
    )
    return item


def _close_item(
    state: _OpenAIResponsesRenderState,
    item: _OpenItemState,
    out: bytearray,
) -> None:
    """Emit the per-type ``.done`` events plus ``output_item.done`` for an open item.

    Writes into ``out`` rather than ``state.out`` so callers can direct
    output to an arbitrary buffer (e.g. the local accumulator in ``close()``
    rather than the shared FSM byte accumulator).
    """
    if item.item_type == "message":
        if item.content_part_opened:
            out += _emit_event(
                "response.output_text.done",
                {
                    "type": "response.output_text.done",
                    "item_id": item.item_id,
                    "output_index": item.output_index,
                    "content_index": 0,
                    "text": item.text_buffer,
                    "logprobs": [],
                    "sequence_number": _bump_seq(state),
                },
            )
            out += _emit_event(
                "response.content_part.done",
                {
                    "type": "response.content_part.done",
                    "item_id": item.item_id,
                    "output_index": item.output_index,
                    "content_index": 0,
                    "part": {
                        "type": "output_text",
                        "annotations": [],
                        "logprobs": [],
                        "text": item.text_buffer,
                    },
                    "sequence_number": _bump_seq(state),
                },
            )
        out += _emit_event(
            "response.output_item.done",
            {
                "type": "response.output_item.done",
                "output_index": item.output_index,
                "item": {
                    "id": item.item_id,
                    "type": "message",
                    "status": "completed",
                    "content": [
                        {
                            "type": "output_text",
                            "annotations": [],
                            "logprobs": [],
                            "text": item.text_buffer,
                        }
                    ],
                    "role": "assistant",
                },
                "sequence_number": _bump_seq(state),
            },
        )
    elif item.item_type == "function_call":
        out += _emit_event(
            "response.function_call_arguments.done",
            {
                "type": "response.function_call_arguments.done",
                "item_id": item.item_id,
                "output_index": item.output_index,
                "name": item.tool_name,
                "arguments": item.args_buffer,
                "sequence_number": _bump_seq(state),
            },
        )
        out += _emit_event(
            "response.output_item.done",
            {
                "type": "response.output_item.done",
                "output_index": item.output_index,
                "item": {
                    "id": item.item_id,
                    "type": "function_call",
                    "status": "completed",
                    "call_id": item.tool_call_id,
                    "name": item.tool_name,
                    "arguments": item.args_buffer,
                },
                "sequence_number": _bump_seq(state),
            },
        )
    elif item.item_type == "reasoning":
        out += _emit_event(
            "response.reasoning_text.done",
            {
                "type": "response.reasoning_text.done",
                "item_id": item.item_id,
                "output_index": item.output_index,
                "content_index": 0,
                "text": item.text_buffer,
                "sequence_number": _bump_seq(state),
            },
        )
        out += _emit_event(
            "response.output_item.done",
            {
                "type": "response.output_item.done",
                "output_index": item.output_index,
                "item": {
                    "id": item.item_id,
                    "type": "reasoning",
                    "status": "completed",
                    "summary": [],
                    "content": [
                        {
                            "type": "reasoning_text",
                            "text": item.text_buffer,
                        }
                    ],
                },
                "sequence_number": _bump_seq(state),
            },
        )


# ── Graph ──────────────────────────────────────────────────────────────────


_g: GraphBuilder[_OpenAIResponsesRenderState, None, None, bytes] = GraphBuilder(
    name="openai_responses_render",
    state_type=_OpenAIResponsesRenderState,
    output_type=bytes,
)


@_g.step
async def take_next_event(
    ctx: StepContext[_OpenAIResponsesRenderState, None, None],
) -> ModelResponseStreamEvent | _RenderDone:
    """Router source: pop the next event from the queue, or signal end via :class:`_RenderDone`."""
    if not ctx.state.pending_events:
        return _RenderDone()
    ctx.state.events_received += 1
    return ctx.state.pending_events.popleft()


# ── Inner subgraph: part_start dispatch ───────────────────────────────────
#
# ``handle_part_start`` previously used an isinstance ladder on ``event.part``.
# This subgraph replaces that: ``open_responses_part`` stashes the event index,
# emits the prelude if needed, then returns ``event.part`` to the decision fan-out.

_psg: GraphBuilder[_OpenAIResponsesRenderState, None, PartStartEvent, None] = GraphBuilder(
    name="openai_responses_render_part_start",
    state_type=_OpenAIResponsesRenderState,
    input_type=PartStartEvent,
)


@_psg.step
async def open_responses_part(
    ctx: StepContext[_OpenAIResponsesRenderState, None, PartStartEvent],
) -> ModelResponsePart:
    """Stash IR index, emit prelude if needed, return part for dispatch."""
    state = ctx.state
    state.current_ir_index = ctx.inputs.index
    _ensure_response_created(state)
    return ctx.inputs.part


@_psg.step
async def handle_text_part_start(ctx: StepContext[_OpenAIResponsesRenderState, None, TextPart]) -> None:
    """``TextPart`` — open a message item and emit any initial text delta."""
    state = ctx.state
    part = ctx.inputs
    item = _open_message_item(state, ir_index=state.current_ir_index)
    if part.content:
        item.text_buffer += part.content
        state.out += _emit_event(
            "response.output_text.delta",
            {
                "type": "response.output_text.delta",
                "item_id": item.item_id,
                "output_index": item.output_index,
                "content_index": 0,
                "delta": part.content,
                "logprobs": [],
                "sequence_number": _bump_seq(state),
            },
        )


@_psg.step
async def handle_tool_call_part_start(ctx: StepContext[_OpenAIResponsesRenderState, None, ToolCallPart]) -> None:
    """``ToolCallPart`` — open a function_call item and emit any initial args delta."""
    state = ctx.state
    part = ctx.inputs
    item = _open_function_call_item(state, ir_index=state.current_ir_index, part=part)
    args_str = _args_to_str(part.args)
    if args_str:
        item.args_buffer += args_str
        state.out += _emit_event(
            "response.function_call_arguments.delta",
            {
                "type": "response.function_call_arguments.delta",
                "item_id": item.item_id,
                "output_index": item.output_index,
                "delta": args_str,
                "sequence_number": _bump_seq(state),
            },
        )


@_psg.step
async def handle_thinking_part_start(ctx: StepContext[_OpenAIResponsesRenderState, None, ThinkingPart]) -> None:
    """``ThinkingPart`` — open a reasoning item and emit any initial reasoning text delta."""
    state = ctx.state
    part = ctx.inputs
    item = _open_reasoning_item(state, ir_index=state.current_ir_index)
    if part.content:
        item.text_buffer += part.content
        state.out += _emit_event(
            "response.reasoning_text.delta",
            {
                "type": "response.reasoning_text.delta",
                "item_id": item.item_id,
                "output_index": item.output_index,
                "content_index": 0,
                "delta": part.content,
                "sequence_number": _bump_seq(state),
            },
        )


@_psg.step
async def handle_unknown_part_start(ctx: StepContext[_OpenAIResponsesRenderState, None, object]) -> None:
    """Other part kinds (NativeToolCall*, CompactionPart, FilePart) — no Responses wire surface."""
    del ctx  # protocol-required parameter; intentionally unused


_psg.add(
    _psg.edge_from(_psg.start_node).to(open_responses_part),
    _psg.edge_from(open_responses_part).to(
        _psg.decision()
        .branch(_psg.match(TextPart).to(handle_text_part_start))
        .branch(_psg.match(ToolCallPart).to(handle_tool_call_part_start))
        .branch(_psg.match(ThinkingPart).to(handle_thinking_part_start))
        .branch(_psg.match(TypeExpression[object]).to(handle_unknown_part_start))
    ),
    _psg.edge_from(
        handle_text_part_start,
        handle_tool_call_part_start,
        handle_thinking_part_start,
        handle_unknown_part_start,
    ).to(_psg.end_node),
)

_part_start_graph = _psg.build()
_dispatch_part_start = _g.add_subgraph(_part_start_graph, label="part_start")  # ty: ignore[unresolved-attribute]


# ── Inner subgraph: part_delta dispatch ───────────────────────────────────
#
# ``handle_part_delta`` previously used an isinstance ladder on ``event.delta``
# with a lazy-open guard before the dispatch. This subgraph keeps the lazy-open
# guard in ``open_responses_delta`` (which resolves the open item and stashes it
# on state) and fans out to per-delta-type handlers.
#
# The lazy-open path returns None when the delta type is unknown and no item can
# be opened; the ``handle_delta_no_item`` leaf is the terminal for that case.


@dataclass
class _ResolvedDelta:
    """Carrier for the resolved open item + the typed delta."""

    item: _OpenItemState
    delta: ModelResponsePartDelta


_pdg: GraphBuilder[_OpenAIResponsesRenderState, None, PartDeltaEvent, None] = GraphBuilder(
    name="openai_responses_render_part_delta",
    state_type=_OpenAIResponsesRenderState,
    input_type=PartDeltaEvent,
)


@_pdg.step
async def open_responses_delta(
    ctx: StepContext[_OpenAIResponsesRenderState, None, PartDeltaEvent],
) -> _ResolvedDelta | _NoDelta:
    """Stash IR index, resolve (or lazily open) the target item, return carrier for dispatch.

    Returns :class:`_NoDelta` when the delta type is unroutable (no item to open
    and no existing item to target), signalling the ``handle_delta_no_item`` terminal.
    """
    event = ctx.inputs
    state = ctx.state
    state.current_ir_index = event.index
    delta = event.delta

    output_index = state.part_to_output_index.get(event.index)
    if output_index is None:
        # PartDelta arrived before PartStart — open a matching item lazily.
        _ensure_response_created(state)
        if isinstance(delta, TextPartDelta):
            item = _open_message_item(state, ir_index=event.index)
        elif isinstance(delta, ToolCallPartDelta):
            synthetic = ToolCallPart(
                tool_name=delta.tool_name_delta or "",
                args=delta.args_delta if isinstance(delta.args_delta, str | dict) else None,
                tool_call_id=delta.tool_call_id or "",
            )
            item = _open_function_call_item(state, ir_index=event.index, part=synthetic)
        elif isinstance(delta, ThinkingPartDelta):
            item = _open_reasoning_item(state, ir_index=event.index)
        else:
            return _NoDelta()
        output_index = item.output_index

    return _ResolvedDelta(item=state.open_items[output_index], delta=delta)


@_pdg.step
async def split_resolved_delta(
    ctx: StepContext[_OpenAIResponsesRenderState, None, _ResolvedDelta],
) -> ModelResponsePartDelta:
    """Unwrap the carrier, returning the delta for the inner type-switch decision."""
    return ctx.inputs.delta


@_pdg.step
async def handle_text_part_delta(ctx: StepContext[_OpenAIResponsesRenderState, None, TextPartDelta]) -> None:
    """``TextPartDelta`` — emit a ``response.output_text.delta`` event."""
    state = ctx.state
    delta = ctx.inputs
    output_index = state.part_to_output_index[state.current_ir_index]
    item = state.open_items[output_index]
    if delta.content_delta:
        item.text_buffer += delta.content_delta
        state.out += _emit_event(
            "response.output_text.delta",
            {
                "type": "response.output_text.delta",
                "item_id": item.item_id,
                "output_index": item.output_index,
                "content_index": 0,
                "delta": delta.content_delta,
                "logprobs": [],
                "sequence_number": _bump_seq(state),
            },
        )


@_pdg.step
async def handle_tool_call_part_delta(ctx: StepContext[_OpenAIResponsesRenderState, None, ToolCallPartDelta]) -> None:
    """``ToolCallPartDelta`` — emit a ``response.function_call_arguments.delta`` event."""
    state = ctx.state
    delta = ctx.inputs
    output_index = state.part_to_output_index[state.current_ir_index]
    item = state.open_items[output_index]
    args_str = _args_to_str(delta.args_delta)
    if args_str:
        item.args_buffer += args_str
        state.out += _emit_event(
            "response.function_call_arguments.delta",
            {
                "type": "response.function_call_arguments.delta",
                "item_id": item.item_id,
                "output_index": item.output_index,
                "delta": args_str,
                "sequence_number": _bump_seq(state),
            },
        )


@_pdg.step
async def handle_thinking_part_delta(ctx: StepContext[_OpenAIResponsesRenderState, None, ThinkingPartDelta]) -> None:
    """``ThinkingPartDelta`` — emit a ``response.reasoning_text.delta`` event."""
    state = ctx.state
    delta = ctx.inputs
    output_index = state.part_to_output_index[state.current_ir_index]
    item = state.open_items[output_index]
    text_delta = delta.content_delta
    if text_delta:
        item.text_buffer += text_delta
        state.out += _emit_event(
            "response.reasoning_text.delta",
            {
                "type": "response.reasoning_text.delta",
                "item_id": item.item_id,
                "output_index": item.output_index,
                "content_index": 0,
                "delta": text_delta,
                "sequence_number": _bump_seq(state),
            },
        )


@_pdg.step
async def handle_delta_no_item(ctx: StepContext[_OpenAIResponsesRenderState, None, _NoDelta]) -> None:
    """Terminal for unroutable deltas that arrived with no resolvable open item."""
    del ctx  # protocol-required parameter; intentionally unused


@_pdg.step
async def handle_unknown_part_delta(ctx: StepContext[_OpenAIResponsesRenderState, None, object]) -> None:
    """Catch-all for delta types with no Responses handler — log instead of silently dropping."""
    logger.debug(
        "openai_responses render: unhandled delta type %s; dropping",
        type(ctx.inputs).__name__,
    )


_pdg.add(
    _pdg.edge_from(_pdg.start_node).to(open_responses_delta),
    _pdg.edge_from(open_responses_delta).to(
        _pdg.decision()
        .branch(_pdg.match(_NoDelta).to(handle_delta_no_item))
        .branch(_pdg.match(_ResolvedDelta).to(split_resolved_delta))
    ),
    _pdg.edge_from(split_resolved_delta).to(
        _pdg.decision()
        .branch(_pdg.match(TextPartDelta).to(handle_text_part_delta))
        .branch(_pdg.match(ToolCallPartDelta).to(handle_tool_call_part_delta))
        .branch(_pdg.match(ThinkingPartDelta).to(handle_thinking_part_delta))
        .branch(_pdg.match(TypeExpression[object]).to(handle_unknown_part_delta))
    ),
    _pdg.edge_from(
        handle_delta_no_item,
        handle_text_part_delta,
        handle_tool_call_part_delta,
        handle_thinking_part_delta,
        handle_unknown_part_delta,
    ).to(_pdg.end_node),
)

_part_delta_graph = _pdg.build()
_dispatch_part_delta = _g.add_subgraph(_part_delta_graph, label="part_delta")  # ty: ignore[unresolved-attribute]


@_g.step
async def handle_part_end(
    ctx: StepContext[_OpenAIResponsesRenderState, None, PartEndEvent],
) -> None:
    """Close the matching open item — emit its per-type ``.done`` plus ``output_item.done``."""
    event = ctx.inputs
    state = ctx.state
    output_index = state.part_to_output_index.get(event.index)
    if output_index is None:
        return
    item = state.open_items.pop(output_index, None)
    if item is None:
        return
    _close_item(state, item, state.out)


@_g.step
async def handle_final_result(
    ctx: StepContext[_OpenAIResponsesRenderState, None, FinalResultEvent],
) -> None:
    """No-op: ``FinalResultEvent`` is an internal agent-loop signal with no Responses wire equivalent."""
    del ctx


@_g.step
async def emit_done(
    ctx: StepContext[_OpenAIResponsesRenderState, None, _RenderDone],
) -> bytes:
    """Terminal step — drain the accumulated wire bytes and reset for the next render call."""
    out = bytes(ctx.state.out)
    ctx.state.bytes_emitted += len(out)
    ctx.state.out = bytearray()
    return out


_g.add(
    _g.edge_from(_g.start_node).to(take_next_event),
    _g.edge_from(take_next_event).to(
        _g.decision()
        .branch(_g.match(_RenderDone).to(emit_done))
        .branch(_g.match(PartStartEvent).to(_dispatch_part_start))
        .branch(_g.match(PartDeltaEvent).to(_dispatch_part_delta))
        .branch(_g.match(PartEndEvent).to(handle_part_end))
        .branch(_g.match(FinalResultEvent).to(handle_final_result))
    ),
    _g.edge_from(
        _dispatch_part_start,
        _dispatch_part_delta,
        handle_part_end,
        handle_final_result,
    ).to(take_next_event),
    _g.edge_from(emit_done).to(_g.end_node),
)


_render_graph = _g.build()


# ── Public class ───────────────────────────────────────────────────────────


class OpenAIResponsesRenderFSM(ResponseRenderFSM[_OpenAIResponsesRenderState]):
    """Async pydantic-graph-driven OpenAI Responses SSE renderer.

    One :meth:`render` call dispatches one
    :class:`ModelResponseStreamEvent` through the FSM and returns the
    emitted SSE bytes. :meth:`close` is imperative — it closes any
    still-open items, then emits the fixed ``response.completed``
    terminator.
    """

    name = "openai_responses"
    _graph = _render_graph

    def _initial_state(self, *, model: str) -> _OpenAIResponsesRenderState:
        return _OpenAIResponsesRenderState(
            response_id=f"resp_{uuid.uuid4().hex[:24]}",
            created_at=int(time.time()),
            model=model,
        )

    async def close(
        self,
        *,
        usage: RequestUsage | None = None,
        raw_extras: Mapping[str, object] | None = None,
        finish_reason: FinishReason | None = None,
    ) -> bytes:
        """Close any still-open items, then emit the terminal envelope event.

        The intake's captured ``finish_reason`` selects that terminal event:
        ``response.completed`` for a turn that finished, ``response.incomplete``
        (with ``incomplete_details.reason``) for one cut short by the token
        ceiling or a content filter, ``response.failed`` for one killed by an
        upstream error. A truncated turn announced as ``response.completed`` is
        indistinguishable from a clean one, which is exactly the signal a
        Responses client needs. When the intake captured usage, the terminal
        event carries the real token counts (funnel re-stamping) rather than
        zeros. Emits a telemetry warning when IR events arrived but no content
        bytes were rendered — a silent empty Responses turn must be explainable
        from logs.
        """
        del raw_extras  # response envelope has no slot for arbitrary upstream metadata
        state = self._state
        self._log_silent_close()
        out = bytearray()

        # Drain any items left open (the upstream FSM may not have emitted
        # PartEndEvent for every open part if the stream cut short).
        for output_index in sorted(state.open_items.keys()):
            item = state.open_items.pop(output_index)
            _close_item(state, item, out)

        # Postlude — the terminal envelope event for the resolved status.
        usage_block = (
            {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
            if _usage.usage_is_empty(usage)
            else _usage.to_openai_responses(cast("RequestUsage", usage))
        )
        status = _finish_reason.to_openai_responses_status(finish_reason)
        snapshot = _response_envelope_snapshot(
            state,
            status=status,
            usage=usage_block,
            incomplete_reason=_finish_reason.to_openai_responses_incomplete_reason(finish_reason),
        )
        event_name = _TERMINAL_EVENT_BY_STATUS[status]
        out += _emit_event(
            event_name,
            {
                "type": event_name,
                "response": snapshot,
                "sequence_number": _bump_seq(state),
            },
        )
        return bytes(out)
