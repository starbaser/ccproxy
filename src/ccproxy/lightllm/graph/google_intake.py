"""Google ``streamGenerateContent`` SSE bytes → pydantic-ai IR events via FSM.

Pydantic-graph FSM port of
:class:`ccproxy.lightllm.response.intake_google.GoogleResponseIntake`. One
graph run per :meth:`GoogleResponseIntakeFSM.feed` call: bytes are appended
to the SSE buffer, complete SSE frames are drained, each frame's ``data:``
payload JSON is checked for the cloudcode-pa ``{response: {...}}`` envelope
and unwrapped if present, then validated into a typed
:class:`GenerateContentResponse`. Each chunk is wrapped in a dispatch
envelope, those envelopes are pushed onto an in-state queue, and the outer
FSM router drains the queue dispatching each envelope into a nested
per-chunk subgraph that pops one ``Part`` at a time and routes it through
the matching arm (text / function_call / inline_data / function_response).

The behavioral contract matches
:mod:`ccproxy.lightllm.response.intake_google` byte-for-byte for unwrapped
input: same SSE framing rules (``\\r\\n\\r\\n`` and ``\\n\\n`` separators),
same dispatch ladder (text → function_call → inline_data → function_response
warning), same multi-part-per-chunk handling, same close-tail-buffer drain.

The cloudcode-pa envelope unwrap (previously done by
:class:`ccproxy.hooks.gemini_envelope.EnvelopeUnwrapStream` on streaming
flows and :func:`ccproxy.hooks.gemini_envelope.unwrap_buffered` on buffered
flows) is folded into :meth:`_parse_event`: if the parsed JSON is a dict
with exactly one key ``"response"`` whose value is a dict, the inner dict
is taken as the chunk payload. Otherwise the JSON is treated as the chunk
payload directly. This makes the FSM-driven path the single source of
truth for Gemini response handling.

The per-chunk subgraph composes into the outer graph via
:meth:`GraphBuilder.add_subgraph` (installed by
:mod:`ccproxy.lightllm.graph._subgraph_patch`). Per-chunk scratch state
(``parts_queue``) is reset implicitly — the queue empties as
``pop_next_part`` drains it.

The persistent-loop bridge between sync mitmproxy callables and this async
FSM lives in :class:`SSEPipeline` (Phase Q). For tests, the parametrize
fixture in ``tests/test_lightllm_response_intake_google.py`` wraps the
async FSM in a one-loop-per-call sync adapter.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from google.genai.types import GenerateContentResponse, Part
from pydantic import TypeAdapter, ValidationError

# Private pydantic-ai imports — same justification as the matching note in
# ``response/intake_google.py``. We need byte-identical dispatch behavior
# and there is no public replacement.
from pydantic_ai._parts_manager import ModelResponsePartsManager
from pydantic_ai.messages import BinaryContent, FilePart, ModelResponseStreamEvent
from pydantic_graph import GraphBuilder, StepContext

import ccproxy.lightllm.graph._subgraph_patch  # noqa: F401  — installs add_subgraph
from ccproxy.lightllm.graph import _finish_reason, _usage
from ccproxy.lightllm.graph._base import IntakeState, ResponseIntakeFSM

if TYPE_CHECKING:
    from pydantic_ai.models import ModelRequestParameters

logger = logging.getLogger(__name__)


_RESPONSE_ADAPTER: TypeAdapter[GenerateContentResponse] = TypeAdapter(GenerateContentResponse)


# ── Dispatch envelopes ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class _GenerateChunk:
    """Chunk carrying one ``GenerateContentResponse`` to dispatch through the per-chunk subgraph."""

    chunk: GenerateContentResponse


@dataclass(frozen=True)
class _PartDispatch:
    """Per-part dispatch envelope routed into one of the four part-type arms."""

    part: Part


class _ChunkDone:
    """Sentinel — no more parts to process for the current chunk."""


class _FeedDone:
    """Marker returned by the outer router when the events queue is exhausted."""


type _PartDispatchRoute = _PartDispatch | _ChunkDone
type _OuterRoute = _GenerateChunk | _FeedDone


# ── State ──────────────────────────────────────────────────────────────────


@dataclass
class _GoogleIntakeState(IntakeState[_GenerateChunk]):
    """FSM state for one Google intake graph run.

    Shared queue/funnel/telemetry slots come from :class:`IntakeState`; usage
    accumulates off each chunk's ``usage_metadata``. ``parts_queue`` is
    per-chunk scratch drained inside the per-chunk subgraph.
    """

    parts_queue: deque[Part] = field(default_factory=deque)
    """Per-chunk queue of ``Part`` instances; drained by the per-chunk subgraph."""


# ── Per-chunk dispatch subgraph ─────────────────────────────────────────────


_cg: GraphBuilder[_GoogleIntakeState, None, _GenerateChunk, None] = GraphBuilder(
    name="google_chunk_dispatch",
    state_type=_GoogleIntakeState,
    input_type=_GenerateChunk,
)


@_cg.step
async def absorb_chunk(
    ctx: StepContext[_GoogleIntakeState, None, _GenerateChunk],
) -> None:
    """Walk ``chunk.candidates[0].content.parts`` and enqueue every ``Part``.

    Mirrors the front matter of the original ``handle_generate_chunk``:
    nothing happens when the chunk has no candidates or no parts. Otherwise
    every part on the first candidate's content is appended to
    ``state.parts_queue`` for the per-chunk loop to drain.
    """
    state = ctx.state
    chunk = ctx.inputs.chunk
    # Funnel: capture usage before the candidate/parts short-circuits — Gemini
    # often carries ``usage_metadata`` on a candidate-less terminal chunk, the
    # exact frame the parts walk skips. Values are cumulative, so replace.
    if chunk.usage_metadata is not None:
        state.usage = _usage.usage_from_google(chunk.usage_metadata)
    if not chunk.candidates:
        return
    candidate = chunk.candidates[0]
    # Funnel: the finish reason rides the candidate, and a turn cut short by
    # MAX_TOKENS or a safety filter often arrives on a candidate carrying no
    # content at all — capture it before the parts short-circuits, or a
    # truncated turn reaches the client indistinguishable from a complete one.
    if candidate.finish_reason is not None:
        state.raw_extras.setdefault("finish_reason", str(candidate.finish_reason.value))
        state.finish_reason = _finish_reason.from_google(candidate.finish_reason)
    if candidate.content is None or candidate.content.parts is None:
        return
    state.parts_queue.extend(candidate.content.parts)


@_cg.step
async def pop_next_part(
    ctx: StepContext[_GoogleIntakeState, None, None],
) -> _PartDispatchRoute:
    """Pop one ``Part`` from the queue, or signal end-of-chunk via :class:`_ChunkDone`."""
    state = ctx.state
    if not state.parts_queue:
        return _ChunkDone()
    return _PartDispatch(part=state.parts_queue.popleft())


# Per-arm dispatch envelopes emitted by :func:`classify_part`. Each wraps
# the same ``Part`` instance; the type discriminator routes through the
# decision branches to the matching handler step.


@dataclass(frozen=True)
class _TextPart:
    part: Part


@dataclass(frozen=True)
class _FunctionCallPart:
    part: Part


@dataclass(frozen=True)
class _InlineDataPart:
    part: Part


@dataclass(frozen=True)
class _FunctionResponsePart:
    part: Part


class _UnknownPart:
    """Sentinel — a Part with no populated field of interest (skipped silently)."""


type _PartRoute = _TextPart | _FunctionCallPart | _InlineDataPart | _FunctionResponsePart | _UnknownPart


@_cg.step
async def classify_part(
    ctx: StepContext[_GoogleIntakeState, None, _PartDispatch],
) -> _PartRoute:
    """Route one ``Part`` to the matching arm via its populated field.

    Preserves the original imperative ladder's order: ``text`` first,
    ``function_call`` second, ``inline_data`` third, ``function_response``
    last (logged + dropped).
    """
    part = ctx.inputs.part
    if part.text is not None:
        return _TextPart(part=part)
    if part.function_call is not None:
        return _FunctionCallPart(part=part)
    if part.inline_data is not None:
        return _InlineDataPart(part=part)
    if part.function_response is not None:
        return _FunctionResponsePart(part=part)
    logger.debug("google intake: unrecognized Part with no known field; skipping")
    return _UnknownPart()


@_cg.step
async def handle_text_typed(
    ctx: StepContext[_GoogleIntakeState, None, _TextPart],
) -> None:
    """Emit text-delta IR event for the typed text-part envelope."""
    state = ctx.state
    text = ctx.inputs.part.text
    if not text:
        return
    state.out_events.extend(state.parts_manager.handle_text_delta(vendor_part_id=None, content=text))


@_cg.step
async def handle_function_call_typed(
    ctx: StepContext[_GoogleIntakeState, None, _FunctionCallPart],
) -> None:
    """Emit tool-call-delta IR event for the typed function-call envelope."""
    state = ctx.state
    fc = ctx.inputs.part.function_call
    if fc is None:
        return
    event = state.parts_manager.handle_tool_call_delta(
        vendor_part_id=uuid4(),
        tool_name=fc.name,
        args=fc.args,
        tool_call_id=fc.id,
    )
    if event is not None:
        state.out_events.append(event)


@_cg.step
async def handle_inline_data_typed(
    ctx: StepContext[_GoogleIntakeState, None, _InlineDataPart],
) -> None:
    """Emit :class:`FilePart` IR event for the typed inline-data envelope."""
    state = ctx.state
    inline = ctx.inputs.part.inline_data
    if inline is None:
        return
    data = inline.data
    mime_type = inline.mime_type
    if not data or not mime_type:
        logger.debug("google intake: skipping inlineData part with missing data/mime_type")
        return
    binary = BinaryContent(data=data, media_type=mime_type)
    state.out_events.append(
        state.parts_manager.handle_part(
            vendor_part_id=uuid4(),
            part=FilePart(content=BinaryContent.narrow_type(binary)),
        )
    )


@_cg.step
async def handle_function_response_typed(
    ctx: StepContext[_GoogleIntakeState, None, _FunctionResponsePart],
) -> None:
    """Log and drop unexpected ``functionResponse`` parts."""
    del ctx  # StepFunction protocol requires ``ctx`` parameter name; nothing to read here
    logger.warning("google intake: unexpected functionResponse part in upstream response; skipping")


@_cg.step
async def handle_unknown_part(
    ctx: StepContext[_GoogleIntakeState, None, _UnknownPart],
) -> None:
    """No-op for parts with no recognized field. Reserved for future part kinds."""
    del ctx  # StepFunction protocol requires ``ctx`` parameter name; nothing to read here


_cg.add(
    _cg.edge_from(_cg.start_node).to(absorb_chunk),
    _cg.edge_from(absorb_chunk).to(pop_next_part),
    _cg.edge_from(pop_next_part).to(
        _cg.decision().branch(_cg.match(_ChunkDone).to(_cg.end_node)).branch(_cg.match(_PartDispatch).to(classify_part))
    ),
    _cg.edge_from(classify_part).to(
        _cg.decision()
        .branch(_cg.match(_TextPart).to(handle_text_typed))
        .branch(_cg.match(_FunctionCallPart).to(handle_function_call_typed))
        .branch(_cg.match(_InlineDataPart).to(handle_inline_data_typed))
        .branch(_cg.match(_FunctionResponsePart).to(handle_function_response_typed))
        .branch(_cg.match(_UnknownPart).to(handle_unknown_part))
    ),
    _cg.edge_from(
        handle_text_typed,
        handle_function_call_typed,
        handle_inline_data_typed,
        handle_function_response_typed,
        handle_unknown_part,
    ).to(pop_next_part),
)


_chunk_dispatch_graph = _cg.build()


# ── Outer intake graph (events queue dispatcher) ──────────────────────────


_g: GraphBuilder[_GoogleIntakeState, None, None, list[ModelResponseStreamEvent]] = GraphBuilder(
    name="google_intake",
    state_type=_GoogleIntakeState,
    output_type=list[ModelResponseStreamEvent],
)


@_g.step
async def frame_next_event(
    ctx: StepContext[_GoogleIntakeState, None, None],
) -> _OuterRoute:
    """Router source: pop the next dispatch envelope from the queue, or signal end via :class:`_FeedDone`."""
    state = ctx.state
    if not state.events_queue:
        return _FeedDone()
    return state.events_queue.popleft()


_dispatch_chunk_step = _g.add_subgraph(_chunk_dispatch_graph, label="dispatch_chunk")  # ty: ignore[unresolved-attribute]


@_g.step
async def emit_done(
    ctx: StepContext[_GoogleIntakeState, None, _FeedDone],
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
        .branch(_g.match(_GenerateChunk).to(_dispatch_chunk_step))
    ),
    _g.edge_from(_dispatch_chunk_step).to(frame_next_event),
    _g.edge_from(emit_done).to(_g.end_node),
)


_intake_graph = _g.build()


# ── Public class ───────────────────────────────────────────────────────────


class GoogleResponseIntakeFSM(ResponseIntakeFSM[_GoogleIntakeState]):
    """Async pydantic-graph-driven Google ``streamGenerateContent`` SSE intake.

    Behavioral twin of
    :class:`ccproxy.lightllm.response.intake_google.GoogleResponseIntake`,
    re-expressed as a two-level :class:`GraphBuilder` FSM: an outer graph
    drains the events queue and dispatches each chunk into a nested
    per-chunk subgraph that pops one ``Part`` at a time and routes it
    through the matching part-type arm. ``parts_manager`` persists across
    feed calls; per-chunk scratch (``parts_queue``) drains naturally.
    """

    name = "google"
    _graph = _intake_graph

    def _initial_state(self, *, model: str, request_params: ModelRequestParameters) -> _GoogleIntakeState:
        del model  # Google tracks no wire-updated model slug on state.
        return _GoogleIntakeState(
            parts_manager=ModelResponsePartsManager(model_request_parameters=request_params),
        )

    def _drain_events(self) -> Iterator[_GenerateChunk]:
        """Validate each complete SSE frame into a dispatch envelope."""
        for frame in self._split_sse_frames():
            envelope = self._parse_event(frame)
            if envelope is not None:
                yield envelope

    async def close(self) -> list[ModelResponseStreamEvent]:
        """Stream end. Drain any complete remaining event in the buffer.

        Some servers omit the trailing blank line on the last event; this
        catches them by treating the tail as a complete frame. Emits a
        telemetry warning when the stream carried chunks but produced no IR
        output — a silent empty Gemini turn must be explainable from logs.
        """
        out: list[ModelResponseStreamEvent] = []
        if self._sse_buffer:
            tail = bytes(self._sse_buffer)
            self._sse_buffer.clear()
            envelope = self._parse_event(tail)
            if envelope is not None:
                self._state.events_queue.append(envelope)
                self._state.frames_seen += 1
                out = await _intake_graph.run(state=self._state)

        self._log_no_ir_events(unit="chunk")
        return out

    def _parse_event(self, event: bytes) -> _GenerateChunk | None:
        """Parse a single SSE event into a ``_GenerateChunk``.

        Concatenates all ``data:`` lines into one JSON payload, peels off
        the cloudcode-pa ``{response: {...}}`` envelope if present, and
        validates the result into a typed ``GenerateContentResponse``.
        """
        payloads: list[bytes] = []
        for raw_line in event.split(b"\n"):
            line = raw_line.strip()
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if not payload:
                continue
            payloads.append(payload)
        if not payloads:
            return None
        raw = b"\n".join(payloads)
        try:
            parsed: object = json.loads(raw)
        except (ValueError, TypeError):
            self._state.frames_unparseable += 1
            logger.debug("google intake: skipping unparseable SSE event", exc_info=True)
            return None
        # cloudcode-pa wraps each chunk in {response: {...}}; standard Gemini
        # generateContent emits the chunk directly. Detect by checking for a
        # single ``response`` key wrapping a dict — anything else falls
        # through as the chunk itself.
        if isinstance(parsed, dict):
            parsed_dict = cast("dict[str, object]", parsed)
            response = parsed_dict.get("response")
            if len(parsed_dict) == 1 and isinstance(response, dict):
                parsed = response
        try:
            chunk = _RESPONSE_ADAPTER.validate_python(parsed)
        except ValidationError:
            self._state.frames_unparseable += 1
            logger.debug("google intake: skipping unparseable SSE event", exc_info=True)
            return None
        return _GenerateChunk(chunk=chunk)
