"""Regression: OAuthAddon must persist refreshed token onto flow.request.headers.

Background — production flow ``ca32b740`` was a 401-storm against a real 429
capacity exhaustion on ``gemini-3.1-pro-preview``:

1. Original request returned 401 (stale token).
2. ``OAuthAddon._retry_with_refreshed_token`` refreshed the token and replayed;
   the replay returned 429 (genuine capacity).
3. ``OAuthAddon`` stamped ``flow.response`` with the 429 but never updated
   ``flow.request.headers["authorization"]`` — it still carried the pre-refresh
   stale token.
4. ``GeminiAddon`` saw the 429, fired its capacity fallback. The fallback's
   ``_attempt_request`` copied ``flow.request.headers`` verbatim (still stale),
   got 401, and bailed.

The fix: after resolving the new token, ``_retry_with_refreshed_token`` writes
it back onto ``flow.request.headers[target_header]`` (with ``Bearer `` prefix
when the target header is ``authorization``, raw otherwise) before issuing the
replay — so any downstream addon (e.g. ``GeminiAddon`` capacity fallback) sees
the fresh credential on the in-memory flow.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccproxy.inspector.oauth_addon import OAuthAddon


def _patch_async_client(mock_response: MagicMock) -> tuple[AsyncMock, AsyncMock]:
    """Build an AsyncMock chain matching httpx.AsyncClient's async-context-manager API."""
    mock_async_client = AsyncMock()
    mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
    mock_async_client.__aexit__ = AsyncMock(return_value=None)
    mock_async_client.request = AsyncMock(return_value=mock_response)
    return mock_async_client, mock_async_client.request


def _make_401_flow(*, provider: str, headers: dict[str, str]) -> MagicMock:
    flow = MagicMock()
    flow.metadata = {
        "ccproxy.oauth_provider": provider,
        "ccproxy.oauth_injected": True,
    }
    flow.request.method = "POST"
    flow.request.pretty_url = "https://api.anthropic.com/v1/messages"
    flow.request.headers = headers
    flow.request.content = b'{"model": "claude-3"}'
    flow.response = MagicMock()
    flow.response.status_code = 401
    flow.response.headers = MagicMock()
    flow.response.headers.clear = MagicMock()
    flow.response.headers.add = MagicMock()
    flow.response.headers.multi_items = MagicMock(return_value=[])
    return flow


def _make_200_response() -> MagicMock:
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers.multi_items.return_value = []
    mock_response.content = b""
    return mock_response


@pytest.mark.asyncio
async def test_default_authorization_header_is_rewritten_on_flow_request() -> None:
    """Default Bearer path: refreshed token is stamped onto flow.request.headers.

    Without the fix, ``flow.request.headers["authorization"]`` would remain
    ``"Bearer stale-token"`` after the retry, and any downstream addon (e.g.
    ``GeminiAddon`` capacity fallback) reading the in-memory flow would forward
    the stale credential.
    """
    flow = _make_401_flow(
        provider="anthropic",
        headers={"authorization": "Bearer stale-token"},
    )
    mock_config = MagicMock()
    mock_config.resolve_oauth_token.return_value = "refreshed-token"
    mock_config.get_auth_header.return_value = None
    mock_config.provider_timeout = None

    mock_async_client, _ = _patch_async_client(_make_200_response())

    with (
        patch("ccproxy.inspector.oauth_addon.get_config", return_value=mock_config),
        patch("ccproxy.inspector.oauth_addon.httpx.AsyncClient", return_value=mock_async_client),
    ):
        await OAuthAddon().response(flow)

    assert flow.request.headers["authorization"] == "Bearer refreshed-token"


@pytest.mark.asyncio
async def test_custom_auth_header_is_rewritten_raw_on_flow_request() -> None:
    """Custom-header path: raw token (no ``Bearer`` prefix) is stamped onto the
    configured target header on flow.request.headers."""
    flow = _make_401_flow(
        provider="gemini",
        headers={"x-api-key": "stale-key"},
    )
    mock_config = MagicMock()
    mock_config.resolve_oauth_token.return_value = "refreshed-token"
    mock_config.get_auth_header.return_value = "x-api-key"
    mock_config.provider_timeout = None

    mock_async_client, _ = _patch_async_client(_make_200_response())

    with (
        patch("ccproxy.inspector.oauth_addon.get_config", return_value=mock_config),
        patch("ccproxy.inspector.oauth_addon.httpx.AsyncClient", return_value=mock_async_client),
    ):
        await OAuthAddon().response(flow)

    assert flow.request.headers["x-api-key"] == "refreshed-token"
