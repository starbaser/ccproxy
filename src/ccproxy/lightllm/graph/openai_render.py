"""IR events → OpenAI Chat Completion SSE wire bytes via pydantic-graph FSM.

Pydantic-graph FSM port of
:class:`ccproxy.lightllm.response.render_openai.OpenAIResponseRender`. One
graph run per :meth:`OpenAIResponseRenderFSM.render` call: the single
:class:`ModelResponseStreamEvent` is pushed onto an in-state queue, the FSM
router drains the queue dispatching the event to a per-variant handler step,
and a terminal step pulls the accumulated SSE bytes out of state.

The behavioral contract matches
:mod:`ccproxy.lightllm.response.render_openai` byte-for-byte: same chunk id
envelope (``chatcmpl-<24-hex>``), same lazy role chunk, same content / tool_call
delta dispatch, same IR-part-index → OpenAI-tool-call-index allocator, same
finish reason tracking, same ``[DONE]`` terminator.

OpenAI Chat Completion SSE is structurally simpler than Anthropic's: no per-
block lifecycle, no ``content_block_start``/``stop`` envelope. Each chunk is
a partial update to a single linear assistant message. :meth:`render` emits
one or two ``chat.completion.chunk`` frames per IR event (the role chunk
is emitted lazily, exactly once, before the first content chunk).

:meth:`close` is intentionally imperative — the terminator sequence (final
``finish_reason`` chunk + ``data: [DONE]\\n\\n``) is fixed and doesn't benefit
from FSM dispatch.

The persistent-loop bridge between sync mitmproxy callables and this async
FSM lives in :class:`SSEPipeline` (Phase Q). For tests, the parametrize
fixture in ``tests/test_lightllm_response_render_openai.py`` wraps the
async FSM in a one-loop-per-call sync adapter.
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


# ── Wire emission helpers (module-level — pure byte emitters) ──────────────


type _ToolCallArgs = str | Mapping[str, object] | None


def _args_to_str(args: _ToolCallArgs) -> str:
    """OpenAI Chat Completion wires tool-call arguments as a JSON string.

    pydantic-ai's IR holds either a string fragment (already-serialized
    JSON), a fully-formed dict, or ``None``. Normalize to the on-wire shape.
    """
    if args is None:
        return ""
    if isinstance(args, str):
        return args
    return json.dumps(args, separators=(",", ":"))


def _emit_chunk(
    *,
    chunk_id: str,
    created: int,
    model: str,
    delta: Mapping[str, object],
    finish_reason: str | None = None,
) -> bytes:
    chunk: dict[str, object] = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
                "logprobs": None,
            }
        ],
    }
    return f"data: {json.dumps(chunk, separators=(',', ':'))}\n\n".encode()


def _emit_usage_chunk(
    *,
    chunk_id: str,
    created: int,
    model: str,
    usage: RequestUsage,
) -> bytes:
    """A terminal ``chat.completion.chunk`` carrying usage with empty ``choices``.

    This is the OpenAI streaming usage-report shape (what ``stream_options.
    include_usage`` requests). ccproxy always emits it when usage was captured,
    so downstream billing never has to fall back to token estimation.
    """
    chunk: dict[str, object] = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [],
        "usage": _usage.to_openai_chat(usage),
    }
    return f"data: {json.dumps(chunk, separators=(',', ':'))}\n\n".encode()


# ── State ──────────────────────────────────────────────────────────────────


@dataclass
class _OpenAIRenderState(RenderState):
    """FSM state for one OpenAI render graph run.

    Shared queue/output/telemetry slots come from :class:`RenderState`. The
    remaining fields (``chunk_id``, ``created``, ``role_emitted``,
    ``part_to_tool_call_index``, ``next_tool_call_index``,
    ``tool_calls_rendered``) persist across render calls so the stream-level
    lifecycle stays consistent. ``current_ir_index`` is a transient scratch
    field written by the inner-subgraph open steps.
    """

    chunk_id: str
    created: int
    role_emitted: bool = False
    part_to_tool_call_index: dict[int, int] = field(default_factory=dict)
    next_tool_call_index: int = 0
    tool_calls_rendered: bool = False
    """True once a tool_call chunk reached the wire — the terminator's fallback
    finish reason when the upstream reported none (or a plain completion)."""
    current_ir_index: int = 0
    """Transient: the IR event index, stashed by the inner-subgraph open step."""


class _RenderDone:
    """Marker returned by the router when the events queue is exhausted."""


# ── Render helpers (operate on state) ──────────────────────────────────────


def _ensure_role(state: _OpenAIRenderState) -> None:
    """Emit the role chunk once, lazily, before any content chunk."""
    if state.role_emitted:
        return
    state.role_emitted = True
    state.out += _emit_chunk(
        chunk_id=state.chunk_id,
        created=state.created,
        model=state.model,
        delta={"role": "assistant"},
    )


# ── Graph ──────────────────────────────────────────────────────────────────


_g: GraphBuilder[_OpenAIRenderState, None, None, bytes] = GraphBuilder(
    name="openai_render",
    state_type=_OpenAIRenderState,
    output_type=bytes,
)


@_g.step
async def take_next_event(
    ctx: StepContext[_OpenAIRenderState, None, None],
) -> ModelResponseStreamEvent | _RenderDone:
    """Router source: pop the next event from the queue, or signal end via :class:`_RenderDone`."""
    if not ctx.state.pending_events:
        return _RenderDone()
    ctx.state.events_received += 1
    return ctx.state.pending_events.popleft()


# ── Inner subgraph: part_start dispatch ───────────────────────────────────
#
# ``handle_part_start`` previously used an isinstance ladder on ``event.part``.
# This subgraph replaces that: ``open_part_start`` stashes the event index and
# emits the role chunk, then returns ``event.part`` to the decision fan-out.

_psg: GraphBuilder[_OpenAIRenderState, None, PartStartEvent, None] = GraphBuilder(
    name="openai_render_part_start",
    state_type=_OpenAIRenderState,
    input_type=PartStartEvent,
)


@_psg.step
async def open_part_start(
    ctx: StepContext[_OpenAIRenderState, None, PartStartEvent],
) -> ModelResponsePart:
    """Stash the IR index, ensure the role chunk is emitted, return the part for dispatch."""
    state = ctx.state
    state.current_ir_index = ctx.inputs.index
    _ensure_role(state)
    return ctx.inputs.part


@_psg.step
async def handle_text_part_start(ctx: StepContext[_OpenAIRenderState, None, TextPart]) -> None:
    """``TextPart`` — emit an initial content chunk if the part arrived pre-populated."""
    state = ctx.state
    part = ctx.inputs
    if part.content:
        state.out += _emit_chunk(
            chunk_id=state.chunk_id,
            created=state.created,
            model=state.model,
            delta={"content": part.content},
        )


@_psg.step
async def handle_tool_call_part_start(ctx: StepContext[_OpenAIRenderState, None, ToolCallPart]) -> None:
    """``ToolCallPart`` — open a new tool_call slot and emit the envelope chunk."""
    state = ctx.state
    part = ctx.inputs
    tc_index = state.next_tool_call_index
    state.next_tool_call_index += 1
    state.part_to_tool_call_index[state.current_ir_index] = tc_index
    state.out += _emit_chunk(
        chunk_id=state.chunk_id,
        created=state.created,
        model=state.model,
        delta={
            "tool_calls": [
                {
                    "index": tc_index,
                    "id": part.tool_call_id,
                    "type": "function",
                    "function": {
                        "name": part.tool_name,
                        "arguments": _args_to_str(part.args),
                    },
                }
            ]
        },
    )
    state.tool_calls_rendered = True


@_psg.step
async def handle_unknown_part_start(ctx: StepContext[_OpenAIRenderState, None, object]) -> None:
    """ThinkingPart, CompactionPart, FilePart, NativeToolCall* etc. — no Chat Completion wire surface."""
    # The role chunk was already emitted by open_part_start; nothing more to do.
    del ctx  # protocol-required parameter; intentionally unused


_psg.add(
    _psg.edge_from(_psg.start_node).to(open_part_start),
    _psg.edge_from(open_part_start).to(
        _psg.decision()
        .branch(_psg.match(TextPart).to(handle_text_part_start))
        .branch(_psg.match(ToolCallPart).to(handle_tool_call_part_start))
        .branch(_psg.match(TypeExpression[object]).to(handle_unknown_part_start))
    ),
    _psg.edge_from(
        handle_text_part_start,
        handle_tool_call_part_start,
        handle_unknown_part_start,
    ).to(_psg.end_node),
)

_part_start_graph = _psg.build()
_dispatch_part_start = _g.add_subgraph(_part_start_graph, label="part_start")  # ty: ignore[unresolved-attribute]


# ── Inner subgraph: part_delta dispatch ───────────────────────────────────
#
# ``handle_part_delta`` previously used an isinstance ladder on ``event.delta``.
# This subgraph replaces that: ``open_part_delta`` stashes the event index then
# returns ``event.delta`` to the decision fan-out.

_pdg: GraphBuilder[_OpenAIRenderState, None, PartDeltaEvent, None] = GraphBuilder(
    name="openai_render_part_delta",
    state_type=_OpenAIRenderState,
    input_type=PartDeltaEvent,
)


@_pdg.step
async def open_part_delta(
    ctx: StepContext[_OpenAIRenderState, None, PartDeltaEvent],
) -> ModelResponsePartDelta:
    """Stash the IR index and return the delta for dispatch."""
    ctx.state.current_ir_index = ctx.inputs.index
    return ctx.inputs.delta


@_pdg.step
async def handle_text_part_delta(ctx: StepContext[_OpenAIRenderState, None, TextPartDelta]) -> None:
    """``TextPartDelta`` — emit a content chunk."""
    state = ctx.state
    _ensure_role(state)
    state.out += _emit_chunk(
        chunk_id=state.chunk_id,
        created=state.created,
        model=state.model,
        delta={"content": ctx.inputs.content_delta},
    )


@_pdg.step
async def handle_tool_call_part_delta(ctx: StepContext[_OpenAIRenderState, None, ToolCallPartDelta]) -> None:
    """``ToolCallPartDelta`` — emit args delta chunk; allocate a tool_call slot on first sighting."""
    state = ctx.state
    delta = ctx.inputs
    _ensure_role(state)
    tc_index = state.part_to_tool_call_index.get(state.current_ir_index)
    if tc_index is None:
        # First sighting of this IR part via a delta — allocate an
        # OpenAI tool-call slot and emit the envelope (id + name + type).
        tc_index = state.next_tool_call_index
        state.next_tool_call_index += 1
        state.part_to_tool_call_index[state.current_ir_index] = tc_index
        envelope: dict[str, object] = {"index": tc_index, "type": "function"}
        if delta.tool_call_id is not None:
            envelope["id"] = delta.tool_call_id
        fn: dict[str, object] = {}
        if delta.tool_name_delta is not None:
            fn["name"] = delta.tool_name_delta
        fn["arguments"] = _args_to_str(delta.args_delta)
        envelope["function"] = fn
        state.tool_calls_rendered = True
        state.out += _emit_chunk(
            chunk_id=state.chunk_id,
            created=state.created,
            model=state.model,
            delta={"tool_calls": [envelope]},
        )
        return

    state.tool_calls_rendered = True
    args_str = _args_to_str(delta.args_delta)
    state.out += _emit_chunk(
        chunk_id=state.chunk_id,
        created=state.created,
        model=state.model,
        delta={
            "tool_calls": [
                {
                    "index": tc_index,
                    "function": {"arguments": args_str},
                }
            ]
        },
    )


@_pdg.step
async def handle_thinking_part_delta(ctx: StepContext[_OpenAIRenderState, None, ThinkingPartDelta]) -> None:
    """``ThinkingPartDelta`` — OpenAI Chat Completion has no on-wire surface for thinking content."""
    del ctx  # protocol-required parameter; intentionally unused


@_pdg.step
async def handle_unknown_part_delta(ctx: StepContext[_OpenAIRenderState, None, object]) -> None:
    """Catch-all for delta types with no Chat Completion handler — log instead of silently dropping."""
    logger.debug("openai render: unhandled delta type %s; dropping", type(ctx.inputs).__name__)


_pdg.add(
    _pdg.edge_from(_pdg.start_node).to(open_part_delta),
    _pdg.edge_from(open_part_delta).to(
        _pdg.decision()
        .branch(_pdg.match(TextPartDelta).to(handle_text_part_delta))
        .branch(_pdg.match(ToolCallPartDelta).to(handle_tool_call_part_delta))
        .branch(_pdg.match(ThinkingPartDelta).to(handle_thinking_part_delta))
        .branch(_pdg.match(TypeExpression[object]).to(handle_unknown_part_delta))
    ),
    _pdg.edge_from(
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
    ctx: StepContext[_OpenAIRenderState, None, PartEndEvent],
) -> None:
    """No-op: OpenAI Chat Completion has no per-block stop marker."""
    del ctx  # protocol-required parameter; intentionally unused


@_g.step
async def handle_final_result(
    ctx: StepContext[_OpenAIRenderState, None, FinalResultEvent],
) -> None:
    """No-op: ``FinalResultEvent`` is an internal agent-loop signal with no OpenAI wire equivalent."""
    del ctx  # protocol-required parameter; intentionally unused


@_g.step
async def emit_done(
    ctx: StepContext[_OpenAIRenderState, None, _RenderDone],
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


class OpenAIResponseRenderFSM(ResponseRenderFSM[_OpenAIRenderState]):
    """Async pydantic-graph-driven OpenAI Chat Completion SSE renderer.

    Behavioral twin of
    :class:`ccproxy.lightllm.response.render_openai.OpenAIResponseRender`,
    re-expressed as a :mod:`pydantic_graph` ``GraphBuilder`` FSM. One
    graph run per :meth:`render` call drives a single
    :class:`ModelResponseStreamEvent` through the per-variant dispatch ladder
    and returns the emitted SSE bytes. :meth:`close` is imperative — the
    terminator sequence is fixed.
    """

    name = "openai_chat"
    _graph = _render_graph

    def _initial_state(self, *, model: str) -> _OpenAIRenderState:
        return _OpenAIRenderState(
            chunk_id=f"chatcmpl-{uuid.uuid4().hex[:24]}",
            created=int(time.time()),
            model=model,
        )

    async def close(
        self,
        *,
        usage: RequestUsage | None = None,
        raw_extras: Mapping[str, object] | None = None,
        finish_reason: FinishReason | None = None,
    ) -> bytes:
        """Emit the final ``finish_reason`` chunk, the usage chunk, then ``[DONE]``.

        Imperative (no FSM): the terminator sequence is fixed. The intake's
        captured ``finish_reason`` is projected onto the Chat Completions wire
        so a turn truncated by the token ceiling, stopped by a content filter,
        or killed by an upstream error is distinguishable from a clean one; a
        rendered tool call supplies the reason when the upstream reported none.
        When the intake captured usage, a terminal usage-only chunk (empty
        ``choices``) is emitted before ``[DONE]`` — the funnel re-stamping the
        token accounting the cross-format transform would otherwise drop. Emits
        a telemetry warning when IR events arrived but no content bytes were
        rendered — a silent empty OpenAI Chat response must be explainable from
        logs.
        """
        del raw_extras  # no OpenAI-chat wire slot for arbitrary upstream metadata
        state = self._state
        self._log_silent_close()
        out = bytearray()
        out += _emit_chunk(
            chunk_id=state.chunk_id,
            created=state.created,
            model=state.model,
            delta={},
            finish_reason=_finish_reason.to_openai_chat(finish_reason, tool_calls=state.tool_calls_rendered),
        )
        if not _usage.usage_is_empty(usage):
            out += _emit_usage_chunk(
                chunk_id=state.chunk_id,
                created=state.created,
                model=state.model,
                usage=cast("RequestUsage", usage),
            )
        out += b"data: [DONE]\n\n"
        return bytes(out)
