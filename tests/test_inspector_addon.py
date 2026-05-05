"""Tests for inspector addon traffic capture."""

import json
from unittest.mock import MagicMock, patch

import pytest

from ccproxy.flows.store import (
    FLOW_ID_HEADER,
    FlowRecord,
    HttpSnapshot,
    InspectorMeta,
    TransformMeta,
    create_flow_record,
)
from ccproxy.inspector.addon import InspectorAddon


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


class TestRequestHeaders:
    """Tests for the requestheaders() defense-in-depth hook."""

    @pytest.mark.asyncio
    async def test_disables_streaming_for_reverse_proxy_flows(self) -> None:
        addon = InspectorAddon()
        flow = _make_mock_flow(reverse=True)
        flow.request.stream = True

        await addon.requestheaders(flow)

        assert flow.request.stream is False

    @pytest.mark.asyncio
    async def test_preserves_streaming_for_wireguard_flows(self) -> None:
        addon = InspectorAddon()
        flow = _make_wg_flow()
        flow.request.stream = True

        await addon.requestheaders(flow)

        assert flow.request.stream is True

    @pytest.mark.asyncio
    async def test_noop_when_not_streaming(self) -> None:
        addon = InspectorAddon()
        flow = _make_mock_flow(reverse=True)
        flow.request.stream = False

        await addon.requestheaders(flow)

        assert flow.request.stream is False


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
    """Tests for _extract_session_id_from_body."""

    def test_no_body(self) -> None:
        assert InspectorAddon._extract_session_id_from_body(None) is None

    def test_empty_body(self) -> None:
        assert InspectorAddon._extract_session_id_from_body({}) is None

    def test_missing_metadata(self) -> None:
        assert InspectorAddon._extract_session_id_from_body({"model": "claude"}) is None

    def test_metadata_not_dict(self) -> None:
        assert InspectorAddon._extract_session_id_from_body({"metadata": "a string"}) is None

    def test_empty_user_id(self) -> None:
        assert InspectorAddon._extract_session_id_from_body({"metadata": {"user_id": ""}}) is None

    def test_json_format_session_id(self) -> None:
        user_id_obj = json.dumps({"session_id": "abc123"})
        assert InspectorAddon._extract_session_id_from_body({"metadata": {"user_id": user_id_obj}}) == "abc123"

    def test_legacy_format(self) -> None:
        assert (
            InspectorAddon._extract_session_id_from_body(
                {"metadata": {"user_id": "user_hash_account_uuid_session_sid123"}}
            )
            == "sid123"
        )

    def test_multiple_session_separators(self) -> None:
        assert InspectorAddon._extract_session_id_from_body({"metadata": {"user_id": "a_session_b_session_c"}}) is None

    def test_neither_format(self) -> None:
        assert InspectorAddon._extract_session_id_from_body({"metadata": {"user_id": "plain-user-id"}}) is None


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

    @pytest.mark.asyncio
    async def test_error_client_disconnect_routes_to_disconnect_tracer(self) -> None:
        """Client disconnect after successful server response records the real
        status via finish_span_client_disconnect, not finish_span_error."""
        addon = InspectorAddon()
        mock_tracer = MagicMock()
        addon.set_tracer(mock_tracer)

        flow = MagicMock()
        flow.error = MagicMock()
        flow.error.__str__ = lambda self: "Client disconnected."
        flow.id = "disconnect-flow-1"
        flow.response = MagicMock()
        flow.response.status_code = 200
        flow.request.timestamp_start = 100.0
        flow.response.timestamp_end = 101.5

        await addon.error(flow)

        mock_tracer.finish_span_client_disconnect.assert_called_once()
        args = mock_tracer.finish_span_client_disconnect.call_args
        assert args.args[1] == 200  # status_code
        assert args.args[2] == 1500.0  # duration_ms (1.5 seconds)
        mock_tracer.finish_span_error.assert_not_called()

    @pytest.mark.asyncio
    async def test_error_client_disconnect_without_response_uses_error_tracer(self) -> None:
        """Client disconnect with no flow.response falls back to finish_span_error."""
        addon = InspectorAddon()
        mock_tracer = MagicMock()
        addon.set_tracer(mock_tracer)

        flow = MagicMock()
        flow.error = MagicMock()
        flow.error.__str__ = lambda self: "Client disconnected."
        flow.id = "disconnect-flow-2"
        flow.response = None

        await addon.error(flow)

        mock_tracer.finish_span_error.assert_called_once()
        mock_tracer.finish_span_client_disconnect.assert_not_called()

    @pytest.mark.asyncio
    async def test_error_client_disconnect_missing_timestamps(self) -> None:
        """Duration_ms is None when either timestamp is missing."""
        addon = InspectorAddon()
        mock_tracer = MagicMock()
        addon.set_tracer(mock_tracer)

        flow = MagicMock()
        flow.error = MagicMock()
        flow.error.__str__ = lambda self: "Client disconnected."
        flow.id = "disconnect-flow-3"
        flow.response = MagicMock()
        flow.response.status_code = 200
        flow.request.timestamp_start = None
        flow.response.timestamp_end = 101.5

        await addon.error(flow)

        args = mock_tracer.finish_span_client_disconnect.call_args
        assert args.args[2] is None  # duration_ms


class TestProviderResponseCapture:
    """Tests for provider_response snapshot in response()."""

    @pytest.mark.asyncio
    async def test_captures_provider_response_before_mutations(self) -> None:
        addon = InspectorAddon()
        record = FlowRecord(direction="inbound")
        flow = MagicMock()
        flow.response = MagicMock()
        flow.response.status_code = 200
        flow.response.content = b'{"raw": "provider data"}'
        flow.response.headers = MagicMock()
        flow.response.headers.items.return_value = [("content-type", "application/json")]
        flow.response.timestamp_end = 1000.5
        flow.request.timestamp_start = 1000.0
        flow.request.pretty_url = "https://api.anthropic.com/v1/messages"
        flow.id = "capture-flow"
        flow.metadata = {InspectorMeta.RECORD: record}

        await addon.response(flow)

        assert record.provider_response is not None
        assert record.provider_response.status_code == 200
        assert record.provider_response.body == b'{"raw": "provider data"}'

    @pytest.mark.asyncio
    async def test_captures_raw_body_from_sse_transformer(self) -> None:
        addon = InspectorAddon()
        record = FlowRecord(direction="inbound")

        class FakeTransformer:
            @property
            def raw_body(self) -> bytes:
                return b"data: raw sse\n\n"

        flow = MagicMock()
        flow.response = MagicMock()
        flow.response.status_code = 200
        flow.response.content = b"data: transformed\n\n"
        flow.response.headers = MagicMock()
        flow.response.headers.items.return_value = [("content-type", "text/event-stream")]
        flow.response.timestamp_end = 1000.5
        flow.request.timestamp_start = 1000.0
        flow.request.pretty_url = "https://api.anthropic.com/v1/messages"
        flow.id = "sse-capture"
        flow.metadata = {
            InspectorMeta.RECORD: record,
            "ccproxy.sse_transformer": FakeTransformer(),
        }

        await addon.response(flow)

        assert record.provider_response is not None
        assert record.provider_response.body == b"data: raw sse\n\n"

    @pytest.mark.asyncio
    async def test_no_capture_when_content_is_none(self) -> None:
        addon = InspectorAddon()
        record = FlowRecord(direction="inbound")
        flow = MagicMock()
        flow.response = MagicMock()
        flow.response.status_code = 200
        flow.response.content = None
        flow.response.headers = MagicMock()
        flow.response.headers.items.return_value = []
        flow.response.timestamp_end = 1000.5
        flow.request.timestamp_start = 1000.0
        flow.request.pretty_url = "https://api.example.com/v1"
        flow.id = "null-content"
        flow.metadata = {InspectorMeta.RECORD: record}

        await addon.response(flow)

        assert record.provider_response is None


class TestResponseExceptionHandling:
    """Verify response() exception trapping."""

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
            provider="anthropic",
            model="claude-3",
            request_data={"messages": []},
            is_streaming=True,
            mode="transform",
        )
        record = FlowRecord(direction="inbound", transform=meta)
        flow = MagicMock()
        flow.response.headers = {"content-type": "text/event-stream"}
        flow.metadata = {InspectorMeta.RECORD: record}

        with patch("ccproxy.lightllm.dispatch.make_sse_transformer", side_effect=RuntimeError("fail")):
            await addon.responseheaders(flow)

        assert flow.response.stream is True


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


class TestGetClientRequestCommand:
    """Tests for InspectorAddon.get_client_request mitmproxy command."""

    def _make_flow_with_client_request(
        self,
        flow_id: str = "flow-abc-123",
        method: str = "POST",
        url: str = "https://api.anthropic.com:443/v1/messages",
        headers: dict[str, str] | None = None,
        body: bytes = b'{"model": "claude-3"}',
    ) -> MagicMock:
        cr = HttpSnapshot(
            headers=headers or {"content-type": "application/json"},
            body=body,
            method=method,
            url=url,
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
            url="https://api.anthropic.com:443/v1/messages",
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


class TestProviderTimeoutDefault:
    """Locked-in default for the provider-timeout knob used by OAuthAddon retries."""

    def test_default_config_has_no_provider_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Portkey parity locked in at the config layer: default provider_timeout is None."""
        from ccproxy.config import CCProxyConfig

        monkeypatch.delenv("CCPROXY_PROVIDER_TIMEOUT", raising=False)
        config = CCProxyConfig()
        assert config.provider_timeout is None
