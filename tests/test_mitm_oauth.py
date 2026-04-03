"""Tests for MITM traffic capture addon."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from ccproxy.config import MitmConfig
from ccproxy.mitm.addon import CCProxyMitmAddon, ProxyDirection


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


class TestRequestMethod:
    """Tests for the request method trace capture."""

    @pytest.mark.asyncio
    async def test_request_works_without_storage(self, mock_flow: MagicMock) -> None:
        """request() should return early without storage configured."""
        config = MitmConfig()
        addon = CCProxyMitmAddon(storage=None, config=config)

        mock_flow.request.pretty_host = "api.anthropic.com"

        await addon.request(mock_flow)


class TestProxyDirectionFiltering:
    """Tests for proxy direction-based traffic filtering via proxy_mode."""

    @pytest.fixture
    def mock_storage(self) -> AsyncMock:
        """Create mock storage."""
        storage = AsyncMock()
        storage.create_trace = AsyncMock()
        return storage

    @pytest.mark.asyncio
    async def test_reverse_proxy_captures_traffic(self, mock_storage: AsyncMock) -> None:
        """Reverse proxy mode flow should be captured with REVERSE direction."""
        config = MitmConfig()
        addon = CCProxyMitmAddon(storage=mock_storage, config=config)

        flow = _make_mock_flow(reverse=True)
        flow.id = "flow-1"
        flow.request.pretty_host = "localhost"
        flow.request.method = "POST"
        flow.request.path = "/v1/chat/completions"
        flow.request.pretty_url = "http://localhost/v1/chat/completions"
        flow.request.content = None

        await addon.request(flow)
        assert mock_storage.create_trace.called

    @pytest.mark.asyncio
    async def test_forward_proxy_captures_traffic(self, mock_storage: AsyncMock) -> None:
        """Forward proxy mode flow should be captured with FORWARD direction."""
        config = MitmConfig()
        addon = CCProxyMitmAddon(storage=mock_storage, config=config)

        flow = _make_mock_flow(reverse=False)
        flow.id = "flow-1"
        flow.request.pretty_host = "api.anthropic.com"
        flow.request.method = "POST"
        flow.request.path = "/v1/messages"
        flow.request.pretty_url = "https://api.anthropic.com/v1/messages"
        flow.request.content = None

        await addon.request(flow)
        assert mock_storage.create_trace.called

    @pytest.mark.asyncio
    async def test_forward_proxy_captures_langfuse(self, mock_storage: AsyncMock) -> None:
        """Forward proxy should capture Langfuse API calls."""
        config = MitmConfig()
        addon = CCProxyMitmAddon(storage=mock_storage, config=config)

        flow = _make_mock_flow(reverse=False)
        flow.id = "flow-1"
        flow.request.pretty_host = "us.cloud.langfuse.com"
        flow.request.method = "GET"
        flow.request.path = "/api/public/projects"
        flow.request.pretty_url = "https://us.cloud.langfuse.com/api/public/projects"
        flow.request.content = None

        await addon.request(flow)
        assert mock_storage.create_trace.called

    @pytest.mark.asyncio
    async def test_proxy_direction_stored_correctly(self, mock_storage: AsyncMock) -> None:
        """Proxy direction should be stored in trace data based on proxy_mode."""
        config = MitmConfig()
        addon = CCProxyMitmAddon(storage=mock_storage, config=config)

        # Test REVERSE direction
        flow_reverse = _make_mock_flow(reverse=True)
        flow_reverse.id = "flow-1"
        flow_reverse.request.pretty_host = "localhost"
        flow_reverse.request.method = "POST"
        flow_reverse.request.path = "/v1/chat/completions"
        flow_reverse.request.pretty_url = "http://localhost/v1/chat/completions"
        flow_reverse.request.content = None

        await addon.request(flow_reverse)
        call_args = mock_storage.create_trace.call_args[0][0]
        assert call_args["proxy_direction"] == ProxyDirection.REVERSE.value

        # Test FORWARD direction
        mock_storage.reset_mock()
        flow_forward = _make_mock_flow(reverse=False)
        flow_forward.id = "flow-2"
        flow_forward.request.pretty_host = "api.anthropic.com"
        flow_forward.request.method = "POST"
        flow_forward.request.path = "/v1/messages"
        flow_forward.request.pretty_url = "https://api.anthropic.com/v1/messages"
        flow_forward.request.content = None

        await addon.request(flow_forward)
        call_args = mock_storage.create_trace.call_args[0][0]
        assert call_args["proxy_direction"] == ProxyDirection.FORWARD.value
