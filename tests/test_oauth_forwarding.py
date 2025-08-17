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
                "model": "claude-sonnet-4-20250514",
                "api_base": "https://api.anthropic.com",
            },
        },
        {
            "model_name": "background",
            "litellm_params": {
                "model": "claude-3-5-haiku-20241022",
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
        hooks=[
            "ccproxy.hooks.rule_evaluator",
            "ccproxy.hooks.model_router",
            "ccproxy.hooks.forward_oauth"
        ],
        rules=[]
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
        "model": "anthropic/claude-3-5-haiku-20241022",
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
async def test_no_oauth_forwarding_for_non_claude_cli(mock_handler):
    """Test that OAuth tokens are NOT forwarded for non-claude-cli requests."""
    handler = mock_handler

    # Test data with different user agent
    data = {
        "model": "anthropic/claude-3-5-haiku-20241022",
        "messages": [{"role": "user", "content": "test"}],
        "metadata": {},
        "provider_specific_header": {"extra_headers": {}},
        "proxy_server_request": {"headers": {"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}},
        "secret_fields": {"raw_headers": {"authorization": "Bearer sk-ant-oat01-test-token-123"}},
    }

    user_api_key_dict = {}
    kwargs = {}

    # Call the hook
    result = await handler.async_pre_call_hook(data, user_api_key_dict, **kwargs)

    # Verify OAuth token was NOT forwarded
    assert "authorization" not in result["provider_specific_header"]["extra_headers"]


@pytest.mark.asyncio
async def test_no_oauth_forwarding_for_non_anthropic_models(mock_handler):
    """Test that OAuth tokens are NOT forwarded when model doesn't route to Anthropic."""
    # Create a handler with proper routing config that includes gemini
    mock_proxy_server = MagicMock()
    mock_proxy_server.llm_router = MagicMock()
    mock_proxy_server.llm_router.model_list = [
        {
            "model_name": "default",
            "litellm_params": {"model": "claude-sonnet-4-20250514"},
        },
        {
            "model_name": "token_count",
            "litellm_params": {"model": "gemini-2.5-pro"},
        },
    ]

    mock_module = MagicMock()
    mock_module.proxy_server = mock_proxy_server

    # Create config with token count rule
    from ccproxy.config import CCProxyConfig, RuleConfig, set_config_instance

    config = CCProxyConfig(
        debug=False,
        hooks=[
            "ccproxy.hooks.rule_evaluator",
            "ccproxy.hooks.model_router",
            "ccproxy.hooks.forward_oauth"
        ],
        rules=[
            RuleConfig(
                name="token_count",
                rule_path="ccproxy.rules.TokenCountRule",
                params=[{"threshold": 100}],  # Low threshold to trigger
            ),
        ],
    )
    set_config_instance(config)

    with patch.dict("sys.modules", {"litellm.proxy": mock_module}):
        clear_router()
        handler = CCProxyHandler()

        # Test data with high token count to trigger routing to gemini
        # Use varied text to get proper token count above 100 threshold
        base_text = "The quick brown fox jumps over the lazy dog. " * 5  # ~51 tokens
        long_message = base_text * 3  # ~153 tokens (above 100 threshold)
        data = {
            "model": "claude-sonnet-4-20250514",
            "messages": [{"role": "user", "content": long_message}],  # >100 tokens
            "metadata": {},
            "provider_specific_header": {"extra_headers": {}},
            "proxy_server_request": {"headers": {"user-agent": "claude-cli/1.0.62 (external, cli)"}},
            "secret_fields": {"raw_headers": {"authorization": "Bearer sk-ant-oat01-test-token-123"}},
        }

        user_api_key_dict = {}
        kwargs = {}

        # Call the hook
        result = await handler.async_pre_call_hook(data, user_api_key_dict, **kwargs)

        # Verify OAuth token was NOT forwarded because we routed to gemini
        assert "authorization" not in result["provider_specific_header"]["extra_headers"]
        assert result["model"] == "gemini-2.5-pro"

    clear_config_instance()
    clear_router()


@pytest.mark.asyncio
async def test_oauth_forwarding_handles_missing_headers(mock_handler):
    """Test that OAuth forwarding handles missing headers gracefully."""
    handler = mock_handler

    # Test data with missing secret_fields
    data = {
        "model": "anthropic/claude-3-5-haiku-20241022",
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
        "model": "anthropic/claude-3-5-haiku-20241022",
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
        "model": "claude-sonnet-4-20250514",
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
    assert result["model"] == "claude-sonnet-4-20250514"


@pytest.mark.asyncio
async def test_no_oauth_forwarding_when_routed_to_non_anthropic(mock_handler):
    """Test that OAuth tokens are NOT forwarded when routing to non-Anthropic models."""
    # Create a handler with a mock router that routes to a non-Anthropic model
    mock_proxy_server = MagicMock()
    mock_proxy_server.llm_router = MagicMock()
    mock_proxy_server.llm_router.model_list = [
        {
            "model_name": "default",
            "litellm_params": {
                "model": "gemini-2.5-pro",
                "api_base": "https://generativelanguage.googleapis.com",
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
        hooks=[
            "ccproxy.hooks.rule_evaluator",
            "ccproxy.hooks.model_router",
            "ccproxy.hooks.forward_oauth"
        ],
        rules=[]
    )
    set_config_instance(config)

    with patch.dict("sys.modules", {"litellm.proxy": mock_module}):
        clear_router()
        handler = CCProxyHandler()

        # Test data from claude-cli that will be routed to a non-Anthropic model
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

        # OAuth should NOT be forwarded since we're routing to a non-Anthropic model
        assert "authorization" not in result["provider_specific_header"]["extra_headers"]

        # Verify the model was routed correctly
        assert result["model"] == "gemini-2.5-pro"


@pytest.mark.asyncio
async def test_no_oauth_forwarding_for_anthropic_model_on_vertex():
    """Test that OAuth tokens are NOT forwarded for Anthropic models served through Vertex AI."""
    # Create a handler with Anthropic model served through Vertex
    mock_proxy_server = MagicMock()
    mock_proxy_server.llm_router = MagicMock()
    mock_proxy_server.llm_router.model_list = [
        {
            "model_name": "default",
            "litellm_params": {
                "model": "vertex/claude-3-5-sonnet",
                "api_base": "https://us-central1-aiplatform.googleapis.com",
                "custom_llm_provider": "vertex",
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
        hooks=[
            "ccproxy.hooks.rule_evaluator",
            "ccproxy.hooks.model_router",
            "ccproxy.hooks.forward_oauth"
        ],
        rules=[]
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

        # OAuth should NOT be forwarded since it's Vertex, not direct Anthropic
        assert "authorization" not in result["provider_specific_header"]["extra_headers"]

        # Verify the model was routed correctly
        assert result["model"] == "vertex/claude-3-5-sonnet"

    clear_config_instance()
    clear_router()


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
                "model": "anthropic/claude-sonnet-4-20250514",
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
        hooks=[
            "ccproxy.hooks.rule_evaluator",
            "ccproxy.hooks.model_router",
            "ccproxy.hooks.forward_oauth"
        ],
        rules=[]
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
        assert result["model"] == "anthropic/claude-sonnet-4-20250514"

    clear_config_instance()
    clear_router()
