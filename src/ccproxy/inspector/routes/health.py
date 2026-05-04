"""Synthetic ``GET /`` and ``GET /health`` alive-signal handler.

Mirrors Portkey AI's gateway convention: a single ``text/plain`` greeting
served directly from ccproxy without forwarding upstream. ccproxy is a
request proxy with no inference engine, so the response asserts only that
the proxy is reachable and routable.

Registered as REQUEST routes at higher priority than
``register_transform_routes`` so the transform router doesn't try to
forward ``/`` or ``/health`` to a provider that doesn't exist (the
placeholder reverse-proxy backend).

Gated to ``ReverseMode`` flows only — WireGuard-tunneled traffic to a real
upstream's ``/`` or ``/health`` continues to forward unchanged.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mitmproxy.http import HTTPFlow

    from ccproxy.inspector.router import InspectorRouter

logger = logging.getLogger(__name__)

_GREETING = b"ccproxy says hey!"


def register_health_routes(router: InspectorRouter) -> None:
    """Register ``GET /`` and ``GET /health`` synthetic handlers on ``router``."""
    from ccproxy.inspector.router import RouteType

    @router.route("/", rtype=RouteType.REQUEST, catch_error=False)
    @router.route("/health", rtype=RouteType.REQUEST, catch_error=False)
    def handle_health(flow: HTTPFlow, **kwargs: object) -> None:  # pyright: ignore[reportUnusedFunction]
        from mitmproxy.proxy.mode_specs import ReverseMode

        if not isinstance(flow.client_conn.proxy_mode, ReverseMode):
            return
        if flow.request.method != "GET":
            return

        from mitmproxy.http import Response

        flow.response = Response.make(
            200,
            _GREETING,
            {"Content-Type": "text/plain"},
        )
        logger.debug("Served %s", flow.request.path)
