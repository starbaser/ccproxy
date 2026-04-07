"""Outbound route handlers — flows from LiteLLM to providers.

Handles beta header injection and auth failure observation on the
outbound leg (LiteLLM → provider API).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ccproxy.constants import ANTHROPIC_BETA_HEADERS

if TYPE_CHECKING:
    from mitmproxy.http import HTTPFlow

    from ccproxy.inspector.routing import InspectorRouter

logger = logging.getLogger(__name__)


def _is_outbound(flow: HTTPFlow) -> bool:
    from mitmproxy.proxy.mode_specs import RegularMode

    return isinstance(flow.client_conn.proxy_mode, RegularMode)


def register_outbound_routes(router: InspectorRouter) -> None:
    """Register all outbound route handlers on the given router."""
    from ccproxy.inspector.routing import RouteType

    @router.route("/{path:.*}", rtype=RouteType.REQUEST)  # type: ignore[untyped-decorator]
    def ensure_beta_headers(flow: HTTPFlow, **kwargs: object) -> None:
        if not _is_outbound(flow):
            return

        flow.metadata["ccproxy.direction"] = "outbound"

        # Provider-agnostic: only merge if anthropic-beta header already present
        # (LiteLLM's hook pipeline sets it; this is a safety net / idempotent merge)
        existing = flow.request.headers.get("anthropic-beta")
        if existing is None:
            return

        existing_list = [h.strip() for h in existing.split(",") if h.strip()]
        merged = list(dict.fromkeys(ANTHROPIC_BETA_HEADERS + existing_list))
        flow.request.headers["anthropic-beta"] = ",".join(merged)

    @router.route("/{path:.*}", rtype=RouteType.RESPONSE)  # type: ignore[untyped-decorator]
    def observe_auth_failure(flow: HTTPFlow, **kwargs: object) -> None:
        if not _is_outbound(flow):
            return

        if flow.response and flow.response.status_code in (401, 403):
            provider = flow.metadata.get("ccproxy.oauth_provider", "unknown")
            logger.warning(
                "Auth failure on outbound: %s %d (provider: %s)",
                flow.request.pretty_url,
                flow.response.status_code,
                provider,
                extra={
                    "event": "outbound_auth_failure",
                    "status": flow.response.status_code,
                    "url": flow.request.pretty_url,
                    "provider": provider,
                },
            )
