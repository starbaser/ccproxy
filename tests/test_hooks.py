"""Comprehensive tests for ccproxy hooks."""

import logging
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ccproxy.classifier import RequestClassifier
from ccproxy.config import clear_config_instance
from ccproxy.hooks import capture_headers, forward_apikey, forward_oauth, model_router, rule_evaluator
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
        data = {
            "model": "claude-sonnet-4-5-20250929",
            "metadata": {"ccproxy_litellm_model": "claude-sonnet-4-5-20250929"},
        }

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
    """Test forward_oauth hook with cached credentials fallback via oat_sources."""

    def test_oauth_uses_header_when_present(self, user_api_key_dict):
        """Test that existing authorization header takes precedence over cached credentials."""
        from ccproxy.config import CCProxyConfig, set_config_instance
        from ccproxy.hooks import forward_oauth

        # Set up config with oat_sources for anthropic
        config = CCProxyConfig(oat_sources={"anthropic": "echo fallback-token"})
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

        # Set up config with oat_sources for anthropic
        config = CCProxyConfig(oat_sources={"anthropic": "echo cached-token-456"})
        config._load_credentials()  # Load the OAuth tokens
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
        config = CCProxyConfig(oat_sources={"anthropic": "echo 'Bearer already-prefixed-token'"})
        config._load_credentials()  # Load the OAuth tokens
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


class TestCaptureHeadersHook:
    """Test the capture_headers hook function.

    The capture_headers hook outputs to metadata["trace_metadata"] for LangFuse compatibility.
    Headers are stored as "header_{name}" keys, plus "http_method" and "http_path".
    """

    def _get_trace_metadata(self, result: dict) -> dict[str, Any]:
        """Extract trace_metadata from result data."""
        return result.get("metadata", {}).get("trace_metadata", {})

    def _get_headers(self, result: dict) -> dict[str, str]:
        """Helper to extract header values into a dict for easier assertions."""
        trace_metadata = self._get_trace_metadata(result)
        headers = {}
        for key, value in trace_metadata.items():
            if key.startswith("header_"):
                header_name = key[7:]  # Remove "header_" prefix
                headers[header_name] = value
        return headers

    def test_basic_header_capture_all_headers(self, user_api_key_dict):
        """Test capturing all headers when no filter is provided."""
        data = {
            "model": "claude-sonnet-4-5-20250929",
            "proxy_server_request": {
                "headers": {
                    "content-type": "application/json",
                    "user-agent": "claude-cli/1.0.0",
                    "x-custom-header": "custom-value",
                },
                "method": "POST",
                "url": "https://api.anthropic.com/v1/messages",
            },
        }

        result = capture_headers(data, user_api_key_dict)

        assert "metadata" in result
        assert "trace_metadata" in result["metadata"]

        headers = self._get_headers(result)
        trace_meta = self._get_trace_metadata(result)
        assert headers["content-type"] == "application/json"
        assert headers["user-agent"] == "claude-cli/1.0.0"
        assert headers["x-custom-header"] == "custom-value"
        assert trace_meta["http_method"] == "POST"
        assert trace_meta["http_path"] == "/v1/messages"

    def test_header_filtering(self, user_api_key_dict):
        """Test capturing only specified headers with filter."""
        data = {
            "model": "claude-sonnet-4-5-20250929",
            "proxy_server_request": {
                "headers": {
                    "content-type": "application/json",
                    "user-agent": "claude-cli/1.0.0",
                    "x-custom-header": "custom-value",
                },
                "method": "POST",
                "url": "https://api.anthropic.com/v1/messages",
            },
        }

        result = capture_headers(data, user_api_key_dict, headers=["content-type", "user-agent"])

        headers = self._get_headers(result)
        assert headers["content-type"] == "application/json"
        assert headers["user-agent"] == "claude-cli/1.0.0"
        assert "x-custom-header" not in headers

    def test_header_filtering_case_insensitive(self, user_api_key_dict):
        """Test header filtering is case-insensitive."""
        data = {
            "model": "claude-sonnet-4-5-20250929",
            "proxy_server_request": {
                "headers": {
                    "Content-Type": "application/json",
                    "User-Agent": "claude-cli/1.0.0",
                },
                "method": "POST",
            },
        }

        result = capture_headers(data, user_api_key_dict, headers=["content-type", "user-agent"])

        headers = self._get_headers(result)
        assert "content-type" in headers
        assert "user-agent" in headers

    def test_authorization_header_redaction(self, user_api_key_dict):
        """Test authorization header is redacted properly."""

        class MockSecretFields:
            def __init__(self):
                self.raw_headers = {"authorization": "Bearer sk-ant-oat01-1234567890abcdef"}

        data = {
            "model": "claude-sonnet-4-5-20250929",
            "proxy_server_request": {"headers": {}, "method": "POST"},
            "secret_fields": MockSecretFields(),
        }

        result = capture_headers(data, user_api_key_dict)

        headers = self._get_headers(result)
        auth_value = headers["authorization"]
        assert auth_value.startswith("Bearer sk-ant-")
        assert auth_value.endswith("cdef")
        assert "..." in auth_value
        assert "1234567890ab" not in auth_value

    def test_authorization_header_redaction_no_prefix(self, user_api_key_dict):
        """Test authorization header redaction when no standard prefix."""

        class MockSecretFields:
            def __init__(self):
                self.raw_headers = {"authorization": "custom-token-1234567890"}

        data = {
            "model": "claude-sonnet-4-5-20250929",
            "proxy_server_request": {"headers": {}, "method": "POST"},
            "secret_fields": MockSecretFields(),
        }

        result = capture_headers(data, user_api_key_dict)

        headers = self._get_headers(result)
        auth_value = headers["authorization"]
        assert "..." in auth_value
        assert auth_value.endswith("7890")

    def test_x_api_key_redaction(self, user_api_key_dict):
        """Test x-api-key header is redacted properly."""

        class MockSecretFields:
            def __init__(self):
                self.raw_headers = {"x-api-key": "sk-openai-1234567890abcdef"}

        data = {
            "model": "gpt-4",
            "proxy_server_request": {"headers": {}, "method": "POST"},
            "secret_fields": MockSecretFields(),
        }

        result = capture_headers(data, user_api_key_dict)

        headers = self._get_headers(result)
        api_key = headers["x-api-key"]
        assert api_key.startswith("sk-openai-")
        assert api_key.endswith("cdef")
        assert "..." in api_key

    def test_cookie_full_redaction(self, user_api_key_dict):
        """Test cookie header is fully redacted."""
        data = {
            "model": "claude-sonnet-4-5-20250929",
            "proxy_server_request": {
                "headers": {"cookie": "session=abc123; user_id=456"},
                "method": "POST",
            },
        }

        result = capture_headers(data, user_api_key_dict)

        headers = self._get_headers(result)
        assert headers["cookie"] == "[REDACTED]"

    def test_missing_headers_handling(self, user_api_key_dict):
        """Test handling of missing or empty headers."""
        data = {
            "model": "claude-sonnet-4-5-20250929",
            "proxy_server_request": {
                "headers": {"empty-header": "", "null-header": None},
                "method": "POST",
            },
        }

        result = capture_headers(data, user_api_key_dict)

        headers = self._get_headers(result)
        assert "empty-header" not in headers
        assert "null-header" not in headers

    def test_metadata_initialization(self, user_api_key_dict):
        """Test metadata is initialized when not present."""
        data = {
            "model": "claude-sonnet-4-5-20250929",
            "proxy_server_request": {"headers": {"content-type": "application/json"}, "method": "POST"},
        }

        result = capture_headers(data, user_api_key_dict)

        assert "metadata" in result
        assert "trace_metadata" in result["metadata"]
        headers = self._get_headers(result)
        assert headers["content-type"] == "application/json"

    def test_existing_metadata_preserved(self, user_api_key_dict):
        """Test existing metadata is preserved."""
        data = {
            "model": "claude-sonnet-4-5-20250929",
            "metadata": {"existing_key": "existing_value"},
            "proxy_server_request": {"headers": {"content-type": "application/json"}, "method": "POST"},
        }

        result = capture_headers(data, user_api_key_dict)

        assert result["metadata"]["existing_key"] == "existing_value"
        assert "trace_metadata" in result["metadata"]

    def test_http_method_capture(self, user_api_key_dict):
        """Test HTTP method is captured correctly."""
        data = {
            "model": "claude-sonnet-4-5-20250929",
            "proxy_server_request": {"headers": {}, "method": "GET"},
        }

        result = capture_headers(data, user_api_key_dict)

        trace_meta = self._get_trace_metadata(result)
        assert trace_meta["http_method"] == "GET"

    def test_http_path_capture(self, user_api_key_dict):
        """Test HTTP path is extracted from URL."""
        data = {
            "model": "claude-sonnet-4-5-20250929",
            "proxy_server_request": {
                "headers": {},
                "method": "POST",
                "url": "https://api.anthropic.com/v1/messages?query=test",
            },
        }

        result = capture_headers(data, user_api_key_dict)

        trace_meta = self._get_trace_metadata(result)
        assert trace_meta["http_path"] == "/v1/messages"

    def test_http_path_empty_url(self, user_api_key_dict):
        """Test HTTP path handling when URL is empty."""
        data = {
            "model": "claude-sonnet-4-5-20250929",
            "proxy_server_request": {"headers": {}, "method": "POST", "url": ""},
        }

        result = capture_headers(data, user_api_key_dict)

        trace_meta = self._get_trace_metadata(result)
        assert "http_path" not in trace_meta

    def test_raw_headers_from_secret_fields(self, user_api_key_dict):
        """Test raw headers from secret_fields are merged."""

        class MockSecretFields:
            def __init__(self):
                self.raw_headers = {"authorization": "Bearer sk-ant-oat01-test1234"}

        data = {
            "model": "claude-sonnet-4-5-20250929",
            "proxy_server_request": {"headers": {"content-type": "application/json"}, "method": "POST"},
            "secret_fields": MockSecretFields(),
        }

        result = capture_headers(data, user_api_key_dict)

        headers = self._get_headers(result)
        assert "content-type" in headers
        assert "authorization" in headers

    def test_raw_headers_priority(self, user_api_key_dict):
        """Test raw headers override regular headers."""

        class MockSecretFields:
            def __init__(self):
                self.raw_headers = {"content-type": "application/json"}

        data = {
            "model": "claude-sonnet-4-5-20250929",
            "proxy_server_request": {"headers": {"content-type": "text/plain"}, "method": "POST"},
            "secret_fields": MockSecretFields(),
        }

        result = capture_headers(data, user_api_key_dict)

        headers = self._get_headers(result)
        assert headers["content-type"] == "application/json"

    def test_no_proxy_server_request(self, user_api_key_dict):
        """Test handling when proxy_server_request is missing."""
        data = {"model": "claude-sonnet-4-5-20250929"}

        result = capture_headers(data, user_api_key_dict)

        assert "metadata" in result
        assert "trace_metadata" in result["metadata"]
        trace_meta = self._get_trace_metadata(result)
        assert trace_meta == {}

    def test_empty_headers_dict(self, user_api_key_dict):
        """Test handling when headers dict is empty."""
        data = {
            "model": "claude-sonnet-4-5-20250929",
            "proxy_server_request": {"headers": {}, "method": "POST"},
        }

        result = capture_headers(data, user_api_key_dict)

        headers = self._get_headers(result)
        assert headers == {}
        trace_meta = self._get_trace_metadata(result)
        assert trace_meta["http_method"] == "POST"

    def test_secret_fields_missing_raw_headers(self, user_api_key_dict):
        """Test handling when secret_fields exists but has no raw_headers."""
        data = {
            "model": "claude-sonnet-4-5-20250929",
            "proxy_server_request": {"headers": {"content-type": "application/json"}, "method": "POST"},
            "secret_fields": {},
        }

        result = capture_headers(data, user_api_key_dict)

        headers = self._get_headers(result)
        assert headers["content-type"] == "application/json"

    def test_secret_fields_with_raw_headers_attribute(self, user_api_key_dict):
        """Test handling when secret_fields is object with raw_headers attribute."""

        class MockSecretFields:
            def __init__(self):
                self.raw_headers = {"authorization": "Bearer sk-ant-test1234"}

        data = {
            "model": "claude-sonnet-4-5-20250929",
            "proxy_server_request": {"headers": {}, "method": "POST"},
            "secret_fields": MockSecretFields(),
        }

        result = capture_headers(data, user_api_key_dict)

        headers = self._get_headers(result)
        assert "authorization" in headers

    def test_secret_fields_raw_headers_none(self, user_api_key_dict):
        """Test handling when raw_headers attribute is None."""

        class MockSecretFields:
            def __init__(self):
                self.raw_headers = None

        data = {
            "model": "claude-sonnet-4-5-20250929",
            "proxy_server_request": {"headers": {"content-type": "application/json"}, "method": "POST"},
            "secret_fields": MockSecretFields(),
        }

        result = capture_headers(data, user_api_key_dict)

        headers = self._get_headers(result)
        assert headers["content-type"] == "application/json"

    def test_long_header_value_truncation(self, user_api_key_dict):
        """Test non-sensitive headers are truncated to 200 chars."""
        long_value = "x" * 300
        data = {
            "model": "claude-sonnet-4-5-20250929",
            "proxy_server_request": {"headers": {"x-long-header": long_value}, "method": "POST"},
        }

        result = capture_headers(data, user_api_key_dict)

        headers = self._get_headers(result)
        assert len(headers["x-long-header"]) == 200
        assert headers["x-long-header"] == "x" * 200

    def test_multiple_headers_with_mixed_filtering(self, user_api_key_dict):
        """Test filtering with mix of allowed and blocked headers."""

        class MockSecretFields:
            def __init__(self):
                self.raw_headers = {"authorization": "Bearer sk-ant-test1234"}

        data = {
            "model": "claude-sonnet-4-5-20250929",
            "proxy_server_request": {
                "headers": {
                    "content-type": "application/json",
                    "user-agent": "claude-cli/1.0.0",
                    "x-custom-1": "value1",
                    "x-custom-2": "value2",
                },
                "method": "POST",
            },
            "secret_fields": MockSecretFields(),
        }

        result = capture_headers(data, user_api_key_dict, headers=["content-type", "authorization"])

        headers = self._get_headers(result)
        assert len(headers) == 2
        assert "content-type" in headers
        assert "authorization" in headers
        assert "user-agent" not in headers
        assert "x-custom-1" not in headers
