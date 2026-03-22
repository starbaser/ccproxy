"""Tests for MITM traffic capture addon."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from ccproxy.config import MitmConfig
from ccproxy.mitm.addon import CCProxyMitmAddon, ProxyDirection


@pytest.fixture
def mock_flow() -> MagicMock:
    """Create a mock HTTP flow."""
    flow = MagicMock()
    flow.request = MagicMock()
    flow.request.headers = {}
    flow.request.content = None
    flow.request.path = "/v1/messages"
    return flow


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
    """Tests for proxy direction-based traffic filtering."""

    @pytest.fixture
    def mock_storage(self) -> AsyncMock:
        """Create mock storage."""
        storage = AsyncMock()
        storage.create_trace = AsyncMock()
        return storage

    @pytest.mark.asyncio
    async def test_reverse_proxy_captures_localhost_only(self, mock_storage: AsyncMock, mock_flow: MagicMock) -> None:
        """Reverse proxy should only capture traffic to localhost."""
        config = MitmConfig()
        addon = CCProxyMitmAddon(storage=mock_storage, config=config, proxy_direction=ProxyDirection.REVERSE)

        # Localhost request should be captured
        mock_flow.id = "flow-1"
        mock_flow.request.pretty_host = "localhost"
        mock_flow.request.method = "POST"
        mock_flow.request.path = "/v1/chat/completions"
        mock_flow.request.pretty_url = "http://localhost/v1/chat/completions"
        mock_flow.request.content = None

        await addon.request(mock_flow)
        assert mock_storage.create_trace.called

        # External request should NOT be captured
        mock_storage.reset_mock()
        mock_flow.request.pretty_host = "api.anthropic.com"
        mock_flow.request.pretty_url = "https://api.anthropic.com/v1/messages"

        await addon.request(mock_flow)
        assert not mock_storage.create_trace.called

    @pytest.mark.asyncio
    async def test_forward_proxy_captures_external_only(self, mock_storage: AsyncMock, mock_flow: MagicMock) -> None:
        """Forward proxy should only capture traffic to external APIs."""
        config = MitmConfig()
        addon = CCProxyMitmAddon(storage=mock_storage, config=config, proxy_direction=ProxyDirection.FORWARD)

        # External request should be captured
        mock_flow.id = "flow-1"
        mock_flow.request.pretty_host = "api.anthropic.com"
        mock_flow.request.method = "POST"
        mock_flow.request.path = "/v1/messages"
        mock_flow.request.pretty_url = "https://api.anthropic.com/v1/messages"
        mock_flow.request.content = None

        await addon.request(mock_flow)
        assert mock_storage.create_trace.called

        # Localhost request should NOT be captured
        mock_storage.reset_mock()
        mock_flow.request.pretty_host = "localhost"
        mock_flow.request.pretty_url = "http://localhost/status"

        await addon.request(mock_flow)
        assert not mock_storage.create_trace.called

    @pytest.mark.asyncio
    async def test_forward_proxy_captures_langfuse(self, mock_storage: AsyncMock, mock_flow: MagicMock) -> None:
        """Forward proxy should capture Langfuse API calls."""
        config = MitmConfig()
        addon = CCProxyMitmAddon(storage=mock_storage, config=config, proxy_direction=ProxyDirection.FORWARD)

        mock_flow.id = "flow-1"
        mock_flow.request.pretty_host = "us.cloud.langfuse.com"
        mock_flow.request.method = "GET"
        mock_flow.request.path = "/api/public/projects"
        mock_flow.request.pretty_url = "https://us.cloud.langfuse.com/api/public/projects"
        mock_flow.request.content = None

        await addon.request(mock_flow)
        assert mock_storage.create_trace.called

    @pytest.mark.asyncio
    async def test_proxy_direction_stored_correctly(self, mock_storage: AsyncMock, mock_flow: MagicMock) -> None:
        """Proxy direction should be stored in trace data."""
        config = MitmConfig()

        # Test REVERSE direction
        addon_reverse = CCProxyMitmAddon(storage=mock_storage, config=config, proxy_direction=ProxyDirection.REVERSE)
        mock_flow.id = "flow-1"
        mock_flow.request.pretty_host = "localhost"
        mock_flow.request.method = "POST"
        mock_flow.request.path = "/v1/chat/completions"
        mock_flow.request.pretty_url = "http://localhost/v1/chat/completions"
        mock_flow.request.content = None

        await addon_reverse.request(mock_flow)
        call_args = mock_storage.create_trace.call_args[0][0]
        assert call_args["proxy_direction"] == ProxyDirection.REVERSE.value

        # Test FORWARD direction
        mock_storage.reset_mock()
        addon_forward = CCProxyMitmAddon(storage=mock_storage, config=config, proxy_direction=ProxyDirection.FORWARD)
        mock_flow.request.pretty_host = "api.anthropic.com"
        mock_flow.request.pretty_url = "https://api.anthropic.com/v1/messages"

        await addon_forward.request(mock_flow)
        call_args = mock_storage.create_trace.call_args[0][0]
        assert call_args["proxy_direction"] == ProxyDirection.FORWARD.value
