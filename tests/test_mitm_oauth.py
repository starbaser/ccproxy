"""Tests for MITM OAuth header fixing."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from ccproxy.config import MitmConfig
from ccproxy.mitm.addon import CCProxyMitmAddon, ProxyDirection


@pytest.fixture
def addon() -> CCProxyMitmAddon:
    """Create addon without storage."""
    config = MitmConfig()
    return CCProxyMitmAddon(storage=None, config=config)


@pytest.fixture
def mock_flow() -> MagicMock:
    """Create a mock HTTP flow."""
    flow = MagicMock()
    flow.request = MagicMock()
    flow.request.headers = {}
    flow.request.content = None  # No body by default
    flow.request.path = "/v1/messages"  # Default to Anthropic-type endpoint
    return flow


class TestFixOAuthHeaders:
    """Tests for _fix_oauth_headers method."""

    def test_removes_x_api_key_when_bearer_present(self, addon: CCProxyMitmAddon, mock_flow: MagicMock) -> None:
        """x-api-key should be removed when Authorization Bearer is present."""
        mock_flow.request.pretty_host = "api.anthropic.com"
        mock_flow.request.headers = {
            "authorization": "Bearer oauth-token-123",
            "x-api-key": "sk-ant-dummy-key",
            "content-type": "application/json",
        }

        addon._fix_oauth_headers(mock_flow)

        assert "x-api-key" not in mock_flow.request.headers
        assert mock_flow.request.headers["authorization"] == "Bearer oauth-token-123"
        assert mock_flow.request.headers["content-type"] == "application/json"

    def test_preserves_x_api_key_when_no_bearer(self, addon: CCProxyMitmAddon, mock_flow: MagicMock) -> None:
        """x-api-key should be preserved when no Bearer token is present."""
        mock_flow.request.pretty_host = "api.anthropic.com"
        mock_flow.request.headers = {
            "x-api-key": "sk-ant-real-key",
            "content-type": "application/json",
        }

        addon._fix_oauth_headers(mock_flow)

        assert mock_flow.request.headers["x-api-key"] == "sk-ant-real-key"

    def test_ignores_non_messages_endpoints(self, addon: CCProxyMitmAddon, mock_flow: MagicMock) -> None:
        """Non-messages endpoints should not have headers modified."""
        mock_flow.request.pretty_host = "api.anthropic.com"
        mock_flow.request.path = "/v1/chat/completions"  # OpenAI-style endpoint
        mock_flow.request.headers = {
            "authorization": "Bearer some-token",
            "x-api-key": "some-key",
        }

        addon._fix_oauth_headers(mock_flow)

        assert mock_flow.request.headers["x-api-key"] == "some-key"
        assert mock_flow.request.headers["authorization"] == "Bearer some-token"

    def test_handles_case_insensitive_bearer(self, addon: CCProxyMitmAddon, mock_flow: MagicMock) -> None:
        """Bearer token check should be case-insensitive."""
        mock_flow.request.pretty_host = "api.anthropic.com"
        mock_flow.request.headers = {
            "authorization": "BEARER oauth-token-123",
            "x-api-key": "sk-ant-dummy",
        }

        addon._fix_oauth_headers(mock_flow)

        assert "x-api-key" not in mock_flow.request.headers

    def test_handles_missing_authorization_header(self, addon: CCProxyMitmAddon, mock_flow: MagicMock) -> None:
        """Should handle missing authorization header gracefully."""
        mock_flow.request.pretty_host = "api.anthropic.com"
        mock_flow.request.headers = {
            "x-api-key": "sk-ant-key",
        }

        addon._fix_oauth_headers(mock_flow)

        assert mock_flow.request.headers["x-api-key"] == "sk-ant-key"

    def test_handles_no_x_api_key(self, addon: CCProxyMitmAddon, mock_flow: MagicMock) -> None:
        """Should not error when x-api-key is not present."""
        mock_flow.request.pretty_host = "api.anthropic.com"
        mock_flow.request.headers = {
            "authorization": "Bearer oauth-token",
        }

        # Should not raise
        addon._fix_oauth_headers(mock_flow)

        assert "x-api-key" not in mock_flow.request.headers

    def test_handles_zai_provider(self, addon: CCProxyMitmAddon, mock_flow: MagicMock) -> None:
        """Should work with api.z.ai and other Anthropic-compatible providers."""
        mock_flow.request.pretty_host = "api.z.ai"
        mock_flow.request.path = "/api/anthropic/v1/messages"
        mock_flow.request.headers = {
            "authorization": "Bearer oauth-token",
            "x-api-key": "dummy",
        }

        addon._fix_oauth_headers(mock_flow)

        assert "x-api-key" not in mock_flow.request.headers

    def test_restores_oauth_from_x_api_key(self, addon: CCProxyMitmAddon, mock_flow: MagicMock) -> None:
        """OAuth token in x-api-key (LiteLLM converted) should be restored to Authorization."""
        mock_flow.request.pretty_host = "api.z.ai"
        mock_flow.request.path = "/api/anthropic/v1/messages"
        # LiteLLM converts Bearer → x-api-key, so no Authorization header
        mock_flow.request.headers = {
            "x-api-key": "oauth-token-without-sk-ant-prefix",
            "content-type": "application/json",
        }

        addon._fix_oauth_headers(mock_flow)

        assert "x-api-key" not in mock_flow.request.headers
        assert mock_flow.request.headers["authorization"] == "Bearer oauth-token-without-sk-ant-prefix"

    def test_preserves_real_api_key(self, addon: CCProxyMitmAddon, mock_flow: MagicMock) -> None:
        """Real API keys (sk-ant-*) should not be converted to Bearer."""
        mock_flow.request.pretty_host = "api.anthropic.com"
        mock_flow.request.path = "/v1/messages"
        mock_flow.request.headers = {
            "x-api-key": "sk-ant-real-api-key-123",
            "content-type": "application/json",
        }

        addon._fix_oauth_headers(mock_flow)

        # Should preserve as-is since it's a real API key
        assert mock_flow.request.headers["x-api-key"] == "sk-ant-real-api-key-123"
        assert "authorization" not in mock_flow.request.headers


class TestRequestMethod:
    """Tests for the request method integration."""

    @pytest.mark.asyncio
    async def test_request_calls_fix_oauth_headers(self, addon: CCProxyMitmAddon, mock_flow: MagicMock) -> None:
        """request() should call _fix_oauth_headers."""
        mock_flow.request.pretty_host = "api.anthropic.com"
        mock_flow.request.headers = {
            "authorization": "Bearer token",
            "x-api-key": "dummy",
        }

        await addon.request(mock_flow)

        assert "x-api-key" not in mock_flow.request.headers

    @pytest.mark.asyncio
    async def test_request_fixes_headers_without_storage(self, mock_flow: MagicMock) -> None:
        """OAuth header fix should work even without storage configured."""
        config = MitmConfig()
        addon = CCProxyMitmAddon(storage=None, config=config)

        mock_flow.request.pretty_host = "api.anthropic.com"
        mock_flow.request.headers = {
            "authorization": "Bearer token",
            "x-api-key": "dummy",
        }

        await addon.request(mock_flow)

        assert "x-api-key" not in mock_flow.request.headers


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
        addon_reverse = CCProxyMitmAddon(
            storage=mock_storage, config=config, proxy_direction=ProxyDirection.REVERSE
        )
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
