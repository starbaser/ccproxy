"""Test OAuth token forwarding for Claude CLI requests."""

from unittest.mock import MagicMock, patch

import pytest

from ccproxy.config import clear_config_instance
from ccproxy.handler import CCProxyHandler
from ccproxy.router import clear_router


@pytest.fixture
def mock_handler():
    """Create a ccproxy handler with mocked router that provides a default model."""
    # Mock proxy server with default model
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
            "model_name": "background",
            "litellm_params": {
                "model": "claude-haiku-4-5-20251001-20241022",
                "api_base": "https://api.anthropic.com",
            },
        },
    ]

    mock_module = MagicMock()
    mock_module.proxy_server = mock_proxy_server

    # Set up config with hooks
    from ccproxy.config import CCProxyConfig, set_config_instance

    config = CCProxyConfig(
        debug=False,
        default_model_passthrough=False,  # Disable passthrough to test actual routing
        hooks=["ccproxy.hooks.rule_evaluator", "ccproxy.hooks.model_router", "ccproxy.hooks.forward_oauth"],
        rules=[],
    )
    set_config_instance(config)

    # Patch the proxy server import
    with patch.dict("sys.modules", {"litellm.proxy": mock_module}):
        clear_router()  # Clear any existing router
        handler = CCProxyHandler()  # Create actual handler instance
        yield handler

    # Cleanup
    clear_config_instance()
    clear_router()


@pytest.mark.asyncio
async def test_oauth_forwarding_for_claude_cli(mock_handler):
    """Test that OAuth tokens are forwarded for claude-cli requests."""
    handler = mock_handler

    # Test data for Anthropic model with required structure
    data = {
        "model": "anthropic/claude-haiku-4-5-20251001-20241022",
        "messages": [{"role": "user", "content": "test"}],
        "metadata": {},
        "provider_specific_header": {"extra_headers": {}},
        "proxy_server_request": {"headers": {"user-agent": "claude-cli/1.0.62 (external, cli)"}},
        "secret_fields": {"raw_headers": {"authorization": "Bearer sk-ant-oat01-test-token-123"}},
    }

    user_api_key_dict = {}
    kwargs = {}

    # Call the hook
    result = await handler.async_pre_call_hook(data, user_api_key_dict, **kwargs)

    # Verify OAuth token was forwarded in authorization header
    assert "provider_specific_header" in result
    assert "extra_headers" in result["provider_specific_header"]
    assert result["provider_specific_header"]["extra_headers"]["authorization"] == "Bearer sk-ant-oat01-test-token-123"


@pytest.mark.asyncio
async def test_oauth_forwarding_handles_missing_headers(mock_handler):
    """Test that OAuth forwarding handles missing headers gracefully."""
    handler = mock_handler

    # Test data with missing secret_fields
    data = {
        "model": "anthropic/claude-haiku-4-5-20251001-20241022",
        "messages": [{"role": "user", "content": "test"}],
        "metadata": {},
        "provider_specific_header": {"extra_headers": {}},
        "proxy_server_request": {"headers": {"user-agent": "claude-cli/1.0.62 (external, cli)"}},
        # secret_fields is missing
    }

    user_api_key_dict = {}
    kwargs = {}

    # Call the hook - should not crash
    result = await handler.async_pre_call_hook(data, user_api_key_dict, **kwargs)

    # Verify no OAuth token was added
    assert "authorization" not in result["provider_specific_header"]["extra_headers"]


@pytest.mark.asyncio
async def test_oauth_forwarding_preserves_existing_extra_headers(mock_handler):
    """Test that OAuth forwarding preserves existing extra_headers."""
    handler = mock_handler

    # Test data with existing extra_headers
    data = {
        "model": "anthropic/claude-haiku-4-5-20251001-20241022",
        "messages": [{"role": "user", "content": "test"}],
        "metadata": {},
        "provider_specific_header": {"extra_headers": {"existing-header": "existing-value"}},
        "proxy_server_request": {"headers": {"user-agent": "claude-cli/1.0.62 (external, cli)"}},
        "secret_fields": {"raw_headers": {"authorization": "Bearer sk-ant-oat01-test-token-123"}},
    }

    user_api_key_dict = {}
    kwargs = {}

    # Call the hook
    result = await handler.async_pre_call_hook(data, user_api_key_dict, **kwargs)

    # Verify both headers are present
    assert "provider_specific_header" in result
    assert "extra_headers" in result["provider_specific_header"]
    assert result["provider_specific_header"]["extra_headers"]["authorization"] == "Bearer sk-ant-oat01-test-token-123"
    assert result["provider_specific_header"]["extra_headers"]["existing-header"] == "existing-value"


@pytest.mark.asyncio
async def test_oauth_forwarding_with_claude_prefix_model(mock_handler):
    """Test that OAuth tokens are forwarded for models starting with 'claude'."""
    handler = mock_handler

    # Test data for model starting with 'claude'
    data = {
        "model": "claude-sonnet-4-5-20250929",
        "messages": [{"role": "user", "content": "test"}],
        "metadata": {},
        "provider_specific_header": {"extra_headers": {}},
        "proxy_server_request": {"headers": {"user-agent": "claude-cli/1.0.62 (external, cli)"}},
        "secret_fields": {"raw_headers": {"authorization": "Bearer sk-ant-oat01-test-token-123"}},
    }

    user_api_key_dict = {}
    kwargs = {}

    # Call the hook
    result = await handler.async_pre_call_hook(data, user_api_key_dict, **kwargs)

    # Verify OAuth token was forwarded
    assert result["provider_specific_header"]["extra_headers"]["authorization"] == "Bearer sk-ant-oat01-test-token-123"


@pytest.mark.asyncio
async def test_oauth_forwarding_with_routed_model(mock_handler):
    """Test that OAuth forwarding works based on the routed model destination."""
    handler = mock_handler

    # Test data that will be routed to an Anthropic model
    data = {
        "model": "default",  # This will be routed to an anthropic model
        "messages": [{"role": "user", "content": "test"}],
        "metadata": {},
        "provider_specific_header": {"extra_headers": {}},
        "proxy_server_request": {"headers": {"user-agent": "claude-cli/1.0.62 (external, cli)"}},
        "secret_fields": {"raw_headers": {"authorization": "Bearer sk-ant-oat01-test-token-123"}},
    }

    user_api_key_dict = {}
    kwargs = {}

    # Call the hook
    result = await handler.async_pre_call_hook(data, user_api_key_dict, **kwargs)

    # OAuth forwarding should be based on the routed model destination
    # Since the routed model is an Anthropic model, OAuth SHOULD be forwarded
    # regardless of what the original model was
    assert result["provider_specific_header"]["extra_headers"]["authorization"] == "Bearer sk-ant-oat01-test-token-123"

    # Verify the model was routed correctly
    assert result["model"] == "claude-sonnet-4-5-20250929"


@pytest.mark.asyncio
async def test_oauth_forwarding_for_anthropic_direct_api():
    """Test that OAuth tokens ARE forwarded for models going to Anthropic's API directly."""
    # Create a handler with Anthropic model going to Anthropic's API
    mock_proxy_server = MagicMock()
    mock_proxy_server.llm_router = MagicMock()
    mock_proxy_server.llm_router.model_list = [
        {
            "model_name": "default",
            "litellm_params": {
                "model": "anthropic/claude-sonnet-4-5-20250929",
                "api_base": "https://api.anthropic.com",
            },
        },
    ]

    mock_module = MagicMock()
    mock_module.proxy_server = mock_proxy_server

    # Set up config with hooks
    from ccproxy.config import CCProxyConfig, set_config_instance

    config = CCProxyConfig(
        debug=False,
        default_model_passthrough=False,  # Disable passthrough to test actual routing
        hooks=["ccproxy.hooks.rule_evaluator", "ccproxy.hooks.model_router", "ccproxy.hooks.forward_oauth"],
        rules=[],
    )
    set_config_instance(config)

    with patch.dict("sys.modules", {"litellm.proxy": mock_module}):
        clear_router()
        handler = CCProxyHandler()

        # Test data from claude-cli
        data = {
            "model": "default",
            "messages": [{"role": "user", "content": "test"}],
            "metadata": {},
            "provider_specific_header": {"extra_headers": {}},
            "proxy_server_request": {"headers": {"user-agent": "claude-cli/1.0.62 (external, cli)"}},
            "secret_fields": {"raw_headers": {"authorization": "Bearer sk-ant-oat01-test-token-123"}},
        }

        user_api_key_dict = {}
        kwargs = {}

        # Call the hook
        result = await handler.async_pre_call_hook(data, user_api_key_dict, **kwargs)

        # OAuth SHOULD be forwarded since it's going to Anthropic directly
        assert (
            result["provider_specific_header"]["extra_headers"]["authorization"] == "Bearer sk-ant-oat01-test-token-123"
        )

        # Verify the model was routed correctly
        assert result["model"] == "anthropic/claude-sonnet-4-5-20250929"

    clear_config_instance()
    clear_router()
