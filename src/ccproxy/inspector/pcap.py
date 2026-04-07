"""PCAP synthesizer for mitmproxy flows.

Constructs fake-but-valid PCAP frames from mitmproxy's HTTP-layer flow data,
allowing Wireshark to consume traffic that mitmproxy intercepted without any
kernel-level packet capture. Based on muzuiget/mitmpcap (MIT license).
"""

from __future__ import annotations

import logging
import shlex
from math import modf
from struct import pack
from subprocess import PIPE, Popen
from time import time
from typing import Any

from mitmproxy.addonmanager import Loader
from mitmproxy.http import HTTPFlow

logger = logging.getLogger(__name__)


class PcapExporter:
    """Base class for PCAP output. Tracks per-flow TCP sequence numbers."""

    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, int]] = {}

    def write(self, data: bytes) -> None:
        raise NotImplementedError

    def flush(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def write_global_header(self) -> None:
        # libpcap global header: magic, version 2.4, thiszone=0, sigfigs=0, snaplen=256K, linktype=ETHERNET
        self.write(pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 0x040000, 1))

    def write_packet(self, src_host: str, src_port: int, dst_host: str, dst_port: int, payload: bytes) -> None:
        key = f"{src_host}:{src_port}-{dst_host}:{dst_port}"
        session = self.sessions.setdefault(key, {"seq": 1})
        seq = session["seq"]

        total = len(payload) + 40  # 20 IPv4 + 20 TCP

        tcp = pack(">HHIIBBHHH", src_port, dst_port, seq, 0, 0x50, 0x18, 0x0200, 0, 0)

        ipv4_parts = [0x45, 0, total, 0, 0, 0x40, 6, 0]
        ipv4_parts.extend(int(x) for x in src_host.split("."))
        ipv4_parts.extend(int(x) for x in dst_host.split("."))
        ipv4 = pack(">BBHHHBBHBBBBBBBB", *ipv4_parts)

        link = b"\x00" * 12 + b"\x08\x00"  # Ethernet: null MACs + IPv4 ethertype

        usec, sec = modf(time())
        size = len(link) + len(ipv4) + len(tcp) + len(payload)
        head = pack("<IIII", int(sec), int(usec * 1_000_000), size, size)

        self.write(head + link + ipv4 + tcp + payload)
        session["seq"] = seq + len(payload)

    def write_packets(self, src_host: str, src_port: int, dst_host: str, dst_port: int, payload: bytes) -> None:
        """Write payload in chunks to avoid oversized TCP frames."""
        chunk_size = 40960
        for i in range(0, len(payload), chunk_size):
            self.write_packet(src_host, src_port, dst_host, dst_port, payload[i : i + chunk_size])


class PcapFile(PcapExporter):
    """Write PCAP frames to a file."""

    def __init__(self, path: str) -> None:
        super().__init__()
        from pathlib import Path

        p = Path(path)
        if p.exists():
            self._file = p.open("ab")
        else:
            self._file = p.open("wb")
            self.write_global_header()

    def write(self, data: bytes) -> None:
        self._file.write(data)

    def flush(self) -> None:
        self._file.flush()

    def close(self) -> None:
        self._file.close()


class PcapPipe(PcapExporter):
    """Stream PCAP frames to a subprocess (e.g., wireshark -k -i -)."""

    def __init__(self, cmd: str) -> None:
        super().__init__()
        self._proc = Popen(shlex.split(cmd), stdin=PIPE)  # noqa: S603
        self.write_global_header()

    def write(self, data: bytes) -> None:
        assert self._proc.stdin is not None
        self._proc.stdin.write(data)

    def flush(self) -> None:
        assert self._proc.stdin is not None
        self._proc.stdin.flush()

    def close(self) -> None:
        self._proc.terminate()
        self._proc.wait()


def _addr_pair(flow: HTTPFlow) -> tuple[tuple[str, int], tuple[str, int]] | None:
    """Extract client and server (host, port) from a flow, or None if unavailable."""
    client_ip = getattr(flow.client_conn, "ip_address", None) if flow.client_conn else None
    server_ip = getattr(flow.server_conn, "ip_address", None) if flow.server_conn else None
    if not client_ip or not server_ip:
        return None

    def normalize(addr: tuple[str, int]) -> tuple[str, int]:
        host = addr[0].replace("::ffff:", "")
        if ":" in host or not all(p.isdigit() for p in host.split(".")):
            host = "127.0.0.1"
        return (host, addr[1])

    return normalize((client_ip[0], client_ip[1])), normalize((server_ip[0], server_ip[1]))


def _build_request_payload(r: Any) -> bytes:
    proto = f"{r.method} {r.path} {r.http_version}\r\n"
    payload = bytearray()
    payload.extend(proto.encode("ascii", errors="replace"))
    payload.extend(bytes(r.headers))
    payload.extend(b"\r\n")
    if r.raw_content:
        payload.extend(r.raw_content)
    return bytes(payload)


def _build_response_payload(r: Any) -> bytes:
    headers = r.headers.copy()
    content = r.raw_content or b""
    if r.http_version.startswith("HTTP/2"):
        headers.setdefault("content-length", str(len(content)))
        proto = f"{r.http_version} {r.status_code}\r\n"
    else:
        headers.setdefault("Content-Length", str(len(content)))
        proto = f"{r.http_version} {r.status_code} {r.reason}\r\n"
    payload = bytearray()
    payload.extend(proto.encode("ascii", errors="replace"))
    payload.extend(bytes(headers))
    payload.extend(b"\r\n")
    payload.extend(content)
    return bytes(payload)


class PcapAddon:
    """Mitmproxy addon that exports flows as PCAP."""

    def __init__(self, pcap_file: str | None = None, pcap_pipe: str | None = None) -> None:
        self._pcap_file = pcap_file
        self._pcap_pipe = pcap_pipe
        self._exporter: PcapExporter | None = None

    def load(self, _loader: Loader) -> None:
        if self._pcap_pipe:
            self._exporter = PcapPipe(self._pcap_pipe)
            logger.info("PCAP pipe started: %s", self._pcap_pipe)
        elif self._pcap_file:
            self._exporter = PcapFile(self._pcap_file)
            logger.info("PCAP file output: %s", self._pcap_file)

    def done(self) -> None:
        if self._exporter:
            self._exporter.close()
            self._exporter = None

    def response(self, flow: HTTPFlow) -> None:
        if not self._exporter:
            return

        addrs = _addr_pair(flow)
        if addrs is None:
            return

        client_addr, server_addr = addrs

        try:
            c_host, c_port = client_addr
            s_host, s_port = server_addr

            req_payload = _build_request_payload(flow.request)
            self._exporter.write_packets(c_host, c_port, s_host, s_port, req_payload)

            if flow.response:
                resp_payload = _build_response_payload(flow.response)
                self._exporter.write_packets(s_host, s_port, c_host, c_port, resp_payload)

            self._exporter.flush()
        except Exception:
            logger.exception("Error writing PCAP for %s", flow.request.pretty_url)
