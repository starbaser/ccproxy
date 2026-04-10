"""Tests for ccproxy patches."""

from __future__ import annotations

from unittest.mock import MagicMock, patch


class TestBetaHeadersPatch:
    def setup_method(self):
        import ccproxy.patches.beta_headers as mod
        mod._applied = False

    def test_apply_patches_beta_filter(self):
        import litellm.anthropic_beta_headers_manager as mgr

        from ccproxy.patches.beta_headers import apply

        mock_handler = MagicMock()
        apply(mock_handler)

        # The patched function should inject ccproxy headers
        result = mgr._load_beta_headers_config()
        from ccproxy.constants import ANTHROPIC_BETA_HEADERS
        for header in ANTHROPIC_BETA_HEADERS:
            assert header in result.get("anthropic", {}), f"Missing header: {header}"

    def test_apply_idempotent(self):
        from ccproxy.patches.beta_headers import apply

        mock_handler = MagicMock()
        apply(mock_handler)
        apply(mock_handler)  # Second call should be no-op

        import ccproxy.patches.beta_headers as mod
        assert mod._applied is True

    def test_existing_headers_preserved(self):
        import litellm.anthropic_beta_headers_manager as mgr

        from ccproxy.patches.beta_headers import apply

        mock_handler = MagicMock()
        # Pre-patch: inject a custom header into the current config
        orig = mgr._load_beta_headers_config
        def orig_with_custom():
            result = orig()
            result.setdefault("anthropic", {})["custom-beta-2025"] = "custom-beta-2025"
            return result
        mgr._load_beta_headers_config = orig_with_custom

        try:
            apply(mock_handler)
            result = mgr._load_beta_headers_config()
            assert "custom-beta-2025" in result.get("anthropic", {})
        finally:
            mgr._load_beta_headers_config = orig
            import ccproxy.patches.beta_headers as mod
            mod._applied = False


class TestPassthroughPatch:
    def setup_method(self):
        import ccproxy.patches.passthrough as mod
        mod._applied = False
        mod._oauth_providers.clear()

    def teardown_method(self):
        import ccproxy.patches.passthrough as mod
        mod._applied = False
        mod._oauth_providers.clear()

    def test_apply_patches_get_credentials(self):
        from ccproxy.patches.passthrough import apply

        mock_handler = MagicMock()
        mock_config = MagicMock()
        mock_config.get_oauth_token.return_value = "test-token"

        with patch("ccproxy.patches.passthrough.get_config", return_value=mock_config):
            apply(mock_handler)

        # The method should now be replaced by the patched version
        import ccproxy.patches.passthrough as mod
        assert mod._applied is True

    def test_apply_idempotent(self):
        from ccproxy.patches.passthrough import apply

        mock_handler = MagicMock()
        mock_config = MagicMock()
        mock_config.get_oauth_token.return_value = None

        with patch("ccproxy.patches.passthrough.get_config", return_value=mock_config):
            apply(mock_handler)
            apply(mock_handler)

        import ccproxy.patches.passthrough as mod
        assert mod._applied is True

    def test_get_credentials_falls_back_to_oauth(self):
        """When original get_credentials returns None, falls back to oat_sources."""
        import ccproxy.patches.passthrough as mod
        from ccproxy.patches.passthrough import _patch_get_credentials

        mock_config = MagicMock()
        mock_config.get_oauth_token.return_value = "my-oauth-token"

        from litellm.proxy.pass_through_endpoints.passthrough_endpoint_router import PassthroughEndpointRouter
        saved = PassthroughEndpointRouter.get_credentials

        # Stub original to return None
        PassthroughEndpointRouter.get_credentials = lambda self, provider, region: None

        try:
            with patch("ccproxy.patches.passthrough.get_config", return_value=mock_config):
                _patch_get_credentials()

            router = PassthroughEndpointRouter()
            result = router.get_credentials("gemini", None)
            assert result == "my-oauth-token"
            assert "gemini" in mod._oauth_providers
        finally:
            PassthroughEndpointRouter.get_credentials = saved

    def test_get_credentials_returns_original_when_available(self):
        """When original get_credentials has a result, it returns that."""
        import ccproxy.patches.passthrough as mod
        from ccproxy.patches.passthrough import _patch_get_credentials

        mock_config = MagicMock()
        mock_config.get_oauth_token.return_value = "oauth-token"

        from litellm.proxy.pass_through_endpoints.passthrough_endpoint_router import PassthroughEndpointRouter
        saved = PassthroughEndpointRouter.get_credentials

        # Stub original to return a credential
        PassthroughEndpointRouter.get_credentials = lambda self, provider, region: "api-key-123"

        try:
            with patch("ccproxy.patches.passthrough.get_config", return_value=mock_config):
                _patch_get_credentials()

            router = PassthroughEndpointRouter()
            result = router.get_credentials("gemini", None)
            assert result == "api-key-123"
            # Provider should NOT be in oauth set since original returned a result
            assert "gemini" not in mod._oauth_providers
        finally:
            PassthroughEndpointRouter.get_credentials = saved

    def test_get_credentials_no_oauth_token_returns_none(self):
        """When original returns None and no OAuth token, returns None."""
        import ccproxy.patches.passthrough as mod
        from ccproxy.patches.passthrough import _patch_get_credentials

        mock_config = MagicMock()
        mock_config.get_oauth_token.return_value = None

        from litellm.proxy.pass_through_endpoints.passthrough_endpoint_router import PassthroughEndpointRouter
        saved = PassthroughEndpointRouter.get_credentials

        PassthroughEndpointRouter.get_credentials = lambda self, provider, region: None

        try:
            with patch("ccproxy.patches.passthrough.get_config", return_value=mock_config):
                _patch_get_credentials()

            router = PassthroughEndpointRouter()
            result = router.get_credentials("openai", None)
            assert result is None
            assert "openai" not in mod._oauth_providers
        finally:
            PassthroughEndpointRouter.get_credentials = saved

    def test_bearer_auth_patch(self):
        """Test _patch_bearer_auth replaces pass_through_request."""
        from litellm.proxy.pass_through_endpoints import pass_through_endpoints as pt_module

        from ccproxy.patches.passthrough import _patch_bearer_auth

        original = pt_module.pass_through_request
        try:
            _patch_bearer_auth()
            assert pt_module.pass_through_request is not original
        finally:
            pt_module.pass_through_request = original

    async def test_bearer_auth_moves_key_to_header(self):
        """Test that Bearer auth patch moves OAuth token from ?key= to Authorization."""
        import ccproxy.patches.passthrough as mod
        mod._oauth_providers.add("gemini")

        from litellm.proxy.pass_through_endpoints import pass_through_endpoints as pt_module

        from ccproxy.patches.passthrough import _patch_bearer_auth

        captured_headers = {}

        async def mock_original(request, target, custom_headers, user_api_key_dict, **kwargs):
            captured_headers.update(custom_headers)
            return MagicMock()

        original = pt_module.pass_through_request
        pt_module.pass_through_request = mock_original

        try:
            _patch_bearer_auth()

            request = MagicMock()
            custom_headers: dict = {}
            query_params = {"key": "my-oauth-token"}

            await pt_module.pass_through_request(
                request,
                "https://generativelanguage.googleapis.com/v1/models",
                custom_headers,
                {},
                query_params=query_params,
                custom_llm_provider="gemini",
            )

            assert captured_headers.get("Authorization") == "Bearer my-oauth-token"
            assert "key" not in query_params
        finally:
            pt_module.pass_through_request = original
            mod._oauth_providers.discard("gemini")
