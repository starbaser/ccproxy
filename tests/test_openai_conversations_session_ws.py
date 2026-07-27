"""Tests for ``ccproxy.openai_conversations.session_ws.SessionWSManager`` — the
persistent session-scoped conduit WebSocket manager (CHATGPT-008 Increment 2).

A real in-process WebSocket server (``websockets.asyncio.server.serve``) backs
every test; the socket is never mocked. Only the ``/celsius/ws/user`` URL
discovery GET (:func:`fetch_ws_url`, which would hit the live chatgpt.com
endpoint) is replaced with a small counting stub that returns the fake server's
``ws://`` address — exactly the seam the per-turn ``run_handoff_bridge`` tests
use, and the only network call that is not a real socket here.

Coverage:
- socket reuse: two sequential turns on one session dial / fetch the URL ONCE;
- multiplex: two concurrent turns on one socket each receive only THEIR topic's
  frames; ``[DONE]`` on topic A ends turn A while turn B keeps streaming;
- EOS per turn: a turn ends on structural ``[DONE]`` and the socket stays open;
- reconnect / no-hang: the server closing the socket mid-turn delivers EOS to the
  active turn (no hang); a later turn re-dials;
- capture: every inbound frame is recorded in the capture sink;
- different sessions get different sockets;
- error path: a ``fetch_ws_url`` failure yields nothing and never raises;
- sidecar seam: the continuation routes through the manager.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from websockets.asyncio.server import ServerConnection
from websockets.asyncio.server import serve as ws_serve

from ccproxy.config import CCProxyConfig, LightllmConfig, OpenAIConversationsConfig, set_config_instance
from ccproxy.openai_conversations.session_ws import (
    SessionWSManager,
    _bearer_value,
    _session_key,
    _session_token_from_cookie,
    clear_session_ws_manager,
    get_session_ws_manager,
)
from ccproxy.openai_conversations.ws_capture import get_ws_capture
from ccproxy.transport import UnknownFingerprintProfileError
from ccproxy.transport.sidecar import CONTINUATION_HEADER, Sidecar

# ── Helpers ───────────────────────────────────────────────────────────────────


def _encoded_sse_frame(topic_id: str, text: str) -> dict[str, Any]:
    """A ``message`` envelope carrying an encoded SSE item for ``topic_id``."""
    return {
        "type": "message",
        "topic_id": topic_id,
        "payload": {"payload": {"encoded_item": f"data: {text}\n\n"}},
    }


def _done_frame(topic_id: str) -> dict[str, Any]:
    return {
        "type": "message",
        "topic_id": topic_id,
        "payload": {"payload": {"encoded_item": "data: [DONE]\n\n"}},
    }


def _cookie_headers(token: str) -> dict[str, str]:
    """Forwarded headers carrying a ``__Secure-next-auth.session-token`` cookie."""
    return {
        "cookie": f"foo=bar; __Secure-next-auth.session-token={token}; baz=qux",
        "authorization": "Bearer ignored-when-cookie-present",
    }


class _CountingFetch:
    """A real (non-mock) ``fetch_ws_url`` replacement that counts invocations
    and returns a fixed ``ws://`` URL — the only network call stubbed here."""

    def __init__(self, ws_url: str) -> None:
        self._ws_url = ws_url
        self.calls = 0

    async def __call__(self, *, client: object, request_headers: object = None, **kwargs: object) -> str:
        self.calls += 1
        return self._ws_url


async def _drain(stream: AsyncIterator[bytes]) -> list[bytes]:
    out: list[bytes] = []
    async for chunk in stream:
        out.append(chunk)
    return out


def _dummy_client() -> httpx.AsyncClient:
    return AsyncMock(spec=httpx.AsyncClient)


# ── Session key derivation ────────────────────────────────────────────────────


class TestSessionKey:
    def test_cookie_token_extracted(self) -> None:
        assert _session_token_from_cookie(_cookie_headers("abc123")) == "abc123"

    def test_cookie_token_absent(self) -> None:
        assert _session_token_from_cookie({"cookie": "foo=bar"}) == ""

    def test_bearer_value_extracted(self) -> None:
        assert _bearer_value({"authorization": "Bearer xyz"}) == "Bearer xyz"

    def test_key_is_hashed_not_raw(self) -> None:
        key = _session_key(_cookie_headers("super-secret-token"))
        assert key
        assert "super-secret-token" not in key
        assert len(key) == 16

    def test_cookie_preferred_over_bearer(self) -> None:
        # When both a cookie token and a bearer are present, the cookie token is
        # the credential — the key hashes the bare cookie value, NOT the bearer.
        headers = _cookie_headers("cookie-tok")
        expected = hashlib.sha256(b"cookie-tok").hexdigest()[:16]
        assert _session_key(headers) == expected
        # A header set carrying only the same bearer value hashes differently
        # (the bearer carrier is "Bearer <tok>"), proving the cookie was used.
        assert _session_key({"authorization": "Bearer cookie-tok"}) != expected

    def test_bearer_fallback_when_no_cookie(self) -> None:
        assert _session_key({"authorization": "Bearer only-bearer"})

    def test_empty_when_no_credential(self) -> None:
        assert _session_key({"x-other": "v"}) == ""

    def test_different_secrets_different_keys(self) -> None:
        assert _session_key(_cookie_headers("a")) != _session_key(_cookie_headers("b"))


# ── Socket reuse / multiplex / EOS / reconnect (fake WS server) ───────────────


class TestSessionWSManagerWithFakeServer:
    """Integration tests against a real in-process WebSocket server."""

    async def test_two_sequential_turns_reuse_one_socket(self) -> None:
        """Two sequential turns for one session fetch the URL and dial ONCE."""
        topic_a = "conversation-turn-A"
        topic_b = "conversation-turn-B"
        connections = 0

        async def _handler(ws: ServerConnection) -> None:
            nonlocal connections
            connections += 1
            await ws.recv()  # init array (once per socket)
            # The single read loop drives both turns over one connection: answer
            # each per-turn subscribe as it arrives.
            seen = 0
            async for raw in ws:
                msgs = json.loads(raw)
                cmd = msgs[0]["command"]
                if cmd.get("type") != "subscribe":
                    continue
                topic = cmd["topic_id"]
                await ws.send(json.dumps([_encoded_sse_frame(topic, f'{{"t": "{topic}"}}')]))
                await ws.send(json.dumps([_done_frame(topic)]))
                seen += 1

        async with ws_serve(_handler, "127.0.0.1", 0, ping_interval=None) as server:
            port = server.sockets[0].getsockname()[1]
            ws_url = f"ws://127.0.0.1:{port}"
            fetch = _CountingFetch(ws_url)
            headers = _cookie_headers("reuse-session")
            manager = SessionWSManager()
            with patch("ccproxy.openai_conversations.session_ws.fetch_ws_url", fetch):
                chunks_a = await _drain(
                    manager.stream_turn(topic_id=topic_a, client=_dummy_client(), request_headers=headers)
                )
                chunks_b = await _drain(
                    manager.stream_turn(topic_id=topic_b, client=_dummy_client(), request_headers=headers)
                )
                await manager.shutdown()

        joined_a = b"".join(chunks_a)
        joined_b = b"".join(chunks_b)
        assert topic_a.encode() in joined_a
        assert topic_b.encode() in joined_b
        # One fetch, one dial — the second turn reused the socket.
        assert fetch.calls == 1
        assert connections == 1

    async def test_concurrent_turns_multiplex_on_one_socket(self) -> None:
        """Two concurrent turns: each receives only its topic's frames; [DONE]
        on topic A ends turn A while turn B keeps streaming on the same socket."""
        topic_a = "conversation-turn-mux-A"
        topic_b = "conversation-turn-mux-B"
        connections = 0

        async def _handler(ws: ServerConnection) -> None:
            nonlocal connections
            connections += 1
            await ws.recv()  # init
            # Wait for both per-turn subscribes before interleaving frames.
            subs: set[str] = set()
            async for raw in ws:
                cmd = json.loads(raw)[0]["command"]
                if cmd.get("type") == "subscribe":
                    subs.add(cmd["topic_id"])
                if subs >= {topic_a, topic_b}:
                    break
            # Interleave A and B; finish A first, then continue B, then finish B.
            await ws.send(json.dumps([_encoded_sse_frame(topic_a, '{"who": "A1"}')]))
            await ws.send(json.dumps([_encoded_sse_frame(topic_b, '{"who": "B1"}')]))
            await ws.send(json.dumps([_done_frame(topic_a)]))  # ends turn A
            await ws.send(json.dumps([_encoded_sse_frame(topic_b, '{"who": "B2"}')]))
            await ws.send(json.dumps([_done_frame(topic_b)]))  # ends turn B

        async with ws_serve(_handler, "127.0.0.1", 0, ping_interval=None) as server:
            port = server.sockets[0].getsockname()[1]
            ws_url = f"ws://127.0.0.1:{port}"
            fetch = _CountingFetch(ws_url)
            headers = _cookie_headers("mux-session")
            manager = SessionWSManager()
            with patch("ccproxy.openai_conversations.session_ws.fetch_ws_url", fetch):
                results = await asyncio.gather(
                    _drain(manager.stream_turn(topic_id=topic_a, client=_dummy_client(), request_headers=headers)),
                    _drain(manager.stream_turn(topic_id=topic_b, client=_dummy_client(), request_headers=headers)),
                )
                await manager.shutdown()

        joined_a = b"".join(results[0])
        joined_b = b"".join(results[1])
        # Turn A got only A's frame; turn B got both of B's frames.
        assert b'"who": "A1"' in joined_a
        assert b"B1" not in joined_a
        assert b"B2" not in joined_a
        assert b'"who": "B1"' in joined_b
        assert b'"who": "B2"' in joined_b
        assert b"A1" not in joined_b
        # One socket carried both turns.
        assert connections == 1
        assert fetch.calls == 1

    async def test_turn_ends_on_done_socket_stays_open(self) -> None:
        """A turn ends on the structural [DONE]; the socket survives for reuse."""
        topic1 = "conversation-turn-stay-1"
        topic2 = "conversation-turn-stay-2"
        connections = 0

        async def _handler(ws: ServerConnection) -> None:
            nonlocal connections
            connections += 1
            await ws.recv()
            async for raw in ws:
                cmd = json.loads(raw)[0]["command"]
                if cmd.get("type") != "subscribe":
                    continue
                topic = cmd["topic_id"]
                await ws.send(json.dumps([_encoded_sse_frame(topic, '{"answer": true}')]))
                await ws.send(json.dumps([_done_frame(topic)]))

        async with ws_serve(_handler, "127.0.0.1", 0, ping_interval=None) as server:
            port = server.sockets[0].getsockname()[1]
            ws_url = f"ws://127.0.0.1:{port}"
            fetch = _CountingFetch(ws_url)
            headers = _cookie_headers("stay-session")
            manager = SessionWSManager()
            with patch("ccproxy.openai_conversations.session_ws.fetch_ws_url", fetch):
                first = await _drain(
                    manager.stream_turn(topic_id=topic1, client=_dummy_client(), request_headers=headers)
                )
                conn = await manager._get_conn(session_key=_session_key(headers))
                assert conn.alive is True  # socket still open after the first turn's DONE
                second = await _drain(
                    manager.stream_turn(topic_id=topic2, client=_dummy_client(), request_headers=headers)
                )
                await manager.shutdown()

        assert b"answer" in b"".join(first)
        assert b"answer" in b"".join(second)
        assert connections == 1  # the second turn reused the socket

    async def test_socket_close_mid_turn_delivers_eos_then_reconnects(self) -> None:
        """The server closing the socket mid-turn delivers EOS to the active turn
        (no hang); a subsequent turn re-dials a fresh socket."""
        topic1 = "conversation-turn-close-1"
        topic2 = "conversation-turn-close-2"
        connections = 0

        async def _handler(ws: ServerConnection) -> None:
            nonlocal connections
            connections += 1
            this_conn = connections
            await ws.recv()
            async for raw in ws:
                cmd = json.loads(raw)[0]["command"]
                if cmd.get("type") != "subscribe":
                    continue
                topic = cmd["topic_id"]
                if this_conn == 1:
                    # First connection: send one frame WITHOUT a DONE, then close
                    # the socket mid-turn. The active turn must still get EOS.
                    await ws.send(json.dumps([_encoded_sse_frame(topic, '{"partial": true}')]))
                    await ws.close()
                    return
                # Second connection: behave normally.
                await ws.send(json.dumps([_encoded_sse_frame(topic, '{"recovered": true}')]))
                await ws.send(json.dumps([_done_frame(topic)]))

        async with ws_serve(_handler, "127.0.0.1", 0, ping_interval=None) as server:
            port = server.sockets[0].getsockname()[1]
            ws_url = f"ws://127.0.0.1:{port}"
            fetch = _CountingFetch(ws_url)
            headers = _cookie_headers("reconnect-session")
            manager = SessionWSManager()
            with patch("ccproxy.openai_conversations.session_ws.fetch_ws_url", fetch):
                # First turn: must terminate (EOS via close), not hang.
                first = await asyncio.wait_for(
                    _drain(manager.stream_turn(topic_id=topic1, client=_dummy_client(), request_headers=headers)),
                    timeout=10.0,
                )
                # Second turn: the dead socket forces a re-dial.
                second = await asyncio.wait_for(
                    _drain(manager.stream_turn(topic_id=topic2, client=_dummy_client(), request_headers=headers)),
                    timeout=10.0,
                )
                await manager.shutdown()

        assert b"partial" in b"".join(first)  # got the partial frame before close
        assert b"recovered" in b"".join(second)
        # A fresh socket was dialed after the mid-turn close (the dead socket is
        # detected and reconnected). The cached websocket_url is still within its
        # ~25-min TTL so it is reused — only the dial repeats, not the URL fetch.
        assert connections == 2
        assert fetch.calls == 1

    async def test_turn_idle_timeout_yields_typed_terminal_signal(self) -> None:
        """A turn that never receives a structural [DONE] gives up after the
        configured idle timeout — and yields the typed idle-timeout terminal
        marker instead of ending silently, so the truncation is distinguishable
        from a genuine completion (P-6 regression)."""
        set_config_instance(
            CCProxyConfig(
                lightllm=LightllmConfig(openai_conversations=OpenAIConversationsConfig(turn_idle_timeout_seconds=0.05))
            )
        )
        topic = "conversation-turn-idle"

        async def _handler(ws: ServerConnection) -> None:
            await ws.recv()  # init
            await ws.recv()  # subscribe
            with contextlib.suppress(Exception):
                await ws.recv()  # never answers — blocks until the test tears the socket down

        async with ws_serve(_handler, "127.0.0.1", 0, ping_interval=None) as server:
            port = server.sockets[0].getsockname()[1]
            ws_url = f"ws://127.0.0.1:{port}"
            fetch = _CountingFetch(ws_url)
            headers = _cookie_headers("idle-session")
            manager = SessionWSManager()
            with patch("ccproxy.openai_conversations.session_ws.fetch_ws_url", fetch):
                chunks = await asyncio.wait_for(
                    _drain(manager.stream_turn(topic_id=topic, client=_dummy_client(), request_headers=headers)),
                    timeout=5.0,
                )
                conn = await manager._get_conn(session_key=_session_key(headers))
                assert conn.alive is True  # only the TURN ends; the socket stays open
                await manager.shutdown()

        joined = b"".join(chunks)
        assert b"[DONE]" not in joined  # never a false completion signal
        assert b"ccproxy_idle_timeout" in joined
        marker = json.loads(joined.decode().removeprefix("data: ").strip())
        assert marker["type"] == "ccproxy_idle_timeout"
        assert marker["idle_seconds"] == pytest.approx(0.05)

    async def test_every_inbound_frame_is_captured(self) -> None:
        """Every inbound WS message is recorded in the capture sink."""
        topic = "conversation-turn-cap"

        async def _handler(ws: ServerConnection) -> None:
            await ws.recv()
            async for raw in ws:
                cmd = json.loads(raw)[0]["command"]
                if cmd.get("type") != "subscribe":
                    continue
                # A non-turn frame (excluded from the answer) + the answer + DONE.
                await ws.send(json.dumps([_encoded_sse_frame("app_notifications", '{"notif": true}')]))
                await ws.send(json.dumps([_encoded_sse_frame(topic, '{"answer": true}')]))
                await ws.send(json.dumps([_done_frame(topic)]))

        get_ws_capture().clear()
        async with ws_serve(_handler, "127.0.0.1", 0, ping_interval=None) as server:
            port = server.sockets[0].getsockname()[1]
            ws_url = f"ws://127.0.0.1:{port}"
            fetch = _CountingFetch(ws_url)
            headers = _cookie_headers("capture-session")
            manager = SessionWSManager()
            with patch("ccproxy.openai_conversations.session_ws.fetch_ws_url", fetch):
                chunks = await _drain(
                    manager.stream_turn(topic_id=topic, client=_dummy_client(), request_headers=headers)
                )
                await manager.shutdown()

        joined = b"".join(chunks)
        assert b"notif" not in joined  # non-turn content excluded from the answer
        assert b"answer" in joined
        raws = "".join(r.raw for r in get_ws_capture().dump())
        assert "notif" in raws  # but it WAS captured — never dropped
        assert "answer" in raws

    async def test_different_sessions_get_different_sockets(self) -> None:
        """Two different session credentials open two distinct sockets."""
        topic = "conversation-turn-multi"
        connections = 0

        async def _handler(ws: ServerConnection) -> None:
            nonlocal connections
            connections += 1
            await ws.recv()
            async for raw in ws:
                cmd = json.loads(raw)[0]["command"]
                if cmd.get("type") != "subscribe":
                    continue
                await ws.send(json.dumps([_encoded_sse_frame(cmd["topic_id"], '{"ok": true}')]))
                await ws.send(json.dumps([_done_frame(cmd["topic_id"])]))

        async with ws_serve(_handler, "127.0.0.1", 0, ping_interval=None) as server:
            port = server.sockets[0].getsockname()[1]
            ws_url = f"ws://127.0.0.1:{port}"
            fetch = _CountingFetch(ws_url)
            manager = SessionWSManager()
            with patch("ccproxy.openai_conversations.session_ws.fetch_ws_url", fetch):
                await _drain(
                    manager.stream_turn(
                        topic_id=topic, client=_dummy_client(), request_headers=_cookie_headers("session-1")
                    )
                )
                await _drain(
                    manager.stream_turn(
                        topic_id=topic, client=_dummy_client(), request_headers=_cookie_headers("session-2")
                    )
                )
                assert len(manager._conns) == 2  # two distinct session connections
                await manager.shutdown()

        assert connections == 2
        assert fetch.calls == 2

    async def test_fetch_failure_yields_nothing_and_falls_back(self) -> None:
        """A ``fetch_ws_url`` failure yields nothing and never raises.

        With a session credential present, the persistent path is tried first;
        its connect fails, so the manager falls back to the per-turn bridge,
        whose own ``fetch_ws_url`` also fails → nothing yielded, no raise.
        """

        async def _boom(*args: object, **kwargs: object) -> str:
            raise RuntimeError("celsius ws/user failed: HTTP 403")

        manager = SessionWSManager()
        with (
            patch("ccproxy.openai_conversations.session_ws.fetch_ws_url", _boom),
            patch("ccproxy.openai_conversations.ws_handoff.fetch_ws_url", _boom),
        ):
            chunks = await _drain(
                manager.stream_turn(
                    topic_id="conversation-turn-x",
                    client=_dummy_client(),
                    request_headers=_cookie_headers("err-session"),
                )
            )
            await manager.shutdown()
        assert chunks == []

    async def test_no_credential_uses_per_turn_fallback(self) -> None:
        """No session credential → the per-turn bridge handles the turn."""
        topic = "conversation-turn-anon"

        async def _fake_bridge(
            *, client: object, topic_id: str, request_headers: dict[str, str] | None = None
        ) -> AsyncIterator[bytes]:
            assert topic_id == topic
            yield b'data: {"fallback": true}\n\n'

        manager = SessionWSManager()
        with patch("ccproxy.openai_conversations.session_ws.run_handoff_bridge", _fake_bridge):
            chunks = await _drain(
                manager.stream_turn(topic_id=topic, client=_dummy_client(), request_headers={"x-none": "1"})
            )
        assert b"fallback" in b"".join(chunks)


# ── Singleton ─────────────────────────────────────────────────────────────────


class TestSessionWSManagerSingleton:
    def test_singleton_identity(self) -> None:
        assert get_session_ws_manager() is get_session_ws_manager()

    def test_clear_resets_singleton(self) -> None:
        first = get_session_ws_manager()
        clear_session_ws_manager()
        assert get_session_ws_manager() is not first


# ── Sidecar seam ──────────────────────────────────────────────────────────────


class _AsyncChunkedStream(httpx.AsyncByteStream):
    """AsyncByteStream that yields pre-set chunks."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk


class TestSidecarRoutesThroughManager:
    """The sidecar continuation routes a handoff through the session manager."""

    async def test_continuation_routes_through_session_manager(self) -> None:
        topic = "conversation-turn-sidecar-mgr"
        mgr_chunk = b'data: {"delta": "via manager"}\n\n'
        captured_topic: dict[str, str] = {}

        class _HandoffBodyTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                payload = json.dumps(
                    {"type": "stream_handoff", "options": [{"type": "subscribe_ws_topic", "topic_id": topic}]}
                )
                return httpx.Response(200, stream=_AsyncChunkedStream([f"data: {payload}\n\n".encode()]))

        mock_client = httpx.AsyncClient(transport=_HandoffBodyTransport())

        async def _fake_stream_turn(
            self: object, *, topic_id: str, client: object, request_headers: dict[str, str]
        ) -> AsyncIterator[bytes]:
            captured_topic["topic"] = topic_id
            yield mgr_chunk

        sidecar = Sidecar()
        with (
            patch("ccproxy.transport.sidecar.transport") as m,
            patch.object(SessionWSManager, "stream_turn", _fake_stream_turn),
        ):
            m.get_client = AsyncMock(return_value=mock_client)
            m.UnknownFingerprintProfileError = UnknownFingerprintProfileError
            m.resolve_captured_fingerprint = MagicMock(return_value=None)
            await sidecar.start()
            try:
                received = bytearray()
                async with (
                    httpx.AsyncClient() as client,
                    client.stream(
                        "POST",
                        f"http://127.0.0.1:{sidecar.port}/backend-api/f/conversation",
                        headers={
                            "x-ccproxy-target-url": "https://chatgpt.com/backend-api/f/conversation",
                            "x-ccproxy-impersonate": "chrome136",
                            CONTINUATION_HEADER: "openai_conversations",
                        },
                        content=b"{}",
                    ) as resp,
                ):
                    async for chunk in resp.aiter_bytes():
                        received.extend(chunk)
            finally:
                await sidecar.stop()
                await mock_client.aclose()

        assert mgr_chunk in bytes(received)
        assert captured_topic["topic"] == topic


@pytest.mark.parametrize("present", [True, False])
async def test_shutdown_is_idempotent_and_safe(present: bool) -> None:
    """``shutdown`` is safe with or without live connections."""
    manager = SessionWSManager()
    if present:
        # Register a holder with no live socket — shutdown must not raise.
        await manager._get_conn(session_key="abc123")
    await manager.shutdown()
    await manager.shutdown()  # second call is a no-op
    assert manager._conns == {}
