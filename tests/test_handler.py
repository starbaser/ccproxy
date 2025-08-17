"""Tests for ccproxy handler and routing function."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest
import yaml

from ccproxy.config import CCProxyConfig, RuleConfig, clear_config_instance, set_config_instance
from ccproxy.handler import CCProxyHandler
from ccproxy.router import ModelRouter, clear_router


class TestCCProxyRouting:
    """Tests for ccproxy handler routing logic."""

    def _create_router_with_models(self, model_list: list) -> ModelRouter:
        """Helper to create a router with mocked models."""
        mock_config = MagicMock(spec=CCProxyConfig)

        mock_proxy_server = MagicMock()
        mock_proxy_server.llm_router = MagicMock()
        mock_proxy_server.llm_router.model_list = model_list

        mock_module = MagicMock()
        mock_module.proxy_server = mock_proxy_server

        with (
            patch("ccproxy.router.get_config", return_value=mock_config),
            patch.dict("sys.modules", {"litellm.proxy": mock_module}),
        ):
            return ModelRouter()

    @pytest.fixture
    def config_files(self):
        """Create temporary ccproxy.yaml and litellm config files."""
        # Create litellm config
        litellm_data = {
            "model_list": [
                {
                    "model_name": "default",
                    "litellm_params": {
                        "model": "claude-3-5-sonnet-20241022",
                    },
                },
                {
                    "model_name": "background",
                    "litellm_params": {
                        "model": "claude-3-5-haiku-20241022",
                    },
                },
                {
                    "model_name": "think",
                    "litellm_params": {
                        "model": "claude-3-5-opus-20250514",
                    },
                },
                {
                    "model_name": "token_count",
                    "litellm_params": {
                        "model": "gemini-2.5-pro",
                    },
                },
                {
                    "model_name": "web_search",
                    "litellm_params": {
                        "model": "perplexity/llama-3.1-sonar-large-128k-online",
                    },
                },
            ],
        }

        # Create ccproxy config
        ccproxy_data = {
            "ccproxy": {
                "debug": False,
                "hooks": [
                    "ccproxy.hooks.rule_evaluator",
                    "ccproxy.hooks.model_router",
                    "ccproxy.hooks.forward_oauth",
                ],
                "rules": [
                    {
                        "name": "token_count",
                        "rule": "ccproxy.rules.TokenCountRule",
                        "params": [{"threshold": 60000}],
                    },
                    {
                        "name": "background",
                        "rule": "ccproxy.rules.MatchModelRule",
                        "params": [{"model_name": "claude-3-5-haiku-20241022"}],
                    },
                    {
                        "name": "think",
                        "rule": "ccproxy.rules.ThinkingRule",
                        "params": [],
                    },
                    {
                        "name": "web_search",
                        "rule": "ccproxy.rules.MatchToolRule",
                        "params": [{"tool_name": "web_search"}],
                    },
                ],
            }
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as litellm_file:
            yaml.dump(litellm_data, litellm_file)
            litellm_path = Path(litellm_file.name)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as ccproxy_file:
            yaml.dump(ccproxy_data, ccproxy_file)
            ccproxy_path = Path(ccproxy_file.name)

        yield ccproxy_path, litellm_path

        # Cleanup
        litellm_path.unlink()
        ccproxy_path.unlink()

    async def test_route_to_default(self, config_files):
        """Test routing simple request to default model."""
        ccproxy_path, litellm_path = config_files

        # Set up config
        config = CCProxyConfig.from_yaml(ccproxy_path, litellm_config_path=litellm_path)
        set_config_instance(config)

        # Create model list for mocking
        test_model_list = [
            {
                "model_name": "default",
                "litellm_params": {"model": "claude-3-5-sonnet-20241022"},
            },
            {
                "model_name": "background",
                "litellm_params": {"model": "claude-3-5-haiku-20241022"},
            },
            {
                "model_name": "think",
                "litellm_params": {"model": "claude-3-5-opus-20250514"},
            },
            {
                "model_name": "token_count",
                "litellm_params": {"model": "gemini-2.5-pro"},
            },
            {
                "model_name": "web_search",
                "litellm_params": {"model": "perplexity/llama-3.1-sonar-large-128k-online"},
            },
        ]

        mock_proxy_server = MagicMock()
        mock_proxy_server.llm_router = MagicMock()
        mock_proxy_server.llm_router.model_list = test_model_list

        mock_module = MagicMock()
        mock_module.proxy_server = mock_proxy_server

        try:
            with patch.dict("sys.modules", {"litellm.proxy": mock_module}):
                handler = CCProxyHandler()
                request_data = {
                    "model": "claude-3-5-sonnet-20241022",
                    "messages": [{"role": "user", "content": "Hello"}],
                }
                user_api_key_dict = {}

                result = await handler.async_pre_call_hook(request_data, user_api_key_dict)
                assert result["model"] == "claude-3-5-sonnet-20241022"
        finally:
            clear_config_instance()
            clear_router()

    async def test_route_to_background(self, config_files):
        """Test routing haiku model to background."""
        ccproxy_path, litellm_path = config_files

        config = CCProxyConfig.from_yaml(ccproxy_path, litellm_config_path=litellm_path)
        set_config_instance(config)

        # Create model list for mocking
        test_model_list = [
            {
                "model_name": "default",
                "litellm_params": {"model": "claude-3-5-sonnet-20241022"},
            },
            {
                "model_name": "background",
                "litellm_params": {"model": "claude-3-5-haiku-20241022"},
            },
            {
                "model_name": "think",
                "litellm_params": {"model": "claude-3-5-opus-20250514"},
            },
            {
                "model_name": "token_count",
                "litellm_params": {"model": "gemini-2.5-pro"},
            },
            {
                "model_name": "web_search",
                "litellm_params": {"model": "perplexity/llama-3.1-sonar-large-128k-online"},
            },
        ]

        mock_proxy_server = MagicMock()
        mock_proxy_server.llm_router = MagicMock()
        mock_proxy_server.llm_router.model_list = test_model_list

        mock_module = MagicMock()
        mock_module.proxy_server = mock_proxy_server

        try:
            with patch.dict("sys.modules", {"litellm.proxy": mock_module}):
                handler = CCProxyHandler()
                request_data = {
                    "model": "claude-3-5-haiku-20241022",
                    "messages": [{"role": "user", "content": "Format this code"}],
                }
                user_api_key_dict = {}

                result = await handler.async_pre_call_hook(request_data, user_api_key_dict)
                assert result["model"] == "claude-3-5-haiku-20241022"
        finally:
            clear_config_instance()
            clear_router()


class TestHandlerHookMethods:
    """Test suite for individual hook methods that haven't been covered."""

    @pytest.fixture
    def config_files(self):
        """Create temporary ccproxy.yaml and litellm config files."""
        # Create litellm config
        litellm_data = {
            "model_list": [
                {
                    "model_name": "default",
                    "litellm_params": {
                        "model": "claude-3-5-sonnet-20241022",
                    },
                },
                {
                    "model_name": "background",
                    "litellm_params": {
                        "model": "claude-3-5-haiku-20241022",
                    },
                },
            ],
        }

        # Create ccproxy config
        ccproxy_data = {
            "ccproxy": {
                "debug": False,
                "hooks": [
                    "ccproxy.hooks.rule_evaluator",
                    "ccproxy.hooks.model_router",
                    "ccproxy.hooks.forward_oauth",
                ],
                "rules": [
                    {
                        "name": "background",
                        "rule": "ccproxy.rules.MatchModelRule",
                        "params": [{"model_name": "claude-3-5-haiku-20241022"}],
                    },
                ],
            }
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as litellm_file:
            yaml.dump(litellm_data, litellm_file)
            litellm_path = Path(litellm_file.name)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as ccproxy_file:
            yaml.dump(ccproxy_data, ccproxy_file)
            ccproxy_path = Path(ccproxy_file.name)

        yield ccproxy_path, litellm_path

        # Cleanup
        litellm_path.unlink()
        ccproxy_path.unlink()

    @pytest.fixture
    def handler(self) -> CCProxyHandler:
        """Create a ccproxy handler instance with mocked router."""
        # Create a minimal config with hooks
        config = CCProxyConfig(
            debug=False,
            hooks=[
                "ccproxy.hooks.rule_evaluator",
                "ccproxy.hooks.model_router",
            ],
            rules=[],
        )
        set_config_instance(config)

        # Mock proxy server with default model
        mock_proxy_server = MagicMock()
        mock_proxy_server.llm_router = MagicMock()
        mock_proxy_server.llm_router.model_list = [
            {
                "model_name": "default",
                "litellm_params": {"model": "claude-3-5-sonnet-20241022"},
            },
        ]

        mock_module = MagicMock()
        mock_module.proxy_server = mock_proxy_server

        try:
            with patch.dict("sys.modules", {"litellm.proxy": mock_module}):
                clear_router()  # Clear any existing router
                handler = CCProxyHandler()
                yield handler
        finally:
            clear_config_instance()
            clear_router()

    @pytest.mark.asyncio
    async def test_log_success_hook(self, handler: CCProxyHandler) -> None:
        """Test async_log_success_event method."""
        kwargs = {
            "litellm_params": {},
            "start_time": 1234567890,
            "end_time": 1234567900,
            "cache_hit": False,
        }
        response_obj = Mock(model="test-model", usage=Mock(completion_tokens=10, prompt_tokens=20, total_tokens=30))

        # Should not raise any exceptions
        await handler.async_log_success_event(kwargs, response_obj, 1234567890, 1234567900)

    @pytest.mark.asyncio
    async def test_log_failure_hook(self, handler: CCProxyHandler) -> None:
        """Test async_log_failure_event method."""
        kwargs = {
            "litellm_params": {},
            "start_time": 1234567890,
            "end_time": 1234567900,
        }
        response_obj = Mock()

        # Should not raise any exceptions
        await handler.async_log_failure_event(kwargs, response_obj, 1234567890, 1234567900)

    @pytest.mark.asyncio
    async def test_logging_hook_with_completion(self, handler: CCProxyHandler) -> None:
        """Test async_pre_call_hook with completion call type."""
        # Create mock data
        data = {
            "model": "claude-3-5-sonnet-20241022",
            "messages": [{"role": "user", "content": "Hello"}],
        }
        user_api_key_dict = {}

        # Should return without error
        result = await handler.async_pre_call_hook(
            data,
            user_api_key_dict,
        )

        # Should return the modified data
        assert isinstance(result, dict)
        assert "model" in result
        assert "metadata" in result

    @pytest.mark.asyncio
    async def test_logging_hook_with_unsupported_call_type(self, handler: CCProxyHandler) -> None:
        """Test async_pre_call_hook with various request data."""
        # Create mock data with a different model
        data = {
            "model": "gpt-4",  # Not in our config, should use default
            "messages": [{"role": "user", "content": "Test"}],
        }
        user_api_key_dict = {}

        # Should return without error
        result = await handler.async_pre_call_hook(
            data,
            user_api_key_dict,
        )

        # Should return the modified data - gpt-4 is not in our config so it routes to default
        assert isinstance(result, dict)
        assert result["model"] == "claude-3-5-sonnet-20241022"  # Should route to default
        # Metadata should be added
        assert "metadata" in result
        assert result["metadata"]["ccproxy_model_name"] == "default"
        assert result["metadata"]["ccproxy_alias_model"] == "gpt-4"

    @pytest.mark.asyncio
    async def test_log_stream_event(self, handler: CCProxyHandler) -> None:
        """Test log_stream_event method."""
        kwargs = {"litellm_params": {}}
        response_obj = Mock()
        start_time = 1234567890
        end_time = 1234567900

        # Should not raise any exceptions
        handler.log_stream_event(kwargs, response_obj, start_time, end_time)

    @pytest.mark.asyncio
    async def test_async_log_stream_event(self, handler: CCProxyHandler) -> None:
        """Test async_log_stream_event method."""
        kwargs = {"litellm_params": {}}
        response_obj = Mock()
        start_time = 1234567890
        end_time = 1234567900

        # Should not raise any exceptions
        await handler.async_log_stream_event(kwargs, response_obj, start_time, end_time)


class TestCCProxyHandler:
    """Tests for ccproxy handler class."""

    @pytest.fixture
    def handler(self, config_files):
        """Create handler with test config."""
        ccproxy_path, litellm_path = config_files

        config = CCProxyConfig.from_yaml(ccproxy_path, litellm_config_path=litellm_path)
        set_config_instance(config)

        # Create model list for mocking
        test_model_list = [
            {
                "model_name": "default",
                "litellm_params": {"model": "claude-3-5-sonnet-20241022"},
            },
            {
                "model_name": "background",
                "litellm_params": {"model": "claude-3-5-haiku-20241022"},
            },
        ]

        mock_proxy_server = MagicMock()
        mock_proxy_server.llm_router = MagicMock()
        mock_proxy_server.llm_router.model_list = test_model_list

        mock_module = MagicMock()
        mock_module.proxy_server = mock_proxy_server

        # We need to patch the proxy_server import for the handler's initialization
        # This will ensure the router gets the mocked model list
        import sys

        original_module = sys.modules.get("litellm.proxy")
        sys.modules["litellm.proxy"] = mock_module

        try:
            handler = CCProxyHandler()
            yield handler
        finally:
            if original_module is None:
                sys.modules.pop("litellm.proxy", None)
            else:
                sys.modules["litellm.proxy"] = original_module
            clear_config_instance()
            clear_router()

    @pytest.fixture
    def config_files(self):
        """Create temporary ccproxy.yaml and litellm config files."""
        # Create litellm config
        litellm_data = {
            "model_list": [
                {
                    "model_name": "default",
                    "litellm_params": {
                        "model": "claude-3-5-sonnet-20241022",
                    },
                },
                {
                    "model_name": "background",
                    "litellm_params": {
                        "model": "claude-3-5-haiku-20241022",
                    },
                },
            ],
        }

        # Create ccproxy config
        ccproxy_data = {
            "ccproxy": {
                "debug": False,
                "hooks": [
                    "ccproxy.hooks.rule_evaluator",
                    "ccproxy.hooks.model_router",
                    "ccproxy.hooks.forward_oauth",
                ],
                "rules": [
                    {
                        "name": "background",
                        "rule": "ccproxy.rules.MatchModelRule",
                        "params": [{"model_name": "claude-3-5-haiku-20241022"}],
                    },
                ],
            }
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as litellm_file:
            yaml.dump(litellm_data, litellm_file)
            litellm_path = Path(litellm_file.name)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as ccproxy_file:
            yaml.dump(ccproxy_data, ccproxy_file)
            ccproxy_path = Path(ccproxy_file.name)

        yield ccproxy_path, litellm_path

        # Cleanup
        litellm_path.unlink()
        ccproxy_path.unlink()

    async def test_async_pre_call_hook(self, handler):
        """Test async_pre_call_hook modifies request correctly."""
        request_data = {
            "model": "claude-3-5-haiku-20241022",
            "messages": [{"role": "user", "content": "Hello"}],
        }
        user_api_key_dict = {}

        # Call the hook
        modified_data = await handler.async_pre_call_hook(
            request_data,
            user_api_key_dict,
        )

        # Check model was routed
        assert modified_data["model"] == "claude-3-5-haiku-20241022"

        # Check metadata was added
        assert "metadata" in modified_data
        assert modified_data["metadata"]["ccproxy_model_name"] == "background"
        assert modified_data["metadata"]["ccproxy_alias_model"] == "claude-3-5-haiku-20241022"

    async def test_async_pre_call_hook_preserves_existing_metadata(self, handler):
        """Test that existing metadata is preserved."""
        request_data = {
            "model": "claude-3-5-sonnet-20241022",
            "messages": [{"role": "user", "content": "Hello"}],
            "metadata": {
                "existing_key": "existing_value",
            },
        }
        user_api_key_dict = {}

        # Call the hook
        modified_data = await handler.async_pre_call_hook(
            request_data,
            user_api_key_dict,
        )

        # Check existing metadata preserved
        assert modified_data["metadata"]["existing_key"] == "existing_value"

        # Check new metadata added
        assert modified_data["metadata"]["ccproxy_model_name"] == "default"
        assert modified_data["metadata"]["ccproxy_alias_model"] == "claude-3-5-sonnet-20241022"

    async def test_handler_uses_config_threshold(self):
        """Test that handler uses context threshold from config."""
        # Create config with custom threshold
        ccproxy_data = {
            "ccproxy": {
                "debug": False,
                "hooks": [
                    "ccproxy.hooks.rule_evaluator",
                    "ccproxy.hooks.model_router",
                ],
                "rules": [
                    {
                        "name": "token_count",
                        "rule": "ccproxy.rules.TokenCountRule",
                        "params": [{"threshold": 10000}],  # Lower threshold
                    },
                ],
            }
        }

        # Create a dummy litellm config file (required by CCProxyConfig)
        litellm_data = {"model_list": []}

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as litellm_file:
            yaml.dump(litellm_data, litellm_file)
            litellm_path = Path(litellm_file.name)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as ccproxy_file:
            yaml.dump(ccproxy_data, ccproxy_file)
            ccproxy_path = Path(ccproxy_file.name)

        try:
            config = CCProxyConfig.from_yaml(ccproxy_path, litellm_config_path=litellm_path)
            set_config_instance(config)

            # Create model list for mocking
            test_model_list = [
                {
                    "model_name": "default",
                    "litellm_params": {
                        "model": "claude-3-5-sonnet-20241022",
                    },
                },
                {
                    "model_name": "token_count",
                    "litellm_params": {
                        "model": "gemini-2.5-pro",
                    },
                },
            ]

            mock_proxy_server = MagicMock()
            mock_proxy_server.llm_router = MagicMock()
            mock_proxy_server.llm_router.model_list = test_model_list

            mock_module = MagicMock()
            mock_module.proxy_server = mock_proxy_server

            with patch.dict("sys.modules", {"litellm.proxy": mock_module}):
                handler = CCProxyHandler()

                # Create request with >10k tokens using varied text
                base_text = "The quick brown fox jumps over the lazy dog. " * 50  # ~501 tokens
                large_message = base_text * 21  # ~10521 tokens (above 10000 threshold)
                request_data = {
                    "model": "claude-3-5-sonnet-20241022",
                    "messages": [{"role": "user", "content": large_message}],
                }
                user_api_key_dict = {}

                # Call the hook
                modified_data = await handler.async_pre_call_hook(
                    request_data,
                    user_api_key_dict,
                )

                # Should route to token_count
                assert modified_data["model"] == "gemini-2.5-pro"
                assert modified_data["metadata"]["ccproxy_model_name"] == "token_count"

        finally:
            ccproxy_path.unlink()
            litellm_path.unlink()
            clear_config_instance()
            clear_router()

    @pytest.mark.asyncio
    async def test_hooks_loaded_from_config(self) -> None:
        """Test that hooks are loaded from configuration file."""
        # Create config with hooks
        ccproxy_data = {
            "ccproxy": {
                "debug": False,
                "hooks": [
                    "ccproxy.hooks.rule_evaluator",
                    "ccproxy.hooks.model_router",
                ],
                "rules": [],
            }
        }

        # Create a dummy litellm config file
        litellm_data = {"model_list": []}

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as litellm_file:
            yaml.dump(litellm_data, litellm_file)
            litellm_path = Path(litellm_file.name)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as ccproxy_file:
            yaml.dump(ccproxy_data, ccproxy_file)
            ccproxy_path = Path(ccproxy_file.name)

        try:
            config = CCProxyConfig.from_yaml(ccproxy_path, litellm_config_path=litellm_path)
            set_config_instance(config)

            # Mock proxy server
            mock_proxy_server = MagicMock()
            mock_proxy_server.llm_router = MagicMock()
            mock_proxy_server.llm_router.model_list = []

            mock_module = MagicMock()
            mock_module.proxy_server = mock_proxy_server

            with patch.dict("sys.modules", {"litellm.proxy": mock_module}):
                handler = CCProxyHandler()

                # Verify hooks were loaded
                assert len(handler.hooks) == 2
                assert any("rule_evaluator" in str(h) for h in handler.hooks)
                assert any("model_router" in str(h) for h in handler.hooks)

        finally:
            ccproxy_path.unlink()
            litellm_path.unlink()
            clear_config_instance()
            clear_router()

    @pytest.mark.asyncio
    async def test_no_default_model_fallback(self) -> None:
        """Test that handler continues processing when no 'default' label is configured."""
        # Create config without a 'default' model
        ccproxy_config = CCProxyConfig(
            debug=False,
            rules=[
                RuleConfig(
                    name="token_count",
                    rule_path="ccproxy.rules.TokenCountRule",
                    params=[{"threshold": 60000}],
                ),
            ],
        )
        set_config_instance(ccproxy_config)

        # Mock proxy server with only token_count model (no default)
        mock_proxy_server = MagicMock()
        mock_proxy_server.llm_router = MagicMock()
        mock_proxy_server.llm_router.model_list = [
            {
                "model_name": "token_count",
                "litellm_params": {"model": "gemini-2.5-pro"},
            },
        ]

        mock_module = MagicMock()
        mock_module.proxy_server = mock_proxy_server

        try:
            with patch.dict("sys.modules", {"litellm.proxy": mock_module}):
                clear_router()  # Clear router to force reload
                handler = CCProxyHandler()

                # Test with request that doesn't match any rule
                request_data = {
                    "model": "claude-3-opus-20240229",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "token_count": 100,  # Below threshold
                }
                user_api_key_dict = {}

                # Should log error but continue processing
                result = await handler.async_pre_call_hook(request_data, user_api_key_dict)

                # Verify request continues with original model
                assert result["model"] == "claude-3-opus-20240229"

                # Test with missing model field
                request_data_no_model = {
                    "messages": [{"role": "user", "content": "Hello"}],
                    "token_count": 100,  # Below threshold
                }

                # Should log error but continue processing
                await handler.async_pre_call_hook(request_data_no_model, user_api_key_dict)

        finally:
            clear_config_instance()
            clear_router()

    @pytest.mark.asyncio
    async def test_log_routing_decision_fallback_scenario(self) -> None:
        """Test _log_routing_decision with fallback scenario (lines 135-136)."""
        # Set up handler with debug mode
        config = CCProxyConfig(debug=True)
        clear_config_instance()
        set_config_instance(config)

        try:
            handler = CCProxyHandler()

            # Test fallback scenario where model_config is None
            # This tests lines 135-136: color = "yellow", routing_type = "FALLBACK"
            handler._log_routing_decision(
                model_name="default",
                original_model="gpt-4",
                routed_model="claude-3-5-sonnet",
                request_id="test-123",
                model_config=None,  # This triggers the fallback path
            )

        finally:
            clear_config_instance()
            clear_router()

    @pytest.mark.asyncio
    async def test_log_routing_decision_passthrough_scenario(self) -> None:
        """Test _log_routing_decision with passthrough scenario (lines 139-140)."""
        # Set up handler with debug mode
        config = CCProxyConfig(debug=True)
        clear_config_instance()
        set_config_instance(config)

        try:
            handler = CCProxyHandler()

            # Test passthrough scenario where original_model == routed_model
            # This tests lines 139-140: color = "dim", routing_type = "PASSTHROUGH"
            model_config = {"model_info": {"some": "config"}}
            handler._log_routing_decision(
                model_name="default",
                original_model="claude-3-5-sonnet",
                routed_model="claude-3-5-sonnet",  # Same as original = passthrough
                request_id="test-456",
                model_config=model_config,
            )

        finally:
            clear_config_instance()
            clear_router()
