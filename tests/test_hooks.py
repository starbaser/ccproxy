"""Comprehensive tests for ccproxy hooks."""

import logging
from unittest.mock import MagicMock, patch

import pytest

from ccproxy.classifier import RequestClassifier
from ccproxy.config import clear_config_instance
from ccproxy.hooks import forward_apikey, forward_oauth, model_router, rule_evaluator
from ccproxy.router import ModelRouter, clear_router


@pytest.fixture
def mock_classifier():
    """Create a mock classifier that returns 'test_model_name'."""
    classifier = MagicMock(spec=RequestClassifier)
    classifier.classify.return_value = "test_model_name"
    return classifier


@pytest.fixture
def mock_router():
    """Create a mock router with test model configurations."""
    router = MagicMock(spec=ModelRouter)

    # Default successful routing
    router.get_model_for_label.return_value = {
        "litellm_params": {"model": "claude-sonnet-4-5-20250929", "api_base": "https://api.anthropic.com"}
    }

    return router


@pytest.fixture
def basic_request_data():
    """Create basic request data for testing."""
    return {
        "model": "claude-haiku-4-5-20251001-20241022",
        "messages": [{"role": "user", "content": "test message"}],
    }


@pytest.fixture
def user_api_key_dict():
    """Create empty user API key dict."""
    return {}


@pytest.fixture(autouse=True)
def cleanup():
    """Clean up config and router between tests."""
    yield
    clear_config_instance()
    clear_router()


class TestRuleEvaluator:
    """Test the rule_evaluator hook function."""

    def test_rule_evaluator_success(self, mock_classifier, basic_request_data, user_api_key_dict):
        """Test successful rule evaluation."""
        # Call rule_evaluator with classifier
        result = rule_evaluator(basic_request_data, user_api_key_dict, classifier=mock_classifier)

        # Verify metadata was added
        assert "metadata" in result
        assert result["metadata"]["ccproxy_alias_model"] == "claude-haiku-4-5-20251001-20241022"
        assert result["metadata"]["ccproxy_model_name"] == "test_model_name"

        # Verify classifier was called
        mock_classifier.classify.assert_called_once_with(basic_request_data)

    def test_rule_evaluator_existing_metadata(self, mock_classifier, user_api_key_dict):
        """Test rule_evaluator preserves existing metadata."""
        data_with_metadata = {
            "model": "claude-haiku-4-5-20251001-20241022",
            "messages": [{"role": "user", "content": "test"}],
            "metadata": {"existing_key": "existing_value"},
        }

        result = rule_evaluator(data_with_metadata, user_api_key_dict, classifier=mock_classifier)

        # Verify existing metadata preserved and new metadata added
        assert result["metadata"]["existing_key"] == "existing_value"
        assert result["metadata"]["ccproxy_alias_model"] == "claude-haiku-4-5-20251001-20241022"
        assert result["metadata"]["ccproxy_model_name"] == "test_model_name"

    def test_rule_evaluator_missing_classifier(self, basic_request_data, user_api_key_dict, caplog):
        """Test rule_evaluator handles missing classifier gracefully."""
        with caplog.at_level(logging.WARNING):
            result = rule_evaluator(basic_request_data, user_api_key_dict)

        # Should return original data unchanged
        assert result == basic_request_data
        assert "Classifier not found or invalid type in rule_evaluator" in caplog.text

    def test_rule_evaluator_invalid_classifier(self, basic_request_data, user_api_key_dict, caplog):
        """Test rule_evaluator handles invalid classifier type."""
        with caplog.at_level(logging.WARNING):
            result = rule_evaluator(basic_request_data, user_api_key_dict, classifier="invalid_classifier")

        # Should return original data unchanged
        assert result == basic_request_data
        assert "Classifier not found or invalid type in rule_evaluator" in caplog.text

    def test_rule_evaluator_no_model_in_data(self, mock_classifier, user_api_key_dict):
        """Test rule_evaluator handles data without model."""
        data_no_model = {
            "messages": [{"role": "user", "content": "test"}],
        }

        result = rule_evaluator(data_no_model, user_api_key_dict, classifier=mock_classifier)

        # Should still add metadata
        assert "metadata" in result
        assert result["metadata"]["ccproxy_alias_model"] is None
        assert result["metadata"]["ccproxy_model_name"] == "test_model_name"


class TestModelRouter:
    """Test the model_router hook function."""

    def test_model_router_success(self, mock_router, user_api_key_dict):
        """Test successful model routing."""
        data_with_metadata = {
            "model": "original_model",
            "messages": [{"role": "user", "content": "test"}],
            "metadata": {"ccproxy_model_name": "test_model"},
        }

        result = model_router(data_with_metadata, user_api_key_dict, router=mock_router)

        # Verify model was routed
        assert result["model"] == "claude-sonnet-4-5-20250929"
        assert result["metadata"]["ccproxy_litellm_model"] == "claude-sonnet-4-5-20250929"
        assert "ccproxy_model_config" in result["metadata"]

        # Verify router was called
        mock_router.get_model_for_label.assert_called_once_with("test_model")

    def test_model_router_missing_router(self, user_api_key_dict, caplog):
        """Test model_router handles missing router gracefully."""
        data = {"model": "original_model", "metadata": {"ccproxy_model_name": "test_model"}}

        with caplog.at_level(logging.WARNING):
            result = model_router(data, user_api_key_dict)

        # Should return original data unchanged
        assert result == data
        assert "Router not found or invalid type in model_router" in caplog.text

    def test_model_router_invalid_router(self, user_api_key_dict, caplog):
        """Test model_router handles invalid router type."""
        data = {"model": "original_model", "metadata": {"ccproxy_model_name": "test_model"}}

        with caplog.at_level(logging.WARNING):
            result = model_router(data, user_api_key_dict, router="invalid_router")

        # Should return original data unchanged
        assert result == data
        assert "Router not found or invalid type in model_router" in caplog.text

    def test_model_router_no_metadata(self, mock_router, user_api_key_dict, caplog):
        """Test model_router handles missing metadata gracefully."""
        data = {"model": "original_model"}

        with caplog.at_level(logging.WARNING):
            result = model_router(data, user_api_key_dict, router=mock_router)

        # Should use default model name and create metadata
        mock_router.get_model_for_label.assert_called_once_with("default")
        assert "metadata" in result

    def test_model_router_empty_model_name(self, mock_router, user_api_key_dict, caplog):
        """Test model_router handles empty model name."""
        data = {"model": "original_model", "metadata": {"ccproxy_model_name": ""}}

        with caplog.at_level(logging.WARNING):
            model_router(data, user_api_key_dict, router=mock_router)

        # Should use default and log warning
        mock_router.get_model_for_label.assert_called_once_with("default")
        assert "No ccproxy_model_name found, using default" in caplog.text

    def test_model_router_no_litellm_params(self, mock_router, user_api_key_dict, caplog):
        """Test model_router handles config without litellm_params."""
        mock_router.get_model_for_label.return_value = {"other_config": "value"}

        data = {"model": "original_model", "metadata": {"ccproxy_model_name": "test_model"}}

        with caplog.at_level(logging.WARNING):
            result = model_router(data, user_api_key_dict, router=mock_router)

        # Should log warning about missing model
        assert "No model found in config for model_name: test_model" in caplog.text
        assert result["metadata"]["ccproxy_litellm_model"] is None

    def test_model_router_no_model_in_litellm_params(self, mock_router, user_api_key_dict, caplog):
        """Test model_router handles litellm_params without model."""
        mock_router.get_model_for_label.return_value = {"litellm_params": {"api_base": "https://api.anthropic.com"}}

        data = {"model": "original_model", "metadata": {"ccproxy_model_name": "test_model"}}

        with caplog.at_level(logging.WARNING):
            result = model_router(data, user_api_key_dict, router=mock_router)

        # Should log warning about missing model
        assert "No model found in config for model_name: test_model" in caplog.text
        assert result["metadata"]["ccproxy_litellm_model"] is None

    def test_model_router_no_config_with_reload_success(self, mock_router, user_api_key_dict, caplog):
        """Test model_router handles missing config with successful reload."""
        # First call returns None, second call (after reload) returns config
        mock_router.get_model_for_label.side_effect = [
            None,  # First call
            {  # Second call after reload
                "litellm_params": {"model": "claude-sonnet-4-5-20250929"}
            },
        ]

        data = {"model": "original_model", "metadata": {"ccproxy_model_name": "test_model"}}

        with caplog.at_level(logging.INFO):
            result = model_router(data, user_api_key_dict, router=mock_router)

        # Should reload and succeed
        mock_router.reload_models.assert_called_once()
        assert mock_router.get_model_for_label.call_count == 2
        assert result["model"] == "claude-sonnet-4-5-20250929"
        assert "Successfully routed after model reload: test_model -> claude-sonnet-4-5-20250929" in caplog.text

    def test_model_router_no_config_reload_fails(self, mock_router, user_api_key_dict):
        """Test model_router raises error when reload fails."""
        # Both calls return None
        mock_router.get_model_for_label.return_value = None

        data = {"model": "original_model", "metadata": {"ccproxy_model_name": "test_model"}}

        with pytest.raises(ValueError, match="No model configured for model_name 'test_model'"):
            model_router(data, user_api_key_dict, router=mock_router)

        # Should try reload
        mock_router.reload_models.assert_called_once()
        assert mock_router.get_model_for_label.call_count == 2

    @patch("ccproxy.hooks.get_config")
    def test_model_router_default_passthrough_enabled(self, mock_get_config, mock_router, user_api_key_dict):
        """Test model_router with default_model_passthrough=True uses original model."""
        # Configure passthrough mode
        mock_config = MagicMock()
        mock_config.default_model_passthrough = True
        mock_get_config.return_value = mock_config

        data = {
            "model": "original_model",
            "metadata": {"ccproxy_model_name": "default", "ccproxy_alias_model": "claude-sonnet-4-5-20250929"},
        }

        result = model_router(data, user_api_key_dict, router=mock_router)

        # Should keep original model and not call router
        assert result["model"] == "original_model"
        assert result["metadata"]["ccproxy_litellm_model"] == "claude-sonnet-4-5-20250929"
        assert result["metadata"]["ccproxy_model_config"] is None
        mock_router.get_model_for_label.assert_not_called()

    @patch("ccproxy.hooks.get_config")
    def test_model_router_default_passthrough_disabled(self, mock_get_config, mock_router, user_api_key_dict):
        """Test model_router with default_model_passthrough=False uses router."""
        # Configure routing mode
        mock_config = MagicMock()
        mock_config.default_model_passthrough = False
        mock_get_config.return_value = mock_config

        # Update mock router to return expected values
        mock_router.get_model_for_label.return_value = {"litellm_params": {"model": "routed_model"}}

        data = {
            "model": "original_model",
            "metadata": {"ccproxy_model_name": "default", "ccproxy_alias_model": "claude-sonnet-4-5-20250929"},
        }

        result = model_router(data, user_api_key_dict, router=mock_router)

        # Should use router for "default" label
        mock_router.get_model_for_label.assert_called_once_with("default")
        assert result["model"] == "routed_model"
        assert result["metadata"]["ccproxy_litellm_model"] == "routed_model"

    @patch("ccproxy.hooks.get_config")
    def test_model_router_passthrough_no_original_model(self, mock_get_config, mock_router, user_api_key_dict, caplog):
        """Test model_router passthrough mode when no original model is available."""
        # Configure passthrough mode
        mock_config = MagicMock()
        mock_config.default_model_passthrough = True
        mock_get_config.return_value = mock_config

        # Update mock router to return expected values
        mock_router.get_model_for_label.return_value = {"litellm_params": {"model": "routed_model"}}

        data = {
            "model": "original_model",
            "metadata": {
                "ccproxy_model_name": "default"
                # No ccproxy_alias_model
            },
        }

        with caplog.at_level(logging.WARNING):
            result = model_router(data, user_api_key_dict, router=mock_router)

        # Should fallback to routing and log warning
        assert "No original model found for passthrough mode" in caplog.text
        mock_router.get_model_for_label.assert_called_once_with("default")
        assert result["model"] == "routed_model"


class TestForwardOAuth:
    """Test the forward_oauth hook function."""

    def test_forward_oauth_no_proxy_request(self, user_api_key_dict):
        """Test forward_oauth handles missing proxy_server_request."""
        data = {"model": "claude-sonnet-4-5-20250929", "metadata": {"ccproxy_litellm_model": "claude-sonnet-4-5-20250929"}}

        result = forward_oauth(data, user_api_key_dict)

        # Should return unchanged data
        assert result == data

    def test_forward_oauth_claude_cli_anthropic_api_base(self, user_api_key_dict, caplog):
        """Test OAuth forwarding for claude-cli with Anthropic API base."""
        data = {
            "model": "claude-sonnet-4-5-20250929",
            "metadata": {
                "ccproxy_litellm_model": "claude-sonnet-4-5-20250929",
                "ccproxy_model_config": {"litellm_params": {"api_base": "https://api.anthropic.com"}},
            },
            "proxy_server_request": {"headers": {"user-agent": "claude-cli/1.0.62 (external, cli)"}},
            "secret_fields": {"raw_headers": {"authorization": "Bearer sk-ant-oat01-test-token"}},
        }

        with caplog.at_level(logging.INFO):
            result = forward_oauth(data, user_api_key_dict)

        # Should forward OAuth token
        assert "provider_specific_header" in result
        assert "extra_headers" in result["provider_specific_header"]
        assert result["provider_specific_header"]["extra_headers"]["authorization"] == "Bearer sk-ant-oat01-test-token"

        # Should log OAuth forwarding
        assert "Forwarding request with Claude Code OAuth authentication" in caplog.text

    def test_forward_oauth_claude_cli_anthropic_hostname(self, user_api_key_dict):
        """Test OAuth forwarding for claude-cli with anthropic.com hostname."""
        data = {
            "model": "claude-sonnet-4-5-20250929",
            "metadata": {
                "ccproxy_litellm_model": "claude-sonnet-4-5-20250929",
                "ccproxy_model_config": {"litellm_params": {"api_base": "https://anthropic.com/v1/messages"}},
            },
            "proxy_server_request": {"headers": {"user-agent": "claude-cli/1.0.62 (external, cli)"}},
            "secret_fields": {"raw_headers": {"authorization": "Bearer sk-ant-oat01-test-token"}},
        }

        result = forward_oauth(data, user_api_key_dict)

        # Should forward OAuth token
        assert result["provider_specific_header"]["extra_headers"]["authorization"] == "Bearer sk-ant-oat01-test-token"

    def test_forward_oauth_claude_cli_custom_provider_anthropic(self, user_api_key_dict):
        """Test OAuth forwarding with custom_llm_provider=anthropic."""
        data = {
            "model": "claude-sonnet-4-5-20250929",
            "metadata": {
                "ccproxy_litellm_model": "claude-sonnet-4-5-20250929",
                "ccproxy_model_config": {"litellm_params": {"custom_llm_provider": "anthropic"}},
            },
            "proxy_server_request": {"headers": {"user-agent": "claude-cli/1.0.62 (external, cli)"}},
            "secret_fields": {"raw_headers": {"authorization": "Bearer sk-ant-oat01-test-token"}},
        }

        result = forward_oauth(data, user_api_key_dict)

        # Should forward OAuth token
        assert result["provider_specific_header"]["extra_headers"]["authorization"] == "Bearer sk-ant-oat01-test-token"

    def test_forward_oauth_claude_cli_anthropic_prefix_model(self, user_api_key_dict):
        """Test OAuth forwarding for anthropic/ prefix models."""
        data = {
            "model": "claude-sonnet-4-5-20250929",
            "metadata": {
                "ccproxy_litellm_model": "anthropic/claude-sonnet-4-5-20250929",
                "ccproxy_model_config": {"litellm_params": {}},
            },
            "proxy_server_request": {"headers": {"user-agent": "claude-cli/1.0.62 (external, cli)"}},
            "secret_fields": {"raw_headers": {"authorization": "Bearer sk-ant-oat01-test-token"}},
        }

        result = forward_oauth(data, user_api_key_dict)

        # Should forward OAuth token
        assert result["provider_specific_header"]["extra_headers"]["authorization"] == "Bearer sk-ant-oat01-test-token"

    def test_forward_oauth_claude_cli_claude_prefix_model(self, user_api_key_dict):
        """Test OAuth forwarding for claude prefix models."""
        data = {
            "model": "claude-sonnet-4-5-20250929",
            "metadata": {
                "ccproxy_litellm_model": "claude-sonnet-4-5-20250929",
                "ccproxy_model_config": {"litellm_params": {}},
            },
            "proxy_server_request": {"headers": {"user-agent": "claude-cli/1.0.62 (external, cli)"}},
            "secret_fields": {"raw_headers": {"authorization": "Bearer sk-ant-oat01-test-token"}},
        }

        result = forward_oauth(data, user_api_key_dict)

        # Should forward OAuth token
        assert result["provider_specific_header"]["extra_headers"]["authorization"] == "Bearer sk-ant-oat01-test-token"

    def test_forward_oauth_non_claude_cli_user_agent(self, user_api_key_dict):
        """Test no OAuth forwarding for non-claude-cli user agents."""
        data = {
            "model": "claude-sonnet-4-5-20250929",
            "metadata": {
                "ccproxy_litellm_model": "claude-sonnet-4-5-20250929",
                "ccproxy_model_config": {"litellm_params": {"api_base": "https://api.anthropic.com"}},
            },
            "proxy_server_request": {"headers": {"user-agent": "Mozilla/5.0"}},
            "secret_fields": {"raw_headers": {"authorization": "Bearer sk-ant-oat01-test-token"}},
        }

        result = forward_oauth(data, user_api_key_dict)

        # Should not forward OAuth token
        assert "provider_specific_header" not in result

    def test_forward_oauth_non_anthropic_provider(self, user_api_key_dict):
        """Test no OAuth forwarding for non-Anthropic providers."""
        data = {
            "model": "gemini-2.5-pro",
            "metadata": {
                "ccproxy_litellm_model": "gemini-2.5-pro",
                "ccproxy_model_config": {"litellm_params": {"api_base": "https://generativelanguage.googleapis.com"}},
            },
            "proxy_server_request": {"headers": {"user-agent": "claude-cli/1.0.62 (external, cli)"}},
            "secret_fields": {"raw_headers": {"authorization": "Bearer sk-ant-oat01-test-token"}},
        }

        result = forward_oauth(data, user_api_key_dict)

        # Should not forward OAuth token
        assert "provider_specific_header" not in result

    def test_forward_oauth_vertex_provider(self, user_api_key_dict):
        """Test no OAuth forwarding for Vertex AI provider."""
        data = {
            "model": "claude-sonnet-4-5-20250929",
            "metadata": {
                "ccproxy_litellm_model": "vertex/claude-sonnet-4-5-20250929",
                "ccproxy_model_config": {
                    "litellm_params": {
                        "api_base": "https://us-central1-aiplatform.googleapis.com",
                        "custom_llm_provider": "vertex",
                    }
                },
            },
            "proxy_server_request": {"headers": {"user-agent": "claude-cli/1.0.62 (external, cli)"}},
            "secret_fields": {"raw_headers": {"authorization": "Bearer sk-ant-oat01-test-token"}},
        }

        result = forward_oauth(data, user_api_key_dict)

        # Should not forward OAuth token
        assert "provider_specific_header" not in result

    def test_forward_oauth_missing_auth_header(self, user_api_key_dict):
        """Test no OAuth forwarding when auth header is missing and no credentials configured."""
        from ccproxy.config import CCProxyConfig, set_config_instance

        # Configure without credentials to disable fallback
        config = CCProxyConfig(credentials=None)
        set_config_instance(config)

        data = {
            "model": "claude-sonnet-4-5-20250929",
            "metadata": {
                "ccproxy_litellm_model": "claude-sonnet-4-5-20250929",
                "ccproxy_model_config": {"litellm_params": {"api_base": "https://api.anthropic.com"}},
            },
            "proxy_server_request": {"headers": {"user-agent": "claude-cli/1.0.62 (external, cli)"}},
            "secret_fields": {
                "raw_headers": {}  # No auth header
            },
        }

        result = forward_oauth(data, user_api_key_dict)

        # Should not forward OAuth token when no header and no fallback
        assert "provider_specific_header" not in result

    def test_forward_oauth_missing_secret_fields(self, user_api_key_dict):
        """Test no OAuth forwarding when secret_fields is missing and no credentials configured."""
        from ccproxy.config import CCProxyConfig, set_config_instance

        # Configure without credentials to disable fallback
        config = CCProxyConfig(credentials=None)
        set_config_instance(config)

        data = {
            "model": "claude-sonnet-4-5-20250929",
            "metadata": {
                "ccproxy_litellm_model": "claude-sonnet-4-5-20250929",
                "ccproxy_model_config": {"litellm_params": {"api_base": "https://api.anthropic.com"}},
            },
            "proxy_server_request": {"headers": {"user-agent": "claude-cli/1.0.62 (external, cli)"}},
            # secret_fields is missing
        }

        result = forward_oauth(data, user_api_key_dict)

        # Should not forward OAuth token when no secret_fields and no fallback
        assert "provider_specific_header" not in result

    def test_forward_oauth_preserves_existing_extra_headers(self, user_api_key_dict):
        """Test OAuth forwarding preserves existing extra_headers."""
        data = {
            "model": "claude-sonnet-4-5-20250929",
            "metadata": {
                "ccproxy_litellm_model": "claude-sonnet-4-5-20250929",
                "ccproxy_model_config": {"litellm_params": {"api_base": "https://api.anthropic.com"}},
            },
            "provider_specific_header": {"extra_headers": {"existing-header": "existing-value"}},
            "proxy_server_request": {"headers": {"user-agent": "claude-cli/1.0.62 (external, cli)"}},
            "secret_fields": {"raw_headers": {"authorization": "Bearer sk-ant-oat01-test-token"}},
        }

        result = forward_oauth(data, user_api_key_dict)

        # Should preserve existing headers and add auth
        assert result["provider_specific_header"]["extra_headers"]["existing-header"] == "existing-value"
        assert result["provider_specific_header"]["extra_headers"]["authorization"] == "Bearer sk-ant-oat01-test-token"

    def test_forward_oauth_creates_provider_specific_header_structure(self, user_api_key_dict):
        """Test OAuth forwarding creates provider_specific_header structure when missing."""
        data = {
            "model": "claude-sonnet-4-5-20250929",
            "metadata": {
                "ccproxy_litellm_model": "claude-sonnet-4-5-20250929",
                "ccproxy_model_config": {"litellm_params": {"api_base": "https://api.anthropic.com"}},
            },
            "proxy_server_request": {"headers": {"user-agent": "claude-cli/1.0.62 (external, cli)"}},
            "secret_fields": {"raw_headers": {"authorization": "Bearer sk-ant-oat01-test-token"}},
            # provider_specific_header is missing
        }

        result = forward_oauth(data, user_api_key_dict)

        # Should create the structure and add auth
        assert "provider_specific_header" in result
        assert "extra_headers" in result["provider_specific_header"]
        assert result["provider_specific_header"]["extra_headers"]["authorization"] == "Bearer sk-ant-oat01-test-token"

    def test_forward_oauth_invalid_api_base_url(self, user_api_key_dict):
        """Test OAuth forwarding handles invalid API base URLs gracefully."""
        data = {
            "model": "claude-sonnet-4-5-20250929",
            "metadata": {
                "ccproxy_litellm_model": "claude-sonnet-4-5-20250929",
                "ccproxy_model_config": {"litellm_params": {"api_base": "invalid-url"}},
            },
            "proxy_server_request": {"headers": {"user-agent": "claude-cli/1.0.62 (external, cli)"}},
            "secret_fields": {"raw_headers": {"authorization": "Bearer sk-ant-oat01-test-token"}},
        }

        result = forward_oauth(data, user_api_key_dict)

        # Should not forward OAuth token for invalid URL
        assert "provider_specific_header" not in result

    def test_forward_oauth_missing_model_config(self, user_api_key_dict):
        """Test OAuth forwarding with missing model config."""
        data = {
            "model": "claude-sonnet-4-5-20250929",
            "metadata": {
                "ccproxy_litellm_model": "claude-sonnet-4-5-20250929"
                # ccproxy_model_config is missing
            },
            "proxy_server_request": {"headers": {"user-agent": "claude-cli/1.0.62 (external, cli)"}},
            "secret_fields": {"raw_headers": {"authorization": "Bearer sk-ant-oat01-test-token"}},
        }

        result = forward_oauth(data, user_api_key_dict)

        # Should still forward for claude prefix model
        assert result["provider_specific_header"]["extra_headers"]["authorization"] == "Bearer sk-ant-oat01-test-token"

    def test_forward_oauth_empty_headers(self, user_api_key_dict):
        """Test OAuth forwarding with empty headers."""
        data = {
            "model": "claude-sonnet-4-5-20250929",
            "metadata": {
                "ccproxy_litellm_model": "claude-sonnet-4-5-20250929",
                "ccproxy_model_config": {"litellm_params": {"api_base": "https://api.anthropic.com"}},
            },
            "proxy_server_request": {
                "headers": {}  # Empty headers
            },
            "secret_fields": {"raw_headers": {"authorization": "Bearer sk-ant-oat01-test-token"}},
        }

        result = forward_oauth(data, user_api_key_dict)

        # Should not forward OAuth token without user-agent
        assert "provider_specific_header" not in result

    def test_forward_oauth_urlparse_exception(self, user_api_key_dict):
        """Test OAuth forwarding handles urlparse exceptions."""
        # Create a data structure that will cause urlparse to fail
        # Using a mock to simulate this
        data = {
            "model": "claude-sonnet-4-5-20250929",
            "metadata": {
                "ccproxy_litellm_model": "claude-sonnet-4-5-20250929",
                "ccproxy_model_config": {"litellm_params": {"api_base": "https://api.anthropic.com"}},
            },
            "proxy_server_request": {"headers": {"user-agent": "claude-cli/1.0.62 (external, cli)"}},
            "secret_fields": {"raw_headers": {"authorization": "Bearer sk-ant-oat01-test-token"}},
        }

        # Patch urlparse to raise an exception
        with patch("ccproxy.hooks.urlparse", side_effect=Exception("URL parse error")):
            result = forward_oauth(data, user_api_key_dict)

        # Should not forward OAuth token when URL parsing fails
        assert "provider_specific_header" not in result

    def test_forward_oauth_no_anthropic_conditions_met(self, user_api_key_dict):
        """Test OAuth forwarding when none of the Anthropic conditions are met."""
        # This test specifically hits the `else: is_anthropic_provider = False` branch
        # Conditions: no api_base, custom_provider != "anthropic", model doesn't start with "anthropic/" or "claude"
        data = {
            "model": "gpt-4",
            "metadata": {
                "ccproxy_litellm_model": "gpt-4",  # Does not start with "anthropic/" or "claude"
                "ccproxy_model_config": {
                    "litellm_params": {
                        # No api_base
                        "custom_llm_provider": "openai"  # Not "anthropic"
                    }
                },
            },
            "proxy_server_request": {"headers": {"user-agent": "claude-cli/1.0.62 (external, cli)"}},
            "secret_fields": {"raw_headers": {"authorization": "Bearer sk-ant-oat01-test-token"}},
        }

        result = forward_oauth(data, user_api_key_dict)

        # Should not forward OAuth token since none of the Anthropic conditions are met
        # This covers the `else: is_anthropic_provider = False` branch (line 129)
        assert "provider_specific_header" not in result

    def test_forward_oauth_none_model_config(self, user_api_key_dict):
        """Test forward_oauth handles None model_config (passthrough mode)."""
        data = {
            "model": "claude-sonnet-4-5-20250929",
            "proxy_server_request": {"headers": {"user-agent": "claude-cli/1.0.0"}},
            "metadata": {
                "ccproxy_litellm_model": "claude-sonnet-4-5-20250929",
                "ccproxy_model_config": None,  # This happens in passthrough mode
            },
            "secret_fields": {"raw_headers": {"authorization": "Bearer sk-ant-api03-test"}},
        }

        # Should not crash and should work for anthropic models
        result = forward_oauth(data, user_api_key_dict)

        # Should forward OAuth for anthropic models even with None config
        assert "provider_specific_header" in result
        assert result["provider_specific_header"]["extra_headers"]["authorization"] == "Bearer sk-ant-api03-test"


class TestForwardOAuthWithCredentialsFallback:
    """Test forward_oauth hook with cached credentials fallback."""

    def test_oauth_uses_header_when_present(self, user_api_key_dict):
        """Test that existing authorization header takes precedence over cached credentials."""
        from ccproxy.config import CCProxyConfig, set_config_instance
        from ccproxy.hooks import forward_oauth

        # Set up config with credentials already cached
        config = CCProxyConfig(credentials=None)
        config._credentials_value = "fallback-token"
        set_config_instance(config)

        data = {
            "model": "claude-sonnet-4-5-20250929",
            "proxy_server_request": {"headers": {"user-agent": "claude-cli/1.0.0"}},
            "metadata": {
                "ccproxy_litellm_model": "claude-sonnet-4-5-20250929",
                "ccproxy_model_config": {
                    "litellm_params": {"model": "claude-sonnet-4-5-20250929", "api_base": "https://api.anthropic.com"}
                },
            },
            "secret_fields": {"raw_headers": {"authorization": "Bearer header-token"}},
        }

        result = forward_oauth(data, user_api_key_dict)

        # Should use header token, not cached credentials
        assert result["provider_specific_header"]["extra_headers"]["authorization"] == "Bearer header-token"

    def test_oauth_uses_cached_credentials_fallback(self, user_api_key_dict):
        """Test that cached credentials are used when no authorization header present."""
        from ccproxy.config import CCProxyConfig, set_config_instance
        from ccproxy.hooks import forward_oauth

        # Set up config with credentials already cached
        config = CCProxyConfig(credentials=None)
        config._credentials_value = "cached-token-456"
        set_config_instance(config)

        data = {
            "model": "claude-sonnet-4-5-20250929",
            "proxy_server_request": {"headers": {"user-agent": "claude-cli/1.0.0"}},
            "metadata": {
                "ccproxy_litellm_model": "claude-sonnet-4-5-20250929",
                "ccproxy_model_config": {
                    "litellm_params": {"model": "claude-sonnet-4-5-20250929", "api_base": "https://api.anthropic.com"}
                },
            },
            "secret_fields": {
                "raw_headers": {}  # No authorization header
            },
        }

        result = forward_oauth(data, user_api_key_dict)

        # Should use cached credentials with Bearer prefix added
        assert result["provider_specific_header"]["extra_headers"]["authorization"] == "Bearer cached-token-456"

    def test_oauth_cached_credentials_bearer_prefix(self, user_api_key_dict):
        """Test that Bearer prefix is added if not present in cached credentials."""
        from ccproxy.config import CCProxyConfig, set_config_instance
        from ccproxy.hooks import forward_oauth

        # Set up config with credentials that already include Bearer
        config = CCProxyConfig(credentials=None)
        config._credentials_value = "Bearer already-prefixed-token"
        set_config_instance(config)

        data = {
            "model": "claude-sonnet-4-5-20250929",
            "proxy_server_request": {"headers": {"user-agent": "claude-cli/1.0.0"}},
            "metadata": {
                "ccproxy_litellm_model": "claude-sonnet-4-5-20250929",
                "ccproxy_model_config": {
                    "litellm_params": {"model": "claude-sonnet-4-5-20250929", "api_base": "https://api.anthropic.com"}
                },
            },
            "secret_fields": {"raw_headers": {}},
        }

        result = forward_oauth(data, user_api_key_dict)

        # Should not double-prefix Bearer
        assert result["provider_specific_header"]["extra_headers"]["authorization"] == "Bearer already-prefixed-token"

    def test_oauth_no_fallback_when_not_configured(self, user_api_key_dict):
        """Test that no fallback occurs when credentials not configured."""
        from ccproxy.config import CCProxyConfig, set_config_instance
        from ccproxy.hooks import forward_oauth

        # Set up config without credentials
        config = CCProxyConfig(credentials=None)
        set_config_instance(config)

        data = {
            "model": "claude-sonnet-4-5-20250929",
            "proxy_server_request": {"headers": {"user-agent": "claude-cli/1.0.0"}},
            "metadata": {
                "ccproxy_litellm_model": "claude-sonnet-4-5-20250929",
                "ccproxy_model_config": {
                    "litellm_params": {"model": "claude-sonnet-4-5-20250929", "api_base": "https://api.anthropic.com"}
                },
            },
            "secret_fields": {"raw_headers": {}},
        }

        result = forward_oauth(data, user_api_key_dict)

        # Should not add any authorization header
        if "provider_specific_header" in result:
            assert "authorization" not in result["provider_specific_header"].get("extra_headers", {})


class TestForwardApiKey:
    """Test the forward_apikey hook function."""

    def test_apikey_forwards_header(self, user_api_key_dict):
        """Test that x-api-key header is forwarded from request."""

        data = {
            "model": "gpt-4",
            "proxy_server_request": {"headers": {"content-type": "application/json"}},
            "secret_fields": {"raw_headers": {"x-api-key": "sk-test-api-key-123"}},
        }

        result = forward_apikey(data, user_api_key_dict)

        assert "provider_specific_header" in result
        assert result["provider_specific_header"]["extra_headers"]["x-api-key"] == "sk-test-api-key-123"

    def test_apikey_no_proxy_request(self, user_api_key_dict):
        """Test that hook handles missing proxy_server_request gracefully."""

        data = {"model": "gpt-4", "secret_fields": {"raw_headers": {"x-api-key": "sk-test-key"}}}

        result = forward_apikey(data, user_api_key_dict)

        # Should return data unchanged
        assert result == data

    def test_apikey_missing_header(self, user_api_key_dict):
        """Test that hook handles missing x-api-key header gracefully."""

        data = {
            "model": "gpt-4",
            "proxy_server_request": {"headers": {"content-type": "application/json"}},
            "secret_fields": {
                "raw_headers": {}  # No x-api-key header
            },
        }

        result = forward_apikey(data, user_api_key_dict)

        # Should not add any x-api-key header
        if "provider_specific_header" in result:
            assert "x-api-key" not in result["provider_specific_header"].get("extra_headers", {})
