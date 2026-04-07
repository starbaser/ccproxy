"""Inbound route handlers — flows heading to LiteLLM.

Handles OAuth sentinel key detection and token substitution for ALL
inbound flows regardless of client type (CLI via WireGuard or HTTP
via reverse proxy). Single entry point for auth.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ccproxy.constants import OAUTH_SENTINEL_PREFIX, OAuthConfigError

if TYPE_CHECKING:
    from mitmproxy.http import HTTPFlow

    from ccproxy.inspector.routing import InspectorRouter

logger = logging.getLogger(__name__)


def _is_inbound(flow: HTTPFlow) -> bool:
    from mitmproxy.proxy.mode_specs import ReverseMode, WireGuardMode

    return isinstance(flow.client_conn.proxy_mode, (WireGuardMode, ReverseMode))


def _get_oauth_token(provider: str) -> str | None:
    """Look up cached OAuth token from ccproxy config."""
    try:
        from ccproxy.config import get_config

        config = get_config()
        return config.get_oauth_token(provider)
    except Exception:
        logger.exception("Failed to load OAuth config")
        return None


def _get_oauth_auth_header(provider: str) -> str | None:
    """Get target auth header name for a provider (e.g., 'x-api-key')."""
    try:
        from ccproxy.config import get_config

        config = get_config()
        return config.get_oauth_auth_header(provider)
    except Exception:
        return None


def register_inbound_routes(router: InspectorRouter) -> None:
    """Register all inbound route handlers on the given router."""
    from ccproxy.inspector.routing import RouteType

    @router.route("/{path:.*}", rtype=RouteType.REQUEST)  # type: ignore[untyped-decorator]
    def handle_inbound(flow: HTTPFlow, **kwargs: object) -> None:
        if not _is_inbound(flow):
            return

        flow.metadata["ccproxy.direction"] = "inbound"

        # OAuth sentinel key detection and substitution
        api_key = flow.request.headers.get("x-api-key") or ""
        if not api_key.startswith(OAUTH_SENTINEL_PREFIX):
            return

        provider = api_key[len(OAUTH_SENTINEL_PREFIX) :]
        token = _get_oauth_token(provider)

        if not token:
            logger.error(
                "Sentinel key for provider '%s' but no token in oat_sources",
                provider,
            )
            raise OAuthConfigError(
                f"Sentinel key for provider '{provider}' but no matching oat_sources entry. "
                f"Add 'oat_sources.{provider}' to ccproxy.yaml."
            )

        # Check if provider uses a custom auth header (e.g., x-api-key for some providers)
        target_header = _get_oauth_auth_header(provider)
        if target_header:
            flow.request.headers[target_header] = token
        else:
            flow.request.headers["authorization"] = f"Bearer {token}"
            flow.request.headers["x-api-key"] = ""

        flow.metadata["ccproxy.oauth_injected"] = True
        flow.metadata["ccproxy.oauth_provider"] = provider

        # Propagate to LiteLLM via header (flow.metadata doesn't cross process boundary)
        flow.request.headers["x-ccproxy-oauth-injected"] = "1"

        logger.info(
            "OAuth token injected for provider '%s' on inbound flow",
            provider,
            extra={"event": "mitmproxy_oauth_injection", "provider": provider},
        )
