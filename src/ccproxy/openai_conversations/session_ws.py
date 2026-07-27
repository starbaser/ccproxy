"""Persistent session-scoped WebSocket manager for the conduit handoff.

The chatgpt.com SPA keeps ONE persistent ``/celsius/ws/user`` WebSocket open per
session — the ``websocket_url`` is cached ~25 minutes, the socket reconnects
indefinitely, the init topics (``conversations`` / ``app_notifications`` /
``calpico-chatgpt``) are subscribed once up front, and each deferred turn
subscribes a per-turn ``conversation-turn-<id>`` topic on that SAME socket when
the HTTP body hands off. ccproxy emulates that here: one wss connection per
session, reused across turns and across separate client requests within the
token window, with each turn's frames multiplexed to that turn's response.

This builds directly on the proven Increment-1 primitives in
:mod:`ccproxy.openai_conversations.ws_handoff` — :func:`fetch_ws_url` (the
authenticated GET that reuses the forwarded ``/f/conversation`` headers),
:func:`_walk_sse_items` (envelope-agnostic structural extraction), and the
``[DONE]`` / ``done`` end-of-stream detectors. It does NOT reimplement them.

Concurrency model: everything runs on the sidecar's asyncio event loop. Each
:class:`_SessionConn` owns one live connection and a single background read-loop
task that fans inbound frames out to per-turn ``asyncio.Queue`` objects keyed by
turn topic. Multiple concurrent turns share one socket; a connect lock prevents
two simultaneous first-turns from double-dialing. A turn's response ends on its
structural ``[DONE]`` / ``done`` (or a bounded per-turn give-up) while the socket
stays open for reuse — two different clocks.

Robustness: any failure on the persistent path (dial fails, URL fetch fails,
socket dies mid-handshake) falls back to the per-turn
:func:`ccproxy.openai_conversations.ws_handoff.run_handoff_bridge`, and on a hard
failure of that too the turn simply yields nothing — the same contract as the
per-turn bridge: a continuation never raises into the sidecar ``body_stream``.

The session key is derived from the forwarded request headers — the
``__Secure-next-auth.session-token`` cookie value when present, else the bearer
``authorization`` value — and is **always hashed** before use; the raw token is
never logged or stored as a dict key.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
from collections.abc import AsyncIterator

import httpx
from websockets.asyncio.client import ClientConnection
from websockets.asyncio.client import connect as ws_connect

from ccproxy.openai_conversations.ws_capture import get_ws_capture
from ccproxy.openai_conversations.ws_handoff import (
    _WS_CHATGPT_ORIGIN,
    _WS_PING_INTERVAL,
    _WS_USER_AGENT,
    _as_sse_bytes,
    _get_turn_idle_timeout_seconds,
    _idle_timeout_frame,
    _is_done_item,
    _message_signals_done,
    _parse_ws_frames,
    _topic_matches,
    _walk_sse_items,
    fetch_ws_url,
    run_handoff_bridge,
)

logger = logging.getLogger(__name__)

# The websocket_url returned by /celsius/ws/user is valid for ~25 minutes in the
# real SPA (``vtt=gtt(_tt,15e5)``). Re-fetch the URL and redial once a connection
# is older than this, or whenever the socket is found dead.
_WS_URL_TTL_SECONDS = 25.0 * 60.0

# Bounded per-turn give-up: a turn that never receives a structural [DONE] must
# not hang the client forever. This is scoped to the TURN, not the socket — the
# socket stays open for reuse; only this turn's response ends. It is the
# turn-response analogue of the per-message idle backstop in ws_handoff — both
# read the shared ``turn_idle_timeout_seconds`` config value via
# :func:`_get_turn_idle_timeout_seconds`. A give-up yields the typed
# idle-timeout terminal frame (:func:`_idle_timeout_frame`) before returning,
# so the truncation is distinguishable from a genuine ``[DONE]`` completion.

# Open timeout for the wss dial.
_WS_OPEN_TIMEOUT = 15.0


def _session_key(request_headers: dict[str, str]) -> str:
    """Derive a stable, hashed session key from the forwarded headers.

    Prefers the ``__Secure-next-auth.session-token`` cookie value; falls back to
    the bearer ``authorization`` value. The credential is **always** SHA-256
    hashed (truncated) so the raw token never becomes a dict key or appears in
    logs. Returns ``""`` when neither credential is present (the caller then
    declines the persistent path).

    Args:
        request_headers: The forwarded ``/f/conversation`` headers.

    Returns:
        A 16-hex-char session key, or ``""`` when no credential is available.
    """
    cookie_token = _session_token_from_cookie(request_headers)
    secret = cookie_token or _bearer_value(request_headers)
    if not secret:
        return ""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]


def _session_token_from_cookie(request_headers: dict[str, str]) -> str:
    """Extract the ``__Secure-next-auth.session-token`` cookie value, or ``""``."""
    for name, value in request_headers.items():
        if name.lower() != "cookie":
            continue
        for part in value.split(";"):
            crumb = part.strip()
            if crumb.startswith("__Secure-next-auth.session-token="):
                return crumb.split("=", 1)[1]
    return ""


def _bearer_value(request_headers: dict[str, str]) -> str:
    """Extract the ``authorization`` header value, or ``""``."""
    for name, value in request_headers.items():
        if name.lower() == "authorization":
            return value
    return ""


# Aurora init array: connect + subscribe the three always-on session topics.
# Subscribed exactly once when the session socket is first dialed (mirrors the
# SPA's session-init subscription), shared across every turn on the socket.
def _init_message() -> str:
    return json.dumps(
        [
            {"id": 1, "command": {"type": "connect", "presence": {"type": "presence", "state": "background"}}},
            {"id": 2, "command": {"type": "subscribe", "topic_id": "calpico-chatgpt"}},
            {"id": 3, "command": {"type": "subscribe", "topic_id": "conversations"}},
            {"id": 4, "command": {"type": "subscribe", "topic_id": "app_notifications"}},
        ]
    )


def _subscribe_message(*, topic_id: str, sub_id: int) -> str:
    """Build a per-turn subscribe frame (``offset:"0"`` → full history catch-up)."""
    return json.dumps([{"id": sub_id, "command": {"type": "subscribe", "topic_id": topic_id, "offset": "0"}}])


def _unsubscribe_message(*, topic_id: str, sub_id: int) -> str:
    """Build a per-turn unsubscribe frame, sent best-effort when a turn ends."""
    return json.dumps([{"id": sub_id, "command": {"type": "unsubscribe", "topic_id": topic_id}}])


class _SessionConn:
    """One persistent wss connection for a single session, on the sidecar loop.

    Owns the live ``websockets`` connection, the single background read-loop
    task, and the per-turn topic → queue routing table. A turn registers its
    topic, drains its queue until the ``None`` end-of-stream sentinel, then
    unregisters — the socket is NOT closed (it is reused by later turns).
    """

    def __init__(self, *, session_key: str) -> None:
        self._session_key = session_key
        self._ws_url: str = ""
        self._fetched_at: float = 0.0
        self._ws: ClientConnection | None = None
        self._read_task: asyncio.Task[None] | None = None
        self._ping_task: asyncio.Task[None] | None = None
        self._topics: dict[str, asyncio.Queue[bytes | None]] = {}
        self._lock = asyncio.Lock()
        self._sub_counter = 4  # init array used ids 1..4; per-turn subs start at 5.

    @property
    def alive(self) -> bool:
        """True when the socket is connected and the read loop is running."""
        return self._ws is not None and self._read_task is not None and not self._read_task.done()

    async def stream_turn(
        self,
        *,
        topic_id: str,
        client: httpx.AsyncClient,
        request_headers: dict[str, str],
    ) -> AsyncIterator[bytes]:
        """Subscribe ``topic_id`` on the shared socket and yield its SSE bytes.

        Lazily connects (dial + init handshake + read loop) when the session has
        no live socket, reusing an existing one otherwise. Registers a queue for
        the turn, sends the per-turn subscribe, drains the queue until the
        end-of-stream sentinel, then unregisters WITHOUT closing the socket.

        Args:
            topic_id: The per-turn ``conversation-turn-<id>`` WS topic.
            client: Authenticated httpx client for the ``/celsius/ws/user`` GET.
            request_headers: Forwarded ``/f/conversation`` headers for the GET.

        Yields:
            SSE ``data: …`` bytes for this turn, ending on the structural
            ``[DONE]`` / ``done`` signal, or on the bounded per-turn give-up —
            which yields the typed idle-timeout terminal frame
            (:func:`~ccproxy.openai_conversations.ws_handoff._idle_timeout_frame`)
            first, so the truncation is distinguishable from a genuine
            completion.
        """
        await self._ensure_connected(client=client, request_headers=request_headers)

        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._topics[topic_id] = queue
        sub_id = self._next_sub_id()
        idle_timeout = _get_turn_idle_timeout_seconds()
        try:
            await self._send(_subscribe_message(topic_id=topic_id, sub_id=sub_id))
            logger.debug("session_ws: turn subscribed topic=%s (session=%s)", topic_id, self._session_key)
            yielded = 0
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=idle_timeout)
                except TimeoutError:
                    logger.warning(
                        "session_ws: turn idle give-up after %.0fs (topic=%s, no [DONE])",
                        idle_timeout,
                        topic_id,
                    )
                    yield _idle_timeout_frame(idle_seconds=idle_timeout)
                    return
                if item is None:
                    logger.debug("session_ws: turn EOS topic=%s yielded=%d", topic_id, yielded)
                    return
                yielded += 1
                yield item
        finally:
            self._topics.pop(topic_id, None)
            with contextlib.suppress(Exception):
                await self._send(_unsubscribe_message(topic_id=topic_id, sub_id=self._next_sub_id()))

    async def _ensure_connected(
        self,
        *,
        client: httpx.AsyncClient,
        request_headers: dict[str, str],
    ) -> None:
        """Dial + handshake + start the read loop if no live socket exists.

        Guarded by :attr:`_lock` so two simultaneous first-turns do not
        double-dial. Re-fetches the ``websocket_url`` and redials when the cached
        URL is stale (older than :data:`_WS_URL_TTL_SECONDS`) or the socket is
        dead.
        """
        async with self._lock:
            if self.alive and not self._url_stale():
                return
            await self._teardown_locked()
            ws_url = await self._resolve_ws_url(client=client, request_headers=request_headers)
            ws = await ws_connect(
                ws_url,
                additional_headers={"User-Agent": _WS_USER_AGENT, "Origin": _WS_CHATGPT_ORIGIN},
                ping_interval=None,
                open_timeout=_WS_OPEN_TIMEOUT,
            )
            await ws.send(_init_message())
            self._ws = ws
            self._read_task = asyncio.create_task(
                self._read_loop(ws=ws), name=f"ccproxy-session-ws-{self._session_key}"
            )
            self._ping_task = asyncio.create_task(
                self._ping_loop(ws=ws), name=f"ccproxy-session-ws-ping-{self._session_key}"
            )
            logger.debug("session_ws: connected session=%s url=%s…", self._session_key, ws_url[:60])

    async def _ping_loop(self, *, ws: ClientConnection) -> None:
        """Keep the persistent socket alive across idle gaps (aurora: 25s).

        The socket is reused across turns within the ``websocket_url`` window, so
        it must be pinged during the idle stretches between turns or the server
        drops it. Exits quietly once the socket is gone.
        """
        while True:
            await asyncio.sleep(_WS_PING_INTERVAL)
            try:
                await ws.ping()
            except Exception:
                return

    async def _resolve_ws_url(
        self,
        *,
        client: httpx.AsyncClient,
        request_headers: dict[str, str],
    ) -> str:
        """Fetch and cache the ``websocket_url`` when stale; reuse otherwise."""
        if self._ws_url and not self._url_stale():
            return self._ws_url
        self._ws_url = await fetch_ws_url(client=client, request_headers=request_headers)
        self._fetched_at = time.monotonic()
        return self._ws_url

    def _url_stale(self) -> bool:
        return (time.monotonic() - self._fetched_at) > _WS_URL_TTL_SECONDS

    def _next_sub_id(self) -> int:
        self._sub_counter += 1
        return self._sub_counter

    async def _send(self, message: str) -> None:
        ws = self._ws
        if ws is None:
            raise RuntimeError("session_ws: socket not connected")
        await ws.send(message)

    async def _read_loop(self, *, ws: ClientConnection) -> None:
        """Single fan-out read loop for the session socket.

        Reads every inbound message, captures it (never dropped), structurally
        extracts each ``(item_topic, sse)`` pair, and routes each to every
        registered turn whose topic matches (per :func:`_topic_matches`). A
        structural ``[DONE]`` or ``done`` for a registered topic pushes the
        ``None`` end-of-stream sentinel into that turn's queue. On socket close
        or read error, ALL registered queues receive the sentinel so no turn
        hangs, and the connection is marked dead for the next reconnect.
        """
        capture = get_ws_capture()
        try:
            async for raw_msg in ws:
                raw_text = raw_msg if isinstance(raw_msg, str) else raw_msg.decode("utf-8", errors="replace")
                forwarded_here = self._route_message(raw_text)
                capture.record(topic=self._session_key, raw=raw_text, forwarded=forwarded_here)
                logger.debug("session_ws: RAWFRAME %s", raw_text)
        except Exception as exc:
            logger.debug("session_ws: read loop ended (session=%s): %s", self._session_key, exc)
        finally:
            self._drain_all_eos()
            self._ws = None
            self._read_task = None

    def _route_message(self, raw_text: str) -> int:
        """Fan one inbound message out to the matching turn queues.

        Returns the number of SSE items forwarded to any turn (for the capture
        sink's ``forwarded`` count). For each registered turn topic, matching
        items are enqueued as SSE bytes and a structural end-of-stream enqueues
        the ``None`` sentinel.
        """
        frames = _parse_ws_frames(raw_text.encode())
        forwarded = 0
        for topic_id, queue in list(self._topics.items()):
            done = False
            for frame in frames:
                for item_topic, sse in _walk_sse_items(frame):
                    if not _topic_matches(item_topic, topic_id):
                        continue
                    queue.put_nowait(_as_sse_bytes(sse))
                    forwarded += 1
                    if _is_done_item(sse):
                        done = True
                if not done and _message_signals_done(frame, turn_topic=topic_id):
                    done = True
            if done:
                queue.put_nowait(None)
        return forwarded

    def _drain_all_eos(self) -> None:
        """Push the end-of-stream sentinel into every registered turn queue."""
        for queue in self._topics.values():
            with contextlib.suppress(Exception):
                queue.put_nowait(None)

    async def close(self) -> None:
        """Cancel the read task and close the socket (shutdown / reconnect)."""
        async with self._lock:
            await self._teardown_locked()

    async def _teardown_locked(self) -> None:
        """Cancel the read + ping tasks, close the socket, drain all turns.

        The connection lock is held by the caller.
        """
        self._drain_all_eos()
        read_task = self._read_task
        ping_task = self._ping_task
        ws = self._ws
        self._read_task = None
        self._ping_task = None
        self._ws = None
        for task in (read_task, ping_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()


class SessionWSManager:
    """Process-wide manager of persistent per-session conduit WebSockets.

    Keyed on the hashed session credential (cookie / bearer). Each session owns
    one :class:`_SessionConn`; turns multiplex over it. The persistent path falls
    back to the per-turn bridge on any failure so a conduit turn never hangs.
    """

    def __init__(self) -> None:
        self._conns: dict[str, _SessionConn] = {}
        self._lock = asyncio.Lock()

    async def stream_turn(
        self,
        *,
        topic_id: str,
        client: httpx.AsyncClient,
        request_headers: dict[str, str],
    ) -> AsyncIterator[bytes]:
        """Stream one conduit turn over the session's persistent socket.

        Resolves (or lazily creates) the session connection, multiplexes the
        turn's frames off the shared read loop, and yields the turn's SSE bytes.
        On ANY failure of the persistent path, transparently falls back to the
        per-turn :func:`run_handoff_bridge`; on a hard failure there too, yields
        nothing. Never raises into the sidecar ``body_stream``.

        Args:
            topic_id: The per-turn ``conversation-turn-<id>`` WS topic.
            client: Authenticated httpx client for the ``/celsius/ws/user`` GET.
            request_headers: Forwarded ``/f/conversation`` headers.

        Yields:
            The turn's SSE ``data: …`` bytes.
        """
        session_key = _session_key(request_headers)
        if not session_key:
            logger.debug("session_ws: no session credential — using per-turn bridge (topic=%s)", topic_id)
            async for chunk in self._fallback(client=client, topic_id=topic_id, request_headers=request_headers):
                yield chunk
            return

        try:
            conn = await self._get_conn(session_key=session_key)
            stream = conn.stream_turn(topic_id=topic_id, client=client, request_headers=request_headers)
        except Exception as exc:
            logger.warning("session_ws: persistent connect failed (session=%s): %s — falling back", session_key, exc)
            async for chunk in self._fallback(client=client, topic_id=topic_id, request_headers=request_headers):
                yield chunk
            return

        try:
            async for chunk in stream:
                yield chunk
        except Exception as exc:
            logger.error("session_ws: turn stream error (topic=%s): %s", topic_id, exc)

    async def _get_conn(self, *, session_key: str) -> _SessionConn:
        """Return the session's connection, creating the holder if absent."""
        async with self._lock:
            conn = self._conns.get(session_key)
            if conn is None:
                conn = _SessionConn(session_key=session_key)
                self._conns[session_key] = conn
            return conn

    async def _fallback(
        self,
        *,
        client: httpx.AsyncClient,
        topic_id: str,
        request_headers: dict[str, str],
    ) -> AsyncIterator[bytes]:
        """Per-turn bridge fallback — opens a fresh socket for this turn only."""
        async for chunk in run_handoff_bridge(client=client, topic_id=topic_id, request_headers=request_headers):
            yield chunk

    async def shutdown(self) -> None:
        """Cancel every read task and close every session socket.

        Called from :meth:`ccproxy.transport.sidecar.Sidecar.stop`.
        """
        async with self._lock:
            conns = list(self._conns.values())
            self._conns.clear()
        for conn in conns:
            with contextlib.suppress(Exception):
                await conn.close()


_manager: SessionWSManager | None = None


def get_session_ws_manager() -> SessionWSManager:
    """Return the process-wide :class:`SessionWSManager` singleton.

    Construction is not awaited (the manager's own lock guards all mutation), so
    this is a plain accessor like the other ccproxy singletons.
    """
    global _manager
    if _manager is None:
        _manager = SessionWSManager()
    return _manager


def clear_session_ws_manager() -> None:
    """Reset the singleton (test-suite teardown). Does not await shutdown.

    Tests that need the sockets actually torn down should await
    ``get_session_ws_manager().shutdown()`` before clearing; this only drops the
    reference so the next access builds a fresh manager.
    """
    global _manager
    _manager = None
