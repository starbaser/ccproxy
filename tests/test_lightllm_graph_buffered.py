"""Tests for the FSM-driven buffered response transform.

Covers the four provider paths in
:func:`transform_buffered_response_sync`:

* **Anthropic buffered** — ``BetaMessage`` JSON → synthetic SSE → FSM intake →
  OpenAI ``ChatCompletion`` JSON.
* **OpenAI buffered** — ``ChatCompletion`` JSON → synthetic SSE → FSM intake →
  Anthropic ``BetaMessage`` JSON (the other direction).
* **OpenAI Responses buffered** — ``Response`` JSON → synthetic Responses SSE →
  FSM intake → listener JSON.
* **Google buffered** — ``GenerateContentResponse`` JSON → one SSE frame →
  FSM intake → OpenAI ``ChatCompletion`` JSON.
* **Perplexity buffered** — concatenated SSE → fed directly → FSM intake →
  OpenAI ``ChatCompletion`` JSON.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic_ai.models import ModelRequestParameters

from ccproxy.lightllm.graph.buffered import transform_buffered_response_sync
from ccproxy.lightllm.parsed import InboundFormat

# ── Anthropic buffered → OpenAI ChatCompletion ─────────────────────────────


def _make_anthropic_text_body(text: str, *, model: str = "claude-3-5-haiku-20241022") -> bytes:
    return json.dumps(
        {
            "id": "msg_buf_test",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "model": model,
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
    ).encode()


def _make_anthropic_tool_body() -> bytes:
    return json.dumps(
        {
            "id": "msg_buf_tool",
            "type": "message",
            "role": "assistant",
            "content": [
                {"type": "text", "text": "I'll check the weather"},
                {
                    "type": "tool_use",
                    "id": "toolu_abc",
                    "name": "get_weather",
                    "input": {"city": "Paris"},
                },
            ],
            "model": "claude-3-5-haiku-20241022",
            "stop_reason": "tool_use",
            "stop_sequence": None,
            "usage": {"input_tokens": 20, "output_tokens": 15},
        }
    ).encode()


class TestAnthropicBufferedToOpenAI:
    def test_simple_text(self) -> None:
        raw = _make_anthropic_text_body("Hello world")
        out_bytes = transform_buffered_response_sync(
            raw_bytes=raw,
            provider_type="anthropic",
            inbound_format=InboundFormat.OPENAI_CHAT,
            model="claude-3-5-haiku-20241022",
            request_params=ModelRequestParameters(),
        )
        out = json.loads(out_bytes)
        assert out["object"] == "chat.completion"
        assert out["choices"][0]["message"]["content"] == "Hello world"
        assert out["choices"][0]["finish_reason"] == "stop"
        assert out["choices"][0]["message"]["role"] == "assistant"
        # Funnel: the upstream usage (10 in / 5 out) is projected into the
        # OpenAI usage block rather than dropped.
        assert out["usage"] == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

    def test_tool_call_extraction(self) -> None:
        raw = _make_anthropic_tool_body()
        out_bytes = transform_buffered_response_sync(
            raw_bytes=raw,
            provider_type="anthropic",
            inbound_format=InboundFormat.OPENAI_CHAT,
            model="claude-3-5-haiku-20241022",
            request_params=ModelRequestParameters(),
        )
        out = json.loads(out_bytes)
        choice = out["choices"][0]
        # Tool call surfaces in OpenAI shape.
        tool_calls = choice["message"].get("tool_calls") or []
        assert len(tool_calls) == 1
        tc = tool_calls[0]
        assert tc["function"]["name"] == "get_weather"
        args = json.loads(tc["function"]["arguments"])
        assert args == {"city": "Paris"}
        # Text-and-tool answer carries text + tool_calls; finish_reason is tool_calls.
        assert "weather" in (choice["message"]["content"] or "")
        assert choice["finish_reason"] == "tool_calls"
        # Funnel: upstream usage (20 in / 15 out) is projected, not dropped.
        assert out["usage"] == {"prompt_tokens": 20, "completion_tokens": 15, "total_tokens": 35}

    def test_alias_providers(self) -> None:
        """The Anthropic synthesizer applies to ``deepseek`` and ``zai`` too."""
        raw = _make_anthropic_text_body("via deepseek", model="deepseek-chat")
        for alias in ("deepseek", "zai"):
            out_bytes = transform_buffered_response_sync(
                raw_bytes=raw,
                provider_type=alias,
                inbound_format=InboundFormat.OPENAI_CHAT,
                model="deepseek-chat",
                request_params=ModelRequestParameters(),
            )
            out = json.loads(out_bytes)
            assert out["choices"][0]["message"]["content"] == "via deepseek"

    def test_minimax_provider(self) -> None:
        """The Anthropic synthesizer applies to the MiniMax provider."""
        raw = _make_anthropic_text_body("via MiniMax", model="MiniMax-M3")

        out_bytes = transform_buffered_response_sync(
            raw_bytes=raw,
            provider_type="minimax",
            inbound_format=InboundFormat.OPENAI_CHAT,
            model="MiniMax-M3",
            request_params=ModelRequestParameters(),
        )

        out = json.loads(out_bytes)
        assert out["choices"][0]["message"]["content"] == "via MiniMax"


# ── Anthropic buffered → OpenAI Responses ──────────────────────────────────


class TestAnthropicBufferedToOpenAIResponses:
    """Phase 4A end-to-end: Anthropic upstream + /v1/responses listener.

    The Codex CLI smoke-test path: client POSTs Responses-shape, ccproxy
    cross-format-transforms to Anthropic upstream, response comes back
    as BetaMessage JSON and gets synthesized into a Responses envelope.
    """

    def test_simple_text(self) -> None:
        raw = _make_anthropic_text_body("Hello world")
        out_bytes = transform_buffered_response_sync(
            raw_bytes=raw,
            provider_type="anthropic",
            inbound_format=InboundFormat.OPENAI_RESPONSES,
            model="claude-3-5-haiku-20241022",
            request_params=ModelRequestParameters(),
        )
        out = json.loads(out_bytes)
        assert out["object"] == "response"
        assert out["model"] == "claude-3-5-haiku-20241022"
        assert out["status"] == "completed"
        assert out["output"] == [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Hello world"}],
            }
        ]
        assert out["id"].startswith("resp_") or out["id"]

    def test_tool_call_extraction(self) -> None:
        raw = _make_anthropic_tool_body()
        out_bytes = transform_buffered_response_sync(
            raw_bytes=raw,
            provider_type="anthropic",
            inbound_format=InboundFormat.OPENAI_RESPONSES,
            model="claude-3-5-haiku-20241022",
            request_params=ModelRequestParameters(),
        )
        out = json.loads(out_bytes)
        kinds = [item["type"] for item in out["output"]]
        assert "message" in kinds
        assert "function_call" in kinds
        fn = next(it for it in out["output"] if it["type"] == "function_call")
        assert fn["name"] == "get_weather"
        assert json.loads(fn["arguments"]) == {"city": "Paris"}


# ── OpenAI buffered → Anthropic BetaMessage ────────────────────────────────


def _make_openai_chat_completion(content: str) -> bytes:
    return json.dumps(
        {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 1700000000,
            "model": "gpt-4o",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": content,
                    },
                    "finish_reason": "stop",
                    "logprobs": None,
                }
            ],
        }
    ).encode()


def _make_openai_tool_completion() -> bytes:
    return json.dumps(
        {
            "id": "chatcmpl-tool",
            "object": "chat.completion",
            "created": 1700000000,
            "model": "gpt-4o",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_abc",
                                "type": "function",
                                "function": {
                                    "name": "get_time",
                                    "arguments": '{"timezone": "UTC"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                    "logprobs": None,
                }
            ],
        }
    ).encode()


class TestOpenAIBufferedToAnthropic:
    def test_simple_text(self) -> None:
        raw = _make_openai_chat_completion("Hi there")
        out_bytes = transform_buffered_response_sync(
            raw_bytes=raw,
            provider_type="openai",
            inbound_format=InboundFormat.ANTHROPIC_MESSAGES,
            model="gpt-4o",
            request_params=ModelRequestParameters(),
        )
        out = json.loads(out_bytes)
        assert out["type"] == "message"
        assert out["role"] == "assistant"
        assert out["model"] == "gpt-4o"
        assert out["stop_reason"] == "end_turn"
        # Single text block carrying the assembled content.
        text_blocks = [b for b in out["content"] if b.get("type") == "text"]
        assert len(text_blocks) == 1
        assert text_blocks[0]["text"] == "Hi there"

    def test_tool_call_extraction(self) -> None:
        raw = _make_openai_tool_completion()
        out_bytes = transform_buffered_response_sync(
            raw_bytes=raw,
            provider_type="openai",
            inbound_format=InboundFormat.ANTHROPIC_MESSAGES,
            model="gpt-4o",
            request_params=ModelRequestParameters(),
        )
        out = json.loads(out_bytes)
        tool_blocks = [b for b in out["content"] if b.get("type") == "tool_use"]
        assert len(tool_blocks) == 1
        tb = tool_blocks[0]
        assert tb["name"] == "get_time"
        assert tb["input"] == {"timezone": "UTC"}
        assert out["stop_reason"] == "tool_use"


# ── OpenAI Responses buffered → OpenAI Chat / Anthropic ───────────────────


def _make_openai_responses_body() -> bytes:
    return json.dumps(
        {
            "id": "resp_buf_test",
            "object": "response",
            "created_at": 1700000000,
            "model": "gpt-5",
            "status": "completed",
            "output": [
                {
                    "id": "msg_001",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Responses text",
                            "annotations": [],
                        }
                    ],
                },
                {
                    "id": "fc_001",
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "lookup",
                    "arguments": '{"q":"hi"}',
                    "status": "completed",
                },
            ],
            "parallel_tool_calls": True,
            "tool_choice": "auto",
            "tools": [],
        }
    ).encode()


class TestOpenAIResponsesBuffered:
    def test_to_openai_chat(self) -> None:
        out_bytes = transform_buffered_response_sync(
            raw_bytes=_make_openai_responses_body(),
            provider_type="openai_responses",
            inbound_format=InboundFormat.OPENAI_CHAT,
            model="gpt-5",
            request_params=ModelRequestParameters(),
        )
        out = json.loads(out_bytes)
        choice = out["choices"][0]
        assert choice["message"]["content"] == "Responses text"
        [tool_call] = choice["message"]["tool_calls"]
        assert tool_call["function"]["name"] == "lookup"
        assert json.loads(tool_call["function"]["arguments"]) == {"q": "hi"}
        assert choice["finish_reason"] == "tool_calls"

    def test_to_anthropic(self) -> None:
        out_bytes = transform_buffered_response_sync(
            raw_bytes=_make_openai_responses_body(),
            provider_type="openai_responses",
            inbound_format=InboundFormat.ANTHROPIC_MESSAGES,
            model="gpt-5",
            request_params=ModelRequestParameters(),
        )
        out = json.loads(out_bytes)
        assert out["type"] == "message"
        assert out["content"][0] == {"type": "text", "text": "Responses text"}
        tool_block = out["content"][1]
        assert tool_block["type"] == "tool_use"
        assert tool_block["name"] == "lookup"
        assert tool_block["input"] == {"q": "hi"}


# ── Google buffered → OpenAI ChatCompletion ────────────────────────────────


def _make_google_generate_content_response(text: str) -> bytes:
    return json.dumps(
        {
            "candidates": [
                {
                    "content": {
                        "parts": [{"text": text}],
                        "role": "model",
                    },
                    "finishReason": "STOP",
                    "index": 0,
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 10,
                "candidatesTokenCount": 3,
                "totalTokenCount": 13,
            },
            "modelVersion": "gemini-2.0-flash",
        }
    ).encode()


def _make_google_cloudcode_wrapped(text: str) -> bytes:
    """cloudcode-pa wraps the response in {response: {...}}."""
    inner = json.loads(_make_google_generate_content_response(text))
    return json.dumps({"response": inner}).encode()


class TestGoogleBufferedToOpenAI:
    def test_simple_text(self) -> None:
        raw = _make_google_generate_content_response("From Gemini")
        out_bytes = transform_buffered_response_sync(
            raw_bytes=raw,
            provider_type="gemini",
            inbound_format=InboundFormat.OPENAI_CHAT,
            model="gemini-2.0-flash",
            request_params=ModelRequestParameters(),
        )
        out = json.loads(out_bytes)
        assert out["object"] == "chat.completion"
        assert out["choices"][0]["message"]["content"] == "From Gemini"

    def test_cloudcode_envelope_unwrap(self) -> None:
        """The Google intake folds the cloudcode-pa ``{response: {...}}`` unwrap
        so the buffered transform inherits the behavior."""
        raw = _make_google_cloudcode_wrapped("Wrapped reply")
        out_bytes = transform_buffered_response_sync(
            raw_bytes=raw,
            provider_type="gemini",
            inbound_format=InboundFormat.OPENAI_CHAT,
            model="gemini-2.0-flash",
            request_params=ModelRequestParameters(),
        )
        out = json.loads(out_bytes)
        assert out["choices"][0]["message"]["content"] == "Wrapped reply"


# ── Perplexity buffered (SSE concatenated) → OpenAI ChatCompletion ─────────


def _make_perplexity_sse(answer_text: str) -> bytes:
    """Build a minimal Perplexity SSE concatenated body.

    Each event is one JSON dict per ``data:`` line. The intake parses any
    valid Perplexity event shape; here we use the diff_block Mode C
    incremental-append pattern + a final ``final_sse_message`` event.
    """
    events: list[dict[str, Any]] = [
        {
            "backend_uuid": "be-1",
            "context_uuid": "ctx-1",
            "read_write_token": "rw-1",
            "thread_url_slug": "slug",
            "blocks": [
                {
                    "intended_usage": "answer",
                    "markdown_block": {
                        "answer": "",
                        "chunks": [""],
                    },
                    "diff_block": {
                        "field": "markdown_block",
                        "patches": [{"path": "/chunks/0", "value": answer_text}],
                    },
                }
            ],
        },
        {
            "final_sse_message": True,
            "blocks": [
                {
                    "intended_usage": "answer",
                    "markdown_block": {"answer": answer_text},
                }
            ],
        },
    ]
    return b"".join(f"data: {json.dumps(e, separators=(',', ':'))}\n\n".encode() for e in events)


class TestPerplexityBufferedToOpenAI:
    def test_simple_text(self) -> None:
        raw = _make_perplexity_sse("Perplexity answer text")
        out_bytes = transform_buffered_response_sync(
            raw_bytes=raw,
            provider_type="perplexity_pro",
            inbound_format=InboundFormat.OPENAI_CHAT,
            model="perplexity/best",
            request_params=ModelRequestParameters(),
        )
        out = json.loads(out_bytes)
        assert out["object"] == "chat.completion"
        # The answer text flows through the intake's prefix-diff machinery
        # into a single TextPart on the assembled IR.
        assert "Perplexity answer text" in (out["choices"][0]["message"]["content"] or "")


# ── Error path ─────────────────────────────────────────────────────────────


class TestErrorPaths:
    def test_unsupported_upstream_raises(self) -> None:
        from ccproxy.lightllm.graph import UnsupportedUpstreamError

        with pytest.raises(UnsupportedUpstreamError, match="no buffered transform"):
            transform_buffered_response_sync(
                raw_bytes=b"{}",
                provider_type="not-a-real-provider",
                inbound_format=InboundFormat.OPENAI_CHAT,
                model="x",
                request_params=ModelRequestParameters(),
            )

    def test_unsupported_listener_raises(self) -> None:
        from ccproxy.lightllm.graph import UnsupportedListenerError

        with pytest.raises(UnsupportedListenerError, match="no buffered renderer"):
            transform_buffered_response_sync(
                raw_bytes=_make_anthropic_text_body("hi"),
                provider_type="anthropic",
                inbound_format=InboundFormat.UNKNOWN,
                model="claude-3",
                request_params=ModelRequestParameters(),
            )

    def test_unparseable_body_yields_empty_response(self) -> None:
        out_bytes = transform_buffered_response_sync(
            raw_bytes=b"not json at all",
            provider_type="anthropic",
            inbound_format=InboundFormat.OPENAI_CHAT,
            model="claude-3",
            request_params=ModelRequestParameters(),
        )
        out = json.loads(out_bytes)
        # Empty body → no parts → a valid but empty ChatCompletion envelope.
        assert out["object"] == "chat.completion"
        assert out["choices"][0]["message"]["content"] is None


class TestSilentDropTelemetry:
    """The buffered transform must surface a WARNING when it assembles a
    contentless object — a silent empty buffered response must be explainable."""

    def test_render_parts_to_listener_empty_parts_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        from ccproxy.lightllm.graph.buffered import render_parts_to_listener

        with caplog.at_level("WARNING", logger="ccproxy.lightllm.graph.buffered"):
            out = render_parts_to_listener(parts=[], inbound_format=InboundFormat.OPENAI_CHAT, model="gpt-4o")
        assert "CONTENTLESS" in caplog.text
        # Still produces a valid (empty-content) object — additive logging only.
        decoded = json.loads(out)
        assert decoded["object"] == "chat.completion"
        assert decoded["choices"][0]["message"]["content"] is None

    def test_render_parts_to_listener_with_parts_no_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        from pydantic_ai.messages import TextPart

        from ccproxy.lightllm.graph.buffered import render_parts_to_listener

        with caplog.at_level("WARNING", logger="ccproxy.lightllm.graph.buffered"):
            render_parts_to_listener(
                parts=[TextPart(content="hi")], inbound_format=InboundFormat.OPENAI_CHAT, model="gpt-4o"
            )
        assert "CONTENTLESS" not in caplog.text

    def test_empty_synthetic_sse_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        """An OpenAI body with no ``choices`` synthesizes EMPTY SSE — the transform
        must WARN that nothing renderable could be built from the upstream bytes."""
        with caplog.at_level("WARNING", logger="ccproxy.lightllm.graph.buffered"):
            transform_buffered_response_sync(
                raw_bytes=json.dumps({"id": "x", "object": "chat.completion", "choices": []}).encode(),
                provider_type="openai",
                inbound_format=InboundFormat.OPENAI_CHAT,
                model="gpt-4o",
                request_params=ModelRequestParameters(),
            )
        assert "EMPTY synthetic SSE" in caplog.text

    def test_unparseable_body_produces_contentless_object_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        """An unparseable upstream body coerces to ``{}``; the synthesized stream
        carries no content, so the transform assembles a CONTENTLESS object — the
        client never gets a silent empty response."""
        with caplog.at_level("WARNING", logger="ccproxy.lightllm.graph.buffered"):
            out_bytes = transform_buffered_response_sync(
                raw_bytes=b"not json at all",
                provider_type="anthropic",
                inbound_format=InboundFormat.OPENAI_CHAT,
                model="claude-3",
                request_params=ModelRequestParameters(),
            )
        assert "CONTENTLESS" in caplog.text
        # The transform still returns a well-formed (empty) object.
        decoded = json.loads(out_bytes)
        assert decoded["object"] == "chat.completion"
