"""Tests for inspector addon traffic capture."""

import json
from unittest.mock import MagicMock

import pytest

from ccproxy.inspector.addon import InspectorAddon
from ccproxy.inspector.flow_store import FLOW_ID_HEADER, InspectorMeta, create_flow_record


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
    """Tests for the request method."""

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
    """Edge cases for _get_direction."""

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

        # Should not raise even if something goes wrong
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
        # Should not raise


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
        # Should not raise
