"""Tests for MITM OAuth header fixing."""

from unittest.mock import MagicMock

import pytest

from ccproxy.config import MitmConfig
from ccproxy.mitm.addon import CCProxyMitmAddon


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

    def test_ignores_non_anthropic_hosts(self, addon: CCProxyMitmAddon, mock_flow: MagicMock) -> None:
        """Non-Anthropic hosts should not have headers modified."""
        mock_flow.request.pretty_host = "api.openai.com"
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

    def test_handles_subdomain(self, addon: CCProxyMitmAddon, mock_flow: MagicMock) -> None:
        """Should work with Anthropic subdomains."""
        mock_flow.request.pretty_host = "messages.api.anthropic.com"
        mock_flow.request.headers = {
            "authorization": "Bearer oauth-token",
            "x-api-key": "dummy",
        }

        addon._fix_oauth_headers(mock_flow)

        assert "x-api-key" not in mock_flow.request.headers


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
