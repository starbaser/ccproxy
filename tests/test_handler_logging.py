"""Additional tests for ccproxy handler logging hook methods."""

from datetime import timedelta
from unittest.mock import Mock, patch

import pytest

from ccproxy.handler import CCProxyHandler


class TestHandlerLoggingHookMethods:
    """Test suite for individual logging hook methods."""

    @pytest.mark.asyncio
    async def test_log_success_event(self) -> None:
        """Test async_log_success_event method."""
        handler = CCProxyHandler()
        kwargs = {"metadata": {"request_id": "test-123", "ccproxy_model_name": "default"}, "model": "test-model"}
        response_obj = Mock(model="test-model", usage=Mock(prompt_tokens=20, completion_tokens=10, total_tokens=30))

        # Should not raise any exceptions
        await handler.async_log_success_event(kwargs, response_obj, 1234567890, 1234567900)

    @pytest.mark.asyncio
    async def test_log_failure_event(self) -> None:
        """Test async_log_failure_event method."""
        handler = CCProxyHandler()
        kwargs = {"metadata": {"request_id": "test-123", "ccproxy_model_name": "default"}, "model": "test-model"}
        response_obj = Exception("Test error")

        # Should not raise any exceptions
        await handler.async_log_failure_event(kwargs, response_obj, 1234567890, 1234567900)

    @pytest.mark.asyncio
    async def test_async_log_stream_event(self) -> None:
        """Test async_log_stream_event method."""
        handler = CCProxyHandler()
        kwargs = {"metadata": {"request_id": "test-123", "ccproxy_model_name": "default"}, "model": "test-model"}
        response_obj = Mock()
        start_time = 1234567890
        end_time = 1234567900

        # Should not raise any exceptions
        await handler.async_log_stream_event(kwargs, response_obj, start_time, end_time)

    @pytest.mark.asyncio
    async def test_async_pre_call_hook_with_invalid_request(self) -> None:
        """Test async_pre_call_hook with invalid request format."""
        # Mock the router to provide a default model
        with (
            patch("ccproxy.handler.get_router") as mock_get_router,
            patch("ccproxy.handler.get_config") as mock_get_config,
        ):
            from ccproxy.router import ModelRouter

            mock_router = Mock(spec=ModelRouter)
            mock_router.get_model_for_label.return_value = {
                "model_name": "default",
                "litellm_params": {"model": "claude-3-5-sonnet-20241022"},
            }
            mock_get_router.return_value = mock_router

            # Mock config to include hooks
            mock_config = Mock()
            mock_config.debug = False
            
            # Create a mock hook that adds metadata and model
            def mock_rule_evaluator(data, user_api_key_dict, **kwargs):
                if "metadata" not in data:
                    data["metadata"] = {}
                data["metadata"]["ccproxy_model_name"] = "default"
                data["metadata"]["ccproxy_alias_model"] = None
                # Add model field if missing (simulating model_router hook)
                if "model" not in data:
                    data["model"] = "claude-3-5-sonnet-20241022"
                return data
            
            mock_config.load_hooks.return_value = [mock_rule_evaluator]
            mock_get_config.return_value = mock_config

            handler = CCProxyHandler()

            # Missing model field - should use default
            data = {"messages": [{"role": "user", "content": "test"}]}

            # Should not raise - adds metadata and uses default model
            result = await handler.async_pre_call_hook(data, {})
            assert "metadata" in result
            assert result["metadata"]["ccproxy_model_name"] == "default"
            assert result["metadata"]["ccproxy_alias_model"] is None
            assert result["model"] == "claude-3-5-sonnet-20241022"

    @pytest.mark.asyncio  
    async def test_handler_with_debug_hook_logging(self) -> None:
        """Test handler debug logging of hooks during initialization."""
        with (
            patch("ccproxy.handler.get_router") as mock_get_router,
            patch("ccproxy.handler.get_config") as mock_get_config,
            patch("ccproxy.handler.logger") as mock_logger,
        ):
            # Mock config with debug=True and hooks
            mock_config = Mock()
            mock_config.debug = True
            
            def mock_hook(data, user_api_key_dict, **kwargs):
                return data
            mock_hook.__module__ = "test_module"
            mock_hook.__name__ = "test_hook"
            
            mock_config.load_hooks.return_value = [mock_hook]
            mock_get_config.return_value = mock_config
            
            mock_router = Mock()
            mock_get_router.return_value = mock_router

            # Create handler - should log hooks
            handler = CCProxyHandler()
            
            # Verify debug logging occurred
            mock_logger.debug.assert_called_once_with("Loaded 1 hooks: test_module.test_hook")

    @pytest.mark.asyncio
    async def test_hook_error_handling(self) -> None:
        """Test handler error handling when hooks fail."""
        with (
            patch("ccproxy.handler.get_router") as mock_get_router,
            patch("ccproxy.handler.get_config") as mock_get_config,
            patch("ccproxy.handler.logger") as mock_logger,
        ):
            # Mock router
            mock_router = Mock()
            mock_get_router.return_value = mock_router

            # Mock config with a failing hook
            mock_config = Mock()
            mock_config.debug = False
            
            def failing_hook(data, user_api_key_dict, **kwargs):
                raise ValueError("Hook failed!")
            failing_hook.__name__ = "failing_hook"
            
            mock_config.load_hooks.return_value = [failing_hook]
            mock_get_config.return_value = mock_config

            handler = CCProxyHandler()
            data = {"messages": [{"role": "user", "content": "test"}]}

            # Should not raise but should log error
            result = await handler.async_pre_call_hook(data, {})
            
            # Verify error was logged
            mock_logger.error.assert_called_once()
            args = mock_logger.error.call_args[0]
            assert "Hook failing_hook failed with error" in args[0]
            assert "Hook failed!" in args[0]

    @pytest.mark.asyncio
    async def test_thinking_parameters_debug_output(self, capsys) -> None:
        """Test debug output for thinking parameters."""
        with (
            patch("ccproxy.handler.get_router") as mock_get_router,
            patch("ccproxy.handler.get_config") as mock_get_config,
        ):
            # Mock router
            mock_router = Mock()
            mock_get_router.return_value = mock_router

            # Mock config with no hooks
            mock_config = Mock()
            mock_config.debug = False
            mock_config.load_hooks.return_value = []
            mock_get_config.return_value = mock_config

            handler = CCProxyHandler()
            
            # Request with thinking parameters
            data = {
                "messages": [{"role": "user", "content": "test"}],
                "thinking": {"mode": "deep", "level": 5}
            }

            await handler.async_pre_call_hook(data, {})
            
            # Check that thinking parameters were printed
            captured = capsys.readouterr()
            assert "🧠 Thinking parameters: {'mode': 'deep', 'level': 5}" in captured.out

    @patch("ccproxy.handler.get_router")
    @patch("ccproxy.handler.get_config")
    def test_debug_routing_output(self, mock_get_config, mock_get_router, capsys) -> None:
        """Test debug routing output with Rich formatting."""
        from ccproxy.router import ModelRouter
        
        # Mock router
        mock_router = Mock(spec=ModelRouter)
        mock_router.get_model_for_label.return_value = {
            "model_name": "gpt-4",
            "litellm_params": {"model": "gpt-4-turbo"}
        }
        mock_get_router.return_value = mock_router

        # Mock config with debug=True
        mock_config = Mock()
        mock_config.debug = True
        mock_config.load_hooks.return_value = []
        mock_get_config.return_value = mock_config

        handler = CCProxyHandler()
        
        # Call _log_routing_decision method directly
        metadata = {"ccproxy_model_name": "gpt-4", "ccproxy_alias_model": "claude-3-5-sonnet-20241022"}
        
        handler._log_routing_decision(
            model_config={"model_name": "gpt-4"}, 
            model_name="gpt-4",
            original_model="claude-3-5-sonnet-20241022",
            routed_model="gpt-4-turbo",
            request_id="test-123"
        )
        
        # Check that rich panel was printed (will contain routing info)
        captured = capsys.readouterr()
        assert "🚀 ccproxy Routing Decision" in captured.out
        assert "ROUTED" in captured.out
        assert "gpt-4" in captured.out

    @patch("ccproxy.handler.logger")
    def test_log_routing_decision(self, mock_logger: Mock) -> None:
        """Test _log_routing_decision method."""
        handler = CCProxyHandler()

        # Test with model config
        model_config = {
            "model_info": {
                "provider": "google",
                "max_tokens": 1000000,
                "api_key": "secret",  # Should be filtered out
            }
        }

        handler._log_routing_decision(
            model_name="token_count",
            original_model="claude-3-5-sonnet",
            routed_model="gemini-2.0-flash-exp",
            request_id="test-123",
            model_config=model_config,
        )

        # Check logger was called
        mock_logger.info.assert_called_once()
        call_args = mock_logger.info.call_args
        assert call_args[0][0] == "ccproxy routing decision"

        # Check extra data
        extra = call_args[1]["extra"]
        assert extra["event"] == "ccproxy_routing"
        assert extra["model_name"] == "token_count"
        assert extra["original_model"] == "claude-3-5-sonnet"
        assert extra["routed_model"] == "gemini-2.0-flash-exp"
        assert extra["request_id"] == "test-123"
        assert extra["fallback_used"] is False

        # Check sensitive data was filtered
        assert "api_key" not in extra["model_info"]
        assert extra["model_info"]["provider"] == "google"
        assert extra["model_info"]["max_tokens"] == 1000000

    @pytest.mark.asyncio
    async def test_timedelta_duration_handling(self) -> None:
        """Test that handler correctly handles timedelta objects for timestamps."""
        handler = CCProxyHandler()
        kwargs = {"metadata": {"request_id": "test-123", "ccproxy_model_name": "default"}, "model": "test-model"}
        response_obj = Mock()

        # Test with timedelta objects (simulating LiteLLM's behavior)
        start_time = timedelta(seconds=100)
        end_time = timedelta(seconds=102, milliseconds=500)

        # Should not raise any exceptions - test success logging
        await handler.async_log_success_event(kwargs, response_obj, start_time, end_time)

        # Should not raise any exceptions - test failure logging
        await handler.async_log_failure_event(kwargs, response_obj, start_time, end_time)

        # Should not raise any exceptions - test streaming logging
        await handler.async_log_stream_event(kwargs, response_obj, start_time, end_time)

    @pytest.mark.asyncio
    async def test_mixed_timestamp_types_handling(self) -> None:
        """Test that handler correctly handles mixed float/timedelta timestamp types."""
        handler = CCProxyHandler()
        kwargs = {"metadata": {"request_id": "test-123", "ccproxy_model_name": "default"}, "model": "test-model"}
        response_obj = Mock()

        # Test with mixed types (float start, timedelta end)
        start_time = 100.0
        end_time = timedelta(seconds=102, milliseconds=500)

        # Should not raise any exceptions and handle gracefully
        await handler.async_log_success_event(kwargs, response_obj, start_time, end_time)
        await handler.async_log_failure_event(kwargs, response_obj, start_time, end_time)
        await handler.async_log_stream_event(kwargs, response_obj, start_time, end_time)
