"""OpenAI Conversations SSE-v1 bytes → pydantic-ai IR events via FSM.

Ported from gproxy (MIT licence — original authors: gproxy contributors):
  sdk/gproxy-channel/src/channels/chatgpt/sse_v1.rs
  sdk/gproxy-channel/src/channels/chatgpt/sse_to_openai.rs

The ChatGPT ``/backend-api/f/conversation`` endpoint streams SSE-v1
JSON-patch deltas rather than OpenAI ``chat.completion.chunk`` frames.
This module parses those bytes directly — no dependency on the OpenAI
SDK shape — into pydantic-ai ``TextPart`` start + delta events compatible
with ``OpenAIResponseRenderFSM`` and ``AnthropicResponseRenderFSM``.

Wire shapes decoded:

* ``event: delta_encoding / data: "v1"`` — encoding banner, consumed silently.
* ``data: {type, ...}`` without ``p`` / ``v`` fields — typed side events:
  ``resume_conversation_token``, ``stream_handoff``, ``server_ste_metadata``.
  These are captured into ``state.continuation`` and surface a typed
  :class:`HandoffDetected` signal; they do NOT produce a false finish.
* ``data: {p, o, v, c}`` — initial "add" event declaring a new channel.
  The intake inspects the embedded message to decide whether this is the
  visible assistant final-answer channel.
* ``data: {o: "patch", v: [{p,o,v}, ...]}`` — explicit batch of patches on
  the current channel.
* ``data: {v: [{p,o,v}, ...]}`` (no ``o``/``p``) — shorthand batch, same
  semantics.
* ``data: {p, o, v}`` — single patch on the current channel.
* ``data: {v: "<token>"}`` (bare string ``v``, no ``o``/``p``) — continuation delta:
  continues the last content path with an implied ``append`` (recon §2 — the
  majority of the stream once content begins).
* ``data: [DONE]`` — stream end.
* ``data: {type: "ccproxy_idle_timeout", ...}`` — ccproxy's own synthetic
  terminal signal (never a real chatgpt.com event), injected by
  :mod:`ccproxy.openai_conversations.session_ws` /
  :mod:`ccproxy.openai_conversations.ws_handoff` when their idle-timeout
  backstop gives up waiting for the next conduit frame. Sets ``finish_reason``
  to ``"error"`` instead of leaving a give-up indistinguishable from a
  genuine ``[DONE]`` completion.

Patch semantics on a text-content path (``/message/content/parts/0`` array shape
or ``/message/content/text`` string shape):

* ``o: "append"`` — emit the full value as a text delta.
* ``o: "replace"`` — compute the suffix not yet accumulated and emit only that.
* bare ``{v}`` — continue the last content path with an implied ``append``.

Finish synthesis:

* ``o: "replace", p: "/message/status", v: "finished_successfully"`` triggers
  a synthetic finish event once content has been emitted.
* ``data: [DONE]`` after content also synthesises a finish, preventing
  duplication via the ``state.final_emitted`` flag.
* ``ccproxy_idle_timeout`` always synthesises a finish (regardless of whether
  content had begun), with ``finish_reason = "error"`` — an idle give-up is
  never left unclassified, and never overwrites an already-recorded finish.

Handoff continuation (the answer arrives over WebSocket):

When a typed side event with ``type`` in ``{resume_conversation_token,
stream_handoff, server_ste_metadata}`` arrives, ``state.continuation`` is
populated for inspection and the FSM surfaces a :class:`_HandoffDetected`
envelope, but no IR event is emitted and nothing is raised. The real answer
arrives as bridged SSE-v1 frames the sidecar appends after the inline HTTP body
(``transport/sidecar.py`` + ``openai_conversations/ws_handoff.py``); this same
FSM parses those frames into the assistant final-answer channel.

MIT attribution:

    The SSE frame decoder (``_drain_sse_frames``), patch shape normalisation
    (``_parse_delta``), and channel-identification logic in ``handle_add`` are
    adapted from the gproxy Rust reference cited above. All behavioural
    decisions, state-machine structure, and Python idioms are original.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic_ai._parts_manager import ModelResponsePartsManager
from pydantic_ai.messages import ModelResponseStreamEvent
from pydantic_graph import GraphBuilder, StepContext

import ccproxy.lightllm.graph._subgraph_patch  # noqa: F401  — installs add_subgraph
from ccproxy.lightllm.graph._base import IntakeState, ResponseIntakeFSM
from ccproxy.openai_conversations.ws_handoff import CCPROXY_IDLE_TIMEOUT_EVENT_TYPE

if TYPE_CHECKING:
    from pydantic_ai.models import ModelRequestParameters

logger = logging.getLogger(__name__)

# ── Public typed signals ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class ContinuationMetadata:
    """Side-event metadata captured during intake.

    Populated when any of ``resume_conversation_token``, ``stream_handoff``,
    or ``server_ste_metadata`` typed events arrive.
    """

    conversation_id: str = ""
    """Conversation id extracted from the initial add or a side event."""

    resume_token: str = ""
    """Token from a ``resume_conversation_token`` event."""

    handoff_topic: str = ""
    """WS topic from a ``stream_handoff`` or ``server_ste_metadata`` event."""

    turn_exchange_id: str = ""
    """Turn exchange id from a ``server_ste_metadata`` event."""


# ── Internal dispatch envelopes ───────────────────────────────────────────────


@dataclass(frozen=True)
class _AddEnvelope:
    """Initial channel-add event. Carries channel id and embedded message wrapper."""

    channel: int
    value: dict[str, Any]


@dataclass(frozen=True)
class _PatchEnvelope:
    """One or more patch operations on the current (or declared) channel.

    ``channel`` is ``None`` when the frame omits the ``c`` field; the handler
    uses ``state.current_channel`` in that case.
    """

    channel: int | None
    patches: list[tuple[str, str, Any]]
    """Normalised ``(path, op, value)`` tuples in application order."""


@dataclass(frozen=True)
class _TypedSideEvent:
    """Typed side event — ``{type, ...}`` without ``p`` or ``v`` fields."""

    kind: str
    raw: dict[str, Any]


class _DoneEvent:
    """``[DONE]`` sentinel from the SSE stream."""


@dataclass(frozen=True)
class _TimeoutEvent:
    """ccproxy's own synthetic idle-timeout terminal signal (never a real
    chatgpt.com event) — see :data:`CCPROXY_IDLE_TIMEOUT_EVENT_TYPE`."""

    idle_seconds: float | None = None


class _FeedDone:
    """Queue exhausted — terminal step returns accumulated IR events."""


class _HandoffDetected:
    """Handoff side event arrived before any visible content."""


# ── State ─────────────────────────────────────────────────────────────────────

_ANSWER_VENDOR_ID = "oaicv-answer"
"""Stable vendor_part_id for the assistant final-answer ``TextPart``."""

_TEXT_CONTENT_PATHS = frozenset({"/message/content/parts/0", "/message/content/text"})
"""Content paths carrying the assistant's visible answer text. ``parts/0`` is the
multimodal/array shape; ``text`` is the single-string shape (recon §2). Both stream
identically — ``append`` + suffix-``replace`` + bare continuation deltas."""


@dataclass
class _ConversationsIntakeState(IntakeState[Any]):
    """FSM state for one OpenAI Conversations SSE-v1 stream.

    Shared queue/funnel/telemetry slots come from :class:`IntakeState`. The
    funnel slots are present for interface parity but stay inert: the ChatGPT
    web conversations stream carries no per-request token usage.

    Channel-tracking fields persist across ``feed()`` calls — they model
    the ongoing server-side JSON-patch state machine.
    """

    # ── Channel tracking ──────────────────────────────────────────────────────
    current_channel: int | None = None
    """Most-recently-declared channel (applies to follow-up patches missing ``c``)."""

    final_channel: int | None = None
    """Channel carrying the assistant's visible final answer text."""

    assistant_text_channels: dict[int, tuple[str, str]] = field(default_factory=dict)
    """Channels that declared an assistant ``content_type == "text"`` message (ANY
    status, not hidden) → ``(message_id, model_slug)``. Candidates for lazy adoption
    as the final-answer channel when content arrives but no in-progress add anchored
    it — e.g. a WebSocket catch-up replaying an already-``finished_successfully``
    message."""

    message_id: str = ""
    """Assistant message id from the final-answer channel's initial add."""

    model_slug: str = ""
    """Model slug from the final-answer channel's message metadata."""

    conversation_id: str = ""
    """Conversation id from the first channel add."""

    # ── Accumulator ───────────────────────────────────────────────────────────
    accumulated_text: str = ""
    """Full text emitted for the final-answer channel; used for suffix diffing."""

    last_path: str = ""
    """Most-recent explicit content patch path on the final-answer channel; a bare
    ``{"v": ...}`` delta continues this target with an implied ``append`` (recon §2)."""

    content_begun: bool = False
    """True once at least one text delta has been emitted."""

    final_emitted: bool = False
    """True once the finish reason has been recorded; prevents duplication."""

    # ── Telemetry (never-silently-drop diagnostics) ───────────────────────────
    content_patches_off_channel: int = 0
    """Content patches (text path or bare delta) seen while the current channel is
    NOT the tracked final-answer channel — the answer may be on a channel we never
    identified. The leading signal for a silent 'no text emitted' failure."""

    assistant_msgs_skipped: int = 0
    """Assistant ``content_type == "text"`` messages NOT adopted as the final-answer
    channel (e.g. ``status == "finished_successfully"`` on a catch-up replay, or a
    hidden message). Surfaces why no channel was tracked."""

    # ── Handoff ───────────────────────────────────────────────────────────────
    continuation: ContinuationMetadata | None = None
    """Populated when a handoff side event is received."""


# ── Pure helpers ──────────────────────────────────────────────────────────────


def _is_final_answer_message(msg: dict[str, Any]) -> bool:
    """True when ``msg`` is the visible assistant text message to track.

    Mirrors gproxy ``handle_add`` heuristics:
    - ``author.role == "assistant"``
    - ``content.content_type == "text"``
    - ``status != "finished_successfully"`` (completed messages are historical)
    - ``metadata.is_visually_hidden_from_conversation`` is falsy
    """
    role = msg.get("author", {}).get("role")
    content_type = msg.get("content", {}).get("content_type")
    status = msg.get("status")
    hidden = msg.get("metadata", {}).get("is_visually_hidden_from_conversation")
    return role == "assistant" and content_type == "text" and status != "finished_successfully" and not hidden


def _handoff_topic_from_side_event(kind: str, raw: dict[str, Any]) -> str:
    """Extract a WS handoff topic id from a typed side event.

    Mirrors aurora ``streamHandoffTopicFromEvent`` + ``streamHandoffTopicFromMetadata``.
    """
    if kind == "stream_handoff":
        options = raw.get("options")
        if isinstance(options, list):
            for option in options:
                if not isinstance(option, dict):
                    continue
                if option.get("type") == "subscribe_ws_topic":
                    topic = option.get("topic_id", "")
                    if isinstance(topic, str) and topic:
                        return topic
        return ""

    if kind == "server_ste_metadata":
        turn_exchange_id = raw.get("turn_exchange_id")
        if isinstance(turn_exchange_id, str) and turn_exchange_id:
            return f"conversation-turn-{turn_exchange_id}"
        metadata = raw.get("metadata")
        if isinstance(metadata, dict):
            tei = metadata.get("turn_exchange_id")
            if isinstance(tei, str) and tei:
                return f"conversation-turn-{tei}"
        return ""

    return ""


def _parse_delta(frame_obj: dict[str, Any]) -> list[tuple[str, str, Any]]:
    """Normalise a frame object into ``(path, op, value)`` tuples.

    Four shapes (gproxy ``parse_delta``):

    - Explicit batch: ``{o: "patch", v: [{p,o,v}, ...]}``
    - Shorthand batch: ``{v: [{p,o,v}, ...]}`` (no ``o``/``p``)
    - Implicit add: ``{v: <object>}`` (no ``o``/``p``) → ``[("", "add", obj)]``
    - Single patch: ``{p, o, v}``
    """
    op_field = frame_obj.get("o")
    op_str = op_field if isinstance(op_field, str) else ""
    v_field = frame_obj.get("v")
    has_p = "p" in frame_obj

    if op_str == "patch" and isinstance(v_field, list):
        return _parse_patch_list(v_field)

    if not op_str and not has_p and v_field is not None:
        if isinstance(v_field, list):
            return _parse_patch_list(v_field)
        if isinstance(v_field, dict):
            return [("", "add", v_field)]

    path = frame_obj.get("p", "")
    path = path if isinstance(path, str) else ""
    return [(path, op_str, v_field)]


def _parse_patch_list(items: list[Any]) -> list[tuple[str, str, Any]]:
    result: list[tuple[str, str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        path = item.get("p", "")
        path = path if isinstance(path, str) else ""
        op = item.get("o", "")
        op = op if isinstance(op, str) else ""
        value = item.get("v")
        result.append((path, op, value))
    return result


def _has_content_patch(patches: list[tuple[str, str, Any]]) -> bool:
    """True when any patch carries assistant answer text — an explicit text-content
    path, or a bare ``{"v": "str"}`` continuation delta (no path/op)."""
    for path, op, value in patches:
        if path in _TEXT_CONTENT_PATHS:
            return True
        if not path and not op and isinstance(value, str) and value:
            return True
    return False


def _channel_from_frame(frame_obj: dict[str, Any]) -> int | None:
    c = frame_obj.get("c")
    if c is None:
        return None
    try:
        return int(c)
    except (TypeError, ValueError):
        return None


# ── Outer intake graph ────────────────────────────────────────────────────────


_g: GraphBuilder[_ConversationsIntakeState, None, None, list[ModelResponseStreamEvent]] = GraphBuilder(
    name="openai_conversations_intake",
    state_type=_ConversationsIntakeState,
    output_type=list[ModelResponseStreamEvent],
)


@_g.step
async def frame_next_event(
    ctx: StepContext[_ConversationsIntakeState, None, None],
) -> Any:
    """Pop the next dispatch envelope from the events queue, or signal completion."""
    state = ctx.state
    if not state.events_queue:
        return _FeedDone()
    return state.events_queue.popleft()


@_g.step
async def handle_add(
    ctx: StepContext[_ConversationsIntakeState, None, _AddEnvelope],
) -> None:
    """Process an initial channel-add event.

    Inspects the embedded message to determine whether this channel carries
    the visible assistant final-answer text. Captures ``conversation_id`` and
    ``model_slug`` on first observation.
    """
    state = ctx.state
    env = ctx.inputs
    state.current_channel = env.channel

    msg_wrap = env.value
    conv_id = msg_wrap.get("conversation_id")
    if isinstance(conv_id, str) and conv_id and not state.conversation_id:
        state.conversation_id = conv_id

    msg = msg_wrap.get("message")
    if not isinstance(msg, dict):
        return

    role = msg.get("author", {}).get("role")
    content_type = msg.get("content", {}).get("content_type")
    hidden = msg.get("metadata", {}).get("is_visually_hidden_from_conversation")
    msg_id = msg.get("id") if isinstance(msg.get("id"), str) else ""
    slug = msg.get("metadata", {}).get("model_slug")
    slug = slug if isinstance(slug, str) else ""

    if role == "assistant" and content_type == "text" and not hidden:
        # Candidate final-answer channel (regardless of status) for lazy adoption.
        state.assistant_text_channels[env.channel] = (msg_id, slug)

    if _is_final_answer_message(msg) and state.final_channel is None:
        state.final_channel = env.channel
        if msg_id:
            state.message_id = msg_id
        if slug:
            state.model_slug = slug
    elif role == "assistant" and content_type == "text":
        # Not adopted eagerly (status already finished, e.g. a WebSocket catch-up
        # replay). Recorded as a lazy-adoption candidate above; logged so a silent
        # "no text emitted" turn stays explainable from the logs alone.
        state.assistant_msgs_skipped += 1
        logger.debug(
            "oaic intake: assistant text message not eagerly adopted "
            "(channel=%s status=%s hidden=%s) — candidate for lazy adoption",
            env.channel,
            msg.get("status"),
            hidden,
        )


@_g.step
async def handle_patch(
    ctx: StepContext[_ConversationsIntakeState, None, _PatchEnvelope],
) -> None:
    """Apply patches, emitting IR text delta events for the final-answer channel.

    Updates ``state.current_channel`` when the envelope declares one.
    Only patches targeting the final-answer channel produce output:

    - ``append`` on a text-content path (``/message/content/parts/0`` array shape
      or ``/message/content/text`` string shape) → text delta.
    - ``replace`` on a text-content path → suffix-only text delta.
    - bare ``{"v": "str"}`` (no ``p``/``o``) → continues the last content path with
      an implied ``append`` (recon §2 — the SSE-v1 majority shape).
    - ``replace`` on ``/message/status`` = ``finished_successfully`` → finish.
    """
    state = ctx.state
    env = ctx.inputs

    if env.channel is not None:
        state.current_channel = env.channel

    relevant = state.final_channel is not None and state.current_channel == state.final_channel
    if not relevant:
        if (
            state.final_channel is None
            and state.current_channel in state.assistant_text_channels
            and _has_content_patch(env.patches)
        ):
            # Lazy adoption: visible content arrived on a candidate assistant channel
            # with no in-progress add to anchor it (e.g. a WebSocket catch-up replays
            # the already-finished message). The answer is wherever content lands.
            state.final_channel = state.current_channel
            mid, slug = state.assistant_text_channels[state.current_channel]
            if mid and not state.message_id:
                state.message_id = mid
            if slug and not state.model_slug:
                state.model_slug = slug
            logger.debug(
                "oaic intake: lazily adopted channel=%s as final-answer (content arrived, no in-progress add)",
                state.current_channel,
            )
            # fall through: final_channel now set, process the content patches below.
        elif _has_content_patch(env.patches):
            # Content text arrived on a channel that is not — and cannot become — the
            # final-answer channel. Logged so an empty/silent OAIC turn never passes
            # unobserved.
            state.content_patches_off_channel += 1
            logger.debug(
                "oaic intake: content patch on channel=%s ignored (final_channel=%s); "
                "answer may be on an untracked channel",
                state.current_channel,
                state.final_channel,
            )
            return
        else:
            return

    for path, op, value in env.patches:
        # A bare continuation delta ({"v": "str"} with no p/o) continues the last
        # content path with an implied "append". When no content path is live yet
        # (last_path empty) it resolves to a non-content path and is dropped.
        if not path and not op and isinstance(value, str):
            path, op = state.last_path, "append"

        if path in _TEXT_CONTENT_PATHS:
            state.last_path = path
            if op == "append" and isinstance(value, str) and value:
                state.accumulated_text += value
                state.out_events.extend(
                    state.parts_manager.handle_text_delta(
                        vendor_part_id=_ANSWER_VENDOR_ID,
                        content=value,
                    )
                )
                state.content_begun = True

            elif op == "replace" and isinstance(value, str):
                # Emit only the new suffix relative to already-accumulated text.
                suffix = value[len(state.accumulated_text) :] if value.startswith(state.accumulated_text) else value
                if suffix:
                    state.accumulated_text += suffix
                    state.out_events.extend(
                        state.parts_manager.handle_text_delta(
                            vendor_part_id=_ANSWER_VENDOR_ID,
                            content=suffix,
                        )
                    )
                    state.content_begun = True

        elif (
            path == "/message/status"
            and op == "replace"
            and value == "finished_successfully"
            and state.content_begun
            and not state.final_emitted
        ):
            state.final_emitted = True
            state.finish_reason = "stop"


@_g.step
async def handle_typed_side_event(
    ctx: StepContext[_ConversationsIntakeState, None, _TypedSideEvent],
) -> Any:
    """Process a typed side event.

    Known handoff types are captured into ``state.continuation``.
    When no content has yet been accumulated the router receives
    :class:`_HandoffDetected`. Unknown types are silently ignored.
    """
    state = ctx.state
    env = ctx.inputs
    kind = env.kind
    raw = env.raw

    _handoff_kinds = frozenset(
        {
            "resume_conversation_token",
            "stream_handoff",
            "server_ste_metadata",
        }
    )

    if kind not in _handoff_kinds:
        return None

    resume_token = ""
    handoff_topic = ""
    turn_exchange_id = ""

    if kind == "resume_conversation_token":
        tok = raw.get("token")
        resume_token = tok if isinstance(tok, str) else ""
    else:
        handoff_topic = _handoff_topic_from_side_event(kind, raw)
        if kind == "server_ste_metadata":
            tei = raw.get("turn_exchange_id")
            if isinstance(tei, str):
                turn_exchange_id = tei
            else:
                meta = raw.get("metadata")
                if isinstance(meta, dict):
                    tei2 = meta.get("turn_exchange_id")
                    if isinstance(tei2, str):
                        turn_exchange_id = tei2

    state.continuation = ContinuationMetadata(
        conversation_id=state.conversation_id,
        resume_token=resume_token,
        handoff_topic=handoff_topic,
        turn_exchange_id=turn_exchange_id,
    )

    if not state.content_begun:
        return _HandoffDetected()
    return None


@_g.step
async def handle_done(
    ctx: StepContext[_ConversationsIntakeState, None, _DoneEvent],
) -> None:
    """``[DONE]`` sentinel: record finish when content has arrived and not yet recorded."""
    state = ctx.state
    if state.content_begun and not state.final_emitted:
        state.final_emitted = True
        state.finish_reason = "stop"


@_g.step
async def handle_timeout(
    ctx: StepContext[_ConversationsIntakeState, None, _TimeoutEvent],
) -> None:
    """ccproxy's own idle-timeout terminal signal: record a distinguishable
    ``"error"`` finish, regardless of whether content had begun, so a
    truncated turn is never left with an unset (silently-"stop"-defaulting)
    finish reason. Never overwrites an already-recorded finish (e.g. a
    genuine ``finished_successfully``/``[DONE]`` that raced the give-up)."""
    state = ctx.state
    if not state.final_emitted:
        state.final_emitted = True
        state.finish_reason = "error"
    logger.warning(
        "oaic intake: turn ended via ccproxy idle-timeout signal (idle_seconds=%s, conv=%s) "
        "— truncated, not a genuine upstream completion",
        ctx.inputs.idle_seconds,
        state.conversation_id[:8] or "?",
    )


@_g.step
async def handle_handoff_detected(
    ctx: StepContext[_ConversationsIntakeState, None, _HandoffDetected],
) -> None:
    """Handoff before content: no-op FSM step; caller reads ``state.continuation``."""


@_g.step
async def emit_done(
    ctx: StepContext[_ConversationsIntakeState, None, _FeedDone],
) -> list[ModelResponseStreamEvent]:
    """Terminal step — drain and return accumulated IR events."""
    out = ctx.state.out_events
    ctx.state.emitted_events += len(out)
    ctx.state.out_events = []
    return out


_g.add(
    _g.edge_from(_g.start_node).to(frame_next_event),
    _g.edge_from(frame_next_event).to(
        _g.decision()
        .branch(_g.match(_FeedDone).to(emit_done))
        .branch(_g.match(_AddEnvelope).to(handle_add))
        .branch(_g.match(_PatchEnvelope).to(handle_patch))
        .branch(_g.match(_TypedSideEvent).to(handle_typed_side_event))
        .branch(_g.match(_DoneEvent).to(handle_done))
        .branch(_g.match(_TimeoutEvent).to(handle_timeout))
    ),
    _g.edge_from(handle_add).to(frame_next_event),
    _g.edge_from(handle_patch).to(frame_next_event),
    _g.edge_from(handle_typed_side_event).to(
        _g.decision()
        .branch(_g.match(_HandoffDetected).to(handle_handoff_detected))
        .branch(_g.match(type(None)).to(frame_next_event))
    ),
    _g.edge_from(handle_handoff_detected).to(frame_next_event),
    _g.edge_from(handle_done).to(frame_next_event),
    _g.edge_from(handle_timeout).to(frame_next_event),
    _g.edge_from(emit_done).to(_g.end_node),
)

_intake_graph = _g.build()


# ── Public class ───────────────────────────────────────────────────────────────


class OpenAIConversationsIntakeFSM(ResponseIntakeFSM[_ConversationsIntakeState]):
    """Async pydantic-graph-driven OpenAI Conversations SSE-v1 intake.

    Parses the ``/backend-api/f/conversation`` SSE-v1 JSON-patch stream
    into pydantic-ai ``TextPart`` events compatible with any render FSM
    (OpenAI Chat, Anthropic, Responses).

    Handoff side events are consumed silently during :meth:`feed`:
    ``state.continuation`` is populated for inspection but no IR event is
    emitted and nothing is raised. The real answer arrives as bridged
    SSE-v1 frames the sidecar WebSocket handoff bridge appends, which this
    same FSM parses.
    """

    name = "openai_conversations"
    _graph = _intake_graph

    def _initial_state(self, *, model: str, request_params: ModelRequestParameters) -> _ConversationsIntakeState:
        del model  # Conversations tracks the model slug off the wire (``model_slug``).
        return _ConversationsIntakeState(
            parts_manager=ModelResponsePartsManager(model_request_parameters=request_params),
        )

    @property
    def conversation_id(self) -> str:
        return self._state.conversation_id

    @property
    def message_id(self) -> str:
        return self._state.message_id

    @property
    def continuation(self) -> ContinuationMetadata | None:
        return self._state.continuation

    async def close(self) -> list[ModelResponseStreamEvent]:
        """End of stream. Emit a telemetry warning when the stream produced no
        visible text despite carrying answer content — so a silent OAIC turn is
        always explainable from the logs (never an unobservable empty response)."""
        s = self._state
        if not s.content_begun and (s.content_patches_off_channel or s.assistant_msgs_skipped):
            logger.warning(
                "oaic intake produced NO text after %d frame(s): "
                "content_patches_off_channel=%d assistant_msgs_skipped=%d final_channel=%s conv=%s "
                "— the visible answer was not on the tracked channel",
                s.frames_seen,
                s.content_patches_off_channel,
                s.assistant_msgs_skipped,
                s.final_channel,
                s.conversation_id[:8] or "?",
            )
        return []

    def _drain_events(self) -> Iterator[Any]:
        """Normalise each complete SSE frame into a dispatch envelope."""
        for frame in self._split_sse_frames():
            envelope = _parse_frame(frame)
            if envelope is not None:
                yield envelope


def _parse_frame(frame: bytes) -> Any:
    """Parse one SSE frame bytes into a dispatch envelope.

    Returns one of :class:`_AddEnvelope`, :class:`_PatchEnvelope`,
    :class:`_TypedSideEvent`, :class:`_DoneEvent`, :class:`_TimeoutEvent`, or
    ``None`` (silently dropped: encoding banner, keepalive comment,
    un-parseable data).
    """
    event_name: str | None = None
    data_lines: list[str] = []

    for raw_line in frame.split(b"\n"):
        line = raw_line.rstrip(b"\r").decode("utf-8", errors="replace")
        if line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())

    data = "\n".join(data_lines).strip()
    if not data:
        return None

    # Encoding banner: drop silently.
    if event_name == "delta_encoding":
        return None

    if data == "[DONE]":
        return _DoneEvent()

    try:
        parsed = json.loads(data)
    except (json.JSONDecodeError, ValueError):
        # Non-empty, non-[DONE] data we cannot parse — log so an unexpected wire
        # encoding (e.g. a non-SSE encoded_item) is never dropped unobserved.
        logger.debug("oaic intake: dropped unparseable SSE data frame (%d bytes): %.160r", len(data), data)
        return None

    if not isinstance(parsed, dict):
        logger.debug("oaic intake: dropped non-object SSE data frame: %.160r", data)
        return None

    # Typed side event: ``{type, ...}`` without ``p`` or ``v`` fields.
    kind = parsed.get("type")
    if isinstance(kind, str) and "v" not in parsed and "p" not in parsed:
        if kind == CCPROXY_IDLE_TIMEOUT_EVENT_TYPE:
            idle_seconds = parsed.get("idle_seconds")
            return _TimeoutEvent(idle_seconds=idle_seconds if isinstance(idle_seconds, (int, float)) else None)
        return _TypedSideEvent(kind=kind, raw=parsed)

    channel = _channel_from_frame(parsed)
    patches = _parse_delta(parsed)
    if not patches:
        return None

    # A single add-at-root with a channel declaration → AddEnvelope.
    if len(patches) == 1:
        path, op, value = patches[0]
        if path == "" and op == "add" and isinstance(value, dict):
            ch = channel if channel is not None else 0
            return _AddEnvelope(channel=ch, value=value)

    return _PatchEnvelope(channel=channel, patches=patches)
