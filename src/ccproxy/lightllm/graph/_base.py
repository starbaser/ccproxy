"""Shared base classes for the response-side intake and render FSMs.

Every ``*_intake.py`` module pairs a mutable pydantic-graph state dataclass
with a public FSM wrapper that buffers SSE bytes, frames them, and drives the
module's graph; every ``*_render.py`` module pairs a state dataclass with a
wrapper that pushes one IR event per :meth:`render` call. The provider-specific
parts are the wire payload parsing and the graph topology — everything else
(state slots, buffering, SSE framing, the funnel interface, telemetry
warnings) is identical across modules and lives here.

The funnel contract (see ``docs/lightllm.md`` § "Response-side conventions")
is guaranteed by these bases: every intake exposes ``usage`` / ``raw_extras``
/ ``finish_reason`` / ``provider_response_id`` — populated where the wire
reports them, inert otherwise — so the render seams re-stamp token accounting
and carried-through metadata without per-provider ``getattr`` plumbing.

Telemetry helpers log through ``logging.getLogger(type(self).__module__)`` so
warning records keep the concrete module's logger name
(``ccproxy.lightllm.graph.<provider>_intake``), not this module's.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

# Private pydantic-ai import — the intakes drive the parts manager directly;
# see the matching note in the intake modules.
from pydantic_ai._parts_manager import ModelResponsePartsManager
from pydantic_ai.messages import ModelResponseStreamEvent
from pydantic_ai.usage import RequestUsage

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from pydantic_ai.messages import FinishReason
    from pydantic_ai.models import ModelRequestParameters
    from pydantic_graph.graph_builder import Graph


# ── Intake ──────────────────────────────────────────────────────────────────


@dataclass(kw_only=True)
class IntakeState[EventT]:
    """Shared FSM state for one intake graph run.

    ``events_queue`` holds the typed events / dispatch envelopes drained from
    the SSE buffer *before* each graph run; the module's router pops from it.
    ``out_events`` accumulates the IR events emitted by handler steps; the
    terminal step drains and returns it. ``parts_manager`` persists across
    feed calls so multi-feed reassembly works.
    """

    parts_manager: ModelResponsePartsManager
    events_queue: deque[EventT] = field(default_factory=deque)
    out_events: list[ModelResponseStreamEvent] = field(default_factory=list)

    # ── Funnel: response usage + carried-through metadata ──────────────────
    # Ubiquitous slots — populated where the wire reports them, inert (empty /
    # None) where it doesn't, so the funnel interface stays homogeneous.
    usage: RequestUsage = field(default_factory=RequestUsage)
    """Token usage accumulated off the stream — the slot pydantic-ai keeps on
    ``StreamedResponse._usage`` and ccproxy otherwise drops when driving
    ``ModelResponsePartsManager`` directly."""

    raw_extras: dict[str, Any] = field(default_factory=dict)
    """Unmodeled response metadata carried through rather than dropped."""

    finish_reason: FinishReason | None = None
    """Finish reason mapped off the wire, when the protocol reports one."""

    provider_response_id: str | None = None
    """Upstream response id, when the protocol reports one."""

    # ── Telemetry (never-silently-drop diagnostics) ─────────────────────────
    frames_seen: int = 0
    """Total typed events / dispatch envelopes drained from the wire (across all feed calls)."""

    frames_unparseable: int = 0
    """SSE frames whose payload failed validation into a typed event."""

    emitted_events: int = 0
    """Total IR ``ModelResponseStreamEvent`` instances emitted (across all feed calls)."""


class ResponseIntakeFSM[StateT: IntakeState[Any]](ABC):
    """Shared wrapper driving one intake graph per :meth:`feed` call.

    Subclasses provide the graph (``_graph`` class attribute), the state
    (:meth:`_initial_state`), and the wire payload parsing
    (:meth:`_drain_events`, built on :meth:`_split_sse_frames`). Everything
    else — byte tee, SSE buffering, queue/telemetry bookkeeping, the funnel
    accessors — is shared.
    """

    name: ClassVar[str]
    _graph: ClassVar[Graph[Any, Any, Any, list[ModelResponseStreamEvent]]]

    def __init__(self, *, model: str, request_params: ModelRequestParameters) -> None:
        self._request_params = request_params
        self._sse_buffer = bytearray()
        self.upstream_raw_bytes = bytearray()
        self._terminated = False
        self._state: StateT = self._initial_state(model=model, request_params=request_params)

    @abstractmethod
    def _initial_state(self, *, model: str, request_params: ModelRequestParameters) -> StateT:
        """Build the provider-specific state dataclass for a fresh stream."""

    @abstractmethod
    def _drain_events(self) -> Iterator[Any]:
        """Frame and parse SSE bytes from the buffer into queue events.

        Implementations iterate :meth:`_split_sse_frames` and yield whatever
        their graph's router dispatches on (typed SDK events or dispatch
        envelopes), counting unparseable payloads into
        ``state.frames_unparseable``. Terminator-aware wires ([``DONE``])
        set ``self._terminated`` and return; frames already sliced off the
        buffer stay consumed, later ones stay buffered-but-dead.
        """

    @property
    def parts_manager(self) -> ModelResponsePartsManager:
        """Expose the underlying parts manager for tests and downstream renderers."""
        return self._state.parts_manager

    @property
    def usage(self) -> RequestUsage:
        """Token usage accumulated off the stream (funnel accessor)."""
        return self._state.usage

    @property
    def raw_extras(self) -> dict[str, Any]:
        """Unmodeled response metadata carried through the funnel."""
        return self._state.raw_extras

    @property
    def finish_reason(self) -> FinishReason | None:
        """Finish reason captured off the stream, when the wire reports one."""
        return self._state.finish_reason

    @property
    def provider_response_id(self) -> str | None:
        """Upstream response id captured off the stream, when the wire reports one."""
        return self._state.provider_response_id

    @property
    def state(self) -> StateT:
        """Expose FSM state for tests and telemetry inspection."""
        return self._state

    async def feed(self, data: bytes) -> list[ModelResponseStreamEvent]:
        """Buffer bytes, frame SSE events, drive the FSM, return emitted IR events."""
        self.upstream_raw_bytes.extend(data)
        if self._terminated or not data:
            return []
        self._sse_buffer.extend(data)
        for event in self._drain_events():
            self._state.events_queue.append(event)
            self._state.frames_seen += 1
        # No complete frames yet — the graph run would produce no events.
        if not self._state.events_queue:
            return []
        result: list[ModelResponseStreamEvent] = await self._graph.run(state=self._state)
        return result

    async def close(self) -> list[ModelResponseStreamEvent]:
        """Stream end. Nothing to flush by default; log a silent-empty stream."""
        self._log_no_ir_events()
        return []

    def _split_sse_frames(self) -> Iterator[bytes]:
        """Frame SSE events out of ``self._sse_buffer``.

        Handles both ``\\r\\n\\r\\n`` (industry standard) and ``\\n\\n`` (some
        servers) separators, taking whichever boundary appears first; partial
        frames remain buffered for the next :meth:`feed` call. Each frame is
        deleted from the buffer *before* it is yielded, so a consumer that
        stops mid-iteration (e.g. on a ``[DONE]`` sentinel) leaves later
        frames buffered.
        """
        while True:
            crlf = self._sse_buffer.find(b"\r\n\r\n")
            lf = self._sse_buffer.find(b"\n\n")
            if crlf == -1 and lf == -1:
                return
            if crlf != -1 and (lf == -1 or crlf < lf):
                sep_idx, sep_len = crlf, 4
            else:
                sep_idx, sep_len = lf, 2
            frame = bytes(self._sse_buffer[:sep_idx])
            del self._sse_buffer[: sep_idx + sep_len]
            yield frame

    def _log_no_ir_events(self, *, unit: str = "frame", extra: str = "") -> None:
        """Warn when the stream carried events but produced no IR output.

        A silent empty turn must be explainable from logs. Logs through the
        concrete module's logger so caplog filters keep working per provider.
        """
        s = self._state
        if not s.frames_seen or s.emitted_events:
            return
        logging.getLogger(type(self).__module__).warning(
            "%s intake produced NO IR events after %d %s(s) (unparseable=%d%s) "
            "— the upstream stream carried no renderable content",
            self.name,
            s.frames_seen,
            unit,
            s.frames_unparseable,
            extra,
        )


# ── Render ──────────────────────────────────────────────────────────────────


@dataclass(kw_only=True)
class RenderState:
    """Shared FSM state for one render graph run.

    ``pending_events`` holds the single :class:`ModelResponseStreamEvent`
    pushed by :meth:`ResponseRenderFSM.render` before each graph run; the
    module's router pops from it. ``out`` accumulates the SSE wire bytes
    emitted by handler steps; the terminal step returns ``bytes(out)`` and
    resets the buffer so the same state can drive the next render call.
    """

    model: str
    pending_events: deque[ModelResponseStreamEvent] = field(default_factory=deque)
    out: bytearray = field(default_factory=bytearray)

    # ── Telemetry (never-silently-drop diagnostics) ─────────────────────────
    events_received: int = 0
    """Total IR events dispatched through the render FSM (across all render calls)."""

    bytes_emitted: int = 0
    """Total content-SSE bytes emitted by render steps (excludes the close terminator)."""


class ResponseRenderFSM[StateT: RenderState](ABC):
    """Shared wrapper driving one render graph per :meth:`render` call.

    Subclasses provide the graph (``_graph`` class attribute), the state
    (:meth:`_initial_state`), and the wire-specific :meth:`close` terminator.
    """

    name: ClassVar[str]
    _graph: ClassVar[Graph[Any, Any, Any, bytes]]

    def __init__(self, *, model: str = "unknown") -> None:
        self._state: StateT = self._initial_state(model=model)

    @abstractmethod
    def _initial_state(self, *, model: str) -> StateT:
        """Build the listener-specific state dataclass for a fresh stream."""

    @property
    def state(self) -> StateT:
        """Expose FSM state for tests and telemetry inspection."""
        return self._state

    async def render(self, event: ModelResponseStreamEvent) -> bytes:
        """One IR event → zero-or-more bytes of listener SSE wire output."""
        self._state.pending_events.append(event)
        result: bytes = await self._graph.run(state=self._state)
        return result

    @abstractmethod
    async def close(
        self,
        *,
        usage: RequestUsage | None = None,
        raw_extras: Mapping[str, object] | None = None,
        finish_reason: FinishReason | None = None,
    ) -> bytes:
        """Emit the wire-specific end-of-stream terminator.

        ``usage`` / ``raw_extras`` / ``finish_reason`` are the intake's funnel
        captures, re-stamped into the terminator where the listener wire has a
        slot for them. ``finish_reason`` in particular is what tells the client
        the turn hit a token ceiling, tripped a content filter, or died on an
        upstream error rather than completing — see :mod:`_finish_reason` for
        the per-listener projection.
        """

    def _log_silent_close(self) -> None:
        """Warn when IR events arrived but no content bytes were rendered.

        A silent empty response must be explainable from logs. Logs through
        the concrete module's logger so caplog filters keep working per
        listener format.
        """
        s = self._state
        if not s.events_received or s.bytes_emitted:
            return
        logging.getLogger(type(self).__module__).warning(
            "%s render received %d IR event(s) but emitted NO content bytes "
            "before close — every event mapped to a no-op wire surface",
            self.name,
            s.events_received,
        )
