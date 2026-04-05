from __future__ import annotations

from abc import ABCMeta
from dataclasses import dataclass
from typing import ClassVar, Literal


@dataclass(frozen=True)
class ProxyMode(metaclass=ABCMeta):
    full_spec: str
    data: str
    custom_listen_host: str | None
    custom_listen_port: int | None
    type_name: ClassVar[str]

    @classmethod
    def parse(cls, spec: str) -> ProxyMode: ...


@dataclass(frozen=True)
class RegularMode(ProxyMode):
    type_name: ClassVar[str]


@dataclass(frozen=True)
class TransparentMode(ProxyMode):
    type_name: ClassVar[str]


@dataclass(frozen=True)
class ReverseMode(ProxyMode):
    type_name: ClassVar[str]
    scheme: Literal["http", "https", "http3", "tls", "dtls", "tcp", "udp", "dns", "quic"]
    address: tuple[str, int]


@dataclass(frozen=True)
class WireGuardMode(ProxyMode):
    type_name: ClassVar[str]


@dataclass(frozen=True)
class UpstreamMode(ProxyMode):
    type_name: ClassVar[str]
    scheme: Literal["http", "https"]
    address: tuple[str, int]


@dataclass(frozen=True)
class Socks5Mode(ProxyMode):
    type_name: ClassVar[str]


@dataclass(frozen=True)
class LocalMode(ProxyMode):
    type_name: ClassVar[str]
