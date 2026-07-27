"""WebSocket handoff continuation bridge for OpenAI Conversations.

When ChatGPT does not finish a turn inline, it ends the HTTP SSE early with a
WebSocket handoff signal — exactly as the chatgpt.com SPA handles it:

* ``stream_handoff`` (carrying a ``subscribe_ws_topic``) → **WebSocket** handoff:
  ``GET /backend-api/celsius/ws/user`` → ``wss`` → subscribe to the topic
  (``run_handoff_bridge``). The SPA's ``celsius/ws/user`` path.
* ``resume_conversation_token`` (a conduit JWT) → the answer also streams over
  the WebSocket; the JWT's ``turn_topic_id`` claim is the topic to subscribe to
  (``_topic_from_resume_jwt``), so these turns route to the same WS bridge.

:class:`HandoffState` scans the streaming SSE bytes (``detect_handoff``) and
exposes :meth:`HandoffState.should_bridge` so the sidecar runs the WS
continuation, which yields SSE-v1 bytes the same intake FSM parses.

The WS reader is **envelope-agnostic**: each inbound message is walked
structurally (:func:`_walk_sse_items`) to pull every ``encoded_item`` SSE payload
at any depth, regardless of transport envelope type, rather than switching on a
frame ``type``. Every raw message is captured (:mod:`ccproxy.openai_conversations.ws_capture`)
so nothing is silently dropped, and the turn ends on the structural ``[DONE]``/
``done`` signal.

MIT attribution: the WS dial + init/subscribe handshake is adapted from aurora
(``internal/chatgpt/request.go``); the structural extraction, capture, and
state-machine structure are original.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
from websockets.asyncio.client import connect as ws_connect

from ccproxy.openai_conversations.ws_capture import get_ws_capture

logger = logging.getLogger(__name__)

_WS_USER_PATH = "/backend-api/celsius/ws/user"
_WS_CHATGPT_ORIGIN = "https://chatgpt.com"
_WS_BASE_URL = "https://chatgpt.com"

# Only ``stream_handoff`` (with a ``subscribe_ws_topic`` option) is a real WS
# handoff directive. The SPA (``sdk.js``) treats ``server_ste_metadata`` as pure
# telemetry (``addServerSteMetadata``) — it carries a ``turn_exchange_id`` on
# every turn and is NOT a handoff topic; the actual continuation for those turns
# is ``resume_conversation_token`` (HTTP resume).
_HANDOFF_SIDE_EVENTS = frozenset({"stream_handoff"})

# Idle backstop on the WS read: the maximum wait for the NEXT frame before the
# turn-response gives up. This is a safety valve, not the functional terminator
# — the real end-of-turn is the structural [DONE]/done signal. A give-up never
# ends the turn silently: it always yields the typed idle-timeout terminal
# frame (below) before returning, so a truncated turn is distinguishable from
# a genuine upstream completion. The socket itself is never torn down on this
# timer (a future session manager keeps it open across turns within the
# websocket_url window).
_FALLBACK_WS_READ_TIMEOUT_SECONDS = 120.0
"""Used when the config singleton is unavailable (early startup, tests without
a config instance) and as :func:`stream_handoff_sse`'s public default.
Production reads
``CCProxyConfig.lightllm.openai_conversations.turn_idle_timeout_seconds`` —
see :func:`_get_turn_idle_timeout_seconds`."""

# Ping interval (aurora: 25s).
_WS_PING_INTERVAL = 25.0
# Browser-shape User-Agent string for the WS dial and /celsius/ws/user GET.
_WS_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

CCPROXY_IDLE_TIMEOUT_EVENT_TYPE = "ccproxy_idle_timeout"
"""Synthetic ``{"type": ...}`` SSE-v1 side-event kind ccproxy injects onto the
wire when the idle-timeout backstop fires — never a real chatgpt.com event.
Shared with :mod:`ccproxy.openai_conversations.session_ws` (the persistent
per-turn queue backstop) so both give-up sites emit the same recognizable
marker; :mod:`ccproxy.lightllm.graph.openai_conversations_intake` recognizes
this kind and marks the turn's ``finish_reason`` as ``"error"`` instead of
leaving termination unclassified (ADR-0002's "typed continuation error")."""


def _get_turn_idle_timeout_seconds() -> float:
    """Lazy-read the active idle-timeout bound from
    ``CCProxyConfig.lightllm.openai_conversations.turn_idle_timeout_seconds``.

    Shared by this module's per-message WS read backstop and
    :mod:`ccproxy.openai_conversations.session_ws`'s per-turn queue backstop —
    both bound the same reliability property (how long to wait for the next
    conduit frame before giving up), so they read one config-declared
    magnitude rather than two independently hardcoded literals. Falls back to
    :data:`_FALLBACK_WS_READ_TIMEOUT_SECONDS` when the config singleton is not
    yet initialized (early startup or tests that bypass config loading).
    """
    try:
        from ccproxy.config import get_config

        return float(get_config().lightllm.openai_conversations.turn_idle_timeout_seconds)
    except Exception:
        return _FALLBACK_WS_READ_TIMEOUT_SECONDS


def _idle_timeout_frame(*, idle_seconds: float) -> bytes:
    """Build the terminal SSE-v1 frame marking an idle-timeout give-up.

    Replaces silent generator exhaustion with an explicit, classifiable
    signal: a genuine upstream completion ends on a structural
    ``[DONE]``/``finished_successfully``; a give-up ends on this instead, so
    a consumer (ultimately :func:`ccproxy.lightllm.graph.openai_conversations_intake
    .OpenAIConversationsIntakeFSM`) can always tell the two apart rather than
    inferring truncation from silence.
    """
    payload = json.dumps({"type": CCPROXY_IDLE_TIMEOUT_EVENT_TYPE, "idle_seconds": idle_seconds})
    return f"data: {payload}\n\n".encode()


class _IdleTimeoutError(Exception):
    """Raised by :func:`_ws_read_loop` when no frame arrives within the idle window.

    Not an upstream failure — a control-flow signal so
    :func:`_stream_handoff_sse_impl` can distinguish "gave up waiting" from
    "the socket closed" and emit the typed idle-timeout terminal frame before
    ending the generator.
    """


# ── Handoff state ─────────────────────────────────────────────────────────────


@dataclass
class HandoffState:
    """Mutable accumulator for scanning SSE bytes for a handoff signal.

    Created once per request; ``feed(chunk)`` is called for each upstream SSE
    chunk; ``topic`` holds the WS topic id (empty string = no handoff detected
    yet); ``http_content_seen`` tracks whether any text-content patch arrived
    via HTTP so the caller can apply the ``shouldUseWebsocketHandoff`` rule.
    """

    topic: str = ""
    """WS topic id from a ``stream_handoff`` event or a ``resume_conversation_token``
    JWT's ``turn_topic_id`` claim."""

    http_content_seen: bool = False
    """True once an ``append``/``replace`` on ``/message/content/parts/0`` arrives."""

    _buf: bytearray = field(default_factory=bytearray)

    def feed(self, chunk: bytes) -> None:
        """Scan ``chunk`` for handoff signals and content patches."""
        self._buf.extend(chunk)
        self._drain()

    def _drain(self) -> None:
        while True:
            crlf = self._buf.find(b"\r\n\r\n")
            lf = self._buf.find(b"\n\n")
            if crlf == -1 and lf == -1:
                return
            if crlf != -1 and (lf == -1 or crlf < lf):
                sep_idx, sep_len = crlf, 4
            else:
                sep_idx, sep_len = lf, 2
            frame = bytes(self._buf[:sep_idx])
            del self._buf[: sep_idx + sep_len]
            self._process_frame(frame)

    def _process_frame(self, frame: bytes) -> None:
        data_lines: list[str] = []
        for raw in frame.split(b"\n"):
            line = raw.rstrip(b"\r").decode("utf-8", errors="replace")
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())

        data = "\n".join(data_lines).strip()
        if not data or data == "[DONE]":
            return

        try:
            parsed = json.loads(data)
        except (json.JSONDecodeError, ValueError):
            return
        if not isinstance(parsed, dict):
            return

        kind = parsed.get("type")
        if isinstance(kind, str):
            if kind == "resume_conversation_token":
                tok = parsed.get("token")
                if isinstance(tok, str) and tok and not self.topic:
                    # The JWT carries turn_topic_id (conversation-turn-<id>) — the
                    # WS topic for the conduit stream. Use it as the topic source.
                    jwt_topic = _topic_from_resume_jwt(tok)
                    if jwt_topic:
                        self.topic = jwt_topic
            elif kind in _HANDOFF_SIDE_EVENTS and not self.topic:
                topic = _extract_handoff_topic(parsed)
                if topic:
                    self.topic = topic

        # Detect HTTP text content (aurora ``shouldUseWebsocketHandoff`` rule).
        # Check ``o`` field for "append"/"replace" on the text path.
        op = parsed.get("o")
        if op in ("append", "replace") and parsed.get("p") == "/message/content/parts/0":
            if isinstance(parsed.get("v"), str) and parsed.get("v"):
                self.http_content_seen = True
            return

        # Also check batch patch arrays.
        if op == "patch" and isinstance(parsed.get("v"), list):
            for item in parsed["v"]:
                if not isinstance(item, dict):
                    continue
                if (
                    item.get("p") == "/message/content/parts/0"
                    and item.get("o") in ("append", "replace")
                    and isinstance(item.get("v"), str)
                    and item.get("v")
                ):
                    self.http_content_seen = True

    def should_bridge(self) -> bool:
        """True when a WS topic was found and no inline HTTP text arrived.

        The WebSocket path (``stream_handoff`` or a ``resume_conversation_token``
        JWT's ``turn_topic_id`` → ``/celsius/ws/user`` → wss) is the SPA's handler
        for a deferred turn. When inline content already streamed there is nothing
        to continue.
        """
        return bool(self.topic) and not self.http_content_seen


def detect_handoff(state: HandoffState, chunk: bytes) -> None:
    """Feed ``chunk`` through ``state``, accumulating handoff signals in-place."""
    state.feed(chunk)


def _topic_from_resume_jwt(token: str) -> str:
    """Decode the ``resume_conversation_token`` JWT and return its ``turn_topic_id``
    claim — the ``conversation-turn-<id>`` WebSocket topic carrying the conduit
    answer stream. Empty string on any decode failure or missing claim.

    The JWT payload also carries ``conduit_uuid`` / ``conduit_location`` (an
    internal IP, not directly reachable) / ``cluster``; the public route to the
    stream is ``/celsius/ws/user`` → wss, subscribed to ``turn_topic_id``.
    """
    parts = token.split(".")
    if len(parts) < 2 or not parts[1]:
        return ""
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, json.JSONDecodeError):
        return ""
    if not isinstance(claims, dict):
        return ""
    topic = claims.get("turn_topic_id")
    return topic if isinstance(topic, str) else ""


def _extract_handoff_topic(raw: dict[str, Any]) -> str:
    """Extract the WS topic id from a ``stream_handoff`` event.

    The SPA (``sdk.js``) only treats a ``stream_handoff`` carrying a
    ``subscribe_ws_topic`` option as a WebSocket handoff (``stream_protocol =
    ws``); ``server_ste_metadata`` is telemetry, not a topic source.
    """
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


# ── WS URL fetch ──────────────────────────────────────────────────────────────


_WS_GET_DROP_HEADERS = frozenset(
    {
        "x-openai-target-path",
        "x-openai-target-route",
        "x-conduit-token",
        "content-length",
        "content-type",
        "accept",
        "host",
    }
)
"""Original /f/conversation headers the WS-url GET replaces (everything else —
bearer, browser shape, sentinel tokens — is reused so the GET is authenticated)."""


def _ws_get_headers(request_headers: dict[str, str]) -> dict[str, str]:
    """Build the ``/celsius/ws/user`` GET headers by reusing the original
    browser-shape + bearer + sentinel headers (aurora sends the full
    conversation header set; the minimal 3-header GET 403s)."""
    headers = {k: v for k, v in request_headers.items() if k.lower() not in _WS_GET_DROP_HEADERS}
    headers["accept"] = "*/*"
    headers["x-openai-target-path"] = _WS_USER_PATH
    headers["x-openai-target-route"] = _WS_USER_PATH
    return headers


async def fetch_ws_url(
    client: httpx.AsyncClient,
    *,
    request_headers: dict[str, str] | None = None,
    extra_headers: dict[str, str] | None = None,
    timeout: float = 15.0,
) -> str:
    """GET /backend-api/celsius/ws/user and return the ``websocket_url`` value.

    Mirrors aurora ``getChatWebsocketURL`` (request.go:632-659, MIT-licensed),
    which sends the full conversation header set (bearer + sentinel + browser);
    a minimal-header GET is rejected with HTTP 403.

    Args:
        client: Cached httpx.AsyncClient (curl-cffi backed, authenticated).
        request_headers: The original forwarded ``/f/conversation`` headers,
            reused for the bearer + browser + sentinel block.
        extra_headers: Optional additional headers to stamp on the GET.
        timeout: Request timeout in seconds.

    Returns:
        Authenticated ``wss://`` URL.

    Raises:
        RuntimeError: On non-200 response or missing ``websocket_url`` field.
    """
    url = f"{_WS_BASE_URL}{_WS_USER_PATH}"
    headers = _ws_get_headers(request_headers or {})
    if extra_headers:
        headers.update(extra_headers)

    resp = await client.get(url=url, headers=headers, timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"celsius ws/user failed: HTTP {resp.status_code} — {resp.text[:200]}")
    try:
        result = resp.json()
    except Exception as exc:
        raise RuntimeError(f"celsius ws/user: invalid JSON: {exc}") from exc

    ws_url = result.get("websocket_url")
    if not isinstance(ws_url, str) or not ws_url:
        raise RuntimeError(f"celsius ws/user: missing websocket_url in {result!r}")
    return ws_url


# ── WS frame helpers ──────────────────────────────────────────────────────────


def _parse_ws_frames(raw: bytes) -> list[dict[str, Any]]:
    """Parse one WebSocket message into a list of frame objects.

    A message is either a JSON array of frames or a single frame object.
    Mirrors aurora ``parseChatWebsocketFrames`` (request.go:721-737,
    MIT-licensed).
    """
    if not raw:
        return []
    try:
        if raw[0:1] == b"[":
            frames: Any = json.loads(raw)
            if isinstance(frames, list):
                return [f for f in frames if isinstance(f, dict)]
            return []
        frame: Any = json.loads(raw)
        if isinstance(frame, dict):
            return [frame]
        return []
    except (json.JSONDecodeError, ValueError):
        return []


def _decode_encoded_item(encoded: str) -> str:
    """Return the SSE text carried by a WS ``encoded_item``.

    Ground truth: chatgpt.com ``sdk.js`` ``_Nn`` parses ``encoded_item`` *as an SSE
    stream* (line parser over ``data:``/``event:``), and aurora checks ``data:
    [DONE]`` literally inside it — so ``encoded_item`` is already a plaintext SSE
    event string. It is returned unchanged for the intake to parse.

    A payload that is NOT SSE-shaped is unexpected; it is still forwarded (the
    intake's unparseable-frame telemetry then surfaces it) — never silently
    transformed on a guess.
    """
    if not encoded.lstrip().startswith(("data:", "event:")):
        logger.debug("ws_handoff: encoded_item is not SSE-shaped (%d bytes): %.120r", len(encoded), encoded)
    return encoded


def _walk_sse_items(obj: Any, topic: str | None = None) -> list[tuple[str | None, str]]:
    """Recursively extract every SSE payload from a parsed WS message.

    The conduit answer is carried by ``encoded_item`` strings nested at varying
    depths inside transport envelopes (``message`` → ``payload.payload``,
    ``conversation-turn-stream`` → ``payload`` (``stream-item``), ``reply`` →
    ``catchups[]…``). Rather than enumerate envelope types — and silently drop any
    shape not anticipated — this walks the whole structure and pulls out every
    ``encoded_item`` it finds, tagging each with the nearest enclosing
    ``topic_id`` so the caller can route it. Returns ``(topic, sse_data)`` pairs
    in document order.
    """
    found: list[tuple[str | None, str]] = []
    if isinstance(obj, dict):
        frame_topic = obj.get("topic_id")
        if isinstance(frame_topic, str) and frame_topic:
            topic = frame_topic
        encoded = obj.get("encoded_item")
        if isinstance(encoded, str) and encoded:
            found.append((topic, _decode_encoded_item(encoded)))
        for value in obj.values():
            found.extend(_walk_sse_items(value, topic))
    elif isinstance(obj, list):
        for value in obj:
            found.extend(_walk_sse_items(value, topic))
    return found


def _is_done_item(item: str) -> bool:
    """True when ``item`` contains the ``[DONE]`` sentinel."""
    return "data: [DONE]" in item or "data:[DONE]" in item


def _topic_matches(item_topic: str | None, turn_topic: str) -> bool:
    """Whether an extracted item routes to the answer stream.

    Accepts the turn's own topic, the general ``conversations`` topic (which can
    carry turn frames), and untagged items (no ``topic_id`` anywhere in the
    frame). Frames for other topics (``app_notifications``, ``calpico-chatgpt``,
    a different turn) are not merged into the answer — but are still captured.
    """
    return item_topic is None or item_topic == turn_topic or item_topic == "conversations"


def _message_signals_done(obj: Any, *, turn_topic: str, topic: str | None = None) -> bool:
    """True when a frame structurally signals end-of-stream for our turn.

    Independent of envelope type: a ``{"type": "done"}`` marker (e.g.
    ``payload.type == "done"``) anywhere whose nearest enclosing topic routes to
    our turn (per :func:`_topic_matches`). Complements the ``[DONE]`` item check.
    """
    if isinstance(obj, dict):
        frame_topic = obj.get("topic_id")
        if isinstance(frame_topic, str) and frame_topic:
            topic = frame_topic
        if obj.get("type") == "done" and _topic_matches(topic, turn_topic):
            return True
        return any(_message_signals_done(v, turn_topic=turn_topic, topic=topic) for v in obj.values())
    if isinstance(obj, list):
        return any(_message_signals_done(v, turn_topic=turn_topic, topic=topic) for v in obj)
    return False


def _as_sse_bytes(sse: str) -> bytes:
    """Normalise an SSE data string to end with the ``\\n\\n`` frame separator."""
    out = sse if sse.endswith("\n") else sse + "\n"
    if not out.endswith("\n\n"):
        out += "\n"
    return out.encode()


# ── WS SSE streaming ─────────────────────────────────────────────────────────


async def stream_handoff_sse(
    *,
    ws_url: str,
    topic_id: str,
    user_agent: str = _WS_USER_AGENT,
    read_timeout: float = _FALLBACK_WS_READ_TIMEOUT_SECONDS,
    ping_interval: float = _WS_PING_INTERVAL,
) -> AsyncIterator[bytes]:
    """Dial the WS, subscribe to ``topic_id``, and yield SSE bytes.

    The wss URL is already auth-bearing (from ``/celsius/ws/user``); no TLS
    fingerprint impersonation is needed — a plain ``websockets`` dial suffices.

    Each inbound WS message is walked structurally (:func:`_walk_sse_items`):
    every ``encoded_item`` SSE payload routed to this turn is yielded as
    ``b"data: {...}\\n\\n"``; every raw message is captured (never dropped). The
    generator stops on the structural ``[DONE]``/``done`` end-of-stream, the
    idle-timeout backstop (which yields :func:`_idle_timeout_frame` first — see
    :data:`CCPROXY_IDLE_TIMEOUT_EVENT_TYPE`), or any error; it never raises into
    the caller.

    WS dial + init/subscribe handshake adapted from aurora ``DialChatWebsocket``
    (request.go:661-708, MIT-licensed).
    """
    return _stream_handoff_sse_impl(
        ws_url=ws_url,
        topic_id=topic_id,
        user_agent=user_agent,
        read_timeout=read_timeout,
        ping_interval=ping_interval,
    )


async def _stream_handoff_sse_impl(
    *,
    ws_url: str,
    topic_id: str,
    user_agent: str,
    read_timeout: float,
    ping_interval: float,
) -> AsyncIterator[bytes]:
    # Aurora init array: connect + subscribe three known topics.
    init_msg = json.dumps(
        [
            {"id": 1, "command": {"type": "connect", "presence": {"type": "presence", "state": "background"}}},
            {"id": 2, "command": {"type": "subscribe", "topic_id": "calpico-chatgpt"}},
            {"id": 3, "command": {"type": "subscribe", "topic_id": "conversations"}},
            {"id": 4, "command": {"type": "subscribe", "topic_id": "app_notifications"}},
        ]
    )

    # Unique subscribe id — aurora uses an atomic counter starting at 5.
    sub_id = 5
    sub_msg = json.dumps([{"id": sub_id, "command": {"type": "subscribe", "topic_id": topic_id, "offset": "0"}}])

    try:
        async with ws_connect(
            ws_url,
            additional_headers={
                "User-Agent": user_agent,
                "Origin": _WS_CHATGPT_ORIGIN,
            },
            # Disable built-in ping; we handle it ourselves to mirror aurora.
            ping_interval=None,
            open_timeout=15.0,
        ) as ws:
            # Send init array (mirrors aurora DialChatWebsocketWithState).
            await ws.send(init_msg)
            # Subscribe to the handoff topic.
            await ws.send(sub_msg)

            # Ping task: every ping_interval seconds send a ping.
            async def _ping_loop() -> None:
                while True:
                    await asyncio.sleep(ping_interval)
                    try:
                        await ws.ping()
                    except Exception:
                        return

            ping_task = asyncio.create_task(_ping_loop(), name="ccproxy-ws-handoff-ping")
            logger.debug("ws_handoff: dialed wss, subscribed topic=%s", topic_id)
            capture = get_ws_capture()
            yielded = 0

            try:
                async for raw_msg in _ws_read_loop(ws=ws, read_timeout=read_timeout):
                    raw_text = raw_msg if isinstance(raw_msg, str) else raw_msg.decode("utf-8", errors="replace")
                    frames = _parse_ws_frames(raw_text.encode())
                    forwarded_here = 0
                    done = False
                    for frame in frames:
                        # Envelope-agnostic: forward every SSE payload for our turn,
                        # regardless of envelope type or nesting depth.
                        for item_topic, sse in _walk_sse_items(frame):
                            if not _topic_matches(item_topic, topic_id):
                                continue
                            forwarded_here += 1
                            yielded += 1
                            yield _as_sse_bytes(sse)
                            if _is_done_item(sse):
                                done = True
                        if not done and _message_signals_done(frame, turn_topic=topic_id):
                            done = True
                    # NEVER drop: every inbound frame is captured (forwarded or not),
                    # so non-turn / unhandled events stay inspectable.
                    capture.record(topic=topic_id, raw=raw_text, forwarded=forwarded_here)
                    logger.debug("ws_handoff: WS msg: %d frame(s), forwarded=%d", len(frames), forwarded_here)
                    logger.debug("ws_handoff: RAWFRAME %s", raw_text)
                    if done:
                        logger.debug("ws_handoff: WS done, yielded=%d items (topic=%s)", yielded, topic_id)
                        return
            except _IdleTimeoutError:
                # Give-up, not completion: yield the typed terminal signal so a
                # truncated turn is distinguishable from a genuine [DONE] before
                # the generator ends — never a silent drop.
                yield _idle_timeout_frame(idle_seconds=read_timeout)
                logger.debug(
                    "ws_handoff: idle-timeout terminal signal emitted (topic=%s, yielded=%d)", topic_id, yielded
                )
            finally:
                ping_task.cancel()
                logger.debug("ws_handoff: WS closed, yielded=%d items (topic=%s)", yielded, topic_id)
    except Exception as exc:
        logger.error("ws_handoff: WS bridge error (topic=%s): %s", topic_id, exc)


async def _ws_read_loop(
    *,
    ws: Any,
    read_timeout: float,
) -> AsyncIterator[str | bytes]:
    """Yield raw WebSocket messages until the socket closes or goes idle.

    ``read_timeout`` is the idle backstop (see :func:`_get_turn_idle_timeout_seconds`)
    — the maximum wait for the next frame, not a per-message delay. Raises
    :class:`_IdleTimeoutError` (rather than returning silently) so the caller can
    emit the typed terminal signal before ending the stream.
    """
    while True:
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=read_timeout)
        except TimeoutError as exc:
            logger.warning("ws_handoff: idle backstop hit after %.0fs (no [DONE] seen)", read_timeout)
            raise _IdleTimeoutError(read_timeout) from exc
        except Exception as exc:
            logger.debug("ws_handoff: WS closed: %s", exc)
            return
        yield msg


# ── Public bridge entry point ─────────────────────────────────────────────────


async def run_handoff_bridge(
    *,
    client: httpx.AsyncClient,
    topic_id: str,
    request_headers: dict[str, str] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> AsyncIterator[bytes]:
    """Fetch WS URL, dial, stream topic, yield SSE bytes.

    On ANY error, logs and returns immediately — never raises into the
    ``body_stream`` caller.

    Args:
        client: Authenticated httpx.AsyncClient for the ``/celsius/ws/user`` GET.
        topic_id: WS topic id (from a ``stream_handoff`` option or the
            ``resume_conversation_token`` JWT's ``turn_topic_id``).
        request_headers: The original forwarded ``/f/conversation`` headers, reused
            so the ``/celsius/ws/user`` GET is authenticated (bearer + sentinel +
            browser); a minimal-header GET 403s.
        extra_headers: Optional additional headers for the ``/celsius/ws/user`` GET.
    """
    try:
        ws_url = await fetch_ws_url(client=client, request_headers=request_headers, extra_headers=extra_headers)
    except Exception as exc:
        logger.error("ws_handoff: failed to fetch WS URL (topic=%s): %s", topic_id, exc)
        return

    logger.debug("ws_handoff: bridging topic=%s via %s…", topic_id, ws_url[:60])

    try:
        async for chunk in _stream_handoff_sse_impl(
            ws_url=ws_url,
            topic_id=topic_id,
            user_agent=_WS_USER_AGENT,
            read_timeout=_get_turn_idle_timeout_seconds(),
            ping_interval=_WS_PING_INTERVAL,
        ):
            yield chunk
    except Exception as exc:
        logger.error("ws_handoff: bridge stream error (topic=%s): %s", topic_id, exc)
