"""ccproxy xepor routing — thin subclass for mitmproxy AddonManager compatibility.

xepor 0.6.0 has two issues with mitmproxy 12.x:
1. Version constraint mitmproxy<12.0.0 (overridden via [tool.uv] in pyproject.toml)
2. remap_host() calls Server((dest, port)) with a positional arg, but mitmproxy 12.x
   Server is @dataclass(kw_only=True) requiring Server(address=(dest, port))

This module provides InspectorRouter — a subclass that fixes the Server() call
and adds a name attribute for mitmproxy's AddonManager (which uses addon names
to avoid collisions between multiple InterceptedAPI instances).
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

    def find_handler(
        self, host: str, path: str, rtype: RouteType = RouteType.REQUEST
    ) -> tuple[Any, Any]:
        """Override to support host=None as a wildcard.

        Upstream xepor uses ``h != host`` which skips routes registered
        with host=None. We treat None as "match any host".
        """
        routes = self.request_routes if rtype == RouteType.REQUEST else self.response_routes
        for h, parser, handler in routes:
            if h is not None and h != host:
                continue
            parse_result = parser.parse(path)  # pyright: ignore[reportUnknownMemberType]
            if parse_result is not None:
                return handler, parse_result
        return None, None

    def remap_host(self, flow: HTTPFlow, overwrite: bool = True) -> str:
        """Override to fix xepor's mitmproxy 12.x incompatibility.

        xepor calls Server((dest, port)) but mitmproxy 12.x requires
        Server(address=(dest, port)) due to kw_only=True on the dataclass.
        """
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
