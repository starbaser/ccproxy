"""Forward OAuth hook — sentinel key substitution and token injection.

Detects ``sk-ant-oat-ccproxy-{provider}`` sentinel keys in the
``x-api-key`` header, resolves the real OAuth token from ``oat_sources``,
and injects it as the appropriate auth header. Falls back to cached
tokens when no auth header is present.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ccproxy.config import get_config
from ccproxy.constants import OAUTH_SENTINEL_PREFIX, OAuthConfigError
from ccproxy.pipeline.hook import hook

if TYPE_CHECKING:
    from ccproxy.pipeline.context import Context

logger = logging.getLogger(__name__)


def forward_oauth_guard(ctx: Context) -> bool:
    """Guard: run if there's an auth header with a potential sentinel key."""
    return bool(ctx.x_api_key or ctx.authorization or ctx.get_header("x-goog-api-key"))


@hook(
    reads=["authorization", "x-api-key"],
    writes=["authorization", "x-api-key"],
)
def forward_oauth(ctx: Context, params: dict[str, Any]) -> Context:
    """Forward OAuth Bearer token to provider.

    Three paths:
    1. Sentinel key in x-api-key/x-goog-api-key -> substitute real token from oat_sources
    2. No auth at all -> try cached token from oat_sources
    3. Real key present -> pass through
    """
    api_key = ctx.x_api_key or ctx.get_header("x-goog-api-key")
    auth = ctx.authorization

    # Path 1: sentinel key substitution
    if api_key.startswith(OAUTH_SENTINEL_PREFIX):
        provider = api_key[len(OAUTH_SENTINEL_PREFIX):]
        token = _get_oauth_token(provider)

        if not token:
            raise OAuthConfigError(
                f"Sentinel key for provider '{provider}' but no matching oat_sources entry. "
                f"Add 'oat_sources.{provider}' to ccproxy.yaml."
            )

        _inject_token(ctx, provider, token)
        ctx.flow.metadata["ccproxy.oauth_provider"] = provider
        logger.info("OAuth token injected for provider '%s' (sentinel)", provider)
        return ctx

    # Path 2: no auth — try cached token
    if not api_key and not auth:
        cached_provider, cached_token = _try_cached_token()
        if cached_provider and cached_token:
            _inject_token(ctx, cached_provider, cached_token)
            ctx.flow.metadata["ccproxy.oauth_provider"] = cached_provider
            logger.info("OAuth token injected for provider '%s' (cached)", cached_provider)

    return ctx


def _get_oauth_token(provider: str) -> str | None:
    """Look up OAuth token from oat_sources config."""
    try:
        config = get_config()
        return config.get_oauth_token(provider)
    except Exception:
        logger.exception("Failed to load OAuth config")
        return None


def _try_cached_token() -> tuple[str | None, str | None]:
    """Try to find any available cached OAuth token from oat_sources."""
    try:
        config = get_config()
        for provider in config.oat_sources:
            token = config.get_oauth_token(provider)
            if token:
                return provider, token
    except Exception:
        logger.exception("Failed to load OAuth config")
    return None, None


def _inject_token(ctx: Context, provider: str, token: str) -> None:
    """Inject OAuth token into the appropriate flow header."""
    config = get_config()
    target_header = config.get_auth_header(provider)

    if target_header:
        ctx.set_header(target_header, token)
    else:
        ctx.set_header("authorization", f"Bearer {token}")

    # Clear sentinel headers that are NOT the auth target
    for sentinel in ("x-goog-api-key", "x-api-key"):
        if sentinel != target_header:
            ctx.set_header(sentinel, "")

    ctx.set_header("x-ccproxy-oauth-injected", "1")
