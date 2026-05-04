"""Forward OAuth hook — sentinel key substitution and token injection.

Detects ``sk-ant-oat-ccproxy-{provider}`` sentinel keys in ``x-api-key``,
resolves the real auth token from ``CCProxyConfig.providers[provider]``,
and injects it via the header named on that Provider's ``auth.header``
(defaulting to ``Authorization: Bearer``). Falls back to walking
``config.providers`` in insertion order when no auth header is present —
the first cached token wins, so YAML order is load-bearing.
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
    return bool(ctx.x_api_key or ctx.authorization or ctx.get_header("x-goog-api-key") or ctx.get_header("api-key"))


@hook(
    reads=["authorization", "x-api-key"],
    writes=["authorization", "x-api-key"],
)
def forward_oauth(ctx: Context, _: dict[str, Any]) -> Context:
    """Forward OAuth Bearer token to provider."""
    api_key = ctx.x_api_key or ctx.get_header("x-goog-api-key")
    auth = ctx.authorization

    if api_key.startswith(OAUTH_SENTINEL_PREFIX):
        provider = api_key[len(OAUTH_SENTINEL_PREFIX) :]
        token = _get_oauth_token(provider)

        if not token:
            raise OAuthConfigError(
                f"Sentinel key for provider '{provider}' but no matching providers entry. "
                f"Add 'providers.{provider}' to ccproxy.yaml."
            )

        _inject_token(ctx, provider, token)
        assert ctx.flow is not None
        ctx.flow.metadata["ccproxy.oauth_provider"] = provider
        logger.info("OAuth token injected for provider '%s' (sentinel)", provider)
        return ctx

    if not api_key and not auth:
        cached_provider, cached_token = _try_cached_token()
        if cached_provider and cached_token:
            _inject_token(ctx, cached_provider, cached_token)
            assert ctx.flow is not None
            ctx.flow.metadata["ccproxy.oauth_provider"] = cached_provider
            logger.info("OAuth token injected for provider '%s' (cached)", cached_provider)

    return ctx


def _get_oauth_token(provider: str) -> str | None:
    """Look up cached auth token for a Provider entry."""
    try:
        config = get_config()
        return config.get_oauth_token(provider)
    except Exception:
        logger.exception("Failed to load OAuth config")
        return None


def _try_cached_token() -> tuple[str | None, str | None]:
    """Walk ``config.providers`` in insertion order, returning the first
    provider that has a cached token. Insertion order is the user-facing
    fallback priority — preserve it in YAML."""
    try:
        config = get_config()
        for provider in config.providers:
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

    assert ctx.flow is not None
    ctx.flow.metadata["ccproxy.oauth_injected"] = True
