"""Tests for response transformation and SSE rewriting."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from mitmproxy.proxy.mode_specs import ProxyMode

from ccproxy.inspector.flow_store import FlowRecord, InspectorMeta, TransformMeta
from ccproxy.lightllm.dispatch import (
    MitmResponseShim,
    SseTransformer,
    _make_response_iterator,
    make_sse_transformer,
)

# --- MitmResponseShim ---


class TestMitmResponseShim:
    def _make_mitm_response(
        self, body: dict[str, Any], status: int = 200, headers: dict[str, str] | None = None,
    ) -> MagicMock:
        mock = MagicMock()
        mock.status_code = status
        mock.content = json.dumps(body).encode()
        mock.headers = MagicMock()
        mock.headers.items = MagicMock(return_value=list((headers or {"content-type": "application/json"}).items()))
        return mock

    def test_status_code(self) -> None:
        shim = MitmResponseShim(self._make_mitm_response({}, status=201))
        assert shim.status_code == 201

    def test_headers(self) -> None:
        shim = MitmResponseShim(self._make_mitm_response({}, headers={"x-foo": "bar"}))
        assert shim.headers["x-foo"] == "bar"

    def test_text(self) -> None:
        shim = MitmResponseShim(self._make_mitm_response({"key": "value"}))
        assert '"key"' in shim.text
        assert '"value"' in shim.text

    def test_json(self) -> None:
        body = {"model": "claude-3", "content": [{"type": "text", "text": "hello"}]}
        shim = MitmResponseShim(self._make_mitm_response(body))
        assert shim.json() == body


# --- SseTransformer ---


class TestSseTransformer:
    def test_passthrough_when_no_iterator(self) -> None:
        """When _make_response_iterator returns None, bytes pass through."""
        with patch("ccproxy.lightllm.dispatch._make_response_iterator", return_value=None):
            transformer = SseTransformer("openai", "gpt-4o", {})

        chunk = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        assert transformer(chunk) == chunk

    def test_passthrough_end_of_stream(self) -> None:
        with patch("ccproxy.lightllm.dispatch._make_response_iterator", return_value=None):
            transformer = SseTransformer("openai", "gpt-4o", {})
        assert transformer(b"") == b""

    def test_transforms_single_event(self) -> None:
        mock_iterator = MagicMock()
        mock_chunk = MagicMock()
        mock_chunk.model_dump.return_value = {"choices": [{"delta": {"content": "transformed"}}]}
        mock_iterator.chunk_parser.return_value = mock_chunk

        with patch("ccproxy.lightllm.dispatch._make_response_iterator", return_value=mock_iterator):
            transformer = SseTransformer("anthropic", "claude-3", {})

        event = b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"hi"}}\n\n'
        result = transformer(event)

        mock_iterator.chunk_parser.assert_called_once()
        assert result.startswith(b"data: ")
        assert result.endswith(b"\n\n")
        parsed = json.loads(result[6:-2])
        assert parsed["choices"][0]["delta"]["content"] == "transformed"

    def test_handles_multiple_events_in_one_chunk(self) -> None:
        mock_iterator = MagicMock()
        chunk1 = MagicMock()
        chunk1.model_dump.return_value = {"id": "1"}
        chunk2 = MagicMock()
        chunk2.model_dump.return_value = {"id": "2"}
        mock_iterator.chunk_parser.side_effect = [chunk1, chunk2]

        with patch("ccproxy.lightllm.dispatch._make_response_iterator", return_value=mock_iterator):
            transformer = SseTransformer("anthropic", "claude-3", {})

        data = b'data: {"type":"event1"}\n\ndata: {"type":"event2"}\n\n'
        result = transformer(data)

        assert mock_iterator.chunk_parser.call_count == 2
        events = [e for e in result.split(b"\n\n") if e]
        assert len(events) == 2

    def test_buffers_partial_events(self) -> None:
        mock_iterator = MagicMock()
        mock_chunk = MagicMock()
        mock_chunk.model_dump.return_value = {"complete": True}
        mock_iterator.chunk_parser.return_value = mock_chunk

        with patch("ccproxy.lightllm.dispatch._make_response_iterator", return_value=mock_iterator):
            transformer = SseTransformer("anthropic", "claude-3", {})

        # First chunk: incomplete event (no trailing \n\n)
        result1 = transformer(b'data: {"type":"part')
        assert result1 == b""

        # Second chunk: completes the event
        result2 = transformer(b'ial"}\n\n')
        assert result2.startswith(b"data: ")
        mock_iterator.chunk_parser.assert_called_once()

    def test_swallows_provider_done_emits_own(self) -> None:
        mock_iterator = MagicMock()

        with patch("ccproxy.lightllm.dispatch._make_response_iterator", return_value=mock_iterator):
            transformer = SseTransformer("anthropic", "claude-3", {})

        result = transformer(b"data: [DONE]\n\n")
        assert result == b""

        result_eos = transformer(b"")
        assert result_eos == b"data: [DONE]\n\n"

    def test_chunk_parser_exception_emits_openai_error(self) -> None:
        mock_iterator = MagicMock()
        mock_iterator.chunk_parser.side_effect = RuntimeError("boom")

        with patch("ccproxy.lightllm.dispatch._make_response_iterator", return_value=mock_iterator):
            transformer = SseTransformer("anthropic", "claude-3", {})

        event = b'data: {"type":"bad"}\n\n'
        result = transformer(event)
        assert result.startswith(b"data: ")
        assert result.endswith(b"\n\n")
        parsed = json.loads(result[6:-2])
        assert parsed["error"]["type"] == "server_error"

    def test_json_decode_error_drops_silently(self) -> None:
        mock_iterator = MagicMock()

        with patch("ccproxy.lightllm.dispatch._make_response_iterator", return_value=mock_iterator):
            transformer = SseTransformer("anthropic", "claude-3", {})

        result = transformer(b"data: not-json\n\n")
        assert result == b""
        mock_iterator.chunk_parser.assert_not_called()

    def test_multi_line_data_concatenation(self) -> None:
        mock_iterator = MagicMock()
        mock_chunk = MagicMock()
        mock_chunk.model_dump.return_value = {"choices": [{"delta": {"content": "hi"}}]}
        mock_iterator.chunk_parser.return_value = mock_chunk

        with patch("ccproxy.lightllm.dispatch._make_response_iterator", return_value=mock_iterator):
            transformer = SseTransformer("anthropic", "claude-3", {})

        event = b'data: {"type":\ndata: "ping"}\n\n'
        result = transformer(event)
        call_arg = mock_iterator.chunk_parser.call_args[0][0]
        assert call_arg == {"type": "ping"}
        assert result.startswith(b"data: ")

    def test_model_dump_uses_exclude_none(self) -> None:
        mock_iterator = MagicMock()
        mock_chunk = MagicMock()
        mock_chunk.model_dump.return_value = {"id": "1", "choices": []}
        mock_iterator.chunk_parser.return_value = mock_chunk

        with patch("ccproxy.lightllm.dispatch._make_response_iterator", return_value=mock_iterator):
            transformer = SseTransformer("anthropic", "claude-3", {})

        transformer(b'data: {"type":"delta"}\n\n')
        mock_chunk.model_dump.assert_called_once_with(mode="json", exclude_none=True)

    def test_chunk_parser_returns_none(self) -> None:
        mock_iterator = MagicMock()
        mock_iterator.chunk_parser.return_value = None

        with patch("ccproxy.lightllm.dispatch._make_response_iterator", return_value=mock_iterator):
            transformer = SseTransformer("anthropic", "claude-3", {})

        result = transformer(b'data: {"type":"ping"}\n\n')
        assert result == b""


class TestMakeSseTransformer:
    def test_returns_sse_transformer(self) -> None:
        with patch("ccproxy.lightllm.dispatch._make_response_iterator", return_value=None):
            transformer = make_sse_transformer("openai", "gpt-4o")
        assert isinstance(transformer, SseTransformer)


# --- responseheaders hook ---


class TestResponseHeaders:
    def _make_flow(
        self,
        content_type: str = "text/event-stream",
        transform: TransformMeta | None = None,
        has_record: bool = True,
    ) -> MagicMock:
        flow = MagicMock()
        flow.response.headers = {"content-type": content_type}
        if has_record:
            record = FlowRecord(direction="inbound", transform=transform)
            flow.metadata = {InspectorMeta.RECORD: record}
        else:
            flow.metadata = {}
        return flow

    @pytest.mark.asyncio
    async def test_enables_passthrough_for_sse_no_transform(self) -> None:
        from ccproxy.inspector.addon import InspectorAddon

        addon = InspectorAddon()
        flow = self._make_flow(transform=None)
        await addon.responseheaders(flow)
        assert flow.response.stream is True

    @pytest.mark.asyncio
    async def test_enables_passthrough_for_sse_no_record(self) -> None:
        from ccproxy.inspector.addon import InspectorAddon

        addon = InspectorAddon()
        flow = self._make_flow(has_record=False)
        await addon.responseheaders(flow)
        assert flow.response.stream is True

    @pytest.mark.asyncio
    async def test_skips_non_sse(self) -> None:
        from ccproxy.inspector.addon import InspectorAddon

        addon = InspectorAddon()
        flow = self._make_flow(content_type="application/json")
        await addon.responseheaders(flow)
        # stream should not have been set to True
        assert not isinstance(flow.response.stream, bool) or flow.response.stream is not True

    @pytest.mark.asyncio
    async def test_creates_transformer_for_cross_provider(self) -> None:
        from ccproxy.inspector.addon import InspectorAddon

        addon = InspectorAddon()
        meta = TransformMeta(
            provider="anthropic",
            model="claude-3",
            request_data={"messages": [], "max_tokens": 100},
            is_streaming=True,
            mode="transform",
        )
        flow = self._make_flow(transform=meta)

        with patch("ccproxy.lightllm.dispatch._make_response_iterator", return_value=None):
            await addon.responseheaders(flow)

        assert isinstance(flow.response.stream, SseTransformer)

    @pytest.mark.asyncio
    async def test_falls_back_to_passthrough_on_error(self) -> None:
        from ccproxy.inspector.addon import InspectorAddon

        addon = InspectorAddon()
        meta = TransformMeta(
            provider="anthropic",
            model="claude-3",
            request_data={"messages": []},
            is_streaming=True,
        )
        flow = self._make_flow(transform=meta)

        with patch("ccproxy.lightllm.dispatch.make_sse_transformer", side_effect=RuntimeError("boom")):
            await addon.responseheaders(flow)

        assert flow.response.stream is True


# --- RESPONSE route handler ---


class TestResponseRouteHandler:
    def _make_flow_with_response(
        self,
        response_body: dict[str, Any],
        transform: TransformMeta | None = None,
        status: int = 200,
    ) -> MagicMock:
        from mitmproxy.proxy.mode_specs import ProxyMode

        flow = MagicMock()
        flow.request.pretty_host = "api.anthropic.com"
        flow.request.host = "api.anthropic.com"
        flow.request.path = "/v1/messages"
        flow.request.port = 443
        flow.request.scheme = "https"
        flow.request.headers = {}
        flow.request.content = b"{}"
        flow.client_conn.proxy_mode = ProxyMode.parse("reverse:http://localhost:1@4001")
        flow.server_conn = MagicMock()

        record = FlowRecord(direction="inbound", transform=transform)
        flow.metadata = {
            InspectorMeta.DIRECTION: "inbound",
            InspectorMeta.RECORD: record,
        }

        flow.response = MagicMock()
        flow.response.status_code = status
        flow.response.content = json.dumps(response_body).encode()
        resp_headers = MagicMock()
        resp_headers.__getitem__ = lambda self, k: "application/json" if k == "content-type" else ""
        resp_headers.get = lambda k, d="": "application/json" if k == "content-type" else d
        resp_headers.items.return_value = [("content-type", "application/json")]
        flow.response.headers = resp_headers
        return flow

    @patch("ccproxy.lightllm.transform_to_openai")
    def test_transforms_non_streaming_response(self, mock_transform: MagicMock, cleanup: None) -> None:
        from ccproxy.config import CCProxyConfig, set_config_instance
        from ccproxy.inspector.router import InspectorRouter
        from ccproxy.inspector.routes.transform import register_transform_routes

        config = CCProxyConfig()
        set_config_instance(config)

        mock_model_response = MagicMock()
        mock_model_response.model_dump.return_value = {
            "id": "chatcmpl-123",
            "object": "chat.completion",
            "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
        }
        mock_transform.return_value = mock_model_response

        router = InspectorRouter(
            name="test_transform", request_passthrough=True, response_passthrough=True,
        )
        register_transform_routes(router)

        meta = TransformMeta(
            provider="anthropic",
            model="claude-3",
            request_data={"messages": [{"role": "user", "content": "hi"}], "max_tokens": 100},
            is_streaming=False,
            mode="transform",
        )
        flow = self._make_flow_with_response(
            {"content": [{"type": "text", "text": "hello"}]},
            transform=meta,
        )

        router.response(flow)

        mock_transform.assert_called_once()
        result = json.loads(flow.response.content)
        assert result["object"] == "chat.completion"

    def test_skips_streaming_response(self, cleanup: None) -> None:
        from ccproxy.config import CCProxyConfig, set_config_instance
        from ccproxy.inspector.router import InspectorRouter
        from ccproxy.inspector.routes.transform import register_transform_routes

        config = CCProxyConfig()
        set_config_instance(config)

        router = InspectorRouter(
            name="test_transform", request_passthrough=True, response_passthrough=True,
        )
        register_transform_routes(router)

        meta = TransformMeta(
            provider="anthropic",
            model="claude-3",
            request_data={"messages": []},
            is_streaming=True,
        )
        flow = self._make_flow_with_response({}, transform=meta)
        original_content = flow.response.content

        router.response(flow)
        assert flow.response.content == original_content

    def test_skips_no_transform(self, cleanup: None) -> None:
        from ccproxy.config import CCProxyConfig, set_config_instance
        from ccproxy.inspector.router import InspectorRouter
        from ccproxy.inspector.routes.transform import register_transform_routes

        config = CCProxyConfig()
        set_config_instance(config)

        router = InspectorRouter(
            name="test_transform", request_passthrough=True, response_passthrough=True,
        )
        register_transform_routes(router)

        flow = self._make_flow_with_response({}, transform=None)
        original_content = flow.response.content

        router.response(flow)
        assert flow.response.content == original_content

    def test_skips_error_response(self, cleanup: None) -> None:
        from ccproxy.config import CCProxyConfig, set_config_instance
        from ccproxy.inspector.router import InspectorRouter
        from ccproxy.inspector.routes.transform import register_transform_routes

        config = CCProxyConfig()
        set_config_instance(config)

        router = InspectorRouter(
            name="test_transform", request_passthrough=True, response_passthrough=True,
        )
        register_transform_routes(router)

        meta = TransformMeta(
            provider="anthropic",
            model="claude-3",
            request_data={"messages": []},
            is_streaming=False,
        )
        flow = self._make_flow_with_response(
            {"error": "bad request"}, transform=meta, status=400,
        )
        original_content = flow.response.content

        router.response(flow)
        assert flow.response.content == original_content


# --- TransformMeta persistence ---


class TestTransformMetaPersistence:
    @patch("ccproxy.lightllm.transform_to_provider")
    def test_stores_transform_meta(self, mock_transform: MagicMock, cleanup: None) -> None:
        from ccproxy.config import (
            CCProxyConfig,
            InspectorConfig,
            TransformRoute,
            set_config_instance,
        )
        from ccproxy.inspector.router import InspectorRouter
        from ccproxy.inspector.routes.transform import register_transform_routes

        transform_routes = [TransformRoute(
            mode="transform",
            match_host="api.openai.com",
            match_path="/v1/chat/completions",
            dest_provider="anthropic",
            dest_model="claude-3",
        )]
        config = CCProxyConfig(inspector=InspectorConfig(transforms=transform_routes))
        set_config_instance(config)

        mock_transform.return_value = ("https://api.anthropic.com/v1/messages", {}, b"{}")

        router = InspectorRouter(
            name="test_transform", request_passthrough=True, response_passthrough=True,
        )
        register_transform_routes(router)

        from mitmproxy.proxy.mode_specs import ProxyMode

        record = FlowRecord(direction="inbound")
        flow = MagicMock()
        flow.request.pretty_host = "api.openai.com"
        flow.request.host = "api.openai.com"
        flow.request.path = "/v1/chat/completions"
        flow.request.port = 443
        flow.request.scheme = "https"
        flow.request.headers = {}
        flow.request.content = json.dumps({
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        }).encode()
        flow.metadata = {
            InspectorMeta.DIRECTION: "inbound",
            InspectorMeta.RECORD: record,
        }
        flow.server_conn = MagicMock()
        flow.response = None
        flow.client_conn.proxy_mode = ProxyMode.parse("reverse:http://localhost:1@4001")

        router.request(flow)

        assert record.transform is not None
        assert record.transform.provider == "anthropic"
        assert record.transform.model == "claude-3"
        assert record.transform.is_streaming is True
        assert "messages" in record.transform.request_data

    def test_redirect_does_not_store_transform_mode(self, cleanup: None) -> None:
        """Redirect mode sets TransformMeta with mode='redirect', not 'transform'."""
        from ccproxy.config import (
            CCProxyConfig,
            InspectorConfig,
            TransformRoute,
            set_config_instance,
        )
        from ccproxy.inspector.router import InspectorRouter
        from ccproxy.inspector.routes.transform import register_transform_routes

        transform_routes = [TransformRoute(
            mode="redirect",
            match_host="api.openai.com",
            match_path="/v1/",
            dest_provider="anthropic",
            dest_host="api.anthropic.com",
        )]
        config = CCProxyConfig(inspector=InspectorConfig(transforms=transform_routes))
        set_config_instance(config)

        router = InspectorRouter(
            name="test_transform", request_passthrough=True, response_passthrough=True,
        )
        register_transform_routes(router)

        record = FlowRecord(direction="inbound")
        flow = MagicMock()
        flow.request.pretty_host = "api.openai.com"
        flow.request.host = "api.openai.com"
        flow.request.path = "/v1/chat/completions"
        flow.request.port = 443
        flow.request.scheme = "https"
        flow.request.headers = {}
        flow.request.content = json.dumps({"model": "claude-3", "messages": []}).encode()
        flow.metadata = {InspectorMeta.DIRECTION: "inbound", InspectorMeta.RECORD: record}
        flow.server_conn = MagicMock()
        flow.response = None
        flow.client_conn.proxy_mode = ProxyMode.parse("reverse:http://localhost:1@4001")

        router.request(flow)

        assert record.transform is not None
        assert record.transform.mode == "redirect"

        # Response handler should skip redirect mode (only processes transform mode)
        flow.response = MagicMock()
        flow.response.status_code = 200
        flow.response.content = b'{"original": true}'
        original_content = flow.response.content
        router.response(flow)
        assert flow.response.content == original_content

    def test_passthrough_does_not_store_transform_meta(self, cleanup: None) -> None:
        from ccproxy.config import (
            CCProxyConfig,
            InspectorConfig,
            TransformRoute,
            set_config_instance,
        )
        from ccproxy.inspector.router import InspectorRouter
        from ccproxy.inspector.routes.transform import register_transform_routes

        transform_routes = [TransformRoute(
            match_host="api.openai.com",
            match_path="/",
            dest_provider="anthropic",
            dest_model="claude-3",
            mode="passthrough",
        )]
        config = CCProxyConfig(inspector=InspectorConfig(transforms=transform_routes))
        set_config_instance(config)

        router = InspectorRouter(
            name="test_transform", request_passthrough=True, response_passthrough=True,
        )
        register_transform_routes(router)

        record = FlowRecord(direction="inbound")
        flow = MagicMock()
        flow.request.pretty_host = "api.openai.com"
        flow.request.host = "api.openai.com"
        flow.request.path = "/v1/chat/completions"
        flow.request.port = 443
        flow.request.scheme = "https"
        flow.request.headers = {}
        flow.request.content = json.dumps({"model": "gpt-4o", "messages": []}).encode()
        flow.metadata = {
            InspectorMeta.DIRECTION: "inbound",
            InspectorMeta.RECORD: record,
        }
        flow.response = None

        router.request(flow)

        assert record.transform is None


class TestMakeResponseIterator:
    """Tests for _make_response_iterator — provider dispatch."""

    def test_gemini_returns_gemini_iterator(self) -> None:
        iterator = _make_response_iterator("gemini", "gemini-2.0-flash", {})
        assert iterator is not None
        assert "Gemini" in type(iterator).__qualname__ or "ModelResponseIterator" in type(iterator).__name__

    def test_anthropic_returns_anthropic_iterator(self) -> None:
        iterator = _make_response_iterator("anthropic", "claude-3", {})
        assert iterator is not None
        assert "ModelResponseIterator" in type(iterator).__name__

    def test_vertex_ai_returns_gemini_iterator(self) -> None:
        iterator = _make_response_iterator("vertex_ai", "gemini-2.0-flash", {})
        assert iterator is not None

    def test_generic_provider_fallback(self) -> None:
        # OpenAI natively outputs OpenAI-format SSE, so iterator may be None
        iterator = _make_response_iterator("openai", "gpt-4o", {})
        # Either returns an iterator or None (both valid for OpenAI)
        assert iterator is None or hasattr(iterator, "chunk_parser")
