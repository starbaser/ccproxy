"""Tests for OAuth token refresh functionality."""

import time
from unittest.mock import MagicMock, patch

import pytest

from ccproxy.config import CCProxyConfig, clear_config_instance, set_config_instance
from ccproxy.handler import CCProxyHandler
from ccproxy.router import clear_router


@pytest.fixture(autouse=True)
def cleanup():
    """Clean up config and router singletons between tests."""
    clear_config_instance()
    clear_router()
    yield
    clear_config_instance()
    clear_router()
    # Reset class-level task
    CCProxyHandler._oauth_refresh_task = None


class TestOAuthTokenExpiration:
    """Test OAuth token expiration detection."""

    def test_is_token_expired_no_token(self):
        """Test that missing tokens are considered expired."""
        config = CCProxyConfig(
            oat_sources={"anthropic": "echo 'test-token'"},
            oauth_ttl=3600,
            oauth_refresh_buffer=0.1,
        )
        # Don't load credentials, so _oat_values is empty
        assert config.is_token_expired("anthropic") is True
        assert config.is_token_expired("unknown_provider") is True

    def test_is_token_expired_fresh_token(self):
        """Test that freshly loaded tokens are not expired."""
        config = CCProxyConfig(
            oat_sources={"anthropic": "echo 'test-token'"},
            oauth_ttl=3600,
            oauth_refresh_buffer=0.1,
        )
        # Manually set a fresh token
        config._oat_values["anthropic"] = ("test-token", time.time())
        assert config.is_token_expired("anthropic") is False

    def test_is_token_expired_at_buffer_threshold(self):
        """Test token expiration at the buffer threshold."""
        config = CCProxyConfig(
            oat_sources={"anthropic": "echo 'test-token'"},
            oauth_ttl=3600,  # 1 hour
            oauth_refresh_buffer=0.1,  # 10% buffer
        )
        # Token loaded 3240 seconds ago (90% of TTL) - should be expired
        old_time = time.time() - 3240
        config._oat_values["anthropic"] = ("test-token", old_time)
        assert config.is_token_expired("anthropic") is True

    def test_is_token_expired_before_buffer(self):
        """Test token not expired before buffer threshold."""
        config = CCProxyConfig(
            oat_sources={"anthropic": "echo 'test-token'"},
            oauth_ttl=3600,  # 1 hour
            oauth_refresh_buffer=0.1,  # 10% buffer
        )
        # Token loaded 3000 seconds ago (83% of TTL) - should NOT be expired
        old_time = time.time() - 3000
        config._oat_values["anthropic"] = ("test-token", old_time)
        assert config.is_token_expired("anthropic") is False


class TestOAuthTokenRefresh:
    """Test OAuth token refresh functionality."""

    def test_refresh_oauth_token_success(self):
        """Test successful token refresh."""
        config = CCProxyConfig(
            oat_sources={"anthropic": "echo 'new-token'"},
            oauth_ttl=3600,
            oauth_refresh_buffer=0.1,
        )
        # Set an old token
        config._oat_values["anthropic"] = ("old-token", time.time() - 4000)

        new_token = config.refresh_oauth_token("anthropic")

        assert new_token == "new-token"
        assert config.get_oauth_token("anthropic") == "new-token"
        # Timestamp should be updated
        _, timestamp = config._oat_values["anthropic"]
        assert time.time() - timestamp < 1  # Should be very recent

    def test_refresh_oauth_token_failure(self):
        """Test token refresh failure."""
        config = CCProxyConfig(
            oat_sources={"anthropic": "exit 1"},  # Command that fails
            oauth_ttl=3600,
            oauth_refresh_buffer=0.1,
        )
        # Set an old token
        config._oat_values["anthropic"] = ("old-token", time.time() - 4000)

        new_token = config.refresh_oauth_token("anthropic")

        assert new_token is None
        # Old token should still be there (refresh failed)
        assert config.get_oauth_token("anthropic") == "old-token"

    def test_refresh_oauth_token_unknown_provider(self):
        """Test refresh for unknown provider returns None."""
        config = CCProxyConfig(
            oat_sources={"anthropic": "echo 'test'"},
            oauth_ttl=3600,
            oauth_refresh_buffer=0.1,
        )

        new_token = config.refresh_oauth_token("unknown_provider")

        assert new_token is None

    def test_refresh_oauth_token_with_user_agent(self):
        """Test that refresh preserves user agent."""
        config = CCProxyConfig(
            oat_sources={
                "gemini": {
                    "command": "echo 'gemini-token'",
                    "user_agent": "CustomAgent/1.0",
                }
            },
            oauth_ttl=3600,
            oauth_refresh_buffer=0.1,
        )
        # Set existing values
        config._oat_values["gemini"] = ("old-token", time.time() - 4000)
        config._oat_user_agents["gemini"] = "CustomAgent/1.0"

        new_token = config.refresh_oauth_token("gemini")

        assert new_token == "gemini-token"
        assert config.get_oauth_user_agent("gemini") == "CustomAgent/1.0"


class TestOAuthConfigFromYaml:
    """Test OAuth config loading from YAML."""

    def test_oauth_ttl_from_yaml(self, tmp_path):
        """Test oauth_ttl is loaded from YAML."""
        yaml_content = """
ccproxy:
  oauth_ttl: 7200
  oauth_refresh_buffer: 0.2
"""
        yaml_path = tmp_path / "ccproxy.yaml"
        yaml_path.write_text(yaml_content)

        config = CCProxyConfig.from_yaml(yaml_path)

        assert config.oauth_ttl == 7200
        assert config.oauth_refresh_buffer == 0.2

    def test_oauth_ttl_defaults(self, tmp_path):
        """Test oauth_ttl defaults when not specified."""
        yaml_content = """
ccproxy:
  debug: false
"""
        yaml_path = tmp_path / "ccproxy.yaml"
        yaml_path.write_text(yaml_content)

        config = CCProxyConfig.from_yaml(yaml_path)

        assert config.oauth_ttl == 28800  # 8 hours default
        assert config.oauth_refresh_buffer == 0.1  # 10% default


class TestOAuthValuesProperty:
    """Test oat_values property returns correct format."""

    def test_oat_values_returns_tokens_only(self):
        """Test that oat_values property returns dict of tokens without timestamps."""
        config = CCProxyConfig()
        config._oat_values = {
            "anthropic": ("token-1", 1000.0),
            "openai": ("token-2", 2000.0),
        }

        values = config.oat_values

        assert values == {"anthropic": "token-1", "openai": "token-2"}
        # Ensure it's a new dict, not a reference
        assert isinstance(values, dict)


class TestHandler401Detection:
    """Test 401 error detection in handler."""

    def test_is_auth_error_with_status_code(self):
        """Test 401 detection via status_code attribute."""
        handler = CCProxyHandler.__new__(CCProxyHandler)

        error_401 = MagicMock(spec=["status_code"])
        error_401.status_code = 401

        error_500 = MagicMock(spec=["status_code"])
        error_500.status_code = 500

        assert handler._is_auth_error(error_401) is True
        assert handler._is_auth_error(error_500) is False

    def test_is_auth_error_with_message(self):
        """Test 401 detection via message attribute."""
        handler = CCProxyHandler.__new__(CCProxyHandler)

        error_with_401 = MagicMock(spec=[])
        error_with_401.message = "Error 401: Unauthorized"

        error_with_auth = MagicMock(spec=[])
        error_with_auth.message = "Authentication failed"

        error_other = MagicMock(spec=[])
        error_other.message = "Internal server error"

        assert handler._is_auth_error(error_with_401) is True
        assert handler._is_auth_error(error_with_auth) is True
        assert handler._is_auth_error(error_other) is False

    def test_is_auth_error_no_attributes(self):
        """Test 401 detection with object lacking relevant attributes."""
        handler = CCProxyHandler.__new__(CCProxyHandler)

        error = object()
        assert handler._is_auth_error(error) is False


class TestHandlerProviderExtraction:
    """Test provider extraction from request metadata."""

    def test_extract_provider_anthropic(self):
        """Test extraction of anthropic provider."""
        handler = CCProxyHandler.__new__(CCProxyHandler)

        kwargs = {"metadata": {"ccproxy_litellm_model": "claude-sonnet-4-5-20250929"}}
        assert handler._extract_provider_from_metadata(kwargs) == "anthropic"

        kwargs = {"metadata": {"ccproxy_litellm_model": "anthropic/claude-3-opus"}}
        assert handler._extract_provider_from_metadata(kwargs) == "anthropic"

    def test_extract_provider_openai(self):
        """Test extraction of openai provider."""
        handler = CCProxyHandler.__new__(CCProxyHandler)

        kwargs = {"metadata": {"ccproxy_litellm_model": "gpt-4-turbo"}}
        assert handler._extract_provider_from_metadata(kwargs) == "openai"

        kwargs = {"model": "openai/gpt-4"}
        assert handler._extract_provider_from_metadata(kwargs) == "openai"

    def test_extract_provider_gemini(self):
        """Test extraction of gemini provider."""
        handler = CCProxyHandler.__new__(CCProxyHandler)

        kwargs = {"metadata": {"ccproxy_litellm_model": "gemini-pro"}}
        assert handler._extract_provider_from_metadata(kwargs) == "gemini"

        kwargs = {"model": "google/gemini-1.5-pro"}
        assert handler._extract_provider_from_metadata(kwargs) == "gemini"

    def test_extract_provider_unknown(self):
        """Test extraction with unknown provider."""
        handler = CCProxyHandler.__new__(CCProxyHandler)

        kwargs = {"metadata": {"ccproxy_litellm_model": "llama-3-70b"}}
        assert handler._extract_provider_from_metadata(kwargs) is None

        kwargs = {}
        assert handler._extract_provider_from_metadata(kwargs) is None


@pytest.mark.asyncio
class TestHandler401Refresh:
    """Test 401-triggered token refresh in handler."""

    async def test_401_triggers_refresh(self):
        """Test that 401 error triggers OAuth token refresh."""
        # Set up config with OAuth source
        config = CCProxyConfig(
            oat_sources={"anthropic": "echo 'refreshed-token'"},
            oauth_ttl=3600,
        )
        config._oat_values["anthropic"] = ("old-token", time.time())
        set_config_instance(config)

        # Create handler (need to mock some dependencies)
        mock_proxy_server = MagicMock()
        mock_proxy_server.llm_router = MagicMock()
        mock_proxy_server.llm_router.model_list = []
        mock_module = MagicMock()
        mock_module.proxy_server = mock_proxy_server

        with patch.dict("sys.modules", {"litellm.proxy": mock_module}):
            clear_router()
            handler = CCProxyHandler()

            # Create a 401 error response
            error_response = MagicMock()
            error_response.status_code = 401
            error_response.message = "Unauthorized"

            kwargs = {
                "metadata": {"ccproxy_litellm_model": "claude-sonnet-4-5-20250929"},
                "model": "claude-sonnet-4-5-20250929",
            }

            # Call the failure handler
            await handler.async_log_failure_event(kwargs, error_response, time.time(), time.time())

            # Token should be refreshed
            assert config.get_oauth_token("anthropic") == "refreshed-token"

    async def test_401_no_refresh_for_unconfigured_provider(self):
        """Test that 401 doesn't refresh for providers without OAuth config."""
        config = CCProxyConfig(
            oat_sources={},  # No OAuth sources configured
            oauth_ttl=3600,
        )
        set_config_instance(config)

        mock_proxy_server = MagicMock()
        mock_proxy_server.llm_router = MagicMock()
        mock_proxy_server.llm_router.model_list = []
        mock_module = MagicMock()
        mock_module.proxy_server = mock_proxy_server

        with patch.dict("sys.modules", {"litellm.proxy": mock_module}):
            clear_router()
            handler = CCProxyHandler()

            error_response = MagicMock()
            error_response.status_code = 401

            kwargs = {
                "metadata": {"ccproxy_litellm_model": "claude-sonnet-4-5-20250929"},
                "model": "claude-sonnet-4-5-20250929",
            }

            # Should not raise even though there's no OAuth config
            await handler.async_log_failure_event(kwargs, error_response, time.time(), time.time())


@pytest.mark.asyncio
class TestBackgroundRefreshTask:
    """Test background OAuth refresh task."""

    async def test_start_oauth_refresh_task_starts_once(self):
        """Test that background task is only started once."""
        import asyncio

        mock_proxy_server = MagicMock()
        mock_proxy_server.llm_router = MagicMock()
        mock_proxy_server.llm_router.model_list = []
        mock_module = MagicMock()
        mock_module.proxy_server = mock_proxy_server

        config = CCProxyConfig()
        set_config_instance(config)

        with patch.dict("sys.modules", {"litellm.proxy": mock_module}):
            clear_router()
            handler = CCProxyHandler()

            # Task should be None initially
            assert CCProxyHandler._oauth_refresh_task is None

            # Start the task
            await handler._start_oauth_refresh_task()
            task1 = CCProxyHandler._oauth_refresh_task
            assert task1 is not None

            # Starting again should return the same task
            await handler._start_oauth_refresh_task()
            task2 = CCProxyHandler._oauth_refresh_task
            assert task1 is task2

            # Cleanup
            task1.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task1
