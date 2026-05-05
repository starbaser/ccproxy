"""Forward OAuth hook — sentinel key substitution and token injection.

Detects ``sk-ant-oat-ccproxy-{provider}`` sentinel keys on any inbound
auth header (``x-api-key``, ``x-goog-api-key``, or ``Authorization: Bearer``),
resolves the real auth token from ``CCProxyConfig.providers[provider]``,
and injects it via the header named on that Provider's ``auth.header``
(defaulting to ``Authorization: Bearer`` when unset). All non-target inbound
auth headers are cleared so the sentinel never leaks upstream.
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


_INBOUND_AUTH_HEADERS: tuple[str, ...] = ("x-api-key", "x-goog-api-key", "authorization")
"""Headers checked inbound for a sentinel key, in priority order. ``authorization``
is matched against its bare token after stripping a ``Bearer `` prefix."""


def forward_oauth_guard(ctx: Context) -> bool:
    """Guard: run if any inbound auth header carries a value."""
    return bool(ctx.x_api_key or ctx.authorization or ctx.get_header("x-goog-api-key") or ctx.get_header("api-key"))


def _bearer_token(value: str) -> str:
    """Strip a leading ``Bearer `` (case-insensitive) from an Authorization value."""
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return value


def _extract_sentinel(ctx: Context) -> str | None:
    """Return the sentinel-key value from any inbound auth header, or None."""
    for header in _INBOUND_AUTH_HEADERS:
        raw = ctx.get_header(header, "")
        candidate = _bearer_token(raw) if header == "authorization" else raw
        if candidate.startswith(OAUTH_SENTINEL_PREFIX):
            return candidate
    return None


@hook(
    reads=["authorization", "x-api-key", "x-goog-api-key"],
    writes=["authorization", "x-api-key", "x-goog-api-key"],
)
def forward_oauth(ctx: Context, _: dict[str, Any]) -> Context:
    """Forward an auth token to the provider, substituting a sentinel key."""
    sentinel = _extract_sentinel(ctx)
    if sentinel is None:
        return ctx

    provider = sentinel[len(OAUTH_SENTINEL_PREFIX) :]
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


def _get_oauth_token(provider: str) -> str | None:
    try:
        config = get_config()
        return config.resolve_oauth_token(provider)
    except Exception:
        logger.exception("Failed to load OAuth config")
        return None


def _inject_token(ctx: Context, provider: str, token: str) -> None:
    """Inject ``token`` into the configured outbound auth header.

    The provider's ``auth.header`` (None defaults to ``authorization``) wins.
    All other inbound auth headers are cleared so the sentinel never leaks
    upstream alongside the real token.
    """
    config = get_config()
    target_header = (config.get_auth_header(provider) or "authorization").lower()

    if target_header == "authorization":
        ctx.set_header("authorization", f"Bearer {token}")
    else:
        ctx.set_header(target_header, token)

    for header in _INBOUND_AUTH_HEADERS:
        if header != target_header:
            ctx.set_header(header, "")

    assert ctx.flow is not None
    ctx.flow.metadata["ccproxy.oauth_injected"] = True
