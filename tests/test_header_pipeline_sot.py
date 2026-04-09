"""Test pipeline as single source of truth for outgoing headers.

Verifies that provider_specific_header["extra_headers"] set by the hook pipeline
are back-propagated into proxy_server_request.headers via Context.to_litellm_data(),
making the pipeline authoritative across all LiteLLM merge paths.
"""

import time
from unittest.mock import MagicMock, patch

import pytest

from ccproxy.config import CCProxyConfig, clear_config_instance, set_config_instance
from ccproxy.constants import ANTHROPIC_BETA_HEADERS
from ccproxy.handler import CCProxyHandler
from ccproxy.hooks.add_beta_headers import add_beta_headers
from ccproxy.pipeline.context import Context
from ccproxy.router import clear_router


@pytest.fixture
def pipeline_handler():
    """Handler with OAuth + beta hooks, fake OAuth token, and one Anthropic model."""
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
    mock_proxy_server.llm_router.get_model_list.return_value = mock_proxy_server.llm_router.model_list

    mock_module = MagicMock()
    mock_module.proxy_server = mock_proxy_server

    config = CCProxyConfig(
        debug=False,
        default_model_passthrough=False,
        hooks=[
            "ccproxy.hooks.rule_evaluator",
            "ccproxy.hooks.model_router",
            "ccproxy.hooks.forward_oauth",
            "ccproxy.hooks.add_beta_headers",
        ],
        rules=[],
    )
    config._oat_values["anthropic"] = ("fake-oauth-token-abc123", time.time())
    set_config_instance(config)

    with patch.dict("sys.modules", {"litellm.proxy": mock_module}):
        clear_router()
        handler = CCProxyHandler()
        yield handler

    clear_config_instance()
    clear_router()


def _sentinel_request_data() -> dict:
    """Request with sentinel key as x-api-key (Anthropic SDK client pattern)."""
    return {
        "model": "default",
        "messages": [{"role": "user", "content": "test"}],
        "metadata": {},
        "provider_specific_header": {"extra_headers": {}},
        "proxy_server_request": {
            "headers": {
                "x-api-key": "sk-ant-oat-ccproxy-anthropic",
                "user-agent": "claude-cli/1.0.62 (external, cli)",
                "x-custom-trace": "abc-123",
            },
        },
        "secret_fields": {
            "raw_headers": {
                "x-api-key": "sk-ant-oat-ccproxy-anthropic",
            },
        },
    }


class TestHeaderBackPropagation:
    """Verify pipeline headers are propagated to proxy_server_request.headers."""

    @pytest.mark.asyncio
    async def test_sentinel_removed_from_proxy_headers(self, pipeline_handler):
        """x-api-key sentinel is overwritten in proxy_server_request.headers."""
        data = _sentinel_request_data()
        result = await pipeline_handler.async_pre_call_hook(data, {})

        proxy_hdrs = result["proxy_server_request"]["headers"]
        assert proxy_hdrs["x-api-key"] == ""

    @pytest.mark.asyncio
    async def test_pipeline_headers_propagate_to_proxy_headers(self, pipeline_handler):
        """authorization from pipeline appears in proxy_server_request.headers."""
        data = _sentinel_request_data()
        result = await pipeline_handler.async_pre_call_hook(data, {})

        proxy_hdrs = result["proxy_server_request"]["headers"]
        assert proxy_hdrs["authorization"] == "Bearer fake-oauth-token-abc123"

    @pytest.mark.asyncio
    async def test_unknown_client_headers_pass_through(self, pipeline_handler):
        """Custom headers the pipeline didn't touch survive unchanged."""
        data = _sentinel_request_data()
        result = await pipeline_handler.async_pre_call_hook(data, {})

        proxy_hdrs = result["proxy_server_request"]["headers"]
        assert proxy_hdrs["x-custom-trace"] == "abc-123"

    @pytest.mark.asyncio
    async def test_client_beta_merged(self, pipeline_handler):
        """Client-forwarded anthropic-beta is merged with ANTHROPIC_BETA_HEADERS."""
        data = _sentinel_request_data()
        data["proxy_server_request"]["headers"]["anthropic-beta"] = "custom-beta-2025"

        result = await pipeline_handler.async_pre_call_hook(data, {})

        beta_header = result["provider_specific_header"]["extra_headers"]["anthropic-beta"]
        beta_values = [b.strip() for b in beta_header.split(",")]

        for expected in ANTHROPIC_BETA_HEADERS:
            assert expected in beta_values, f"Missing required beta: {expected}"
        assert "custom-beta-2025" in beta_values, "Client beta was dropped"

    def test_context_propagation_unit(self):
        """Pure unit test: from_litellm_data → set extra_headers → to_litellm_data."""
        data = {
            "model": "test-model",
            "messages": [],
            "metadata": {},
            "provider_specific_header": {"extra_headers": {}},
            "proxy_server_request": {
                "headers": {
                    "X-Api-Key": "original-key",
                    "x-custom": "keep-me",
                },
            },
        }

        ctx = Context.from_litellm_data(data)
        ctx.set_provider_header("x-api-key", "")
        ctx.set_provider_header("authorization", "Bearer new-token")
        result = ctx.to_litellm_data()

        proxy_hdrs = result["proxy_server_request"]["headers"]
        assert proxy_hdrs["x-api-key"] == ""
        assert proxy_hdrs["authorization"] == "Bearer new-token"
        assert proxy_hdrs["x-custom"] == "keep-me"
        # Original mixed-case key should be replaced
        assert "X-Api-Key" not in proxy_hdrs


class TestClientBetaMerge:
    """Verify client anthropic-beta headers merge into add_beta_headers hook."""

    def _call_hook(self, data: dict) -> dict:
        ctx = Context.from_litellm_data(data)
        result_ctx = add_beta_headers(ctx, {})
        return result_ctx.to_litellm_data()

    def test_client_beta_from_headers(self):
        """Client anthropic-beta in proxy_server_request.headers gets merged."""
        data = {
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
            "proxy_server_request": {
                "headers": {
                    "anthropic-beta": "client-feature-2025",
                    "user-agent": "claude-cli/1.0.62",
                },
            },
        }

        result = self._call_hook(data)

        beta_header = result["provider_specific_header"]["extra_headers"]["anthropic-beta"]
        beta_values = [b.strip() for b in beta_header.split(",")]

        for expected in ANTHROPIC_BETA_HEADERS:
            assert expected in beta_values
        assert "client-feature-2025" in beta_values

    def test_client_beta_deduplicates(self):
        """Client beta that duplicates a constant beta is deduplicated."""
        data = {
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
            "proxy_server_request": {
                "headers": {
                    "anthropic-beta": "oauth-2025-04-20",
                    "user-agent": "claude-cli/1.0.62",
                },
            },
        }

        result = self._call_hook(data)

        beta_header = result["provider_specific_header"]["extra_headers"]["anthropic-beta"]
        beta_values = [b.strip() for b in beta_header.split(",")]

        assert beta_values.count("oauth-2025-04-20") == 1
