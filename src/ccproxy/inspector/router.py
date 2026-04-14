"""ccproxy xepor routing — thin subclass with mitmproxy 12.x fixes.

Patches:
  - ``remap_host``: keyword ``Server(address=...)`` for mitmproxy 12.x kw_only dataclass
  - ``find_handler``: ``host=None`` wildcard support
  - ``name`` attribute for AddonManager dedup across multiple InterceptedAPI instances
  - ``request``/``response``: short-circuit when the router has no routes of
    that type so routeless stages don't set passthrough flags that block
    downstream routers from processing the flow
"""

from __future__ import annotations

import re
from typing import Any

from mitmproxy.connection import Server
from mitmproxy.http import HTTPFlow
from xepor import FlowMeta, InterceptedAPI, RouteType

__all__ = ["FlowMeta", "InspectorRouter", "InterceptedAPI", "RouteType"]


class InspectorRouter(InterceptedAPI):
    """xepor router with unique addon name for mitmproxy AddonManager."""

    def __init__(self, name: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.name = name

    def request(self, flow: HTTPFlow) -> None:
        """Skip the request hook entirely when no request routes are registered.

        xepor's default ``request()`` sets ``REQ_PASSTHROUGH=True`` when a
        route lookup returns no handler, which then blocks later routers in
        the chain from running their own handlers. Routers with zero request
        routes should not participate at all.
        """
        if not self.request_routes:
            return
        super().request(flow)

    def response(self, flow: HTTPFlow) -> None:
        """Skip the response hook entirely when no response routes are registered.

        Without this, the first routeless router in the addon chain sets
        ``RESP_PASSTHROUGH=True``, which causes xepor to log a spurious
        ``skipped because of previous passthrough`` warning on subsequent
        routers AND prevents the transform router's
        ``handle_transform_response`` from ever running.
        """
        if not self.response_routes:
            return
        super().response(flow)

    def find_handler(
        self, host: str, path: str, rtype: RouteType = RouteType.REQUEST
    ) -> tuple[Any, Any]:
        """Support host=None as a wildcard (xepor skips None-registered routes)."""
        routes = self.request_routes if rtype == RouteType.REQUEST else self.response_routes
        for h, parser, handler in routes:
            if h is not None and h != host:
                continue
            parse_result = parser.parse(path)  # pyright: ignore[reportUnknownMemberType]
            if parse_result is not None:
                return handler, parse_result
        return None, None

    def remap_host(self, flow: HTTPFlow, overwrite: bool = True) -> str:
        """Use keyword Server(address=...) for mitmproxy 12.x kw_only dataclass."""
        host, port = self.get_host(flow)
        for src, dest in self.host_mapping:
            if (isinstance(src, re.Pattern) and src.match(host)) or (
                isinstance(src, str) and host == src
            ):
                if overwrite and (
                    flow.request.host != dest or flow.request.port != port
                ):
                    if self.respect_proxy_headers:
                        flow.request.scheme = flow.request.headers["X-Forwarded-Proto"]
                    flow.server_conn = Server(address=(dest, port))
                    flow.request.host = dest
                    flow.request.port = port
                return dest
        return host
