"""Tests for ``ccproxy.openai_conversations.ws_handoff`` and the sidecar
continuation seam.

Coverage:
- ``HandoffState.feed`` / ``detect_handoff`` — topic extraction from
  ``stream_handoff`` / ``server_ste_metadata`` (telemetry, ignored) /
  ``resume_conversation_token`` (JWT ``turn_topic_id``).
- ``HandoffState.should_bridge`` — respects "no HTTP content" rule.
- ``_parse_ws_frames`` — JSON array vs single object.
- ``_sse_items_from_frame`` — ``encoded_item`` path and
  ``conversation-update`` path.
- ``_process_reply_frame`` — ``reply.topic_id`` filtering + ``catchups``
  iteration + ``[DONE]`` terminal.
- ``stream_handoff_sse`` using a fake in-process WebSocket server: frames
  fed by the server → SSE bytes yielded by the generator.
- End-to-end: fake HTTP SSE ending with a handoff marker + fake WS feeding
  frames → assembled SSE bytes piped through ``OpenAIConversationsIntakeFSM``
  → expected assistant text.
- Sidecar ``body_stream`` continuation: upstream body ends with a handoff
  marker + ``X-CCProxy-Continuation`` header → ``body_stream`` emits the
  WS-derived continuation.
- ``_refresh_sentinel`` chat-requirements expiry: decoded from the finalize
  token's JWT ``exp`` claim (unit test, no network).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from pydantic_ai.messages import TextPart
from pydantic_ai.models import ModelRequestParameters
from websockets.asyncio.server import ServerConnection
from websockets.asyncio.server import serve as ws_serve

from ccproxy.config import CCProxyConfig, Provider, set_config_instance
from ccproxy.inspector.openai_conversations_addon import _refresh_sentinel
from ccproxy.inspector.transport_override_addon import TransportOverrideAddon
from ccproxy.lightllm.graph.openai_conversations_intake import OpenAIConversationsIntakeFSM
from ccproxy.openai_conversations.ws_capture import get_ws_capture
from ccproxy.openai_conversations.ws_handoff import (
    HandoffState,
    _as_sse_bytes,
    _decode_encoded_item,
    _is_done_item,
    _message_signals_done,
    _parse_ws_frames,
    _topic_matches,
    _walk_sse_items,
    detect_handoff,
    run_handoff_bridge,
    stream_handoff_sse,
)
from ccproxy.transport import UnknownFingerprintProfileError
from ccproxy.transport.sidecar import CONTINUATION_HEADER, Sidecar

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_fsm(model: str = "gpt-5") -> OpenAIConversationsIntakeFSM:
    return OpenAIConversationsIntakeFSM(
        model=model,
        request_params=ModelRequestParameters(),
    )


_loop = asyncio.new_event_loop()


def _run(coro: Any) -> Any:
    return _loop.run_until_complete(coro)


def _sse(data: str) -> bytes:
    return f"data: {data}\n\n".encode()


def _stream_handoff_frame(topic_id: str) -> bytes:
    payload = {
        "type": "stream_handoff",
        "options": [
            {"type": "subscribe_ws_topic", "topic_id": topic_id},
        ],
    }
    return _sse(json.dumps(payload))


def _server_ste_frame(turn_exchange_id: str) -> bytes:
    payload = {"type": "server_ste_metadata", "turn_exchange_id": turn_exchange_id}
    return _sse(json.dumps(payload))


def _server_ste_nested_frame(turn_exchange_id: str) -> bytes:
    payload = {
        "type": "server_ste_metadata",
        "metadata": {"turn_exchange_id": turn_exchange_id},
    }
    return _sse(json.dumps(payload))


def _resume_token_frame(token: str = "tok123", conversation_id: str = "conv-resume") -> bytes:  # noqa: S107
    payload = {"type": "resume_conversation_token", "token": token, "conversation_id": conversation_id}
    return _sse(json.dumps(payload))


def _make_resume_jwt(turn_topic_id: str) -> str:
    """Build a resume_conversation_token JWT whose payload carries turn_topic_id
    (the conversation-turn-<id> WS topic) — mirrors the real conduit token."""
    header = base64.urlsafe_b64encode(b'{"alg":"ES256","typ":"JWT"}').rstrip(b"=").decode()
    claims = {"conduit_uuid": "u", "conduit_location": "10.0.0.1:8307", "turn_topic_id": turn_topic_id}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.sig"


def _resume_jwt_frame(turn_topic_id: str, conversation_id: str = "conv-jwt") -> bytes:
    payload = {
        "type": "resume_conversation_token",
        "kind": "topic",
        "token": _make_resume_jwt(turn_topic_id),
        "conversation_id": conversation_id,
    }
    return _sse(json.dumps(payload))


def _content_append_frame(channel: int, text: str) -> bytes:
    patch = {"p": "/message/content/parts/0", "o": "append", "v": text, "c": channel}
    return _sse(json.dumps(patch))


# ── HandoffState / detect_handoff ─────────────────────────────────────────────


class TestHandoffStateDetection:
    def test_no_handoff_initially(self) -> None:
        state = HandoffState()
        assert state.topic == ""
        assert not state.should_bridge()

    def test_stream_handoff_sets_topic(self) -> None:
        state = HandoffState()
        detect_handoff(state, _stream_handoff_frame("conversation-turn-abc123"))
        assert state.topic == "conversation-turn-abc123"

    def test_server_ste_metadata_is_not_a_handoff_topic(self) -> None:
        # server_ste_metadata is telemetry, not a WS handoff (it rides every turn);
        # it must not set a topic or trigger the WS bridge.
        state = HandoffState()
        detect_handoff(state, _server_ste_frame("xyz789"))
        assert state.topic == ""
        assert not state.should_bridge()

    def test_server_ste_metadata_nested_is_not_a_handoff_topic(self) -> None:
        state = HandoffState()
        detect_handoff(state, _server_ste_nested_frame("nested999"))
        assert state.topic == ""
        assert not state.should_bridge()

    def test_server_ste_with_resume_token_is_not_a_topic(self) -> None:
        # The real shape: a turn carries server_ste_metadata (telemetry) + a bare
        # (non-JWT) resume token. Neither sets a WS topic → no continuation.
        state = HandoffState()
        detect_handoff(state, _resume_token_frame(token="tokR", conversation_id="conv-R"))  # noqa: S106
        detect_handoff(state, _server_ste_frame("xyz789"))
        assert state.topic == ""
        assert state.should_bridge() is False

    def test_resume_jwt_turn_topic_id_drives_ws_bridge(self) -> None:
        # The conduit answer is on the WS; the resume_conversation_token JWT carries
        # turn_topic_id (conversation-turn-<id>) — the topic to subscribe to.
        state = HandoffState()
        detect_handoff(state, _resume_jwt_frame("conversation-turn-abc", conversation_id="c1"))
        assert state.topic == "conversation-turn-abc"
        assert state.should_bridge() is True  # JWT topic → WS path

    def test_resume_jwt_not_bridged_when_http_content_seen(self) -> None:
        # Inline content already streamed → nothing to continue, even with a JWT topic.
        state = HandoffState()
        detect_handoff(state, _resume_jwt_frame("conversation-turn-abc", conversation_id="c1"))
        detect_handoff(state, _content_append_frame(0, "inline answer"))
        assert state.http_content_seen is True
        assert state.should_bridge() is False

    def test_should_bridge_true_when_topic_and_no_http_content(self) -> None:
        state = HandoffState()
        detect_handoff(state, _stream_handoff_frame("conversation-turn-1"))
        assert state.should_bridge() is True

    def test_should_bridge_false_when_http_content_seen(self) -> None:
        state = HandoffState()
        detect_handoff(state, _stream_handoff_frame("conversation-turn-1"))
        # Simulate an HTTP content append arriving before the handoff.
        detect_handoff(state, _content_append_frame(1, "Hello"))
        assert state.http_content_seen is True
        assert state.should_bridge() is False

    def test_stream_handoff_topic_set_with_bare_resume_token(self) -> None:
        state = HandoffState()
        # A bare (non-JWT) resume_conversation_token plus a stream_handoff → the
        # stream_handoff's subscribe_ws_topic is the topic that drives the bridge.
        detect_handoff(state, _resume_token_frame(token="tokR", conversation_id="conv-R"))  # noqa: S106
        detect_handoff(state, _stream_handoff_frame("conversation-turn-2"))
        assert state.topic == "conversation-turn-2"
        assert state.should_bridge() is True

    def test_feed_handles_split_chunks(self) -> None:
        state = HandoffState()
        # topic_id in the frame is stored verbatim (no prefix added for stream_handoff).
        full = _stream_handoff_frame("turn-split-topic")
        mid = len(full) // 2
        detect_handoff(state, full[:mid])
        detect_handoff(state, full[mid:])
        assert state.topic == "turn-split-topic"

    def test_stream_handoff_with_multiple_options_picks_subscribe_ws_topic(self) -> None:
        payload = {
            "type": "stream_handoff",
            "options": [
                {"type": "other_option", "topic_id": "wrong"},
                {"type": "subscribe_ws_topic", "topic_id": "correct-topic"},
            ],
        }
        state = HandoffState()
        detect_handoff(state, _sse(json.dumps(payload)))
        assert state.topic == "correct-topic"

    def test_stream_handoff_with_no_matching_option_sets_empty_topic(self) -> None:
        payload = {"type": "stream_handoff", "options": [{"type": "other_option"}]}
        state = HandoffState()
        detect_handoff(state, _sse(json.dumps(payload)))
        # no subscribe_ws_topic → topic stays empty, should_bridge is False
        assert state.topic == ""
        assert not state.should_bridge()

    def test_batch_patch_triggers_http_content_seen(self) -> None:
        state = HandoffState()
        detect_handoff(state, _stream_handoff_frame("t1"))
        patch_batch = {
            "o": "patch",
            "v": [{"p": "/message/content/parts/0", "o": "append", "v": "Hi"}],
        }
        detect_handoff(state, _sse(json.dumps(patch_batch)))
        assert state.http_content_seen is True
        assert not state.should_bridge()


# ── Parametrized handoff extraction ──────────────────────────────────────────


@dataclass(frozen=True)
class HandoffExtractionCase:
    name: str
    """Scenario identifier."""

    frame_bytes: bytes
    """SSE bytes to feed."""

    expected_topic: str
    """Expected ``state.topic`` after feed."""

    expected_bridge: bool
    """Expected ``state.should_bridge()`` after feed."""


_HANDOFF_EXTRACTION_CASES: list[HandoffExtractionCase] = [
    HandoffExtractionCase(
        name="stream_handoff_basic",
        frame_bytes=_stream_handoff_frame("conversation-turn-basic"),
        expected_topic="conversation-turn-basic",
        expected_bridge=True,
    ),
    HandoffExtractionCase(
        name="server_ste_top_level_is_telemetry",
        frame_bytes=_server_ste_frame("top-level-id"),
        expected_topic="",
        expected_bridge=False,
    ),
    HandoffExtractionCase(
        name="server_ste_nested_is_telemetry",
        frame_bytes=_server_ste_nested_frame("nested-id"),
        expected_topic="",
        expected_bridge=False,
    ),
    HandoffExtractionCase(
        name="resume_token_no_bridge",
        frame_bytes=_resume_token_frame("resume123"),
        expected_topic="",
        expected_bridge=False,
    ),
]


@pytest.mark.parametrize(
    "case",
    [pytest.param(c, id=c.name) for c in _HANDOFF_EXTRACTION_CASES],
)
def test_handoff_extraction(case: HandoffExtractionCase) -> None:
    state = HandoffState()
    detect_handoff(state, case.frame_bytes)
    assert state.topic == case.expected_topic
    assert state.should_bridge() == case.expected_bridge


# ── _parse_ws_frames ──────────────────────────────────────────────────────────


class TestParseWsFrames:
    def test_json_array_returns_list(self) -> None:
        frames = [{"type": "reply", "reply": {}}, {"type": "message"}]
        raw = json.dumps(frames).encode()
        result = _parse_ws_frames(raw)
        assert len(result) == 2
        assert result[0]["type"] == "reply"

    def test_single_object_returns_list_of_one(self) -> None:
        frame = {"type": "message", "topic_id": "conv-turn-1"}
        raw = json.dumps(frame).encode()
        result = _parse_ws_frames(raw)
        assert result == [frame]

    def test_empty_bytes_returns_empty(self) -> None:
        assert _parse_ws_frames(b"") == []

    def test_invalid_json_returns_empty(self) -> None:
        assert _parse_ws_frames(b"not json") == []

    def test_array_with_non_dict_items_filtered(self) -> None:
        raw = json.dumps([{"type": "reply"}, "not a dict", 42]).encode()
        result = _parse_ws_frames(raw)
        assert result == [{"type": "reply"}]


# ── _walk_sse_items (envelope-agnostic structural extraction) ─────────────────


class TestWalkSseItems:
    def test_message_two_level_nesting(self) -> None:
        # message envelope: payload.payload.encoded_item
        topic = "conversation-turn-abc"
        frame = {"type": "message", "topic_id": topic, "payload": {"payload": {"encoded_item": "data: hi\n\n"}}}
        assert _walk_sse_items(frame) == [(topic, "data: hi\n\n")]

    def test_stream_item_one_level_nesting(self) -> None:
        # SPA shape: conversation-turn-stream / stream-item / payload.encoded_item
        topic = "conversation-turn-spa"
        frame = {
            "type": "conversation-turn-stream",
            "topic_id": topic,
            "payload": {"type": "stream-item", "stream_item_id": "s1", "encoded_item": "data: spa\n\n"},
        }
        assert _walk_sse_items(frame) == [(topic, "data: spa\n\n")]

    def test_reply_catchups_extracted_with_nearest_topic(self) -> None:
        topic = "conversation-turn-reply"
        frame = {
            "type": "reply",
            "reply": {
                "topic_id": topic,
                "catchups": [
                    {"topic_id": topic, "payload": {"payload": {"encoded_item": "data: c1\n\n"}}},
                    {"topic_id": topic, "payload": {"payload": {"encoded_item": "data: [DONE]\n\n"}}},
                ],
            },
        }
        items = _walk_sse_items(frame)
        assert items == [(topic, "data: c1\n\n"), (topic, "data: [DONE]\n\n")]

    def test_no_topic_anywhere_yields_none_topic(self) -> None:
        frame = {"payload": {"payload": {"encoded_item": "data: ok\n"}}}
        assert _walk_sse_items(frame) == [(None, "data: ok\n")]

    def test_nearest_enclosing_topic_wins(self) -> None:
        # An inner topic_id overrides an outer one for its subtree.
        frame = {
            "topic_id": "outer",
            "payload": {"topic_id": "inner", "encoded_item": "data: x\n\n"},
        }
        assert _walk_sse_items(frame) == [("inner", "data: x\n\n")]

    def test_multiple_items_in_document_order(self) -> None:
        frame = {
            "topic_id": "t",
            "items": [
                {"encoded_item": "data: a\n\n"},
                {"encoded_item": "data: b\n\n"},
            ],
        }
        assert _walk_sse_items(frame) == [("t", "data: a\n\n"), ("t", "data: b\n\n")]

    def test_empty_or_non_string_encoded_item_skipped(self) -> None:
        frame = {"topic_id": "t", "payload": {"encoded_item": ""}, "other": {"encoded_item": 42}}
        assert _walk_sse_items(frame) == []


class TestDecodeEncodedItem:
    # Ground truth (sdk.js _Nn + aurora): encoded_item is already plaintext SSE.
    # _decode_encoded_item is an honest passthrough; non-SSE payloads are forwarded
    # unchanged (the intake's telemetry surfaces them), never transformed on a guess.
    def test_plaintext_sse_passthrough(self) -> None:
        assert _decode_encoded_item("data: hello\n\n") == "data: hello\n\n"

    def test_event_prefixed_passthrough(self) -> None:
        assert _decode_encoded_item("event: delta\ndata: {}\n\n") == "event: delta\ndata: {}\n\n"

    def test_non_sse_payload_forwarded_unchanged(self) -> None:
        # A non-SSE blob is NOT transformed — returned verbatim so the intake
        # records it via its unparseable-frame telemetry.
        assert _decode_encoded_item('{"foo": 1}') == '{"foo": 1}'
        assert _decode_encoded_item("Zm9v") == "Zm9v"


class TestTopicMatches:
    def test_turn_topic_matches(self) -> None:
        assert _topic_matches("conversation-turn-1", "conversation-turn-1") is True

    def test_conversations_matches(self) -> None:
        assert _topic_matches("conversations", "conversation-turn-1") is True

    def test_untagged_matches(self) -> None:
        assert _topic_matches(None, "conversation-turn-1") is True

    def test_other_topic_excluded(self) -> None:
        assert _topic_matches("app_notifications", "conversation-turn-1") is False


class TestMessageSignalsDone:
    def test_done_marker_on_turn_topic(self) -> None:
        frame = {"topic_id": "t", "payload": {"type": "done"}}
        assert _message_signals_done(frame, turn_topic="t") is True

    def test_done_marker_on_other_topic_ignored(self) -> None:
        frame = {"topic_id": "app_notifications", "payload": {"type": "done"}}
        assert _message_signals_done(frame, turn_topic="t") is False

    def test_no_done_marker(self) -> None:
        frame = {"topic_id": "t", "payload": {"type": "stream-item", "encoded_item": "data: x\n\n"}}
        assert _message_signals_done(frame, turn_topic="t") is False


# ── _is_done_item / _as_sse_bytes ─────────────────────────────────────────────


class TestIsDoneItem:
    def test_done_with_space(self) -> None:
        assert _is_done_item("data: [DONE]\n\n") is True

    def test_done_without_space(self) -> None:
        assert _is_done_item("data:[DONE]\n") is True

    def test_non_done_item(self) -> None:
        assert _is_done_item("data: {}\n\n") is False


class TestAsSseBytes:
    def test_adds_double_newline(self) -> None:
        assert _as_sse_bytes("data: x") == b"data: x\n\n"

    def test_single_newline_completed(self) -> None:
        assert _as_sse_bytes("data: x\n") == b"data: x\n\n"

    def test_already_terminated_unchanged(self) -> None:
        assert _as_sse_bytes("data: x\n\n") == b"data: x\n\n"


class TestWsFrameCapture:
    def test_record_dump_clear(self) -> None:
        cap = get_ws_capture()
        cap.clear()
        cap.record(topic="t1", raw='{"a": 1}', forwarded=2)
        cap.record(topic="t1", raw='{"b": 2}', forwarded=0)
        records = cap.dump()
        assert [r.raw for r in records] == ['{"a": 1}', '{"b": 2}']
        assert [r.forwarded for r in records] == [2, 0]
        assert all(r.topic == "t1" for r in records)
        cap.clear()
        assert cap.dump() == []

    def test_singleton_identity(self) -> None:
        assert get_ws_capture() is get_ws_capture()

    def test_bounded_ring_evicts_oldest(self) -> None:
        from ccproxy.openai_conversations.ws_capture import WSFrameCapture

        cap = WSFrameCapture(max_frames=3)
        for i in range(5):
            cap.record(topic="t", raw=str(i), forwarded=0)
        assert [r.raw for r in cap.dump()] == ["2", "3", "4"]


# ── stream_handoff_sse — fake WS server ───────────────────────────────────────


def _make_encoded_sse_frame(topic_id: str, text: str) -> dict[str, Any]:
    """Build a WS message frame carrying an encoded SSE item."""
    return {
        "type": "message",
        "topic_id": topic_id,
        "payload": {"payload": {"encoded_item": f"data: {text}\n\n"}},
    }


def _make_done_frame(topic_id: str) -> dict[str, Any]:
    return {
        "type": "message",
        "topic_id": topic_id,
        "payload": {"payload": {"encoded_item": "data: [DONE]\n\n"}},
    }


async def _collect_ws_sse(ws_url: str, topic_id: str) -> list[bytes]:
    """Collect all bytes yielded by ``stream_handoff_sse`` into a list."""
    chunks: list[bytes] = []
    async for chunk in await stream_handoff_sse(ws_url=ws_url, topic_id=topic_id):
        chunks.append(chunk)
    return chunks


class TestStreamHandoffSseWithFakeServer:
    """Integration tests using a real in-process WebSocket server."""

    async def test_receives_encoded_items_from_ws(self) -> None:
        topic = "conversation-turn-ws1"
        received_texts: list[bytes] = []

        async def _handler(ws: ServerConnection) -> None:
            # Receive init + subscribe messages; ignore them.
            await ws.recv()  # init array
            await ws.recv()  # subscribe
            # Send two content frames then DONE.
            await ws.send(json.dumps([_make_encoded_sse_frame(topic, '{"delta": "Hello"}')]))
            await ws.send(json.dumps([_make_encoded_sse_frame(topic, '{"delta": " world"}')]))
            await ws.send(json.dumps([_make_done_frame(topic)]))

        async with ws_serve(_handler, "127.0.0.1", 0, ping_interval=None) as server:
            port = server.sockets[0].getsockname()[1]
            ws_url = f"ws://127.0.0.1:{port}"
            received_texts = await _collect_ws_sse(ws_url=ws_url, topic_id=topic)

        assert len(received_texts) >= 2
        joined = b"".join(received_texts)
        assert b'{"delta": "Hello"}' in joined
        assert b'{"delta": " world"}' in joined

    async def test_stops_on_done(self) -> None:
        topic = "conversation-turn-ws2"

        async def _handler(ws: ServerConnection) -> None:
            await ws.recv()
            await ws.recv()
            await ws.send(json.dumps([_make_done_frame(topic)]))
            # Additional frames after DONE should be ignored.
            await ws.send(json.dumps([_make_encoded_sse_frame(topic, '{"extra": true}')]))

        async with ws_serve(_handler, "127.0.0.1", 0, ping_interval=None) as server:
            port = server.sockets[0].getsockname()[1]
            ws_url = f"ws://127.0.0.1:{port}"
            chunks = await _collect_ws_sse(ws_url=ws_url, topic_id=topic)

        joined = b"".join(chunks)
        assert b"[DONE]" in joined
        assert b"extra" not in joined

    async def test_filters_wrong_topic_message_frames(self) -> None:
        topic = "conversation-turn-ws3"
        wrong_topic = "other-topic"

        async def _handler(ws: ServerConnection) -> None:
            await ws.recv()
            await ws.recv()
            # Wrong topic frame — should be ignored.
            await ws.send(json.dumps([_make_encoded_sse_frame(wrong_topic, '{"filtered": true}')]))
            # Correct topic frame.
            await ws.send(json.dumps([_make_encoded_sse_frame(topic, '{"correct": true}')]))
            await ws.send(json.dumps([_make_done_frame(topic)]))

        async with ws_serve(_handler, "127.0.0.1", 0, ping_interval=None) as server:
            port = server.sockets[0].getsockname()[1]
            ws_url = f"ws://127.0.0.1:{port}"
            chunks = await _collect_ws_sse(ws_url=ws_url, topic_id=topic)

        joined = b"".join(chunks)
        assert b"filtered" not in joined
        assert b"correct" in joined

    async def test_captures_every_frame_including_non_turn(self) -> None:
        """Non-turn frames are excluded from the answer but never dropped — every
        inbound WS message is recorded in the capture sink."""
        topic = "conversation-turn-cap"

        async def _handler(ws: ServerConnection) -> None:
            await ws.recv()
            await ws.recv()
            # A non-turn frame (app_notifications) — excluded from the answer, captured.
            await ws.send(json.dumps([_make_encoded_sse_frame("app_notifications", '{"notif": true}')]))
            await ws.send(json.dumps([_make_encoded_sse_frame(topic, '{"answer": true}')]))
            await ws.send(json.dumps([_make_done_frame(topic)]))

        get_ws_capture().clear()
        async with ws_serve(_handler, "127.0.0.1", 0, ping_interval=None) as server:
            port = server.sockets[0].getsockname()[1]
            ws_url = f"ws://127.0.0.1:{port}"
            chunks = await _collect_ws_sse(ws_url=ws_url, topic_id=topic)

        joined = b"".join(chunks)
        assert b"notif" not in joined  # non-turn content kept out of the answer
        assert b"answer" in joined

        captured = get_ws_capture().dump()
        raws = "".join(r.raw for r in captured)
        assert "notif" in raws  # but it WAS captured — never silently dropped
        assert "answer" in raws
        notif_recs = [r for r in captured if "notif" in r.raw]
        assert notif_recs and all(r.forwarded == 0 for r in notif_recs)

    async def test_reply_frame_with_catchups_processed(self) -> None:
        topic = "conversation-turn-ws4"

        async def _handler(ws: ServerConnection) -> None:
            await ws.recv()
            await ws.recv()
            # Reply frame with catchups.
            reply_frame = {
                "type": "reply",
                "reply": {
                    "topic_id": topic,
                    "catchups": [
                        {
                            "topic_id": topic,
                            "payload": {"payload": {"encoded_item": 'data: {"from": "catchup"}\n\n'}},
                        }
                    ],
                },
            }
            await ws.send(json.dumps([reply_frame]))
            await ws.send(json.dumps([_make_done_frame(topic)]))

        async with ws_serve(_handler, "127.0.0.1", 0, ping_interval=None) as server:
            port = server.sockets[0].getsockname()[1]
            ws_url = f"ws://127.0.0.1:{port}"
            chunks = await _collect_ws_sse(ws_url=ws_url, topic_id=topic)

        joined = b"".join(chunks)
        assert b"catchup" in joined

    async def test_error_on_connect_yields_nothing(self) -> None:
        # Non-existent WS server — run_handoff_bridge should yield nothing.
        mock_client = AsyncMock(spec=httpx.AsyncClient)

        async def _bad_fetch(*args: object, **kwargs: object) -> str:
            raise RuntimeError("no server")

        with patch("ccproxy.openai_conversations.ws_handoff.fetch_ws_url", _bad_fetch):
            chunks: list[bytes] = []
            async for chunk in run_handoff_bridge(client=mock_client, topic_id="t1"):
                chunks.append(chunk)
        assert chunks == []


# ── Idle-timeout terminal signal (P-6 regression) ─────────────────────────────


class TestIdleTimeoutTerminalSignal:
    """A turn idled past the read-timeout backstop must end on an explicit,
    classifiable terminal marker — never silent generator exhaustion — so it
    is distinguishable from a genuine upstream ``[DONE]`` completion."""

    async def test_idle_timeout_yields_typed_marker_instead_of_silence(self) -> None:
        topic = "conversation-turn-idle"

        async def _handler(ws: ServerConnection) -> None:
            await ws.recv()  # init
            await ws.recv()  # subscribe
            # Never send another frame — blocks until the client tears the
            # socket down once its idle timeout fires.
            with contextlib.suppress(Exception):
                await ws.recv()

        async with ws_serve(_handler, "127.0.0.1", 0, ping_interval=None) as server:
            port = server.sockets[0].getsockname()[1]
            ws_url = f"ws://127.0.0.1:{port}"
            chunks: list[bytes] = []
            async for chunk in await stream_handoff_sse(ws_url=ws_url, topic_id=topic, read_timeout=0.05):
                chunks.append(chunk)

        joined = b"".join(chunks)
        assert b"[DONE]" not in joined  # never a false completion signal
        assert b"ccproxy_idle_timeout" in joined
        marker = json.loads(joined.decode().removeprefix("data: ").strip())
        assert marker["type"] == "ccproxy_idle_timeout"
        assert marker["idle_seconds"] == pytest.approx(0.05)

    async def test_idle_timeout_distinguishable_from_done_completion(self) -> None:
        """The idle-timeout marker and a genuine [DONE] never both appear, and
        each termination shape uniquely identifies its own cause."""
        topic_done = "conversation-turn-cmp-done"
        topic_idle = "conversation-turn-cmp-idle"

        async def _done_handler(ws: ServerConnection) -> None:
            await ws.recv()
            await ws.recv()
            await ws.send(json.dumps([_make_done_frame(topic_done)]))

        async def _idle_handler(ws: ServerConnection) -> None:
            await ws.recv()
            await ws.recv()
            with contextlib.suppress(Exception):
                await ws.recv()

        async with ws_serve(_done_handler, "127.0.0.1", 0, ping_interval=None) as server:
            port = server.sockets[0].getsockname()[1]
            done_chunks = await _collect_ws_sse(ws_url=f"ws://127.0.0.1:{port}", topic_id=topic_done)

        async with ws_serve(_idle_handler, "127.0.0.1", 0, ping_interval=None) as server:
            port = server.sockets[0].getsockname()[1]
            idle_chunks: list[bytes] = []
            async for chunk in await stream_handoff_sse(
                ws_url=f"ws://127.0.0.1:{port}", topic_id=topic_idle, read_timeout=0.05
            ):
                idle_chunks.append(chunk)

        done_joined = b"".join(done_chunks)
        idle_joined = b"".join(idle_chunks)
        assert b"[DONE]" in done_joined
        assert b"ccproxy_idle_timeout" not in done_joined
        assert b"[DONE]" not in idle_joined
        assert b"ccproxy_idle_timeout" in idle_joined


# ── End-to-end: HTTP SSE + WS frames → intake FSM → text ─────────────────────


class TestEndToEndHandoffToIntake:
    """Feed a full HTTP-handoff-SSE body + WS frames through the intake FSM."""

    async def test_full_handoff_flow_yields_assistant_text(self) -> None:
        topic = "conversation-turn-e2e"

        # 1. Build the HTTP SSE body: add frame + handoff marker.
        add_frame = json.dumps(
            {
                "p": "",
                "o": "add",
                "v": {
                    "message": {
                        "id": "msg-e2e-1",
                        "author": {"role": "assistant"},
                        "content": {"content_type": "text", "parts": [""]},
                        "status": "in_progress",
                        "metadata": {"model_slug": "gpt-5"},
                    },
                    "conversation_id": "conv-e2e-1",
                },
                "c": 0,
            }
        )
        http_body = f"data: {add_frame}\n\n".encode() + _stream_handoff_frame(topic)

        # 2. The WS server sends the actual text content via SSE patches.

        async def _handler(ws: ServerConnection) -> None:
            await ws.recv()
            await ws.recv()
            await ws.send(
                json.dumps(
                    [
                        {
                            "type": "message",
                            "topic_id": topic,
                            "payload": {
                                "payload": {
                                    "encoded_item": (
                                        # An SSE item as produced by ChatGPT: a content-parts append.
                                        "data: "
                                        + json.dumps(
                                            {
                                                "p": "/message/content/parts/0",
                                                "o": "append",
                                                "v": "Hello from WS",
                                                "c": 0,
                                            }
                                        )
                                        + "\n\n"
                                    )
                                }
                            },
                        }
                    ]
                )
            )
            # Status patch → finish.
            await ws.send(
                json.dumps(
                    [
                        {
                            "type": "message",
                            "topic_id": topic,
                            "payload": {
                                "payload": {
                                    "encoded_item": (
                                        "data: "
                                        + json.dumps(
                                            {
                                                "p": "/message/status",
                                                "o": "replace",
                                                "v": "finished_successfully",
                                                "c": 0,
                                            }
                                        )
                                        + "\n\n"
                                    )
                                }
                            },
                        }
                    ]
                )
            )
            await ws.send(json.dumps([_make_done_frame(topic)]))

        async with ws_serve(_handler, "127.0.0.1", 0, ping_interval=None) as server:
            port = server.sockets[0].getsockname()[1]
            ws_url = f"ws://127.0.0.1:{port}"

            # Collect WS SSE bytes.
            ws_chunks: list[bytes] = await _collect_ws_sse(ws_url=ws_url, topic_id=topic)

        # 3. Feed HTTP body + WS continuation through the intake FSM.
        # The HTTP handoff side event is consumed silently (continuation metadata
        # set, no IR event, no raise); the WS chunks carry the real answer.
        fsm = _make_fsm()
        await fsm.feed(http_body)

        # Feed WS continuation bytes.
        for chunk in ws_chunks:
            await fsm.feed(chunk)
        await fsm.close()

        # 4. Assert we got the text from WS.
        parts = list(fsm.parts_manager.get_parts())
        text = "".join(p.content for p in parts if isinstance(p, TextPart))
        assert "Hello from WS" in text


# ── Sidecar continuation seam ─────────────────────────────────────────────────


class _AsyncChunkedStream(httpx.AsyncByteStream):
    """AsyncByteStream that yields pre-set chunks (mirrors test_transport_sidecar)."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk


class TestSidecarContinuationSeam:
    """body_stream() yields WS continuation bytes after HTTP body ends."""

    async def test_continuation_header_stripped_before_upstream(self) -> None:
        """The CONTINUATION_HEADER must not be forwarded to the upstream."""
        received_headers: list[dict[str, str]] = []

        class _RecordingTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                received_headers.append({k.lower(): v for k, v in request.headers.items()})
                return httpx.Response(
                    200,
                    stream=_AsyncChunkedStream([b"ok"]),
                )

        mock_client = httpx.AsyncClient(transport=_RecordingTransport())
        sidecar = Sidecar()
        with patch("ccproxy.transport.sidecar.transport") as m:
            m.get_client = AsyncMock(return_value=mock_client)
            m.UnknownFingerprintProfileError = UnknownFingerprintProfileError
            m.resolve_captured_fingerprint = MagicMock(return_value=None)
            await sidecar.start()
            try:
                async with (
                    httpx.AsyncClient() as client,
                    client.stream(
                        "POST",
                        f"http://127.0.0.1:{sidecar.port}/v1/messages",
                        headers={
                            "x-ccproxy-target-url": "https://chatgpt.com/backend-api/f/conversation",
                            "x-ccproxy-impersonate": "chrome136",
                            CONTINUATION_HEADER: "openai_conversations",
                        },
                        content=b"{}",
                    ) as resp,
                ):
                    await resp.aread()
            finally:
                await sidecar.stop()
                await mock_client.aclose()

        assert len(received_headers) == 1
        assert CONTINUATION_HEADER not in received_headers[0]

    async def test_no_continuation_noop_for_non_oaic_providers(self) -> None:
        """Without CONTINUATION_HEADER the sidecar is byte-for-byte unchanged."""

        class _OkTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                return httpx.Response(
                    200,
                    stream=_AsyncChunkedStream([b"plain-body"]),
                )

        mock_client = httpx.AsyncClient(transport=_OkTransport())
        sidecar = Sidecar()
        with patch("ccproxy.transport.sidecar.transport") as m:
            m.get_client = AsyncMock(return_value=mock_client)
            m.UnknownFingerprintProfileError = UnknownFingerprintProfileError
            m.resolve_captured_fingerprint = MagicMock(return_value=None)
            await sidecar.start()
            try:
                async with (
                    httpx.AsyncClient() as client,
                    client.stream(
                        "POST",
                        f"http://127.0.0.1:{sidecar.port}/v1/messages",
                        headers={
                            "x-ccproxy-target-url": "https://api.anthropic.com/v1/messages",
                            "x-ccproxy-impersonate": "chrome131",
                            # No CONTINUATION_HEADER.
                        },
                        content=b"{}",
                    ) as resp,
                ):
                    body = await resp.aread()
            finally:
                await sidecar.stop()
                await mock_client.aclose()

        assert body == b"plain-body"

    async def test_continuation_bridges_after_http_body(self) -> None:
        """When CONTINUATION_HEADER is set and a handoff topic is found, the
        sidecar routes through the session WS manager, which (with no session
        credential in these headers) falls back to the per-turn bridge."""
        topic = "conversation-turn-sidecar-test"
        ws_chunk = b'data: {"delta": "WS answer"}\n\n'

        class _HandoffBodyTransport(httpx.AsyncBaseTransport):
            """Returns HTTP body ending with a stream_handoff marker."""

            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                payload = json.dumps(
                    {
                        "type": "stream_handoff",
                        "options": [{"type": "subscribe_ws_topic", "topic_id": topic}],
                    }
                )
                body = f"data: {payload}\n\n".encode()
                return httpx.Response(
                    200,
                    stream=_AsyncChunkedStream([body]),
                )

        mock_client = httpx.AsyncClient(transport=_HandoffBodyTransport())

        async def _fake_bridge(
            *, client: object, topic_id: str, request_headers: dict[str, str] | None = None
        ) -> AsyncIterator[bytes]:
            assert topic_id == topic
            yield ws_chunk

        sidecar = Sidecar()
        with (
            patch("ccproxy.transport.sidecar.transport") as m,
            patch("ccproxy.openai_conversations.session_ws.run_handoff_bridge", _fake_bridge),
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

        assert ws_chunk in bytes(received)


# ── TransportOverrideAddon stamps CONTINUATION_HEADER ─────────────────────────


class TestTransportOverrideAddonContinuationHeader:
    """TransportOverrideAddon stamps X-CCProxy-Continuation for openai_conversations."""

    async def test_openai_conversations_provider_stamps_continuation_header(self) -> None:
        provider = Provider(
            base_url="https://chatgpt.com",
            type="openai_conversations",
            fingerprint_profile="chrome136",
        )
        cfg = CCProxyConfig(providers={"oaic": provider})
        set_config_instance(cfg)

        flow = MagicMock()
        flow.id = "flow-oaic"
        flow.metadata = {"ccproxy.auth_provider": "oaic"}
        flow.request.pretty_url = "https://chatgpt.com/backend-api/f/conversation"
        flow.request.headers = {}

        addon = TransportOverrideAddon(sidecar_port=19300)
        await addon.request(flow)

        assert flow.request.headers.get(CONTINUATION_HEADER) == "openai_conversations"

    async def test_non_openai_conversations_provider_no_continuation_header(self) -> None:
        provider = Provider(
            base_url="https://api.anthropic.com",
            type="anthropic",
            fingerprint_profile="chrome131",
        )
        cfg = CCProxyConfig(providers={"anthropic": provider})
        set_config_instance(cfg)

        flow = MagicMock()
        flow.id = "flow-anth"
        flow.metadata = {"ccproxy.auth_provider": "anthropic"}
        flow.request.pretty_url = "https://api.anthropic.com/v1/messages"
        flow.request.headers = {}

        addon = TransportOverrideAddon(sidecar_port=19300)
        await addon.request(flow)

        assert CONTINUATION_HEADER not in flow.request.headers


# ── expires_at unit guard ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class ExpiresAtCase:
    name: str
    """Scenario identifier."""

    exp_seconds: int
    """``exp`` claim (unix seconds) encoded into the finalize JWT."""

    expected_ms: int
    """Expected ``chat_req_token_expires_at_ms`` stored after decoding."""


_EXPIRES_AT_CASES: list[ExpiresAtCase] = [
    ExpiresAtCase(
        name="jwt_exp_seconds_to_ms",
        exp_seconds=1_750_000_000,
        expected_ms=1_750_000_000 * 1000,
    ),
    ExpiresAtCase(
        name="later_jwt_exp_seconds_to_ms",
        exp_seconds=1_900_000_000,
        expected_ms=1_900_000_000 * 1000,
    ),
]


def _jwt_with_exp(exp_seconds: int) -> str:
    """Build a minimal unsigned JWT carrying the given exp claim."""
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp_seconds}).encode()).decode().rstrip("=")
    return f"{header}.{payload}.sig"


@pytest.mark.parametrize(
    "case",
    [pytest.param(c, id=c.name) for c in _EXPIRES_AT_CASES],
)
async def test_sentinel_expires_decoded_from_finalize_jwt(case: ExpiresAtCase) -> None:
    """_refresh_sentinel decodes chat_req_token_expires_at_ms from the finalize JWT exp."""
    stored: dict[str, object] = {}
    finalize_token = _jwt_with_exp(case.exp_seconds)

    async def _fake_post(url: str, **kwargs: object) -> MagicMock:
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        if url.endswith("/finalize"):
            resp.json = MagicMock(return_value={"token": finalize_token, "persona": "chatgpt-paid"})
        else:
            resp.json = MagicMock(
                return_value={"prepare_token": "prep", "proofofwork": {"required": False}, "persona": "chatgpt-paid"}
            )
        return resp

    def _fake_update(path: str, **kwargs: object) -> None:
        stored.update(kwargs)

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.post = _fake_post

    with (
        patch("ccproxy.inspector.openai_conversations_addon.update_sentinel_fields", _fake_update),
        patch("ccproxy.inspector.openai_conversations_addon.build_requirements_token", return_value="gAAAAACp"),
    ):
        result = await _refresh_sentinel(
            client=mock_client,
            credential_path="/tmp/fake-creds.json",  # noqa: S108
            access_token="bearer-jwt",  # noqa: S106
            device_id="device-1",
            timeout=5.0,
        )

    assert stored["chat_req_token_expires_at_ms"] == case.expected_ms
    assert stored["chat_req_token"] == finalize_token
    assert result.expires_at_ms == case.expected_ms
    assert result.persona == "chatgpt-paid"
