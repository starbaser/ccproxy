"""Vendored xepor routing framework for mitmproxy addons.

Flask-style URL routing on top of mitmproxy's addon API. Vendored from
xepor 0.6.0 (Apache-2.0, github.com/xepor/xepor) with mitmproxy 12.x
compatibility fix (Server positional → keyword arg).

Original author: ttimasdf
"""

from __future__ import annotations

import functools
import logging
import re
import sys
import traceback
import urllib.parse
from enum import Enum
from typing import Any, ClassVar

from mitmproxy import ctx
from mitmproxy.addonmanager import Loader
from mitmproxy.connection import Server
from mitmproxy.http import HTTPFlow, Response
from mitmproxy.net.http import url
from parse import Parser  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)


class RouteType(Enum):
    REQUEST = 1
    RESPONSE = 2


class _FlowMeta:
    """Per-flow metadata keys (plain strings for dict[str, Any] compatibility)."""

    REQ_PASSTHROUGH = "xepor-request-passthrough"
    RESP_PASSTHROUGH = "xepor-response-passthrough"
    REQ_URLPARSE = "xepor-request-urlparse"
    REQ_HOST = "xepor-request-host"


FlowMeta = _FlowMeta


class InterceptedAPI:
    _REGEX_HOST_HEADER = re.compile(r"^(?P<host>[^:]+|\[.+\])(?::(?P<port>\d+))?$")

    _PROXY_FORWARDED_HEADERS: ClassVar[list[str]] = [
        "X-Forwarded-For",
        "X-Forwarded-Host",
        "X-Forwarded-Port",
        "X-Forwarded-Proto",
        "X-Forwarded-Server",
        "X-Real-Ip",
    ]

    def __init__(
        self,
        default_host: str | None = None,
        host_mapping: list[tuple[str | re.Pattern[str], str]] | None = None,
        blacklist_domain: list[str] | None = None,
        request_passthrough: bool = True,
        response_passthrough: bool = True,
        respect_proxy_headers: bool = False,
    ) -> None:
        self.default_host = default_host
        self.host_mapping = host_mapping or []
        self.request_routes: list[tuple[str | None, Parser, Any]] = []
        self.response_routes: list[tuple[str | None, Parser, Any]] = []
        self.blacklist_domain = blacklist_domain or []
        self.request_passthrough = request_passthrough
        self.response_passthrough = response_passthrough
        self.respect_proxy_headers = respect_proxy_headers
        self._log = logging.getLogger(__name__)

    def load(self, loader: Loader) -> None:
        self._log.info("Setting option connection_strategy=lazy")
        ctx.options.connection_strategy = "lazy"

    def request(self, flow: HTTPFlow) -> None:
        if FlowMeta.REQ_URLPARSE in flow.metadata:
            parsed = flow.metadata[FlowMeta.REQ_URLPARSE]
        else:
            parsed = urllib.parse.urlparse(flow.request.path)
            flow.metadata[FlowMeta.REQ_URLPARSE] = parsed
        path = parsed.path

        if flow.metadata.get(FlowMeta.REQ_PASSTHROUGH) is True:
            return

        host = self.remap_host(flow)
        handler, params = self.find_handler(host, path, RouteType.REQUEST)

        if handler is not None:
            self._log.info("<= [%s] %s", flow.request.method, path)
            handler(flow, *params.fixed, **params.named)
        elif not self.request_passthrough or self.get_host(flow)[0] in self.blacklist_domain:
            flow.response = self.default_response()
        else:
            flow.metadata[FlowMeta.REQ_PASSTHROUGH] = True

    def response(self, flow: HTTPFlow) -> None:
        if FlowMeta.REQ_URLPARSE in flow.metadata:
            parsed = flow.metadata[FlowMeta.REQ_URLPARSE]
        else:
            parsed = urllib.parse.urlparse(flow.request.path)
            flow.metadata[FlowMeta.REQ_URLPARSE] = parsed
        path = parsed.path

        if flow.metadata.get(FlowMeta.RESP_PASSTHROUGH) is True:
            return

        handler, params = self.find_handler(self.get_host(flow)[0], path, RouteType.RESPONSE)

        if handler is not None:
            status = flow.response.status_code if flow.response else 0
            self._log.info("=> [%s] %s", status, path)
            handler(flow, *params.fixed, **params.named)
        elif not self.response_passthrough or self.get_host(flow)[0] in self.blacklist_domain:
            flow.response = self.default_response()
        else:
            flow.metadata[FlowMeta.RESP_PASSTHROUGH] = True

    def route(
        self,
        path: str,
        host: str | None = None,
        rtype: RouteType = RouteType.REQUEST,
        catch_error: bool = True,
        return_error: bool = False,
    ) -> Any:
        host = host or self.default_host

        def catcher(func: Any) -> Any:
            @functools.wraps(func)
            def handler(flow: HTTPFlow, *args: Any, **kwargs: Any) -> Any:
                try:
                    return func(flow, *args, **kwargs)
                except Exception as e:
                    etype, value, tback = sys.exc_info()
                    tb = "".join(traceback.format_exception(etype, value, tback))
                    self._log.error("Exception in handler for %s:\n%s", flow.request.pretty_url, tb)
                    if return_error:
                        flow.response = self.error_response(str(e))

            return handler

        def wrapper(handler: Any) -> Any:
            if catch_error:
                handler = catcher(handler)
            if rtype == RouteType.REQUEST:
                self.request_routes.append((host, Parser(path), handler))
            elif rtype == RouteType.RESPONSE:
                self.response_routes.append((host, Parser(path), handler))
            else:
                raise ValueError(f"Invalid route type: {rtype}")
            return handler

        return wrapper

    def remap_host(self, flow: HTTPFlow, overwrite: bool = True) -> str:
        host, port = self.get_host(flow)
        for src, dest in self.host_mapping:
            if (isinstance(src, re.Pattern) and src.match(host)) or (isinstance(src, str) and host == src):
                if overwrite and (flow.request.host != dest or flow.request.port != port):
                    if self.respect_proxy_headers:
                        flow.request.scheme = flow.request.headers["X-Forwarded-Proto"]
                    flow.server_conn = Server(address=(dest, port))
                    flow.request.host = dest
                    flow.request.port = port
                return dest
        return host

    def get_host(self, flow: HTTPFlow) -> tuple[str, int]:
        if FlowMeta.REQ_HOST not in flow.metadata:
            if self.respect_proxy_headers:
                host = flow.request.headers["X-Forwarded-Host"]
                port = int(flow.request.headers["X-Forwarded-Port"])
            else:
                host, port_or_none = url.parse_authority(flow.request.pretty_host, check=False)
                port = port_or_none or url.default_port(flow.request.scheme) or 80
            flow.metadata[FlowMeta.REQ_HOST] = (host, port)
        result: tuple[str, int] = flow.metadata[FlowMeta.REQ_HOST]
        return result

    def default_response(self) -> Response:
        return Response.make(404, "Not Found", {"X-Intercepted-By": "xepor"})

    def error_response(self, msg: str = "APIServer Error") -> Response:
        return Response.make(502, msg)

    def find_handler(self, host: str, path: str, rtype: RouteType = RouteType.REQUEST) -> tuple[Any, Any]:
        if rtype == RouteType.REQUEST:
            routes = self.request_routes
        elif rtype == RouteType.RESPONSE:
            routes = self.response_routes
        else:
            raise ValueError(f"Invalid route type: {rtype}")

        for h, parser, handler in routes:
            if h is not None and h != host:
                continue
            parse_result = parser.parse(path)
            if parse_result is not None:
                return handler, parse_result

        return None, None


class InspectorRouter(InterceptedAPI):
    """ccproxy's xepor-based router with unique addon name."""

    def __init__(self, name: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.name = name
