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
        kwargs = {"metadata": {"ccproxy_model_name": "default"}, "model": "test-model"}
        response_obj = Mock(model="test-model", usage=Mock(prompt_tokens=20, completion_tokens=10, total_tokens=30))

        # Should not raise any exceptions
        await handler.async_log_success_event(kwargs, response_obj, 1234567890, 1234567900)

    @pytest.mark.asyncio
    async def test_log_failure_event(self) -> None:
        """Test async_log_failure_event method."""
        handler = CCProxyHandler()
        kwargs = {"metadata": {"ccproxy_model_name": "default"}, "model": "test-model"}
        response_obj = Exception("Test error")

        # Should not raise any exceptions
        await handler.async_log_failure_event(kwargs, response_obj, 1234567890, 1234567900)

    @pytest.mark.asyncio
    async def test_async_log_stream_event(self) -> None:
        """Test async_log_stream_event method."""
        handler = CCProxyHandler()
        kwargs = {"metadata": {"ccproxy_model_name": "default"}, "model": "test-model"}
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
                "litellm_params": {"model": "claude-sonnet-4-5-20250929"},
            }
            mock_get_router.return_value = mock_router

            # Mock config
            mock_config = Mock()
            mock_config.debug = False
            mock_config.default_model_passthrough = False
            mock_get_config.return_value = mock_config

            handler = CCProxyHandler()

            # Missing model field - pipeline should handle gracefully
            data = {"messages": [{"role": "user", "content": "test"}]}

            # Should not raise - pipeline adds metadata
            result = await handler.async_pre_call_hook(data, {})
            assert "metadata" in result
            # Pipeline should have processed the request
            assert result["metadata"].get("ccproxy_model_name") is not None or result["metadata"].get("ccproxy_alias_model") == ""

    @pytest.mark.asyncio
    async def test_handler_with_debug_hook_logging(self) -> None:
        """Test handler debug logging of pipeline initialization."""
        with (
            patch("ccproxy.handler.get_router") as mock_get_router,
            patch("ccproxy.handler.get_config") as mock_get_config,
            patch("ccproxy.handler.logger") as mock_logger,
        ):
            # Mock config with debug=True
            mock_config = Mock()
            mock_config.debug = True
            mock_config.default_model_passthrough = False
            mock_get_config.return_value = mock_config

            mock_router = Mock()
            mock_get_router.return_value = mock_router

            # Create handler - should log pipeline initialization
            handler = CCProxyHandler()

            # Verify debug logging occurred for pipeline initialization
            # Pipeline logs: "Pipeline initialized with %d hooks: %s"
            debug_calls = [str(call) for call in mock_logger.debug.call_args_list]
            assert any("Pipeline initialized" in str(call) or "hooks:" in str(call) for call in debug_calls)

    @pytest.mark.asyncio
    async def test_hook_error_handling(self) -> None:
        """Test pipeline error isolation when hooks fail."""
        with (
            patch("ccproxy.handler.get_router") as mock_get_router,
            patch("ccproxy.handler.get_config") as mock_get_config,
        ):
            # Mock router with proper method
            mock_router = Mock()
            mock_router.get_model_for_label.return_value = {
                "model_name": "default",
                "litellm_params": {"model": "test-model"},
            }
            mock_get_router.return_value = mock_router

            # Mock config
            mock_config = Mock()
            mock_config.debug = False
            mock_config.default_model_passthrough = False
            mock_get_config.return_value = mock_config

            handler = CCProxyHandler()

            # Use data that would trigger a hook but with invalid structure
            # The pipeline has error isolation so hooks can fail without stopping
            data = {
                "messages": [{"role": "user", "content": "test"}],
                "metadata": {},
            }

            # Should not raise - pipeline has error isolation
            result = await handler.async_pre_call_hook(data, {})

            # Result should still have metadata even if some hooks fail
            assert "metadata" in result

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
            original_model="claude-sonnet-4-5-20250929",
            routed_model="gemini-2.0-flash-exp",
            model_config=model_config,
        )

        # Check logger was called with structured data
        mock_logger.info.assert_called_once()
        call_args = mock_logger.info.call_args

        # Check structured data (important for monitoring/alerting)
        extra = call_args[1]["extra"]
        assert extra["event"] == "ccproxy_routing"
        assert extra["model_name"] == "token_count"
        assert extra["original_model"] == "claude-sonnet-4-5-20250929"
        assert extra["routed_model"] == "gemini-2.0-flash-exp"
        assert extra["is_passthrough"] is False

        # Check sensitive data was filtered
        assert "api_key" not in extra["model_info"]
        assert extra["model_info"]["provider"] == "google"
        assert extra["model_info"]["max_tokens"] == 1000000

    @pytest.mark.asyncio
    async def test_timedelta_duration_handling(self) -> None:
        """Test that handler correctly handles timedelta objects for timestamps."""
        handler = CCProxyHandler()
        kwargs = {"metadata": {"ccproxy_model_name": "default"}, "model": "test-model"}
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
        kwargs = {"metadata": {"ccproxy_model_name": "default"}, "model": "test-model"}
        response_obj = Mock()

        # Test with mixed types (float start, timedelta end)
        start_time = 100.0
        end_time = timedelta(seconds=102, milliseconds=500)

        # Should not raise any exceptions and handle gracefully
        await handler.async_log_success_event(kwargs, response_obj, start_time, end_time)
        await handler.async_log_failure_event(kwargs, response_obj, start_time, end_time)
        await handler.async_log_stream_event(kwargs, response_obj, start_time, end_time)
