from __future__ import annotations

from mitmproxy.proxy.mode_specs import ProxyMode

Address = tuple[str, int]


class Connection:
    id: str
    error: str | None
    tls: bool
    tls_version: str | None


class Client(Connection):
    peername: Address
    sockname: Address
    proxy_mode: ProxyMode
    timestamp_start: float
    ip_address: tuple[str, int] | None


class Server(Connection):
    address: Address | None
    peername: Address | None
    sockname: Address | None
    ip_address: tuple[str, int] | None
    timestamp_start: float | None
    timestamp_end: float | None
    def __init__(self, address: Address | None = ...) -> None: ...
