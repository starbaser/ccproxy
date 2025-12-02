"""Tests for custom User-Agent support in OAuth token sources."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ccproxy.config import CCProxyConfig, OAuthSource, clear_config_instance
from ccproxy.handler import CCProxyHandler
from ccproxy.router import clear_router


class TestOAuthSource:
    """Tests for OAuthSource model."""

    def test_oauth_source_with_command_only(self) -> None:
        """Test OAuthSource with just command (no user_agent)."""
        source = OAuthSource(command="echo 'test-token'")
        assert source.command == "echo 'test-token'"
        assert source.user_agent is None

    def test_oauth_source_with_user_agent(self) -> None:
        """Test OAuthSource with both command and user_agent."""
        source = OAuthSource(command="echo 'test-token'", user_agent="MyApp/1.0.0")
        assert source.command == "echo 'test-token'"
        assert source.user_agent == "MyApp/1.0.0"


class TestOAuthSourceConfigLoading:
    """Tests for loading OAuth sources with user-agent from YAML."""

    def test_string_format_backwards_compatibility(self) -> None:
        """Test that simple string format still works (backwards compatible)."""
        yaml_content = """
ccproxy:
  oat_sources:
    anthropic: echo 'anthropic-token-123'
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            yaml_path = Path(f.name)

        try:
            config = CCProxyConfig.from_yaml(yaml_path)

            # Token should be loaded
            assert config.get_oauth_token("anthropic") == "anthropic-token-123"
            # No user-agent should be configured
            assert config.get_oauth_user_agent("anthropic") is None

        finally:
            yaml_path.unlink()

    def test_extended_format_with_user_agent(self) -> None:
        """Test loading OAuth source with custom user_agent."""
        yaml_content = """
ccproxy:
  oat_sources:
    vertex_ai:
      command: echo 'vertex-ai-token-456'
      user_agent: MyApp/1.0.0
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            yaml_path = Path(f.name)

        try:
            config = CCProxyConfig.from_yaml(yaml_path)

            # Token should be loaded
            assert config.get_oauth_token("vertex_ai") == "vertex-ai-token-456"
            # User-agent should be configured
            assert config.get_oauth_user_agent("vertex_ai") == "MyApp/1.0.0"

        finally:
            yaml_path.unlink()

    def test_mixed_format_sources(self) -> None:
        """Test mixing string and extended formats in same config."""
        yaml_content = """
ccproxy:
  oat_sources:
    anthropic: echo 'anthropic-token-123'
    vertex_ai:
      command: echo 'vertex-ai-token-456'
      user_agent: VertexAIClient/2.1.0
    openai: echo 'openai-token-789'
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            yaml_path = Path(f.name)

        try:
            config = CCProxyConfig.from_yaml(yaml_path)

            # All tokens should be loaded
            assert config.get_oauth_token("anthropic") == "anthropic-token-123"
            assert config.get_oauth_token("vertex_ai") == "vertex-ai-token-456"
            assert config.get_oauth_token("openai") == "openai-token-789"

            # Only gemini should have user-agent
            assert config.get_oauth_user_agent("anthropic") is None
            assert config.get_oauth_user_agent("vertex_ai") == "VertexAIClient/2.1.0"
            assert config.get_oauth_user_agent("openai") is None

        finally:
            yaml_path.unlink()

    def test_extended_format_without_user_agent(self) -> None:
        """Test extended format with only command field."""
        yaml_content = """
ccproxy:
  oat_sources:
    vertex_ai:
      command: echo 'vertex-ai-token-456'
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            yaml_path = Path(f.name)

        try:
            config = CCProxyConfig.from_yaml(yaml_path)

            # Token should be loaded
            assert config.get_oauth_token("vertex_ai") == "vertex-ai-token-456"
            # No user-agent
            assert config.get_oauth_user_agent("vertex_ai") is None

        finally:
            yaml_path.unlink()

    def test_user_agent_cached_during_load(self) -> None:
        """Test that user-agent is cached when credentials are loaded."""
        yaml_content = """
ccproxy:
  oat_sources:
    provider1:
      command: echo 'token-1'
      user_agent: Provider1Client/1.0
    provider2:
      command: echo 'token-2'
      user_agent: Provider2Client/2.0
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            yaml_path = Path(f.name)

        try:
            config = CCProxyConfig.from_yaml(yaml_path)

            # Check internal _oat_user_agents cache
            assert config._oat_user_agents == {
                "provider1": "Provider1Client/1.0",
                "provider2": "Provider2Client/2.0",
            }

        finally:
            yaml_path.unlink()

    def test_get_oauth_user_agent_nonexistent_provider(self) -> None:
        """Test getting user-agent for non-configured provider."""
        config = CCProxyConfig()
        assert config.get_oauth_user_agent("nonexistent") is None


class TestOAuthUserAgentForwarding:
    """Tests for User-Agent header forwarding in forward_oauth hook."""

    @pytest.mark.asyncio
    async def test_custom_user_agent_forwarded(self) -> None:
        """Test that custom user-agent is forwarded in request."""
        # Set up mock proxy server
        mock_proxy_server = MagicMock()
        mock_proxy_server.llm_router = MagicMock()
        mock_proxy_server.llm_router.model_list = [
            {
                "model_name": "default",
                "litellm_params": {
                    "model": "gemini-2.5-pro",
                },
            },
        ]

        mock_module = MagicMock()
        mock_module.proxy_server = mock_proxy_server

        # Create config with gemini OAuth source that has custom user-agent
        yaml_content = """
ccproxy:
  oat_sources:
    vertex_ai:
      command: echo 'vertex-ai-token-123'
      user_agent: MyCustomApp/3.0.0
  default_model_passthrough: false
  hooks:
    - ccproxy.hooks.rule_evaluator
    - ccproxy.hooks.model_router
    - ccproxy.hooks.forward_oauth
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            yaml_path = Path(f.name)

        try:
            config = CCProxyConfig.from_yaml(yaml_path)
            from ccproxy.config import set_config_instance

            set_config_instance(config)

            with patch.dict("sys.modules", {"litellm.proxy": mock_module}):
                clear_router()
                handler = CCProxyHandler()

                # Test data for Gemini model
                data = {
                    "model": "gemini-2.5-pro",
                    "messages": [{"role": "user", "content": "test"}],
                    "metadata": {},
                    "provider_specific_header": {"extra_headers": {}},
                    "proxy_server_request": {"headers": {"user-agent": "original-client/1.0"}},
                    "secret_fields": {"raw_headers": {"authorization": "Bearer vertex-ai-token-123"}},
                }

                user_api_key_dict = {}
                kwargs = {}

                # Call the hook
                result = await handler.async_pre_call_hook(data, user_api_key_dict, **kwargs)

                # Verify custom User-Agent was set
                assert "provider_specific_header" in result
                assert "extra_headers" in result["provider_specific_header"]
                assert result["provider_specific_header"]["extra_headers"]["user-agent"] == "MyCustomApp/3.0.0"
                # Authorization should also be forwarded
                assert result["provider_specific_header"]["extra_headers"]["authorization"] == "Bearer vertex-ai-token-123"

        finally:
            yaml_path.unlink()
            clear_config_instance()
            clear_router()

    @pytest.mark.asyncio
    async def test_no_user_agent_when_not_configured(self) -> None:
        """Test that no user-agent is set when not configured for provider."""
        # Set up mock proxy server
        mock_proxy_server = MagicMock()
        mock_proxy_server.llm_router = MagicMock()
        mock_proxy_server.llm_router.model_list = [
            {
                "model_name": "default",
                "litellm_params": {
                    "model": "claude-sonnet-4-5-20250929",
                    "api_base": "https://api.anthropic.com",
                },
            },
        ]

        mock_module = MagicMock()
        mock_module.proxy_server = mock_proxy_server

        # Create config with anthropic OAuth source WITHOUT custom user-agent
        yaml_content = """
ccproxy:
  oat_sources:
    anthropic: echo 'anthropic-token-123'
  default_model_passthrough: false
  hooks:
    - ccproxy.hooks.rule_evaluator
    - ccproxy.hooks.model_router
    - ccproxy.hooks.forward_oauth
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            yaml_path = Path(f.name)

        try:
            config = CCProxyConfig.from_yaml(yaml_path)
            from ccproxy.config import set_config_instance

            set_config_instance(config)

            with patch.dict("sys.modules", {"litellm.proxy": mock_module}):
                clear_router()
                handler = CCProxyHandler()

                # Test data for Anthropic model
                data = {
                    "model": "claude-sonnet-4-5-20250929",
                    "messages": [{"role": "user", "content": "test"}],
                    "metadata": {},
                    "provider_specific_header": {"extra_headers": {}},
                    "proxy_server_request": {"headers": {"user-agent": "claude-cli/1.0.62"}},
                    "secret_fields": {"raw_headers": {"authorization": "Bearer anthropic-token-123"}},
                }

                user_api_key_dict = {}
                kwargs = {}

                # Call the hook
                result = await handler.async_pre_call_hook(data, user_api_key_dict, **kwargs)

                # Verify custom User-Agent was NOT set (because not configured)
                assert "provider_specific_header" in result
                assert "extra_headers" in result["provider_specific_header"]
                # user-agent should not be in extra_headers
                assert "user-agent" not in result["provider_specific_header"]["extra_headers"]
                # Authorization should still be forwarded
                assert result["provider_specific_header"]["extra_headers"]["authorization"] == "Bearer anthropic-token-123"

        finally:
            yaml_path.unlink()
            clear_config_instance()
            clear_router()

    @pytest.mark.asyncio
    async def test_user_agent_overrides_original(self) -> None:
        """Test that configured user-agent overrides the original client user-agent."""
        # Set up mock proxy server
        mock_proxy_server = MagicMock()
        mock_proxy_server.llm_router = MagicMock()
        mock_proxy_server.llm_router.model_list = [
            {
                "model_name": "default",
                "litellm_params": {
                    "model": "gemini-2.5-pro",
                },
            },
        ]

        mock_module = MagicMock()
        mock_module.proxy_server = mock_proxy_server

        # Create config with gemini OAuth source with custom user-agent
        yaml_content = """
ccproxy:
  oat_sources:
    vertex_ai:
      command: echo 'vertex-ai-token-123'
      user_agent: ProxyOverride/1.0
  default_model_passthrough: false
  hooks:
    - ccproxy.hooks.rule_evaluator
    - ccproxy.hooks.model_router
    - ccproxy.hooks.forward_oauth
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            yaml_path = Path(f.name)

        try:
            config = CCProxyConfig.from_yaml(yaml_path)
            from ccproxy.config import set_config_instance

            set_config_instance(config)

            with patch.dict("sys.modules", {"litellm.proxy": mock_module}):
                clear_router()
                handler = CCProxyHandler()

                # Test data with original user-agent that should be overridden
                data = {
                    "model": "gemini-2.5-pro",
                    "messages": [{"role": "user", "content": "test"}],
                    "metadata": {},
                    "provider_specific_header": {"extra_headers": {}},
                    "proxy_server_request": {"headers": {"user-agent": "OriginalClient/9.9.9"}},
                    "secret_fields": {"raw_headers": {"authorization": "Bearer vertex-ai-token-123"}},
                }

                user_api_key_dict = {}
                kwargs = {}

                # Call the hook
                result = await handler.async_pre_call_hook(data, user_api_key_dict, **kwargs)

                # Verify custom User-Agent overrode the original
                assert result["provider_specific_header"]["extra_headers"]["user-agent"] == "ProxyOverride/1.0"
                # Not the original
                assert result["provider_specific_header"]["extra_headers"]["user-agent"] != "OriginalClient/9.9.9"

        finally:
            yaml_path.unlink()
            clear_config_instance()
            clear_router()

    @pytest.mark.asyncio
    async def test_multiple_providers_with_different_user_agents(self) -> None:
        """Test that different providers can have different user-agents."""
        # Set up mock proxy server with multiple providers
        mock_proxy_server = MagicMock()
        mock_proxy_server.llm_router = MagicMock()
        mock_proxy_server.llm_router.model_list = [
            {
                "model_name": "default",
                "litellm_params": {
                    "model": "claude-sonnet-4-5-20250929",
                    "api_base": "https://api.anthropic.com",
                },
            },
            {
                "model_name": "vertex_model",
                "litellm_params": {
                    "model": "gemini-2.5-pro",
                },
            },
        ]

        mock_module = MagicMock()
        mock_module.proxy_server = mock_proxy_server

        # Create config with multiple providers with different user-agents
        # Use passthrough mode so the requested model is used directly
        yaml_content = """
ccproxy:
  oat_sources:
    anthropic:
      command: echo 'anthropic-token-123'
      user_agent: AnthropicClient/1.0
    vertex_ai:
      command: echo 'vertex-ai-token-456'
      user_agent: VertexAIClient/2.0
  default_model_passthrough: true
  hooks:
    - ccproxy.hooks.rule_evaluator
    - ccproxy.hooks.model_router
    - ccproxy.hooks.forward_oauth
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            yaml_path = Path(f.name)

        try:
            config = CCProxyConfig.from_yaml(yaml_path)
            from ccproxy.config import set_config_instance

            set_config_instance(config)

            with patch.dict("sys.modules", {"litellm.proxy": mock_module}):
                clear_router()
                handler = CCProxyHandler()

                # Test Anthropic request
                anthropic_data = {
                    "model": "claude-sonnet-4-5-20250929",
                    "messages": [{"role": "user", "content": "test"}],
                    "metadata": {},
                    "provider_specific_header": {"extra_headers": {}},
                    "proxy_server_request": {"headers": {"user-agent": "original/1.0"}},
                    "secret_fields": {"raw_headers": {"authorization": "Bearer anthropic-token-123"}},
                }

                result = await handler.async_pre_call_hook(anthropic_data, {})
                assert result["provider_specific_header"]["extra_headers"]["user-agent"] == "AnthropicClient/1.0"

                # Test Gemini request
                gemini_data = {
                    "model": "gemini-2.5-pro",
                    "messages": [{"role": "user", "content": "test"}],
                    "metadata": {},
                    "provider_specific_header": {"extra_headers": {}},
                    "proxy_server_request": {"headers": {"user-agent": "original/1.0"}},
                    "secret_fields": {"raw_headers": {"authorization": "Bearer vertex-ai-token-456"}},
                }

                result = await handler.async_pre_call_hook(gemini_data, {})
                assert result["provider_specific_header"]["extra_headers"]["user-agent"] == "VertexAIClient/2.0"

        finally:
            yaml_path.unlink()
            clear_config_instance()
            clear_router()
