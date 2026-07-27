"""Sync ``flow.response.stream`` callable backed by a persistent asyncio loop.

The graph-side replacement for
:class:`ccproxy.lightllm.response.pipeline.SSEPipeline` (sync). The intakes /
renderers under :mod:`ccproxy.lightllm.graph` are async (each chunk drives one
``await graph.run(...)``), but mitmproxy installs sync callables on
``flow.response.stream``. This pipeline owns one daemon thread + one
:class:`asyncio.AbstractEventLoop` per instance and submits each chunk via
:func:`asyncio.run_coroutine_threadsafe`, paying ~10-50 µs of cross-thread
hop per chunk against an upstream-network-bound 10-100 ms-per-chunk floor.

Compare to the pathological pattern Phase Q replaces: the
``_GoogleSyncIntake`` / ``_PerplexitySyncIntake`` adapters in
``response/intake.py`` spawn one fresh ``asyncio.new_event_loop()`` per
``feed`` call — ~200 chunks in a 5-second stream means 200 fresh loops, each
allocating its own selectors, signal handlers, and task graph.

Exception handling: failures inside ``intake.feed()`` or ``render.render()``
are caught and the offending chunk is passed through unmodified so mitmproxy
doesn't stall. Catastrophic failures in :meth:`close` still emit the render's
terminator so the client sees a well-formed end-of-stream.

Lifecycle: the daemon thread dies with the process, so a missed
:meth:`close` won't leak — but explicit cleanup on
:meth:`InspectorAddon.response` / the ``done`` mitmproxy event is preferred
so the loop tears down promptly when a flow finishes.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from concurrent.futures import Future
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pydantic_ai.messages import ModelResponsePart

    from ccproxy.lightllm.graph import AnyAsyncIntakeFSM, AnyAsyncRenderFSM

logger = logging.getLogger(__name__)


class SSEPipeline:
    """Sync mitmproxy stream callable bridging upstream SSE → listener SSE.

    Drives an async intake FSM + render FSM pair via a persistent asyncio loop
    in a dedicated daemon thread. Behavioral contract matches the legacy sync
    :class:`ccproxy.lightllm.response.pipeline.SSEPipeline`:

    * ``__call__(bytes) -> bytes | list[bytes]`` returns the rendered chunk;
      ``[]`` when nothing was emitted (no-op chunk like an incomplete SSE
      frame), ``bytes`` otherwise.
    * Empty ``data`` (``b""``) is mitmproxy's end-of-stream sentinel — drains
      the intake's :meth:`close`, renders any trailing IR events, then emits
      the render's :meth:`close` terminator.
    * :attr:`upstream_raw_bytes` byte-for-byte tee of every chunk fed in.
    * :attr:`raw_body` alias of :attr:`upstream_raw_bytes` (old
      ``SSETransformer`` callsites — e.g. :class:`PerplexityAddon`).
    * :meth:`close` explicit cleanup. Idempotent.

    **Streaming vs collect mode.** Pass ``render`` for the streaming default:
    each IR event is rendered to a listener SSE chunk as it arrives, and EOS
    emits the render's terminator. Pass ``buffered_render`` instead for a
    client that asked for a single buffered object while the upstream is still
    intrinsically streamed (e.g. ``openai_conversations``, where the answer may
    arrive over a WebSocket handoff): every chunk feeds the intake but emits
    nothing, and EOS assembles the complete IR parts into one listener-format
    JSON object via the supplied callable. Exactly one of ``render`` /
    ``buffered_render`` must be set.
    """

    def __init__(
        self,
        *,
        intake: AnyAsyncIntakeFSM,
        render: AnyAsyncRenderFSM | None = None,
        buffered_render: Callable[[list[ModelResponsePart]], bytes] | None = None,
    ) -> None:
        if (render is None) == (buffered_render is None):
            raise ValueError("SSEPipeline requires exactly one of render / buffered_render")
        self._intake = intake
        self._render = render
        self._buffered_render = buffered_render
        self._closed = False
        self._terminator_emitted = False
        self._content_bytes_emitted = 0
        """Telemetry: rendered content bytes emitted before EOS (excludes the terminator)."""
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever,
            daemon=True,
            name="ccproxy-sse-loop",
        )
        self._thread.start()

    def __call__(self, data: bytes) -> bytes | list[bytes]:
        if data == b"":
            return self._flush_and_close()

        if self._closed:
            # The loop has been torn down; pass the chunk through so we don't
            # silently drop bytes.
            logger.debug("SSEPipeline: chunk received after close; passing through")
            return data

        try:
            future: Future[bytes] = asyncio.run_coroutine_threadsafe(self._process_chunk(data), self._loop)
            out = future.result()
        except Exception:
            logger.exception("SSEPipeline.feed failed mid-stream; passing chunk through")
            return data
        return out if out else []

    async def _process_chunk(self, data: bytes) -> bytes:
        """Drive one chunk through the intake. Runs on the persistent loop.

        Streaming mode renders each IR event to a listener SSE chunk. Collect
        mode only accumulates IR into the intake's parts manager and emits
        nothing until EOS.
        """
        if self._buffered_render is not None:
            await self._intake.feed(data)
            return b""
        assert self._render is not None  # guarded by __init__
        out = bytearray()
        for event in await self._intake.feed(data):
            out.extend(await self._render.render(event))
        self._content_bytes_emitted += len(out)
        return bytes(out)

    def _flush_and_close(self) -> bytes | list[bytes]:
        """Drain the intake, emit the terminator (or buffered object), tear down the loop."""
        if self._closed:
            return []

        out = bytearray()

        if self._loop.is_running():
            drain = self._drain_and_collect if self._buffered_render is not None else self._drain_and_terminate
            try:
                future: Future[bytes] = asyncio.run_coroutine_threadsafe(drain(), self._loop)
                out.extend(future.result())
            except Exception:
                logger.exception("SSEPipeline.close failed mid-drain; emitting render terminator only")
                # Fall through: still try to emit the render terminator below.

        # Tear down the loop regardless. ``self._closed`` is the gate for
        # idempotency; once True, further ``__call__`` invocations no-op.
        self._closed = True
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=1.0)
        except Exception:
            logger.exception("SSEPipeline: failed to tear down persistent loop")

        return bytes(out) if out else []

    async def _drain_and_terminate(self) -> bytes:
        """Async tail: ``intake.close()`` → render each trailing event → ``render.close()``."""
        assert self._render is not None  # guarded by __init__ (streaming mode)
        out = bytearray()
        try:
            for event in await self._intake.close():
                rendered = await self._render.render(event)
                self._content_bytes_emitted += len(rendered)
                out.extend(rendered)
        except Exception:
            logger.exception("SSEPipeline intake.close failed; emitting render terminator only")
        if not self._content_bytes_emitted:
            logger.warning(
                "SSEPipeline EOS produced NO content bytes for intake=%s render=%s "
                "— only the SSE terminator will be sent (empty response to the client)",
                self._intake.name,
                self._render.name,
            )
        if not self._terminator_emitted:
            self._terminator_emitted = True
            try:
                # Funnel: hand the intake's accumulated usage, carried-through
                # metadata, and captured finish reason to the render terminator
                # so the token accounting AND the reason the turn ended — the
                # difference between a completed turn and one truncated by a
                # token ceiling, a content filter, or an upstream error — are
                # re-stamped instead of dropped by the cross-format transform.
                out.extend(
                    await self._render.close(
                        usage=self._intake.usage,
                        raw_extras=self._intake.raw_extras,
                        finish_reason=self._intake.finish_reason,
                    )
                )
            except Exception:
                logger.exception("SSEPipeline render.close failed; no terminator emitted")
        return bytes(out)

    async def _drain_and_collect(self) -> bytes:
        """Collect-mode tail: ``intake.close()`` flushes trailing parts, then the
        complete IR parts are assembled into one listener-format buffered object.

        Never calls ``render.close()`` — a buffered object carries no SSE
        terminator, so emitting ``[DONE]`` / ``message_stop`` here would corrupt
        the single JSON body.
        """
        assert self._buffered_render is not None  # guarded by __init__ (collect mode)
        try:
            await self._intake.close()
        except Exception:
            logger.exception("SSEPipeline intake.close failed in collect mode; rendering parts seen so far")
        if self._terminator_emitted:
            return b""
        self._terminator_emitted = True
        parts = list(self._intake.parts_manager.get_parts())
        if not parts:
            logger.warning(
                "SSEPipeline collect mode assembled NO parts for intake=%s — the buffered "
                "object sent to the client carries no content (empty response)",
                self._intake.name,
            )
        try:
            return self._buffered_render(parts)
        except Exception:
            logger.exception("SSEPipeline buffered_render failed; emitting empty object")
            return b"{}"

    def close(self) -> None:
        """Explicit cleanup. Idempotent. Tears down the persistent loop.

        Does NOT emit a terminator — that's the EOS path. Use this when a
        flow is being abandoned (client disconnect, mitmproxy ``done`` event)
        and the bytes are no longer being delivered.
        """
        if self._closed:
            return
        self._closed = True
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=1.0)
        except Exception:
            logger.exception("SSEPipeline.close: failed to tear down persistent loop")

    @property
    def upstream_raw_bytes(self) -> bytes:
        """Byte-for-byte tee of every chunk fed in (for pplx_addon etc.)."""
        return bytes(self._intake.upstream_raw_bytes)

    @property
    def raw_body(self) -> bytes:
        """Alias of :attr:`upstream_raw_bytes` for old ``SSETransformer.raw_body`` callsites."""
        return self.upstream_raw_bytes
