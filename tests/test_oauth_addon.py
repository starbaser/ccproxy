"""Tests for OAuthAddon — response-side 401 detect/refresh/replay loop."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccproxy import transport
from ccproxy.inspector.oauth_addon import OAuthAddon


def _make_oauth_flow(
    *,
    provider: str = "anthropic",
    method: str = "POST",
    url: str = "https://api.anthropic.com/v1/messages",
    content: bytes = b'{"model": "claude-3"}',
    status_code: int = 401,
    oauth_injected: bool = True,
) -> MagicMock:
    """Build a minimal mock flow that mimics a forward_oauth-stamped 401 response."""
    flow = MagicMock()
    metadata: dict[str, object] = {"ccproxy.oauth_provider": provider}
    if oauth_injected:
        metadata["ccproxy.oauth_injected"] = True
    flow.metadata = metadata
    flow.request.method = method
    flow.request.pretty_url = url
    flow.request.pretty_host = "api.anthropic.com"
    flow.request.headers = {"authorization": "Bearer old-token"}
    flow.request.content = content
    flow.response = MagicMock()
    flow.response.status_code = status_code
    flow.response.headers = MagicMock()
    flow.response.headers.clear = MagicMock()
    flow.response.headers.add = MagicMock()
    flow.response.headers.multi_items = MagicMock(return_value=[])
    return flow


def _make_mock_client(mock_response: MagicMock) -> tuple[AsyncMock, AsyncMock]:
    """Build a mock httpx.AsyncClient returned by transport.get_client."""
    mock_client = AsyncMock()
    mock_client.request = AsyncMock(return_value=mock_response)
    return mock_client, mock_client.request


class TestResponseEntryPoint:
    """Tests for OAuthAddon.response — the gate that decides whether to retry."""

    @pytest.mark.asyncio
    async def test_noop_when_no_response(self) -> None:
        """Flow with no response object is a no-op."""
        addon = OAuthAddon()
        flow = MagicMock()
        flow.response = None

        await addon.response(flow)

    @pytest.mark.asyncio
    async def test_noop_when_status_is_not_401(self) -> None:
        """200 responses do not trigger a retry, even when oauth_injected is set."""
        addon = OAuthAddon()
        flow = _make_oauth_flow(status_code=200)

        with patch.object(addon, "_retry_with_refreshed_token", new_callable=AsyncMock) as retry:
            await addon.response(flow)

        retry.assert_not_called()

    @pytest.mark.asyncio
    async def test_noop_when_oauth_not_injected(self) -> None:
        """A 401 on a flow ccproxy did not inject into is left alone."""
        addon = OAuthAddon()
        flow = _make_oauth_flow(status_code=401, oauth_injected=False)

        with patch.object(addon, "_retry_with_refreshed_token", new_callable=AsyncMock) as retry:
            await addon.response(flow)

        retry.assert_not_called()

    @pytest.mark.asyncio
    async def test_triggers_retry_on_401_with_oauth_injected(self) -> None:
        """A 401 on a forward_oauth-injected flow triggers _retry_with_refreshed_token."""
        addon = OAuthAddon()
        flow = _make_oauth_flow(status_code=401, oauth_injected=True)

        with patch.object(addon, "_retry_with_refreshed_token", new_callable=AsyncMock) as retry:
            await addon.response(flow)

        retry.assert_awaited_once_with(flow)

    @pytest.mark.asyncio
    async def test_swallows_unexpected_retry_exception(self) -> None:
        """Unexpected exceptions raised during retry are caught and logged."""
        addon = OAuthAddon()
        flow = _make_oauth_flow()

        with patch.object(
            addon,
            "_retry_with_refreshed_token",
            new_callable=AsyncMock,
            side_effect=RuntimeError("kaboom"),
        ):
            # Should not propagate
            await addon.response(flow)


class TestRetryWithRefreshedToken:
    """Tests for OAuthAddon._retry_with_refreshed_token."""

    @pytest.mark.asyncio
    async def test_returns_false_when_no_provider(self) -> None:
        """Flow without ccproxy.oauth_provider metadata returns False immediately."""
        flow = MagicMock()
        flow.metadata = {}

        addon = OAuthAddon()
        result = await addon._retry_with_refreshed_token(flow)
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_empty_provider(self) -> None:
        """Empty provider string returns False without touching the config."""
        flow = MagicMock()
        flow.metadata = {"ccproxy.oauth_provider": ""}

        addon = OAuthAddon()
        result = await addon._retry_with_refreshed_token(flow)
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_no_token_available(self) -> None:
        """If resolve_oauth_token returns None — token resolution failed — returns False."""
        flow = _make_oauth_flow(provider="anthropic")
        mock_config = MagicMock()
        mock_config.resolve_oauth_token.return_value = None

        with patch("ccproxy.inspector.oauth_addon.get_config", return_value=mock_config):
            addon = OAuthAddon()
            result = await addon._retry_with_refreshed_token(flow)

        assert result is False

    @pytest.mark.asyncio
    async def test_retries_with_new_token_and_returns_true(self) -> None:
        """401 with a refreshed token issues an httpx retry and returns True."""
        flow = _make_oauth_flow(provider="anthropic")
        mock_config = MagicMock()
        mock_config.resolve_oauth_token.return_value = "new-token"
        mock_config.get_auth_header.return_value = None
        mock_config.provider_timeout = None

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers.multi_items.return_value = [("content-type", "application/json")]
        mock_response.content = b'{"id": "msg-1"}'
        mock_client, mock_request = _make_mock_client(mock_response)

        with (
            patch("ccproxy.inspector.oauth_addon.get_config", return_value=mock_config),
            patch("ccproxy.inspector.oauth_addon.transport.get_client", new=AsyncMock(return_value=mock_client)),
        ):
            addon = OAuthAddon()
            result = await addon._retry_with_refreshed_token(flow)

        assert result is True
        mock_request.assert_called_once()
        call_kwargs = mock_request.call_args.kwargs
        assert call_kwargs["method"] == "POST"
        assert call_kwargs["url"] == "https://api.anthropic.com/v1/messages"

    @pytest.mark.asyncio
    async def test_retry_preserves_request_body_and_method(self) -> None:
        """Retry forwards the original method and body verbatim."""
        flow = _make_oauth_flow(
            provider="anthropic",
            method="PUT",
            content=b'{"model": "claude-3", "messages": [{"role": "user", "content": "hi"}]}',
        )
        mock_config = MagicMock()
        mock_config.resolve_oauth_token.return_value = "new-token"
        mock_config.get_auth_header.return_value = None
        mock_config.provider_timeout = None

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers.multi_items.return_value = []
        mock_response.content = b"{}"
        mock_client, mock_request = _make_mock_client(mock_response)

        with (
            patch("ccproxy.inspector.oauth_addon.get_config", return_value=mock_config),
            patch("ccproxy.inspector.oauth_addon.transport.get_client", new=AsyncMock(return_value=mock_client)),
        ):
            addon = OAuthAddon()
            await addon._retry_with_refreshed_token(flow)

        call_kwargs = mock_request.call_args.kwargs
        assert call_kwargs["method"] == "PUT"
        assert call_kwargs["content"] == b'{"model": "claude-3", "messages": [{"role": "user", "content": "hi"}]}'

    @pytest.mark.asyncio
    async def test_retry_uses_custom_auth_header(self) -> None:
        """When get_auth_header returns a custom header name, it is used for the new token."""
        flow = _make_oauth_flow(provider="gemini")
        flow.request.pretty_host = "gemini.googleapis.com"
        mock_config = MagicMock()
        mock_config.resolve_oauth_token.return_value = "new-gemini-token"
        mock_config.get_auth_header.return_value = "x-api-key"
        mock_config.provider_timeout = None

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers.multi_items.return_value = []
        mock_response.content = b"{}"
        mock_client, mock_request = _make_mock_client(mock_response)

        with (
            patch("ccproxy.inspector.oauth_addon.get_config", return_value=mock_config),
            patch("ccproxy.inspector.oauth_addon.transport.get_client", new=AsyncMock(return_value=mock_client)),
        ):
            addon = OAuthAddon()
            result = await addon._retry_with_refreshed_token(flow)

        assert result is True
        sent_headers = mock_request.call_args.kwargs["headers"]
        assert sent_headers.get("x-api-key") == "new-gemini-token"
        # Default Authorization header should not be set when a custom header is configured
        assert sent_headers.get("authorization") == "Bearer old-token"

    @pytest.mark.asyncio
    async def test_retry_does_not_send_internal_headers(self) -> None:
        """Internal ccproxy headers are not forwarded on retry."""
        flow = _make_oauth_flow(provider="anthropic")
        flow.request.headers = {
            "authorization": "Bearer old-token",
            "x-ccproxy-oauth-injected": "1",
        }
        mock_config = MagicMock()
        mock_config.resolve_oauth_token.return_value = "new-token"
        mock_config.get_auth_header.return_value = None
        mock_config.provider_timeout = None

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers.multi_items.return_value = []
        mock_response.content = b"{}"
        mock_client, mock_request = _make_mock_client(mock_response)

        with (
            patch("ccproxy.inspector.oauth_addon.get_config", return_value=mock_config),
            patch("ccproxy.inspector.oauth_addon.transport.get_client", new=AsyncMock(return_value=mock_client)),
        ):
            addon = OAuthAddon()
            await addon._retry_with_refreshed_token(flow)

        sent_headers = mock_request.call_args.kwargs["headers"]
        assert "x-ccproxy-oauth-injected" not in sent_headers

    @pytest.mark.asyncio
    async def test_retry_updates_flow_response_in_place(self) -> None:
        """Successful retry updates flow.response status_code and content in place."""
        flow = _make_oauth_flow(provider="anthropic")
        mock_config = MagicMock()
        mock_config.resolve_oauth_token.return_value = "new-token"
        mock_config.get_auth_header.return_value = None
        mock_config.provider_timeout = None

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers.multi_items.return_value = [("content-type", "application/json")]
        mock_response.content = b'{"ok": true}'
        mock_client, _ = _make_mock_client(mock_response)

        with (
            patch("ccproxy.inspector.oauth_addon.get_config", return_value=mock_config),
            patch("ccproxy.inspector.oauth_addon.transport.get_client", new=AsyncMock(return_value=mock_client)),
        ):
            addon = OAuthAddon()
            await addon._retry_with_refreshed_token(flow)

        assert flow.response.status_code == 200
        assert flow.response.content == b'{"ok": true}'

    @pytest.mark.asyncio
    async def test_retry_updates_flow_request_headers_in_place(self) -> None:
        """Regression: flow.request.headers must reflect the refreshed token after retry.

        Downstream addons (e.g. capacity fallback) re-fire the request and read
        flow.request.headers directly. If we only update flow.response, the
        replay-from-flow path sends the stale token.
        """
        flow = _make_oauth_flow(provider="anthropic")
        # Use a real dict so writes are observable.
        flow.request.headers = {"authorization": "Bearer old-token"}
        mock_config = MagicMock()
        mock_config.resolve_oauth_token.return_value = "fresh-token"
        mock_config.get_auth_header.return_value = None
        mock_config.provider_timeout = None

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers.multi_items.return_value = []
        mock_response.content = b"{}"
        mock_client, _ = _make_mock_client(mock_response)

        with (
            patch("ccproxy.inspector.oauth_addon.get_config", return_value=mock_config),
            patch("ccproxy.inspector.oauth_addon.transport.get_client", new=AsyncMock(return_value=mock_client)),
        ):
            addon = OAuthAddon()
            await addon._retry_with_refreshed_token(flow)

        assert flow.request.headers["authorization"] == "Bearer fresh-token"

    @pytest.mark.asyncio
    async def test_retry_updates_flow_request_headers_with_custom_header(self) -> None:
        """Regression: custom auth header (e.g. x-api-key) is also written back to flow.request.headers."""
        flow = _make_oauth_flow(provider="gemini")
        flow.request.headers = {"x-api-key": "old-key"}
        mock_config = MagicMock()
        mock_config.resolve_oauth_token.return_value = "fresh-key"
        mock_config.get_auth_header.return_value = "x-api-key"
        mock_config.provider_timeout = None

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers.multi_items.return_value = []
        mock_response.content = b"{}"
        mock_client, _ = _make_mock_client(mock_response)

        with (
            patch("ccproxy.inspector.oauth_addon.get_config", return_value=mock_config),
            patch("ccproxy.inspector.oauth_addon.transport.get_client", new=AsyncMock(return_value=mock_client)),
        ):
            addon = OAuthAddon()
            await addon._retry_with_refreshed_token(flow)

        assert flow.request.headers["x-api-key"] == "fresh-key"

    @pytest.mark.asyncio
    async def test_retry_uses_configured_provider_timeout(self) -> None:
        """Opt-in path: provider_timeout is passed as timeout= to client.request()."""
        flow = _make_oauth_flow(provider="anthropic")
        mock_config = MagicMock()
        mock_config.resolve_oauth_token.return_value = "new-token"
        mock_config.get_auth_header.return_value = None
        mock_config.provider_timeout = 120.0

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers.multi_items.return_value = []
        mock_response.content = b"{}"
        mock_client, mock_request = _make_mock_client(mock_response)

        with (
            patch("ccproxy.inspector.oauth_addon.get_config", return_value=mock_config),
            patch("ccproxy.inspector.oauth_addon.transport.get_client", new=AsyncMock(return_value=mock_client)),
        ):
            addon = OAuthAddon()
            await addon._retry_with_refreshed_token(flow)

        assert mock_request.call_args.kwargs["timeout"] == 120.0

    @pytest.mark.asyncio
    async def test_retry_honors_disabled_timeout(self) -> None:
        """Default path: provider_timeout=None passes timeout=None to client.request()."""
        flow = _make_oauth_flow(provider="anthropic")
        mock_config = MagicMock()
        mock_config.resolve_oauth_token.return_value = "new-token"
        mock_config.get_auth_header.return_value = None
        mock_config.provider_timeout = None

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers.multi_items.return_value = []
        mock_response.content = b"{}"
        mock_client, mock_request = _make_mock_client(mock_response)

        with (
            patch("ccproxy.inspector.oauth_addon.get_config", return_value=mock_config),
            patch("ccproxy.inspector.oauth_addon.transport.get_client", new=AsyncMock(return_value=mock_client)),
        ):
            addon = OAuthAddon()
            await addon._retry_with_refreshed_token(flow)

        assert mock_request.call_args.kwargs["timeout"] is None

    @pytest.mark.asyncio
    async def test_httpx_error_propagates_from_helper(self) -> None:
        """An httpx error during retry surfaces from _retry_with_refreshed_token —
        the response() entry point catches it. Verifies the response() error path
        is exercised end-to-end via the addon entry point."""
        import httpx

        flow = _make_oauth_flow(provider="anthropic")
        mock_config = MagicMock()
        mock_config.resolve_oauth_token.return_value = "new-token"
        mock_config.get_auth_header.return_value = None
        mock_config.provider_timeout = None

        mock_client = AsyncMock()
        mock_client.request = AsyncMock(side_effect=httpx.ConnectError("network down"))

        with (
            patch("ccproxy.inspector.oauth_addon.get_config", return_value=mock_config),
            patch("ccproxy.inspector.oauth_addon.transport.get_client", new=AsyncMock(return_value=mock_client)),
        ):
            addon = OAuthAddon()
            # response() must swallow the exception and not propagate
            await addon.response(flow)


class TestTransportDispatchIntegration:
    """New assertions for the transport dispatcher swap."""

    @pytest.mark.asyncio
    async def test_retry_stamps_transport_and_profile_metadata(self) -> None:
        """After a successful retry, flow.metadata records transport and profile used."""
        flow = _make_oauth_flow(provider="anthropic")
        mock_config = MagicMock()
        mock_config.resolve_oauth_token.return_value = "new-token"
        mock_config.get_auth_header.return_value = None
        mock_config.provider_timeout = None

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers.multi_items.return_value = []
        mock_response.content = b"{}"
        mock_client, _ = _make_mock_client(mock_response)

        with (
            patch("ccproxy.inspector.oauth_addon.get_config", return_value=mock_config),
            patch("ccproxy.inspector.oauth_addon.transport.get_client", new=AsyncMock(return_value=mock_client)),
        ):
            addon = OAuthAddon()
            await addon._retry_with_refreshed_token(flow)

        assert flow.metadata["ccproxy.retry_transport"] == "curl_cffi"
        assert flow.metadata["ccproxy.retry_profile"] == transport.DEFAULT_PROFILE

    @pytest.mark.asyncio
    async def test_retry_uses_fingerprint_profile_from_flow_metadata(self) -> None:
        """When flow.metadata carries a fingerprint_profile, get_client is called with it."""
        flow = _make_oauth_flow(provider="anthropic")
        flow.metadata["ccproxy.fingerprint_profile"] = "firefox133"
        mock_config = MagicMock()
        mock_config.resolve_oauth_token.return_value = "new-token"
        mock_config.get_auth_header.return_value = None
        mock_config.provider_timeout = None

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers.multi_items.return_value = []
        mock_response.content = b"{}"
        mock_client, _ = _make_mock_client(mock_response)

        mock_get_client = AsyncMock(return_value=mock_client)
        with (
            patch("ccproxy.inspector.oauth_addon.get_config", return_value=mock_config),
            patch("ccproxy.inspector.oauth_addon.transport.get_client", new=mock_get_client),
        ):
            addon = OAuthAddon()
            await addon._retry_with_refreshed_token(flow)

        mock_get_client.assert_awaited_once_with(host="api.anthropic.com", profile="firefox133")
        assert flow.metadata["ccproxy.retry_profile"] == "firefox133"
