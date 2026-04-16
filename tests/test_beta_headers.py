"""Test anthropic-beta header injection for Claude Code impersonation."""

from unittest.mock import MagicMock, patch

import pytest

from ccproxy.config import clear_config_instance
from ccproxy.hooks import ANTHROPIC_BETA_HEADERS, add_beta_headers
from ccproxy.router import clear_router


@pytest.fixture
def cleanup():
    """Clean up config and router after each test."""
    yield
    clear_config_instance()
    clear_router()


@pytest.fixture
def anthropic_model_data():
    """Request data routed to an Anthropic model."""
    return {
        "model": "anthropic/claude-sonnet-4-5-20250929",
        "messages": [{"role": "user", "content": "test"}],
        "metadata": {
            "ccproxy_litellm_model": "anthropic/claude-sonnet-4-5-20250929",
            "ccproxy_model_config": {
                "litellm_params": {
                    "model": "anthropic/claude-sonnet-4-5-20250929",
                    "api_base": "https://api.anthropic.com",
                },
            },
        },
        "provider_specific_header": {"extra_headers": {}},
        "proxy_server_request": {"headers": {"user-agent": "claude-cli/1.0.62"}},
    }


@pytest.fixture
def openai_model_data():
    """Request data routed to an OpenAI model."""
    return {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "test"}],
        "metadata": {
            "ccproxy_litellm_model": "gpt-4o",
            "ccproxy_model_config": {
                "litellm_params": {
                    "model": "gpt-4o",
                    "api_base": "https://api.openai.com",
                },
            },
        },
        "provider_specific_header": {"extra_headers": {}},
        "proxy_server_request": {"headers": {"user-agent": "claude-cli/1.0.62"}},
    }


class TestAddBetaHeaders:
    """Tests for the add_beta_headers hook."""

    def test_adds_beta_headers_for_anthropic(self, anthropic_model_data, cleanup):
        """Verify all required beta headers are added for Anthropic provider."""
        result = add_beta_headers(anthropic_model_data, {})

        assert "provider_specific_header" in result
        assert "extra_headers" in result["provider_specific_header"]

        beta_header = result["provider_specific_header"]["extra_headers"]["anthropic-beta"]
        beta_values = [b.strip() for b in beta_header.split(",")]

        for expected in ANTHROPIC_BETA_HEADERS:
            assert expected in beta_values, f"Missing beta header: {expected}"

    def test_skips_non_anthropic_providers(self, openai_model_data, cleanup):
        """Verify no headers added for non-Anthropic providers."""
        result = add_beta_headers(openai_model_data, {})

        extra_headers = result.get("provider_specific_header", {}).get("extra_headers", {})
        assert "anthropic-beta" not in extra_headers

    def test_merges_with_existing_beta_headers(self, anthropic_model_data, cleanup):
        """Verify existing beta headers are preserved and merged."""
        existing_beta = "some-custom-beta-2025"
        anthropic_model_data["provider_specific_header"]["extra_headers"]["anthropic-beta"] = (
            existing_beta
        )

        result = add_beta_headers(anthropic_model_data, {})

        beta_header = result["provider_specific_header"]["extra_headers"]["anthropic-beta"]
        beta_values = [b.strip() for b in beta_header.split(",")]

        # All required headers present
        for expected in ANTHROPIC_BETA_HEADERS:
            assert expected in beta_values

        # Original custom header preserved
        assert existing_beta in beta_values

    def test_deduplicates_beta_headers(self, anthropic_model_data, cleanup):
        """Verify duplicate beta headers are removed."""
        # Pre-populate with a header that will be added by the hook
        anthropic_model_data["provider_specific_header"]["extra_headers"]["anthropic-beta"] = (
            "oauth-2025-04-20"
        )

        result = add_beta_headers(anthropic_model_data, {})

        beta_header = result["provider_specific_header"]["extra_headers"]["anthropic-beta"]
        beta_values = [b.strip() for b in beta_header.split(",")]

        # Should only appear once
        assert beta_values.count("oauth-2025-04-20") == 1

    def test_skips_when_no_routed_model(self, cleanup):
        """Verify hook skips gracefully when no routed model in metadata."""
        data = {
            "model": "anthropic/claude-sonnet-4-5-20250929",
            "messages": [{"role": "user", "content": "test"}],
            "metadata": {},
            "provider_specific_header": {"extra_headers": {}},
        }

        result = add_beta_headers(data, {})

        extra_headers = result.get("provider_specific_header", {}).get("extra_headers", {})
        assert "anthropic-beta" not in extra_headers

    def test_creates_header_structure_if_missing(self, cleanup):
        """Verify hook creates provider_specific_header structure if missing."""
        data = {
            "model": "anthropic/claude-sonnet-4-5-20250929",
            "messages": [{"role": "user", "content": "test"}],
            "metadata": {
                "ccproxy_litellm_model": "anthropic/claude-sonnet-4-5-20250929",
                "ccproxy_model_config": {
                    "litellm_params": {"model": "anthropic/claude-sonnet-4-5-20250929"},
                },
            },
        }

        result = add_beta_headers(data, {})

        assert "provider_specific_header" in result
        assert "extra_headers" in result["provider_specific_header"]
        assert "anthropic-beta" in result["provider_specific_header"]["extra_headers"]

    def test_handles_none_model_config(self, cleanup):
        """Verify hook handles None model_config gracefully (passthrough mode)."""
        data = {
            "model": "anthropic/claude-sonnet-4-5-20250929",
            "messages": [{"role": "user", "content": "test"}],
            "metadata": {
                "ccproxy_litellm_model": "anthropic/claude-sonnet-4-5-20250929",
                "ccproxy_model_config": None,
            },
            "provider_specific_header": {"extra_headers": {}},
        }

        result = add_beta_headers(data, {})

        # Should still add headers since we have a routed model
        beta_header = result["provider_specific_header"]["extra_headers"]["anthropic-beta"]
        assert "oauth-2025-04-20" in beta_header
