"""OpenAI Chat Completion SSE bytes → pydantic-ai IR events via FSM.

Pydantic-graph FSM port of
:class:`ccproxy.lightllm.response.intake_openai.OpenAIResponseIntake`. One
graph run per :meth:`OpenAIResponseIntakeFSM.feed` call: bytes are appended
to the SSE buffer, complete SSE frames are drained, the ``[DONE]`` sentinel
flips a terminator flag, surviving frames are validated into typed
:class:`ChatCompletionChunk` instances and wrapped in dispatch envelopes,
those envelopes are pushed onto an in-state queue, and the FSM router drains
the queue dispatching each envelope to a per-variant handler step. Handler
steps mutate ``state.parts_manager`` and append emitted
:class:`ModelResponseStreamEvent` objects to ``state.out_events``.

Unlike Anthropic's string-discriminated SSE union, OpenAI's wire is a single
``chat.completion.chunk`` envelope with optional fields on ``choices[0].delta``.
The intake wraps each post-validation chunk in one of three frozen
dispatch envelopes — ``_RefusalChunk`` (refusal short-circuits text), the
generic ``_StandardChunk`` (text + tool_calls), and ``_EmptyChoicesChunk``
(usage-only final chunks). The router routes by Python type, mirroring the
Anthropic FSM topology.

The ``_StandardChunk`` branch is handled by a per-chunk tool-calls subgraph:
the subgraph emits text content (in the same order as before — text first,
tool calls second), then drains ``choice.delta.tool_calls`` from a per-chunk
queue one at a time via a ``pop_next_tool_call`` loop. No imperative
``for``/``isinstance`` ladder remains hidden inside a handler step.

The behavioral contract matches
:mod:`ccproxy.lightllm.response.intake_openai` byte-for-byte: same SSE
framing rules, same ``[DONE]`` terminator, same dispatch ladder, same
``finish_reason`` mapping, same refusal handling, same multi-choice warning,
same provider-details collection.

The persistent-loop bridge between sync mitmproxy callables and this async
FSM lives in :class:`SSEPipeline` (Phase Q). For tests, the parametrize
fixture in ``tests/test_lightllm_response_intake_openai.py`` wraps the
async FSM in a one-loop-per-call sync adapter.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from openai.types.chat import ChatCompletionChunk
from openai.types.chat.chat_completion_chunk import Choice as _ChunkChoice
from openai.types.chat.chat_completion_chunk import ChoiceDeltaToolCall
from pydantic import TypeAdapter, ValidationError

# Private pydantic-ai imports — see the matching note in
# ``response/intake_openai.py``. We need byte-identical dispatch behavior
# and there is no public replacement.
from pydantic_ai._parts_manager import ModelResponsePartsManager
from pydantic_ai.messages import ModelResponseStreamEvent
from pydantic_graph import GraphBuilder, StepContext

import ccproxy.lightllm.graph._subgraph_patch  # noqa: F401  — installs GraphBuilder.add_subgraph
from ccproxy.lightllm.graph import _finish_reason, _usage
from ccproxy.lightllm.graph._base import IntakeState, ResponseIntakeFSM

if TYPE_CHECKING:
    from pydantic_ai.models import ModelRequestParameters

logger = logging.getLogger(__name__)


_CHUNK_ADAPTER: TypeAdapter[ChatCompletionChunk] = TypeAdapter(ChatCompletionChunk)


# ── Dispatch envelopes ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class _RefusalChunk:
    """Chunk where ``choices[0].delta.refusal`` is set — short-circuit text emission."""

    chunk: ChatCompletionChunk


@dataclass(frozen=True)
class _StandardChunk:
    """Chunk carrying a normal delta (text content or tool_calls or empty)."""

    chunk: ChatCompletionChunk


@dataclass(frozen=True)
class _EmptyChoicesChunk:
    """Usage-only chunk with ``choices == []`` — no IR emission, but provider id/model still update."""

    chunk: ChatCompletionChunk


class _FeedDone:
    """Marker returned by the router when the events queue is exhausted."""


type _QueueEvent = _RefusalChunk | _StandardChunk | _EmptyChoicesChunk
type _RoutedEvent = _QueueEvent | _FeedDone


# ── State ──────────────────────────────────────────────────────────────────


@dataclass
class _OpenAIIntakeState(IntakeState[_QueueEvent]):
    """FSM state for one OpenAI intake graph run.

    Shared queue/funnel/telemetry slots come from :class:`IntakeState`;
    usage reads off the terminal (``include_usage``) chunk. ``model`` and the
    refusal fields persist across feed calls so multi-feed reassembly works.
    ``tool_calls_queue`` is per-chunk scratch drained by the tool-calls
    subgraph.
    """

    model: str
    has_refusal: bool = False
    refusal_text: str = ""
    provider_details: dict[str, object] | None = None
    tool_calls_queue: deque[ChoiceDeltaToolCall] = field(default_factory=deque)
    """Per-chunk queue of tool-call deltas; drained by the tool-calls subgraph."""


# ── Helpers ────────────────────────────────────────────────────────────────


def _absorb_chunk_metadata(state: _OpenAIIntakeState, chunk: ChatCompletionChunk) -> None:
    """Update stream-level metadata (id, model, usage) from any chunk.

    OpenAI carries usage on the terminal empty-``choices`` chunk (emitted when
    ``stream_options.include_usage`` is set, which pydantic-ai's own OpenAI model
    always requests). Capture it into the funnel accumulator so a cross-format
    transform re-stamps the token accounting instead of dropping it.
    """
    if chunk.id:
        state.provider_response_id = chunk.id
    if chunk.model:
        state.model = chunk.model
    if chunk.usage is not None:
        state.usage = _usage.usage_from_openai_chat(chunk.usage)


def _map_provider_details(choice: _ChunkChoice) -> dict[str, object] | None:
    """Mirror of pydantic-ai's ``_map_provider_details`` for a single chunk choice.

    We don't carry logprobs across the wire boundary (they ride the
    chunks unmodified), so this only surfaces the raw ``finish_reason``.
    """
    details: dict[str, object] = {}
    if raw := choice.finish_reason:
        details["finish_reason"] = raw
    return details or None


# ── Inner subgraph: standard chunk tool-calls loop ─────────────────────────
#
# Receives a ``_StandardChunk``, emits text content (in the same order as the
# original imperative handler — text delta first, tool calls second), then
# drains ``choice.delta.tool_calls`` from ``state.tool_calls_queue`` one at a
# time. The loop exit is a ``_ToolCallsDone`` sentinel; no type-switch is needed
# because all tool-call deltas are handled identically.


class _ToolCallsDone:
    """Sentinel — no more tool-call deltas to process for the current chunk."""


_tcg: GraphBuilder[_OpenAIIntakeState, None, _StandardChunk, None] = GraphBuilder(
    name="openai_standard_chunk_dispatch",
    state_type=_OpenAIIntakeState,
    input_type=_StandardChunk,
)


@_tcg.step
async def open_standard_chunk(
    ctx: StepContext[_OpenAIIntakeState, None, _StandardChunk],
) -> None:
    """Absorb chunk metadata, emit text content, and enqueue tool-call deltas for the loop.

    This step does exactly what the original imperative ``handle_standard_chunk``
    did: metadata / finish_reason / provider_details first, then text delta
    (emitted before tool calls to preserve ordering), then the tool_calls list
    is appended to ``state.tool_calls_queue`` for ``pop_next_tool_call`` to drain.
    """
    state = ctx.state
    chunk = ctx.inputs.chunk
    _absorb_chunk_metadata(state, chunk)
    choice = chunk.choices[0]

    if (raw_finish_reason := choice.finish_reason) and not state.has_refusal:
        state.finish_reason = _finish_reason.from_openai_chat(raw_finish_reason)

    if provider_details := _map_provider_details(choice):
        if state.has_refusal:
            provider_details.pop("finish_reason", None)
        state.provider_details = {**(state.provider_details or {}), **provider_details}

    content = choice.delta.content
    if content:
        state.out_events.extend(
            state.parts_manager.handle_text_delta(
                vendor_part_id="content",
                content=content,
            )
        )

    if choice.delta.tool_calls:
        state.tool_calls_queue.extend(choice.delta.tool_calls)


@_tcg.step
async def pop_next_tool_call(
    ctx: StepContext[_OpenAIIntakeState, None, None],
) -> _ToolCallsDone | ChoiceDeltaToolCall:
    """Pop one tool-call delta from the queue, or signal end-of-chunk via :class:`_ToolCallsDone`."""
    state = ctx.state
    if not state.tool_calls_queue:
        return _ToolCallsDone()
    return state.tool_calls_queue.popleft()


@_tcg.step
async def handle_tool_call(
    ctx: StepContext[_OpenAIIntakeState, None, ChoiceDeltaToolCall],
) -> None:
    """Dispatch one tool-call delta to the parts manager."""
    state = ctx.state
    dtc = ctx.inputs
    fn = dtc.function
    tool_name = fn.name if fn is not None else None
    args = fn.arguments if fn is not None else None
    maybe_event = state.parts_manager.handle_tool_call_delta(
        vendor_part_id=dtc.index,
        tool_name=tool_name,
        args=args,
        tool_call_id=dtc.id,
    )
    if maybe_event is not None:
        state.out_events.append(maybe_event)


_tcg.add(
    _tcg.edge_from(_tcg.start_node).to(open_standard_chunk),
    _tcg.edge_from(open_standard_chunk).to(pop_next_tool_call),
    _tcg.edge_from(pop_next_tool_call).to(
        _tcg.decision()
        .branch(_tcg.match(_ToolCallsDone).to(_tcg.end_node))
        .branch(_tcg.match(ChoiceDeltaToolCall).to(handle_tool_call))
    ),
    _tcg.edge_from(handle_tool_call).to(pop_next_tool_call),
)

_tool_calls_graph = _tcg.build()


# ── Outer intake graph ─────────────────────────────────────────────────────


_g: GraphBuilder[_OpenAIIntakeState, None, None, list[ModelResponseStreamEvent]] = GraphBuilder(
    name="openai_intake",
    state_type=_OpenAIIntakeState,
    output_type=list[ModelResponseStreamEvent],
)


@_g.step
async def frame_next_event(
    ctx: StepContext[_OpenAIIntakeState, None, None],
) -> _RoutedEvent:
    """Router source: pop the next dispatch envelope from the queue, or signal end."""
    state = ctx.state
    if not state.events_queue:
        return _FeedDone()
    return state.events_queue.popleft()


_dispatch_standard_chunk = _g.add_subgraph(_tool_calls_graph, label="standard_chunk")  # ty: ignore[unresolved-attribute]


@_g.step
async def handle_empty_choices(
    ctx: StepContext[_OpenAIIntakeState, None, _EmptyChoicesChunk],
) -> None:
    """Usage-only chunks: absorb id/model, no IR event."""
    _absorb_chunk_metadata(ctx.state, ctx.inputs.chunk)


@_g.step
async def handle_refusal(
    ctx: StepContext[_OpenAIIntakeState, None, _RefusalChunk],
) -> None:
    """Refusal short-circuits text emission and stashes refusal text on state."""
    state = ctx.state
    chunk = ctx.inputs.chunk
    _absorb_chunk_metadata(state, chunk)
    choice = chunk.choices[0]
    # The dispatch wrapped this in ``_RefusalChunk`` only if delta.refusal was truthy.
    state.has_refusal = True
    state.finish_reason = "content_filter"
    state.refusal_text += choice.delta.refusal or ""


@_g.step
async def emit_done(
    ctx: StepContext[_OpenAIIntakeState, None, _FeedDone],
) -> list[ModelResponseStreamEvent]:
    """Terminal step — drain the accumulated IR events and reset for the next feed."""
    out = ctx.state.out_events
    ctx.state.emitted_events += len(out)
    ctx.state.out_events = []
    return out


_g.add(
    _g.edge_from(_g.start_node).to(frame_next_event),
    _g.edge_from(frame_next_event).to(
        _g.decision()
        .branch(_g.match(_FeedDone).to(emit_done))
        .branch(_g.match(_EmptyChoicesChunk).to(handle_empty_choices))
        .branch(_g.match(_RefusalChunk).to(handle_refusal))
        .branch(_g.match(_StandardChunk).to(_dispatch_standard_chunk))
    ),
    _g.edge_from(
        handle_empty_choices,
        handle_refusal,
        _dispatch_standard_chunk,
    ).to(frame_next_event),
    _g.edge_from(emit_done).to(_g.end_node),
)


_intake_graph = _g.build()


# ── Public class ───────────────────────────────────────────────────────────


class OpenAIResponseIntakeFSM(ResponseIntakeFSM[_OpenAIIntakeState]):
    """Async pydantic-graph-driven OpenAI Chat Completion SSE intake.

    Behavioral twin of
    :class:`ccproxy.lightllm.response.intake_openai.OpenAIResponseIntake`,
    re-expressed as a :mod:`pydantic_graph` ``GraphBuilder`` FSM. One
    graph run per :meth:`feed` call drains all complete SSE frames buffered
    by that call into typed OpenAI chunks, wraps each in a dispatch envelope,
    dispatches each to a handler step, and returns the accumulated IR events.
    Partial frames remain in the SSE buffer for the next call. ``parts_manager``
    and the stream-level metadata persist across calls.
    """

    name = "openai"
    _graph = _intake_graph

    def _initial_state(self, *, model: str, request_params: ModelRequestParameters) -> _OpenAIIntakeState:
        # Stream-level fields live on the FSM state but are surfaced under the
        # same private names the legacy intake exposes so tests reaching for
        # them work unchanged.
        return _OpenAIIntakeState(
            parts_manager=ModelResponsePartsManager(model_request_parameters=request_params),
            model=model,
        )

    @property
    def _model(self) -> str:
        """Legacy attribute name — tests inspect this directly."""
        return self._state.model

    @property
    def _has_refusal(self) -> bool:
        return self._state.has_refusal

    @property
    def _refusal_text(self) -> str:
        return self._state.refusal_text

    @property
    def provider_details(self) -> dict[str, object] | None:
        return self._state.provider_details

    async def close(self) -> list[ModelResponseStreamEvent]:
        """Stream end. Refusal text is stashed on ``provider_details`` per pydantic-ai.

        Emits a telemetry warning when the stream carried chunks but produced no
        IR output and no refusal — a silent empty OpenAI Chat turn must be
        explainable from logs.
        """
        s = self._state
        if s.refusal_text:
            s.provider_details = {
                **(s.provider_details or {}),
                "refusal": s.refusal_text,
            }
        if not s.has_refusal:
            self._log_no_ir_events(unit="chunk", extra=f" finish_reason={s.finish_reason}")
        return []

    def _drain_events(self) -> Iterator[_QueueEvent]:
        """Validate complete SSE frames into dispatch envelopes; flip ``_terminated`` on ``[DONE]``."""
        for frame in self._split_sse_frames():
            payload = _extract_data_payload(frame)
            if payload is None:
                continue
            if payload == b"[DONE]":
                self._terminated = True
                return
            try:
                chunk = _CHUNK_ADAPTER.validate_json(payload)
            except ValidationError:
                self._state.frames_unparseable += 1
                logger.debug("openai intake: skipping unparseable chunk: %r", payload)
                continue
            envelope = self._classify_chunk(chunk)
            if envelope is not None:
                yield envelope

    def _classify_chunk(self, chunk: ChatCompletionChunk) -> _QueueEvent | None:
        """Wrap a validated chunk in the matching dispatch envelope.

        Returns ``None`` to skip the chunk entirely (Azure-style ``delta=None`` defense).
        """
        if not chunk.choices:
            return _EmptyChoicesChunk(chunk=chunk)
        if len(chunk.choices) > 1:
            logger.warning(
                "openai intake: chunk has %d choices; only choices[0] is processed",
                len(chunk.choices),
            )
        choice = chunk.choices[0]
        if choice.delta.refusal:
            return _RefusalChunk(chunk=chunk)
        return _StandardChunk(chunk=chunk)


def _extract_data_payload(frame: bytes) -> bytes | None:
    """Return the payload of the first ``data:`` line in a frame, or ``None``."""
    for line in frame.split(b"\n"):
        stripped = line.strip()
        if stripped.startswith(b"data:"):
            return stripped[5:].strip() or None
    return None
