"""Tests for PCAP synthesizer."""

from struct import unpack
from unittest.mock import MagicMock

import pytest

from ccproxy.inspector.pcap import (
    PcapAddon,
    PcapExporter,
    PcapFile,
    _addr_pair,
    _build_request_payload,
    _build_response_payload,
)


def _make_flow_with_addrs(
    client_ip: tuple[str, int] = ("10.0.0.1", 50000),
    server_ip: tuple[str, int] = ("93.184.216.34", 443),
) -> MagicMock:
    flow = MagicMock()
    flow.client_conn.ip_address = client_ip
    flow.server_conn.ip_address = server_ip
    flow.request.method = "GET"
    flow.request.path = "/test"
    flow.request.http_version = "HTTP/1.1"
    flow.request.headers = MagicMock()
    flow.request.headers.__bytes__ = lambda self: b"Host: example.com\r\n"
    flow.request.raw_content = b"request body"
    flow.request.pretty_url = "https://example.com/test"
    flow.response = MagicMock()
    flow.response.status_code = 200
    flow.response.reason = "OK"
    flow.response.http_version = "HTTP/1.1"
    flow.response.headers = MagicMock()
    flow.response.headers.copy.return_value = MagicMock()
    flow.response.headers.copy.return_value.__bytes__ = lambda self: b"Content-Type: text/plain\r\n"
    flow.response.headers.copy.return_value.setdefault = MagicMock()
    flow.response.raw_content = b"response body"
    return flow


class TestPcapGlobalHeader:
    def test_global_header_magic(self, tmp_path: pytest.TempPathFactory) -> None:
        path = str(tmp_path / "test.pcap")  # type: ignore[operator]
        pcap = PcapFile(path)
        pcap.close()

        with open(path, "rb") as f:
            data = f.read()

        magic, major, minor = unpack("<IHH", data[:8])
        assert magic == 0xA1B2C3D4
        assert major == 2
        assert minor == 4


class TestPcapPacketConstruction:
    def test_write_packet_produces_valid_frame(self) -> None:
        exporter = PcapExporter()
        chunks: list[bytes] = []
        exporter.write = lambda data: chunks.append(data)  # type: ignore[assignment]

        exporter.write_packet("10.0.0.1", 50000, "93.184.216.34", 443, b"hello")

        frame = b"".join(chunks)
        # pcap record header (16) + ethernet (14) + ipv4 (20) + tcp (20) + payload (5) = 75
        assert len(frame) == 16 + 14 + 20 + 20 + 5

    def test_sequence_numbers_increment(self) -> None:
        exporter = PcapExporter()
        exporter.write = lambda data: None  # type: ignore[assignment]

        exporter.write_packet("10.0.0.1", 50000, "93.184.216.34", 443, b"hello")
        key = "10.0.0.1:50000-93.184.216.34:443"
        assert exporter.sessions[key]["seq"] == 6  # 1 + len("hello")

        exporter.write_packet("10.0.0.1", 50000, "93.184.216.34", 443, b"world")
        assert exporter.sessions[key]["seq"] == 11

    def test_distinct_sessions_per_flow(self) -> None:
        exporter = PcapExporter()
        exporter.write = lambda data: None  # type: ignore[assignment]

        exporter.write_packet("10.0.0.1", 50000, "1.2.3.4", 80, b"a")
        exporter.write_packet("10.0.0.1", 50001, "1.2.3.4", 80, b"b")
        assert len(exporter.sessions) == 2

    def test_write_packets_chunks_large_payload(self) -> None:
        exporter = PcapExporter()
        call_count = [0]
        original_write_packet = exporter.write_packet

        def counting_write_packet(*args: object, **kwargs: object) -> None:
            call_count[0] += 1

        exporter.write_packet = counting_write_packet  # type: ignore[assignment]
        exporter.write_packets("10.0.0.1", 50000, "1.2.3.4", 80, b"x" * 100000)
        # 100000 / 40960 = 2.44 → 3 chunks
        assert call_count[0] == 3


class TestPcapFile:
    def test_creates_new_file_with_header(self, tmp_path: pytest.TempPathFactory) -> None:
        path = str(tmp_path / "new.pcap")  # type: ignore[operator]
        pcap = PcapFile(path)
        pcap.close()
        with open(path, "rb") as f:
            data = f.read()
        assert len(data) == 24  # global header only

    def test_appends_to_existing_file(self, tmp_path: pytest.TempPathFactory) -> None:
        path = str(tmp_path / "existing.pcap")  # type: ignore[operator]
        # Create initial file
        pcap1 = PcapFile(path)
        pcap1.write_packet("10.0.0.1", 80, "10.0.0.2", 80, b"first")
        pcap1.close()
        size1 = len(open(path, "rb").read())

        # Reopen — should append, no new global header
        pcap2 = PcapFile(path)
        pcap2.write_packet("10.0.0.1", 80, "10.0.0.2", 80, b"second")
        pcap2.close()
        size2 = len(open(path, "rb").read())
        assert size2 > size1


class TestAddrPair:
    def test_returns_addresses(self) -> None:
        flow = _make_flow_with_addrs()
        result = _addr_pair(flow)
        assert result is not None
        client, server = result
        assert client == ("10.0.0.1", 50000)
        assert server == ("93.184.216.34", 443)

    def test_strips_ipv6_mapped_prefix(self) -> None:
        flow = _make_flow_with_addrs(client_ip=("::ffff:10.0.0.1", 50000))
        result = _addr_pair(flow)
        assert result is not None
        assert result[0][0] == "10.0.0.1"

    def test_returns_none_for_missing_server_conn(self) -> None:
        flow = MagicMock()
        flow.client_conn.ip_address = ("10.0.0.1", 80)
        flow.server_conn = None
        assert _addr_pair(flow) is None

    def test_returns_none_for_missing_ip_address(self) -> None:
        flow = MagicMock()
        flow.client_conn = MagicMock(spec=[])  # no ip_address attr
        flow.server_conn = MagicMock()
        flow.server_conn.ip_address = ("1.2.3.4", 80)
        assert _addr_pair(flow) is None


class TestBuildPayload:
    def test_request_payload(self) -> None:
        req = MagicMock()
        req.method = "POST"
        req.path = "/api/chat"
        req.http_version = "HTTP/1.1"
        req.headers = MagicMock()
        req.headers.__bytes__ = lambda self: b"Content-Type: application/json\r\n"
        req.raw_content = b'{"msg":"hi"}'

        payload = _build_request_payload(req)
        assert payload.startswith(b"POST /api/chat HTTP/1.1\r\n")
        assert b'{"msg":"hi"}' in payload

    def test_response_payload_http2(self) -> None:
        resp = MagicMock()
        resp.http_version = "HTTP/2.0"
        resp.status_code = 200
        resp.headers = MagicMock()
        resp.headers.copy.return_value = MagicMock()
        resp.headers.copy.return_value.__bytes__ = lambda self: b""
        resp.headers.copy.return_value.setdefault = MagicMock()
        resp.raw_content = b"body"

        payload = _build_response_payload(resp)
        assert payload.startswith(b"HTTP/2.0 200\r\n")
        assert b"body" in payload

    def test_response_payload_http11(self) -> None:
        resp = MagicMock()
        resp.http_version = "HTTP/1.1"
        resp.status_code = 404
        resp.reason = "Not Found"
        resp.headers = MagicMock()
        resp.headers.copy.return_value = MagicMock()
        resp.headers.copy.return_value.__bytes__ = lambda self: b""
        resp.headers.copy.return_value.setdefault = MagicMock()
        resp.raw_content = b""

        payload = _build_response_payload(resp)
        assert payload.startswith(b"HTTP/1.1 404 Not Found\r\n")


class TestPcapAddon:
    def test_does_nothing_when_unconfigured(self) -> None:
        addon = PcapAddon()
        addon.load(MagicMock())
        assert addon._exporter is None

    def test_creates_file_exporter(self, tmp_path: pytest.TempPathFactory) -> None:
        path = str(tmp_path / "capture.pcap")  # type: ignore[operator]
        addon = PcapAddon(pcap_file=path)
        addon.load(MagicMock())
        assert addon._exporter is not None
        addon.done()

    def test_response_writes_packets(self, tmp_path: pytest.TempPathFactory) -> None:
        path = str(tmp_path / "capture.pcap")  # type: ignore[operator]
        addon = PcapAddon(pcap_file=path)
        addon.load(MagicMock())

        flow = _make_flow_with_addrs()
        addon.response(flow)
        addon.done()

        with open(path, "rb") as f:
            data = f.read()
        assert len(data) > 24  # more than just the global header

    def test_response_skips_flow_without_addrs(self, tmp_path: pytest.TempPathFactory) -> None:
        path = str(tmp_path / "capture.pcap")  # type: ignore[operator]
        addon = PcapAddon(pcap_file=path)
        addon.load(MagicMock())

        flow = MagicMock()
        flow.client_conn = None
        flow.server_conn = None
        addon.response(flow)  # Should not raise
        addon.done()
