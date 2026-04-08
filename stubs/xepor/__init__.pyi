from __future__ import annotations

import re
from collections.abc import Callable
from enum import Enum
from typing import Any, ClassVar

from mitmproxy.addonmanager import Loader
from mitmproxy.http import HTTPFlow, Response
from parse import Parser  # type: ignore[import-untyped]

__all__ = ["InterceptedAPI", "RouteType", "FlowMeta"]


class RouteType(Enum):
    REQUEST = 1
    RESPONSE = 2


class FlowMeta(Enum):
    REQ_PASSTHROUGH = "xepor-request-passthrough"
    RESP_PASSTHROUGH = "xepor-response-passthrough"
    REQ_URLPARSE = "xepor-request-urlparse"
    REQ_HOST = "xepor-request-host"


class InterceptedAPI:
    _REGEX_HOST_HEADER: ClassVar[re.Pattern[str]]

    default_host: str | None
    host_mapping: list[tuple[str | re.Pattern[str], str]]
    request_routes: list[tuple[str | None, Parser, Callable[..., Any]]]
    response_routes: list[tuple[str | None, Parser, Callable[..., Any]]]
    blacklist_domain: list[str]
    request_passthrough: bool
    response_passthrough: bool
    respect_proxy_headers: bool

    def __init__(
        self,
        default_host: str | None = ...,
        host_mapping: list[tuple[str | re.Pattern[str], str]] | None = ...,
        blacklist_domain: list[str] | None = ...,
        request_passthrough: bool = ...,
        response_passthrough: bool = ...,
        respect_proxy_headers: bool = ...,
    ) -> None: ...

    def load(self, loader: Loader) -> None: ...
    def request(self, flow: HTTPFlow) -> None: ...
    def response(self, flow: HTTPFlow) -> None: ...

    def route(
        self,
        path: str,
        host: str | None = ...,
        rtype: RouteType = ...,
        catch_error: bool = ...,
        return_error: bool = ...,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]: ...

    def remap_host(self, flow: HTTPFlow, overwrite: bool = ...) -> str: ...
    def get_host(self, flow: HTTPFlow) -> tuple[str, int]: ...
    def default_response(self) -> Response: ...
    def error_response(self, msg: str = ...) -> Response: ...
    def find_handler(
        self,
        host: str,
        path: str,
        rtype: RouteType = ...,
    ) -> tuple[Callable[..., Any] | None, Any]: ...
