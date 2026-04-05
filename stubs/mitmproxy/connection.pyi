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
