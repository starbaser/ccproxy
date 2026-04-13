"""Tests for inspector addon traffic capture."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccproxy.inspector.addon import InspectorAddon
from ccproxy.inspector.flow_store import (
    FLOW_ID_HEADER,
    ClientRequest,
    FlowRecord,
    InspectorMeta,
    TransformMeta,
    create_flow_record,
)


def _make_mock_flow(*, reverse: bool = True) -> MagicMock:
    """Create a mock HTTP flow with proxy_mode set for direction detection.

    Args:
        reverse: If True, simulate ReverseMode; if False, simulate RegularMode.
    """
    from mitmproxy.proxy.mode_specs import ProxyMode as MitmProxyMode

    flow = MagicMock()
    flow.request = MagicMock()
    flow.request.headers = {}
    flow.request.content = None
    flow.request.path = "/v1/messages"
    flow.metadata = {}

    # Set proxy_mode for per-flow direction detection
    if reverse:
        flow.client_conn.proxy_mode = MitmProxyMode.parse("reverse:http://localhost:4001@4002")
    else:
        flow.client_conn.proxy_mode = MitmProxyMode.parse("regular@4003")

    return flow


@pytest.fixture
def mock_flow() -> MagicMock:
    """Create a mock HTTP flow (reverse mode by default)."""
    return _make_mock_flow(reverse=True)


def _make_wg_flow(host: str = "api.anthropic.com", path: str = "/v1/messages") -> MagicMock:
    """Create a mock HTTP flow in WireGuard mode."""
    from mitmproxy.proxy.mode_specs import ProxyMode as MitmProxyMode

    flow = MagicMock()
    flow.request = MagicMock()
    flow.request.headers = {}
    flow.request.content = None
    flow.request.pretty_host = host
    flow.request.host = host
    flow.request.port = 443
    flow.request.scheme = "https"
    flow.request.method = "POST"
    flow.request.path = path
    flow.request.pretty_url = f"https://{host}{path}"
    flow.id = "wg-flow-1"
    flow.metadata = {}
    flow.client_conn.proxy_mode = MitmProxyMode.parse("wireguard@51820")
    return flow


class TestRequestMethod:
    @pytest.mark.asyncio
    async def test_request_runs_without_error(self, mock_flow: MagicMock) -> None:
        """request() should run without error."""
        addon = InspectorAddon()

        mock_flow.request.pretty_host = "api.anthropic.com"

        await addon.request(mock_flow)


class TestWireGuardDirectionDetection:
    """Tests for WireGuard direction detection — all WG and reverse flows are inbound."""

    @pytest.mark.asyncio
    async def test_wireguard_direction_is_inbound(self) -> None:
        addon = InspectorAddon(wg_cli_port=51820)
        flow = _make_wg_flow(host="api.anthropic.com")
        await addon.request(flow)
        assert flow.metadata.get("ccproxy.direction") == "inbound"

    @pytest.mark.asyncio
    async def test_reverse_direction_is_inbound(self) -> None:
        addon = InspectorAddon()
        flow = _make_mock_flow(reverse=True)
        flow.id = "rev-dir-1"
        flow.request.pretty_host = "localhost"
        flow.request.host = "localhost"
        flow.request.method = "POST"
        flow.request.path = "/v1/messages"
        flow.request.pretty_url = "http://localhost/v1/messages"
        flow.request.content = None
        await addon.request(flow)
        assert flow.metadata.get("ccproxy.direction") == "inbound"

    @pytest.mark.asyncio
    async def test_wireguard_cli_does_not_forward_non_llm(self) -> None:
        addon = InspectorAddon(wg_cli_port=51820)
        flow = _make_wg_flow(host="github.com", path="/api/v3")
        await addon.request(flow)
        assert flow.metadata.get("ccproxy.direction") == "inbound"

    def test_direction_is_string_literal(self) -> None:
        """Direction metadata uses string literals, not an enum."""
        addon = InspectorAddon(wg_cli_port=51820)
        flow = _make_wg_flow(host="api.anthropic.com")
        direction = addon._get_direction(flow)
        assert direction == "inbound"

    def test_reverse_mode_returns_inbound(self) -> None:
        """ReverseMode flows return 'inbound'."""
        addon = InspectorAddon()
        flow = _make_mock_flow(reverse=True)
        direction = addon._get_direction(flow)
        assert direction == "inbound"


class TestGetDirectionEdgeCases:
    def test_regular_mode_returns_none(self) -> None:
        from mitmproxy.proxy.mode_specs import ProxyMode as MitmProxyMode

        addon = InspectorAddon()
        flow = MagicMock()
        flow.client_conn.proxy_mode = MitmProxyMode.parse("regular@8080")
        assert addon._get_direction(flow) is None

    def test_wireguard_mode_returns_inbound(self) -> None:
        """WireGuard mode always returns 'inbound'."""
        from mitmproxy.proxy.mode_specs import ProxyMode as MitmProxyMode

        addon = InspectorAddon()
        flow = MagicMock()
        flow.client_conn.proxy_mode = MitmProxyMode.parse("wireguard")
        direction = addon._get_direction(flow)
        assert direction == "inbound"


class TestExtractSessionId:
    """Tests for _extract_session_id."""

    def _make_request(self, content: bytes | None) -> MagicMock:
        req = MagicMock()
        req.content = content
        return req

    def test_no_content(self) -> None:
        addon = InspectorAddon()
        req = self._make_request(None)
        assert addon._extract_session_id(req) is None

    def test_invalid_json(self) -> None:
        addon = InspectorAddon()
        req = self._make_request(b"not-json{{{")
        assert addon._extract_session_id(req) is None

    def test_missing_metadata(self) -> None:
        addon = InspectorAddon()
        req = self._make_request(json.dumps({"model": "claude"}).encode())
        assert addon._extract_session_id(req) is None

    def test_metadata_not_dict(self) -> None:
        addon = InspectorAddon()
        req = self._make_request(json.dumps({"metadata": "a string"}).encode())
        assert addon._extract_session_id(req) is None

    def test_empty_user_id(self) -> None:
        addon = InspectorAddon()
        req = self._make_request(json.dumps({"metadata": {"user_id": ""}}).encode())
        assert addon._extract_session_id(req) is None

    def test_json_format_session_id(self) -> None:
        addon = InspectorAddon()
        user_id_obj = json.dumps({"session_id": "abc123"})
        req = self._make_request(json.dumps({"metadata": {"user_id": user_id_obj}}).encode())
        assert addon._extract_session_id(req) == "abc123"

    def test_legacy_format(self) -> None:
        addon = InspectorAddon()
        req = self._make_request(
            json.dumps({"metadata": {"user_id": "user_hash_account_uuid_session_sid123"}}).encode()
        )
        assert addon._extract_session_id(req) == "sid123"

    def test_multiple_session_separators(self) -> None:
        addon = InspectorAddon()
        req = self._make_request(
            json.dumps({"metadata": {"user_id": "a_session_b_session_c"}}).encode()
        )
        assert addon._extract_session_id(req) is None

    def test_neither_format(self) -> None:
        addon = InspectorAddon()
        req = self._make_request(
            json.dumps({"metadata": {"user_id": "plain-user-id"}}).encode()
        )
        assert addon._extract_session_id(req) is None


class TestRequestFlowStore:
    """Tests verifying flow store interaction during request()."""

    @pytest.mark.asyncio
    async def test_creates_flow_record_and_stamps_header(self) -> None:
        addon = InspectorAddon()
        flow = _make_wg_flow(host="api.anthropic.com")
        flow.request.headers = {}

        await addon.request(flow)

        assert FLOW_ID_HEADER in flow.request.headers
        assert flow.metadata.get(InspectorMeta.RECORD) is not None

    @pytest.mark.asyncio
    async def test_reuses_existing_record(self) -> None:
        addon = InspectorAddon()
        flow = _make_wg_flow(host="api.anthropic.com")

        flow_id, existing_record = create_flow_record("inbound")
        flow.request.headers = {FLOW_ID_HEADER: flow_id}

        await addon.request(flow)

        assert flow.metadata.get(InspectorMeta.RECORD) is existing_record


class TestResponseAndError:
    """Tests for response() and error() early-exit guards."""

    @pytest.mark.asyncio
    async def test_response_none_response(self) -> None:
        addon = InspectorAddon()
        flow = MagicMock()
        flow.response = None
        flow.request.timestamp_start = None

        await addon.response(flow)

    @pytest.mark.asyncio
    async def test_error_none_error(self) -> None:
        addon = InspectorAddon()
        flow = MagicMock()
        flow.error = None

        await addon.error(flow)

    @pytest.mark.asyncio
    async def test_response_with_tracer(self) -> None:
        from unittest.mock import MagicMock

        addon = InspectorAddon()
        mock_tracer = MagicMock()
        addon.set_tracer(mock_tracer)

        flow = MagicMock()
        flow.response = MagicMock()
        flow.response.status_code = 200
        flow.response.timestamp_end = 1000.5
        flow.request.timestamp_start = 1000.0
        flow.request.pretty_url = "https://api.anthropic.com/v1/messages"
        flow.id = "resp-flow-1"

        await addon.response(flow)
        mock_tracer.finish_span.assert_called_once()

    @pytest.mark.asyncio
    async def test_response_exception_handled(self) -> None:
        addon = InspectorAddon()
        flow = MagicMock()
        flow.response = MagicMock()
        flow.response.status_code = 200
        flow.response.timestamp_end = MagicMock()
        flow.request.timestamp_start = None  # Will cause TypeError in duration calc
        flow.request.pretty_url = "https://api.anthropic.com/v1/messages"
        flow.id = "error-test"

        await addon.response(flow)

    @pytest.mark.asyncio
    async def test_error_with_tracer(self) -> None:
        addon = InspectorAddon()
        mock_tracer = MagicMock()
        addon.set_tracer(mock_tracer)

        flow = MagicMock()
        flow.error = MagicMock()
        flow.error.__str__ = lambda self: "connection timeout"
        flow.id = "error-flow-1"

        await addon.error(flow)
        mock_tracer.finish_span_error.assert_called_once()

    @pytest.mark.asyncio
    async def test_error_exception_handled(self) -> None:
        addon = InspectorAddon()
        mock_tracer = MagicMock()
        mock_tracer.finish_span_error.side_effect = RuntimeError("tracer error")
        addon.set_tracer(mock_tracer)

        flow = MagicMock()
        flow.error = MagicMock()
        flow.error.__str__ = lambda self: "connection error"
        flow.id = "error-flow-2"

        await addon.error(flow)


class TestResponseRetryPath:
    """Tests for the 401 retry codepath inside response()."""

    @pytest.mark.asyncio
    async def test_response_401_with_oauth_triggers_retry(self) -> None:
        addon = InspectorAddon()
        flow = MagicMock()
        flow.response = MagicMock()
        flow.response.status_code = 401
        flow.response.timestamp_end = 1000.5
        flow.request.timestamp_start = 1000.0
        flow.request.pretty_url = "https://api.anthropic.com/v1/messages"
        flow.request.headers = {}
        flow.metadata = {InspectorMeta.RECORD: FlowRecord(direction="inbound"), "ccproxy.oauth_injected": True}
        flow.id = "retry-flow"

        with patch.object(addon, "_retry_with_refreshed_token", new_callable=AsyncMock, return_value=True):
            await addon.response(flow)

    @pytest.mark.asyncio
    async def test_response_exception_triggers_error_handler(self) -> None:
        """Verify the except block in response() fires when an unexpected error occurs."""
        addon = InspectorAddon()
        flow = MagicMock()
        # Make .response a property that raises on status_code access
        type(flow).response = property(lambda self: (_ for _ in ()).throw(RuntimeError("kaboom")))
        flow.id = "err-flow"

        # Should not propagate
        await addon.response(flow)


class TestResponseHeadersEdgeCases:
    """Cover remaining edge cases in responseheaders()."""

    @pytest.mark.asyncio
    async def test_responseheaders_no_response(self) -> None:
        addon = InspectorAddon()
        flow = MagicMock()
        flow.response = None
        await addon.responseheaders(flow)

    @pytest.mark.asyncio
    async def test_responseheaders_sse_transformer_error_with_transform_mode(self) -> None:
        """When mode=transform and make_sse_transformer raises, fall back to passthrough."""
        addon = InspectorAddon()
        meta = TransformMeta(
            provider="anthropic", model="claude-3",
            request_data={"messages": []}, is_streaming=True, mode="transform",
        )
        record = FlowRecord(direction="inbound", transform=meta)
        flow = MagicMock()
        flow.response.headers = {"content-type": "text/event-stream"}
        flow.metadata = {InspectorMeta.RECORD: record}

        with patch("ccproxy.lightllm.dispatch.make_sse_transformer", side_effect=RuntimeError("fail")):
            await addon.responseheaders(flow)

        assert flow.response.stream is True


class TestObserveCompliance:
    """Tests for _observe_compliance static method."""

    def test_compliance_disabled_skips(self) -> None:
        mock_config = MagicMock()
        mock_config.compliance.enabled = False
        with patch("ccproxy.config.get_config", return_value=mock_config):
            InspectorAddon._observe_compliance(MagicMock(), MagicMock())

    def test_compliance_exception_handled(self) -> None:
        with patch("ccproxy.config.get_config", side_effect=RuntimeError("oops")):
            InspectorAddon._observe_compliance(MagicMock(), MagicMock())


class TestSetTracer:
    def test_set_tracer(self) -> None:
        addon = InspectorAddon()
        assert addon.tracer is None

        mock_tracer = MagicMock()
        addon.set_tracer(mock_tracer)

        assert addon.tracer is mock_tracer


class TestRequestWithTracer:
    @pytest.mark.asyncio
    async def test_request_with_tracer(self) -> None:
        addon = InspectorAddon()
        mock_tracer = MagicMock()
        addon.set_tracer(mock_tracer)

        flow = _make_mock_flow(reverse=True)
        flow.id = "tracer-test-1"
        flow.request.pretty_host = "api.anthropic.com"
        flow.request.method = "POST"
        flow.request.path = "/v1/messages"
        flow.request.pretty_url = "https://api.anthropic.com/v1/messages"
        flow.request.content = None

        await addon.request(flow)
        mock_tracer.start_span.assert_called_once()

    @pytest.mark.asyncio
    async def test_unknown_mode_skipped(self) -> None:
        """Flows with non-reverse, non-WireGuard modes are skipped."""
        from mitmproxy.proxy.mode_specs import ProxyMode as MitmProxyMode

        addon = InspectorAddon()
        flow = MagicMock()
        flow.client_conn.proxy_mode = MitmProxyMode.parse("regular@4003")
        flow.request = MagicMock()
        flow.metadata = {}

        await addon.request(flow)
        # direction is None, should return early without setting metadata
        assert flow.metadata == {}

    @pytest.mark.asyncio
    async def test_request_exception_handled(self) -> None:
        """Exception during request processing is logged but not raised."""
        addon = InspectorAddon()
        mock_tracer = MagicMock()
        mock_tracer.start_span.side_effect = RuntimeError("tracer failure")
        addon.set_tracer(mock_tracer)

        flow = _make_wg_flow(host="api.anthropic.com")
        await addon.request(flow)


class TestUnwrapGeminiResponse:
    """Tests for InspectorAddon._unwrap_gemini_response."""

    def _make_flow_with_transform(
        self,
        provider: str = "gemini",
        is_streaming: bool = False,
    ) -> MagicMock:
        record = FlowRecord(direction="inbound")
        record.transform = TransformMeta(
            provider=provider,
            model="gemini-2.5-flash",
            request_data={},
            is_streaming=is_streaming,
        )
        flow = MagicMock()
        flow.metadata = {InspectorMeta.RECORD: record}
        return flow

    def test_unwraps_gemini_redirect_response_envelope(self) -> None:
        """Gemini redirect transform with {response: {inner: true}} unwraps to inner dict."""
        flow = self._make_flow_with_transform(provider="gemini", is_streaming=False)
        inner = {"candidates": [{"content": "hello"}], "inner": True}
        response = MagicMock()
        response.content = json.dumps({"response": inner}).encode()

        InspectorAddon._unwrap_gemini_response(flow, response)

        result = json.loads(response.content)
        assert result == inner

    def test_skips_when_no_record(self) -> None:
        """Flow without a FlowRecord is a no-op."""
        flow = MagicMock()
        flow.metadata = {}
        response = MagicMock()
        original_content = json.dumps({"response": {"inner": True}}).encode()
        response.content = original_content

        InspectorAddon._unwrap_gemini_response(flow, response)

        assert response.content == original_content

    def test_skips_when_no_transform(self) -> None:
        """Flow with a record but no transform is a no-op."""
        record = FlowRecord(direction="inbound")
        record.transform = None
        flow = MagicMock()
        flow.metadata = {InspectorMeta.RECORD: record}
        response = MagicMock()
        original_content = json.dumps({"response": {"inner": True}}).encode()
        response.content = original_content

        InspectorAddon._unwrap_gemini_response(flow, response)

        assert response.content == original_content

    def test_skips_for_non_gemini_provider(self) -> None:
        """Non-gemini provider transform is a no-op — envelope is provider-specific."""
        flow = self._make_flow_with_transform(provider="anthropic", is_streaming=False)
        response = MagicMock()
        original_content = json.dumps({"response": {"inner": True}}).encode()
        response.content = original_content

        InspectorAddon._unwrap_gemini_response(flow, response)

        assert response.content == original_content

    def test_skips_for_streaming(self) -> None:
        """Streaming responses are not unwrapped — SSE frames are handled in responseheaders."""
        flow = self._make_flow_with_transform(provider="gemini", is_streaming=True)
        response = MagicMock()
        original_content = json.dumps({"response": {"inner": True}}).encode()
        response.content = original_content

        InspectorAddon._unwrap_gemini_response(flow, response)

        assert response.content == original_content

    def test_noop_when_response_field_not_a_dict(self) -> None:
        """If the 'response' field is not a dict, body is left untouched."""
        flow = self._make_flow_with_transform(provider="gemini", is_streaming=False)
        response = MagicMock()
        original_content = json.dumps({"response": "not-a-dict"}).encode()
        response.content = original_content

        InspectorAddon._unwrap_gemini_response(flow, response)

        assert response.content == original_content

    def test_noop_when_response_field_absent(self) -> None:
        """Body without a 'response' key is left unchanged."""
        flow = self._make_flow_with_transform(provider="gemini", is_streaming=False)
        response = MagicMock()
        original_content = json.dumps({"other": "data"}).encode()
        response.content = original_content

        InspectorAddon._unwrap_gemini_response(flow, response)

        assert response.content == original_content

    def test_noop_on_invalid_json(self) -> None:
        """Invalid JSON in response body does not raise — exception is suppressed."""
        flow = self._make_flow_with_transform(provider="gemini", is_streaming=False)
        response = MagicMock()
        response.content = b"not-json{{{"

        InspectorAddon._unwrap_gemini_response(flow, response)

    def test_noop_on_empty_content(self) -> None:
        """Empty response content does not raise."""
        flow = self._make_flow_with_transform(provider="gemini", is_streaming=False)
        response = MagicMock()
        response.content = b""

        InspectorAddon._unwrap_gemini_response(flow, response)


class TestGetClientRequestCommand:
    """Tests for InspectorAddon.get_client_request mitmproxy command."""

    def _make_flow_with_client_request(
        self,
        flow_id: str = "flow-abc-123",
        method: str = "POST",
        scheme: str = "https",
        host: str = "api.anthropic.com",
        port: int = 443,
        path: str = "/v1/messages",
        headers: dict[str, str] | None = None,
        body: bytes = b'{"model": "claude-3"}',
    ) -> MagicMock:
        cr = ClientRequest(
            method=method,
            scheme=scheme,
            host=host,
            port=port,
            path=path,
            headers=headers or {"content-type": "application/json"},
            body=body,
            content_type="application/json",
        )
        record = FlowRecord(direction="inbound")
        record.client_request = cr

        flow = MagicMock()
        flow.id = flow_id
        flow.metadata = {InspectorMeta.RECORD: record}
        return flow

    def test_returns_json_with_method_url_headers_body(self) -> None:
        """Flow with snapshot returns JSON containing method, url, headers, body."""
        flow = self._make_flow_with_client_request(
            flow_id="test-flow-1",
            method="POST",
            scheme="https",
            host="api.anthropic.com",
            port=443,
            path="/v1/messages",
            headers={"content-type": "application/json", "x-api-key": "sk-test"},
            body=b'{"model": "claude-3", "messages": []}',
        )
        addon = InspectorAddon()

        result_str = addon.get_client_request([flow])
        result = json.loads(result_str)

        assert len(result) == 1
        entry = result[0]
        assert entry["flow_id"] == "test-flow-1"
        assert entry["method"] == "POST"
        assert entry["url"] == "https://api.anthropic.com:443/v1/messages"
        assert entry["headers"]["content-type"] == "application/json"
        assert entry["body"] == {"model": "claude-3", "messages": []}

    def test_returns_error_json_when_no_snapshot(self) -> None:
        """Flow without a client_request snapshot returns error entry."""
        record = FlowRecord(direction="inbound")
        record.client_request = None

        flow = MagicMock()
        flow.id = "no-snap-flow"
        flow.metadata = {InspectorMeta.RECORD: record}

        addon = InspectorAddon()
        result_str = addon.get_client_request([flow])
        result = json.loads(result_str)

        assert len(result) == 1
        assert result[0]["flow_id"] == "no-snap-flow"
        assert result[0]["error"] == "no snapshot"

    def test_returns_error_json_when_no_record(self) -> None:
        """Flow with no FlowRecord at all returns error entry."""
        flow = MagicMock()
        flow.id = "no-record-flow"
        flow.metadata = {}

        addon = InspectorAddon()
        result_str = addon.get_client_request([flow])
        result = json.loads(result_str)

        assert len(result) == 1
        assert result[0]["error"] == "no snapshot"

    def test_multiple_flows_mixed(self) -> None:
        """Multiple flows: some with snapshots, some without."""
        flow_ok = self._make_flow_with_client_request(flow_id="flow-ok")
        record_no_cr = FlowRecord(direction="inbound")
        record_no_cr.client_request = None
        flow_err = MagicMock()
        flow_err.id = "flow-err"
        flow_err.metadata = {InspectorMeta.RECORD: record_no_cr}

        addon = InspectorAddon()
        result_str = addon.get_client_request([flow_ok, flow_err])
        result = json.loads(result_str)

        assert len(result) == 2
        ids = {r["flow_id"] for r in result}
        assert "flow-ok" in ids
        assert "flow-err" in ids

        ok_entry = next(r for r in result if r["flow_id"] == "flow-ok")
        err_entry = next(r for r in result if r["flow_id"] == "flow-err")
        assert "method" in ok_entry
        assert err_entry["error"] == "no snapshot"

    def test_body_decoded_as_string_on_invalid_json(self) -> None:
        """Non-JSON body bytes are returned as a decoded string, not parsed."""
        flow = self._make_flow_with_client_request(
            flow_id="non-json-flow",
            body=b"not-json-content",
        )
        addon = InspectorAddon()
        result_str = addon.get_client_request([flow])
        result = json.loads(result_str)

        entry = result[0]
        assert entry["body"] == "not-json-content"

    def test_empty_body_is_none(self) -> None:
        """Empty body bytes produce None for the body field."""
        flow = self._make_flow_with_client_request(flow_id="empty-body-flow", body=b"")
        addon = InspectorAddon()
        result_str = addon.get_client_request([flow])
        result = json.loads(result_str)

        assert result[0]["body"] is None

    def test_empty_flows_list(self) -> None:
        """Empty flow list returns an empty JSON array."""
        addon = InspectorAddon()
        result_str = addon.get_client_request([])
        result = json.loads(result_str)
        assert result == []


class TestRetryWithRefreshedToken:
    """Tests for InspectorAddon._retry_with_refreshed_token."""

    def _make_oauth_flow(
        self,
        provider: str = "anthropic",
        method: str = "POST",
        url: str = "https://api.anthropic.com/v1/messages",
        content: bytes = b'{"model": "claude-3"}',
    ) -> MagicMock:
        flow = MagicMock()
        flow.metadata = {"ccproxy.oauth_provider": provider}
        flow.request.method = method
        flow.request.pretty_url = url
        flow.request.headers = {"authorization": "Bearer old-token"}
        flow.request.content = content
        flow.response = MagicMock()
        flow.response.status_code = 401
        flow.response.headers = MagicMock()
        flow.response.headers.clear = MagicMock()
        flow.response.headers.add = MagicMock()
        flow.response.headers.multi_items = MagicMock(return_value=[])
        return flow

    @pytest.mark.asyncio
    async def test_returns_false_when_no_provider(self) -> None:
        """Flow without ccproxy.oauth_provider metadata returns False immediately."""
        flow = MagicMock()
        flow.metadata = {}

        addon = InspectorAddon()
        result = await addon._retry_with_refreshed_token(flow)
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_empty_provider(self) -> None:
        """Empty provider string returns False without touching the config."""
        flow = MagicMock()
        flow.metadata = {"ccproxy.oauth_provider": ""}

        addon = InspectorAddon()
        result = await addon._retry_with_refreshed_token(flow)
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_token_unchanged(self) -> None:
        """401 with an unchanged token (already fresh) returns False — not retried."""
        flow = self._make_oauth_flow(provider="anthropic")
        mock_config = MagicMock()
        mock_config.refresh_oauth_token.return_value = ("same-token", False)

        with patch("ccproxy.config.get_config", return_value=mock_config):
            addon = InspectorAddon()
            result = await addon._retry_with_refreshed_token(flow)

        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_new_token_is_none(self) -> None:
        """If refresh returns (None, False) — token resolution failed — returns False."""
        flow = self._make_oauth_flow(provider="anthropic")
        mock_config = MagicMock()
        mock_config.refresh_oauth_token.return_value = (None, False)

        with patch("ccproxy.config.get_config", return_value=mock_config):
            addon = InspectorAddon()
            result = await addon._retry_with_refreshed_token(flow)

        assert result is False

    @pytest.mark.asyncio
    async def test_retries_with_new_token_and_returns_true(self) -> None:
        """401 with a refreshed token issues an httpx retry and returns True."""
        flow = self._make_oauth_flow(provider="anthropic")
        mock_config = MagicMock()
        mock_config.refresh_oauth_token.return_value = ("new-token", True)
        mock_config.get_auth_header.return_value = None  # use Authorization header

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers.multi_items.return_value = [("content-type", "application/json")]
        mock_response.content = b'{"id": "msg-1"}'

        mock_async_client = AsyncMock()
        mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
        mock_async_client.__aexit__ = AsyncMock(return_value=None)
        mock_async_client.request = AsyncMock(return_value=mock_response)

        with (
            patch("ccproxy.config.get_config", return_value=mock_config),
            patch("httpx.AsyncClient", return_value=mock_async_client),
        ):
            addon = InspectorAddon()
            result = await addon._retry_with_refreshed_token(flow)

        assert result is True
        mock_async_client.request.assert_called_once()
        call_kwargs = mock_async_client.request.call_args
        assert call_kwargs.kwargs["method"] == "POST"
        assert call_kwargs.kwargs["url"] == "https://api.anthropic.com/v1/messages"

    @pytest.mark.asyncio
    async def test_retry_uses_custom_auth_header(self) -> None:
        """When get_auth_header returns a custom header name, it is used for the new token."""
        flow = self._make_oauth_flow(provider="gemini")
        mock_config = MagicMock()
        mock_config.refresh_oauth_token.return_value = ("new-gemini-token", True)
        mock_config.get_auth_header.return_value = "x-goog-api-key"

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers.multi_items.return_value = []
        mock_response.content = b"{}"

        mock_async_client = AsyncMock()
        mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
        mock_async_client.__aexit__ = AsyncMock(return_value=None)
        mock_async_client.request = AsyncMock(return_value=mock_response)

        with (
            patch("ccproxy.config.get_config", return_value=mock_config),
            patch("httpx.AsyncClient", return_value=mock_async_client),
        ):
            addon = InspectorAddon()
            result = await addon._retry_with_refreshed_token(flow)

        assert result is True
        sent_headers = mock_async_client.request.call_args.kwargs["headers"]
        assert sent_headers.get("x-goog-api-key") == "new-gemini-token"

    @pytest.mark.asyncio
    async def test_retry_does_not_send_internal_headers(self) -> None:
        """Internal ccproxy headers are not forwarded on retry."""
        flow = self._make_oauth_flow(provider="anthropic")
        mock_config = MagicMock()
        mock_config.refresh_oauth_token.return_value = ("new-token", True)
        mock_config.get_auth_header.return_value = None

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers.multi_items.return_value = []
        mock_response.content = b"{}"

        mock_async_client = AsyncMock()
        mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
        mock_async_client.__aexit__ = AsyncMock(return_value=None)
        mock_async_client.request = AsyncMock(return_value=mock_response)

        with (
            patch("ccproxy.config.get_config", return_value=mock_config),
            patch("httpx.AsyncClient", return_value=mock_async_client),
        ):
            addon = InspectorAddon()
            await addon._retry_with_refreshed_token(flow)

        sent_headers = mock_async_client.request.call_args.kwargs["headers"]
        assert "x-ccproxy-oauth-injected" not in sent_headers

    @pytest.mark.asyncio
    async def test_retry_updates_flow_response(self) -> None:
        """Successful retry updates flow.response status_code and content in place."""
        flow = self._make_oauth_flow(provider="anthropic")
        mock_config = MagicMock()
        mock_config.refresh_oauth_token.return_value = ("new-token", True)
        mock_config.get_auth_header.return_value = None

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers.multi_items.return_value = [("content-type", "application/json")]
        mock_response.content = b'{"ok": true}'

        mock_async_client = AsyncMock()
        mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
        mock_async_client.__aexit__ = AsyncMock(return_value=None)
        mock_async_client.request = AsyncMock(return_value=mock_response)

        with (
            patch("ccproxy.config.get_config", return_value=mock_config),
            patch("httpx.AsyncClient", return_value=mock_async_client),
        ):
            addon = InspectorAddon()
            await addon._retry_with_refreshed_token(flow)

        assert flow.response.status_code == 200
        assert flow.response.content == b'{"ok": true}'

    @pytest.mark.asyncio
    async def test_retry_uses_configured_upstream_timeout(self) -> None:
        """Retry client is instantiated with the config-driven upstream_timeout_seconds,
        not httpx's default 5-second timeout which is too short for LLM inference."""
        import httpx

        flow = self._make_oauth_flow(provider="anthropic")
        mock_config = MagicMock()
        mock_config.refresh_oauth_token.return_value = ("new-token", True)
        mock_config.get_auth_header.return_value = None
        mock_config.upstream_timeout_seconds = 600.0

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers.multi_items.return_value = []
        mock_response.content = b"{}"

        mock_async_client = AsyncMock()
        mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
        mock_async_client.__aexit__ = AsyncMock(return_value=None)
        mock_async_client.request = AsyncMock(return_value=mock_response)

        with (
            patch("ccproxy.config.get_config", return_value=mock_config),
            patch("httpx.AsyncClient", return_value=mock_async_client) as client_cls,
        ):
            addon = InspectorAddon()
            await addon._retry_with_refreshed_token(flow)

        timeout = client_cls.call_args.kwargs["timeout"]
        assert isinstance(timeout, httpx.Timeout)
        assert timeout.read == 600.0
        assert timeout.connect == 600.0

    @pytest.mark.asyncio
    async def test_retry_honors_disabled_timeout(self) -> None:
        """Setting upstream_timeout_seconds=None disables all timeouts on the retry client."""
        import httpx

        flow = self._make_oauth_flow(provider="anthropic")
        mock_config = MagicMock()
        mock_config.refresh_oauth_token.return_value = ("new-token", True)
        mock_config.get_auth_header.return_value = None
        mock_config.upstream_timeout_seconds = None

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers.multi_items.return_value = []
        mock_response.content = b"{}"

        mock_async_client = AsyncMock()
        mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
        mock_async_client.__aexit__ = AsyncMock(return_value=None)
        mock_async_client.request = AsyncMock(return_value=mock_response)

        with (
            patch("ccproxy.config.get_config", return_value=mock_config),
            patch("httpx.AsyncClient", return_value=mock_async_client) as client_cls,
        ):
            addon = InspectorAddon()
            await addon._retry_with_refreshed_token(flow)

        timeout = client_cls.call_args.kwargs["timeout"]
        assert isinstance(timeout, httpx.Timeout)
        assert timeout.read is None
        assert timeout.connect is None
