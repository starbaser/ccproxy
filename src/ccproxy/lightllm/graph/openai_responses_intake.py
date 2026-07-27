"""OpenAI Responses SSE bytes -> pydantic-ai IR events via FSM."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from openai.types import responses
from pydantic import TypeAdapter, ValidationError
from pydantic_ai._parts_manager import ModelResponsePartsManager
from pydantic_ai.messages import ModelResponseStreamEvent
from pydantic_graph import GraphBuilder, StepContext, TypeExpression

import ccproxy.lightllm.graph._subgraph_patch  # noqa: F401  — installs GraphBuilder.add_subgraph
from ccproxy.lightllm.graph import _finish_reason, _usage
from ccproxy.lightllm.graph._base import IntakeState, ResponseIntakeFSM

if TYPE_CHECKING:
    from pydantic_ai.models import ModelRequestParameters

logger = logging.getLogger(__name__)


_EVENT_ADAPTER: TypeAdapter[responses.ResponseStreamEvent] = TypeAdapter(responses.ResponseStreamEvent)

type _ResponseEnvelopeWireEvent = (
    responses.ResponseCreatedEvent
    | responses.ResponseQueuedEvent
    | responses.ResponseInProgressEvent
    | responses.ResponseCompletedEvent
    | responses.ResponseFailedEvent
    | responses.ResponseIncompleteEvent
)


@dataclass(frozen=True)
class _ResponseEnvelopeEvent:
    event: _ResponseEnvelopeWireEvent


@dataclass(frozen=True)
class _OutputItemAddedEvent:
    event: responses.ResponseOutputItemAddedEvent


@dataclass(frozen=True)
class _OutputItemDoneEvent:
    event: responses.ResponseOutputItemDoneEvent


@dataclass(frozen=True)
class _TextDeltaEvent:
    event: responses.ResponseTextDeltaEvent


@dataclass(frozen=True)
class _TextDoneEvent:
    event: responses.ResponseTextDoneEvent


@dataclass(frozen=True)
class _FunctionArgumentsDeltaEvent:
    event: responses.ResponseFunctionCallArgumentsDeltaEvent


@dataclass(frozen=True)
class _FunctionArgumentsDoneEvent:
    event: responses.ResponseFunctionCallArgumentsDoneEvent


@dataclass(frozen=True)
class _ReasoningSummaryPartAddedEvent:
    event: responses.ResponseReasoningSummaryPartAddedEvent


@dataclass(frozen=True)
class _ReasoningSummaryTextDeltaEvent:
    event: responses.ResponseReasoningSummaryTextDeltaEvent


@dataclass(frozen=True)
class _ReasoningTextDeltaEvent:
    event: responses.ResponseReasoningTextDeltaEvent


@dataclass(frozen=True)
class _RefusalDeltaEvent:
    event: responses.ResponseRefusalDeltaEvent


@dataclass(frozen=True)
class _RefusalDoneEvent:
    event: responses.ResponseRefusalDoneEvent


@dataclass(frozen=True)
class _NoOpEvent:
    event_type: str


class _FeedDone:
    """Marker returned by the router when the events queue is exhausted."""


type _QueueEvent = (
    _ResponseEnvelopeEvent
    | _OutputItemAddedEvent
    | _OutputItemDoneEvent
    | _TextDeltaEvent
    | _TextDoneEvent
    | _FunctionArgumentsDeltaEvent
    | _FunctionArgumentsDoneEvent
    | _ReasoningSummaryPartAddedEvent
    | _ReasoningSummaryTextDeltaEvent
    | _ReasoningTextDeltaEvent
    | _RefusalDeltaEvent
    | _RefusalDoneEvent
    | _NoOpEvent
)
type _RoutedEvent = _QueueEvent | _FeedDone


# ── Item-added discriminant envelopes ───────────────────────────────────────
#
# ``ResponseToolSearchCall`` is one class with an ``execution: Literal["server", "client"]``
# field, so it can't be split by class-level matching alone. These frozen envelopes let the
# subgraph decision route on them as distinct Python types.


@dataclass(frozen=True)
class _ClientToolSearchAdded:
    """``output_item.added`` for a client-execution tool-search call."""

    item: responses.ResponseToolSearchCall


@dataclass(frozen=True)
class _ItemAddedNoOp:
    """``output_item.added`` for items that produce no IR output (server tool-search, unknown)."""

    item_type: str


type _ItemAddedDiscriminand = (
    responses.ResponseFunctionToolCall | responses.ResponseOutputMessage | _ClientToolSearchAdded | _ItemAddedNoOp
)


# ── Item-done discriminant envelopes ────────────────────────────────────────


@dataclass(frozen=True)
class _ClientToolSearchDone:
    """``output_item.done`` for a client-execution tool-search call."""

    item: responses.ResponseToolSearchCall


@dataclass(frozen=True)
class _ItemDoneNoOp:
    """``output_item.done`` for items that produce no IR output (server tool-search, unknown)."""

    item_type: str


type _ItemDoneDiscriminand = responses.ResponseReasoningItem | _ClientToolSearchDone | _ItemDoneNoOp


@dataclass
class _OpenAIResponsesIntakeState(IntakeState[_QueueEvent]):
    """FSM state for one OpenAI Responses intake graph run.

    Shared queue/funnel/telemetry slots come from :class:`IntakeState`; usage
    reads off the response envelope (``response.completed`` etc.).
    """

    model: str
    provider_details: dict[str, object] | None = None
    has_refusal: bool = False
    refusal_text: str = ""
    phase_by_item: dict[str, str] = field(default_factory=dict)


_g: GraphBuilder[_OpenAIResponsesIntakeState, None, None, list[ModelResponseStreamEvent]] = GraphBuilder(
    name="openai_responses_intake",
    state_type=_OpenAIResponsesIntakeState,
    output_type=list[ModelResponseStreamEvent],
)


@_g.step
async def frame_next_event(
    ctx: StepContext[_OpenAIResponsesIntakeState, None, None],
) -> _RoutedEvent:
    if not ctx.state.events_queue:
        return _FeedDone()
    return ctx.state.events_queue.popleft()


def _response_from_event(event: _ResponseEnvelopeWireEvent) -> responses.Response:
    return event.response


def _record_response_metadata(state: _OpenAIResponsesIntakeState, event: _ResponseEnvelopeWireEvent) -> None:
    response = _response_from_event(event)

    if response.id:
        state.provider_response_id = response.id
    if response.model:
        state.model = response.model

    # Funnel: the response envelope carries cumulative usage (populated on the
    # terminal ``response.completed``); replace as it arrives.
    if response.usage is not None:
        state.usage = _usage.usage_from_openai_responses(response.usage)

    if response.conversation is not None and response.conversation.id:
        state.provider_details = {
            **(state.provider_details or {}),
            "conversation_id": response.conversation.id,
        }

    raw_finish_reason: str | None = None
    if isinstance(
        event,
        (
            responses.ResponseCompletedEvent,
            responses.ResponseFailedEvent,
            responses.ResponseIncompleteEvent,
        ),
    ):
        candidate = (
            incomplete_details.reason
            if (incomplete_details := response.incomplete_details) is not None and incomplete_details.reason
            else response.status
        )
        if isinstance(candidate, str):
            raw_finish_reason = candidate

    if raw_finish_reason and not state.has_refusal:
        state.provider_details = {
            **(state.provider_details or {}),
            "finish_reason": raw_finish_reason,
        }
        state.finish_reason = _finish_reason.from_openai_responses(raw_finish_reason)


@_g.step
async def handle_response_envelope(
    ctx: StepContext[_OpenAIResponsesIntakeState, None, _ResponseEnvelopeEvent],
) -> None:
    _record_response_metadata(ctx.state, ctx.inputs.event)


# ── Inner subgraph: output_item_added dispatch ──────────────────────────────
#
# ``open_item_added`` classifies the item into a discriminant type and hands it
# to a decision. Direct class matches for ``ResponseFunctionToolCall`` and
# ``ResponseOutputMessage``; frozen envelopes split ``ResponseToolSearchCall``
# by ``execution`` value since it is one class with a literal field.

_oiag: GraphBuilder[_OpenAIResponsesIntakeState, None, _OutputItemAddedEvent, None] = GraphBuilder(
    name="openai_responses_item_added_dispatch",
    state_type=_OpenAIResponsesIntakeState,
    input_type=_OutputItemAddedEvent,
)


@_oiag.step
async def open_item_added(
    ctx: StepContext[_OpenAIResponsesIntakeState, None, _OutputItemAddedEvent],
) -> _ItemAddedDiscriminand:
    """Classify the added item into a discriminant the decision can route on."""
    item = ctx.inputs.event.item
    if isinstance(item, responses.ResponseFunctionToolCall):
        return item
    if isinstance(item, responses.ResponseOutputMessage):
        return item
    if isinstance(item, responses.ResponseToolSearchCall) and item.execution == "client":
        return _ClientToolSearchAdded(item=item)
    return _ItemAddedNoOp(item_type=type(item).__name__)


@_oiag.step
async def handle_added_function_tool_call(
    ctx: StepContext[_OpenAIResponsesIntakeState, None, responses.ResponseFunctionToolCall],
) -> None:
    """``function_tool_call`` item added — open a tool-call part; args arrive via later deltas."""
    state = ctx.state
    item = ctx.inputs
    provider_details: dict[str, object] | None = None
    if item.namespace:
        provider_details = {"namespace": item.namespace}
    state.out_events.append(
        state.parts_manager.handle_tool_call_part(
            vendor_part_id=item.id,
            tool_name=item.name,
            args=item.arguments,
            tool_call_id=item.call_id,
            id=item.id,
            provider_name="openai",
            provider_details=provider_details,
        )
    )


@_oiag.step
async def handle_added_output_message(
    ctx: StepContext[_OpenAIResponsesIntakeState, None, responses.ResponseOutputMessage],
) -> None:
    """``output_message`` item added — record the phase for later text-done events."""
    item = ctx.inputs
    phase = getattr(item, "phase", None)
    if phase is not None:
        ctx.state.phase_by_item[item.id] = phase


@_oiag.step
async def handle_added_client_tool_search(
    ctx: StepContext[_OpenAIResponsesIntakeState, None, _ClientToolSearchAdded],
) -> None:
    """``tool_search_call`` with client execution added — open a tool-call part."""
    state = ctx.state
    item = ctx.inputs.item
    state.out_events.append(
        state.parts_manager.handle_tool_call_part(
            vendor_part_id=item.id,
            tool_name=getattr(item, "name", "tool_search"),
            args=None,
            tool_call_id=item.call_id or item.id,
            id=item.id,
            provider_name="openai",
        )
    )


@_oiag.step
async def handle_unknown_item_added(ctx: StepContext[_OpenAIResponsesIntakeState, None, object]) -> None:
    """Catch-all for item-added variants with no IR handler — log instead of silently dropping."""
    logger.debug("openai responses intake: unhandled output_item.added type %s; skipping", type(ctx.inputs).__name__)


_oiag.add(
    _oiag.edge_from(_oiag.start_node).to(open_item_added),
    _oiag.edge_from(open_item_added).to(
        _oiag.decision()
        .branch(_oiag.match(responses.ResponseFunctionToolCall).to(handle_added_function_tool_call))
        .branch(_oiag.match(responses.ResponseOutputMessage).to(handle_added_output_message))
        .branch(_oiag.match(_ClientToolSearchAdded).to(handle_added_client_tool_search))
        .branch(_oiag.match(TypeExpression[object]).to(handle_unknown_item_added))
    ),
    _oiag.edge_from(
        handle_added_function_tool_call,
        handle_added_output_message,
        handle_added_client_tool_search,
        handle_unknown_item_added,
    ).to(_oiag.end_node),
)

_item_added_graph = _oiag.build()
_dispatch_item_added = _g.add_subgraph(_item_added_graph, label="item_added")  # ty: ignore[unresolved-attribute]


# ── Inner subgraph: output_item_done dispatch ───────────────────────────────
#
# Symmetric to the item-added subgraph. ``open_item_done`` classifies the item;
# direct class match for ``ResponseReasoningItem``, envelope for client-execution
# tool-search calls.

_oidg: GraphBuilder[_OpenAIResponsesIntakeState, None, _OutputItemDoneEvent, None] = GraphBuilder(
    name="openai_responses_item_done_dispatch",
    state_type=_OpenAIResponsesIntakeState,
    input_type=_OutputItemDoneEvent,
)


@_oidg.step
async def open_item_done(
    ctx: StepContext[_OpenAIResponsesIntakeState, None, _OutputItemDoneEvent],
) -> _ItemDoneDiscriminand:
    """Classify the done item into a discriminant the decision can route on."""
    item = ctx.inputs.event.item
    if isinstance(item, responses.ResponseReasoningItem):
        return item
    if isinstance(item, responses.ResponseToolSearchCall) and item.execution == "client":
        return _ClientToolSearchDone(item=item)
    return _ItemDoneNoOp(item_type=type(item).__name__)


@_oidg.step
async def handle_done_reasoning(
    ctx: StepContext[_OpenAIResponsesIntakeState, None, responses.ResponseReasoningItem],
) -> None:
    """``reasoning`` item done — emit the encrypted thinking part if present."""
    state = ctx.state
    item = ctx.inputs
    if item.encrypted_content:
        state.out_events.extend(
            state.parts_manager.handle_thinking_delta(
                vendor_part_id=item.id,
                id=item.id,
                signature=item.encrypted_content,
                provider_name="openai",
            )
        )


@_oidg.step
async def handle_done_client_tool_search(
    ctx: StepContext[_OpenAIResponsesIntakeState, None, _ClientToolSearchDone],
) -> None:
    """``tool_search_call`` with client execution done — finalize the tool-call args."""
    state = ctx.state
    item = ctx.inputs.item
    maybe_event = state.parts_manager.handle_tool_call_delta(
        vendor_part_id=item.id,
        args={},
        tool_call_id=item.call_id or item.id,
        provider_name="openai",
    )
    if maybe_event is not None:
        state.out_events.append(maybe_event)


@_oidg.step
async def handle_unknown_item_done(ctx: StepContext[_OpenAIResponsesIntakeState, None, object]) -> None:
    """Catch-all for item-done variants with no IR handler — log instead of silently dropping."""
    logger.debug("openai responses intake: unhandled output_item.done type %s; skipping", type(ctx.inputs).__name__)


_oidg.add(
    _oidg.edge_from(_oidg.start_node).to(open_item_done),
    _oidg.edge_from(open_item_done).to(
        _oidg.decision()
        .branch(_oidg.match(responses.ResponseReasoningItem).to(handle_done_reasoning))
        .branch(_oidg.match(_ClientToolSearchDone).to(handle_done_client_tool_search))
        .branch(_oidg.match(TypeExpression[object]).to(handle_unknown_item_done))
    ),
    _oidg.edge_from(
        handle_done_reasoning,
        handle_done_client_tool_search,
        handle_unknown_item_done,
    ).to(_oidg.end_node),
)

_item_done_graph = _oidg.build()
_dispatch_item_done = _g.add_subgraph(_item_done_graph, label="item_done")  # ty: ignore[unresolved-attribute]


@_g.step
async def handle_text_delta(
    ctx: StepContext[_OpenAIResponsesIntakeState, None, _TextDeltaEvent],
) -> None:
    event = ctx.inputs.event
    if event.delta is None:
        return
    ctx.state.out_events.extend(
        ctx.state.parts_manager.handle_text_delta(
            vendor_part_id=event.item_id,
            content=event.delta,
            id=event.item_id,
            provider_name="openai",
        )
    )


@_g.step
async def handle_text_done(
    ctx: StepContext[_OpenAIResponsesIntakeState, None, _TextDoneEvent],
) -> None:
    state = ctx.state
    event = ctx.inputs.event
    provider_details: dict[str, object] = {}
    phase = state.phase_by_item.get(event.item_id)
    if phase is not None:
        provider_details["phase"] = phase
    if provider_details:
        state.out_events.extend(
            state.parts_manager.handle_text_delta(
                vendor_part_id=event.item_id,
                content="",
                provider_name="openai",
                provider_details=provider_details,
            )
        )


@_g.step
async def handle_function_arguments_delta(
    ctx: StepContext[_OpenAIResponsesIntakeState, None, _FunctionArgumentsDeltaEvent],
) -> None:
    event = ctx.inputs.event
    maybe_event = ctx.state.parts_manager.handle_tool_call_delta(
        vendor_part_id=event.item_id,
        args=event.delta,
        provider_name="openai",
    )
    if maybe_event is not None:
        ctx.state.out_events.append(maybe_event)


@_g.step
async def handle_function_arguments_done(
    ctx: StepContext[_OpenAIResponsesIntakeState, None, _FunctionArgumentsDoneEvent],
) -> None:
    del ctx


@_g.step
async def handle_reasoning_summary_part_added(
    ctx: StepContext[_OpenAIResponsesIntakeState, None, _ReasoningSummaryPartAddedEvent],
) -> None:
    event = ctx.inputs.event
    vendor_id = event.item_id if event.summary_index == 0 else f"{event.item_id}-{event.summary_index}"
    text = getattr(event.part, "text", "")
    if text:
        ctx.state.out_events.extend(
            ctx.state.parts_manager.handle_thinking_delta(
                vendor_part_id=vendor_id,
                content=text,
                id=event.item_id,
                provider_name="openai",
            )
        )


@_g.step
async def handle_reasoning_summary_text_delta(
    ctx: StepContext[_OpenAIResponsesIntakeState, None, _ReasoningSummaryTextDeltaEvent],
) -> None:
    event = ctx.inputs.event
    vendor_id = event.item_id if event.summary_index == 0 else f"{event.item_id}-{event.summary_index}"
    ctx.state.out_events.extend(
        ctx.state.parts_manager.handle_thinking_delta(
            vendor_part_id=vendor_id,
            content=event.delta,
            id=event.item_id,
            provider_name="openai",
        )
    )


@_g.step
async def handle_reasoning_text_delta(
    ctx: StepContext[_OpenAIResponsesIntakeState, None, _ReasoningTextDeltaEvent],
) -> None:
    event = ctx.inputs.event
    ctx.state.out_events.extend(
        ctx.state.parts_manager.handle_thinking_delta(
            vendor_part_id=event.item_id,
            content=event.delta,
            id=event.item_id,
            provider_name="openai",
        )
    )


@_g.step
async def handle_refusal_delta(
    ctx: StepContext[_OpenAIResponsesIntakeState, None, _RefusalDeltaEvent],
) -> None:
    state = ctx.state
    state.has_refusal = True
    state.finish_reason = "content_filter"
    state.refusal_text += ctx.inputs.event.delta


@_g.step
async def handle_refusal_done(
    ctx: StepContext[_OpenAIResponsesIntakeState, None, _RefusalDoneEvent],
) -> None:
    state = ctx.state
    state.has_refusal = True
    state.finish_reason = "content_filter"
    state.refusal_text = ctx.inputs.event.refusal


@_g.step
async def handle_noop(
    ctx: StepContext[_OpenAIResponsesIntakeState, None, _NoOpEvent],
) -> None:
    logger.debug("openai responses intake: no-op event %s", ctx.inputs.event_type)


@_g.step
async def emit_done(
    ctx: StepContext[_OpenAIResponsesIntakeState, None, _FeedDone],
) -> list[ModelResponseStreamEvent]:
    out = ctx.state.out_events
    ctx.state.emitted_events += len(out)
    ctx.state.out_events = []
    return out


_g.add(
    _g.edge_from(_g.start_node).to(frame_next_event),
    _g.edge_from(frame_next_event).to(
        _g.decision()
        .branch(_g.match(_FeedDone).to(emit_done))
        .branch(_g.match(_ResponseEnvelopeEvent).to(handle_response_envelope))
        .branch(_g.match(_OutputItemAddedEvent).to(_dispatch_item_added))
        .branch(_g.match(_OutputItemDoneEvent).to(_dispatch_item_done))
        .branch(_g.match(_TextDeltaEvent).to(handle_text_delta))
        .branch(_g.match(_TextDoneEvent).to(handle_text_done))
        .branch(_g.match(_FunctionArgumentsDeltaEvent).to(handle_function_arguments_delta))
        .branch(_g.match(_FunctionArgumentsDoneEvent).to(handle_function_arguments_done))
        .branch(_g.match(_ReasoningSummaryPartAddedEvent).to(handle_reasoning_summary_part_added))
        .branch(_g.match(_ReasoningSummaryTextDeltaEvent).to(handle_reasoning_summary_text_delta))
        .branch(_g.match(_ReasoningTextDeltaEvent).to(handle_reasoning_text_delta))
        .branch(_g.match(_RefusalDeltaEvent).to(handle_refusal_delta))
        .branch(_g.match(_RefusalDoneEvent).to(handle_refusal_done))
        .branch(_g.match(_NoOpEvent).to(handle_noop))
    ),
    _g.edge_from(
        handle_response_envelope,
        _dispatch_item_added,
        _dispatch_item_done,
        handle_text_delta,
        handle_text_done,
        handle_function_arguments_delta,
        handle_function_arguments_done,
        handle_reasoning_summary_part_added,
        handle_reasoning_summary_text_delta,
        handle_reasoning_text_delta,
        handle_refusal_delta,
        handle_refusal_done,
        handle_noop,
    ).to(frame_next_event),
    _g.edge_from(emit_done).to(_g.end_node),
)


_intake_graph = _g.build()


class OpenAIResponsesIntakeFSM(ResponseIntakeFSM[_OpenAIResponsesIntakeState]):
    """Async pydantic-graph-driven OpenAI Responses SSE intake."""

    name = "openai_responses"
    _graph = _intake_graph

    def _initial_state(self, *, model: str, request_params: ModelRequestParameters) -> _OpenAIResponsesIntakeState:
        return _OpenAIResponsesIntakeState(
            parts_manager=ModelResponsePartsManager(model_request_parameters=request_params),
            model=model,
        )

    @property
    def _model(self) -> str:
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
        """Stream end. Refusal text is stashed on ``provider_details``; warn on silent-empty."""
        s = self._state
        if s.refusal_text:
            s.provider_details = {
                **(s.provider_details or {}),
                "refusal": s.refusal_text,
            }
        if not s.has_refusal:
            self._log_no_ir_events(extra=f" finish_reason={s.finish_reason}")
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
                event = _EVENT_ADAPTER.validate_json(payload)
            except ValidationError:
                self._state.frames_unparseable += 1
                logger.debug("openai responses intake: skipping unparseable frame: %r", payload)
                continue
            yield _classify_event(event)


def _classify_event(event: responses.ResponseStreamEvent) -> _QueueEvent:
    if isinstance(
        event,
        (
            responses.ResponseCreatedEvent,
            responses.ResponseQueuedEvent,
            responses.ResponseInProgressEvent,
            responses.ResponseCompletedEvent,
            responses.ResponseFailedEvent,
            responses.ResponseIncompleteEvent,
        ),
    ):
        return _ResponseEnvelopeEvent(event=event)
    if isinstance(event, responses.ResponseOutputItemAddedEvent):
        return _OutputItemAddedEvent(event=event)
    if isinstance(event, responses.ResponseOutputItemDoneEvent):
        return _OutputItemDoneEvent(event=event)
    if isinstance(event, responses.ResponseTextDeltaEvent):
        return _TextDeltaEvent(event=event)
    if isinstance(event, responses.ResponseTextDoneEvent):
        return _TextDoneEvent(event=event)
    if isinstance(event, responses.ResponseFunctionCallArgumentsDeltaEvent):
        return _FunctionArgumentsDeltaEvent(event=event)
    if isinstance(event, responses.ResponseFunctionCallArgumentsDoneEvent):
        return _FunctionArgumentsDoneEvent(event=event)
    if isinstance(event, responses.ResponseReasoningSummaryPartAddedEvent):
        return _ReasoningSummaryPartAddedEvent(event=event)
    if isinstance(event, responses.ResponseReasoningSummaryTextDeltaEvent):
        return _ReasoningSummaryTextDeltaEvent(event=event)
    if isinstance(event, responses.ResponseReasoningTextDeltaEvent):
        return _ReasoningTextDeltaEvent(event=event)
    if isinstance(event, responses.ResponseRefusalDeltaEvent):
        return _RefusalDeltaEvent(event=event)
    if isinstance(event, responses.ResponseRefusalDoneEvent):
        return _RefusalDoneEvent(event=event)
    event_type = getattr(event, "type", None)
    return _NoOpEvent(event_type=event_type if isinstance(event_type, str) else type(event).__name__)


def _extract_data_payload(frame: bytes) -> bytes | None:
    data_lines: list[bytes] = []
    for line in frame.splitlines():
        stripped = line.strip()
        if stripped.startswith(b"data:"):
            data_lines.append(stripped[5:].strip())
    if not data_lines:
        return None
    payload = b"\n".join(data_lines).strip()
    return payload or None
