"""Tests for ccproxy.inspector.contentview.ClientRequestContentview."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from ccproxy.inspector.contentview import ClientRequestContentview, ProviderResponseContentview
from ccproxy.flows.store import FlowRecord, HttpSnapshot, InspectorMeta


def _make_cr(
    method: str = "POST",
    url: str = "https://api.example.com:443/v1/messages",
    headers: dict[str, str] | None = None,
    body: bytes = b"",
) -> HttpSnapshot:
    return HttpSnapshot(
        headers=headers or {},
        body=body,
        method=method,
        url=url,
    )


def _make_metadata(record: FlowRecord | None = None) -> MagicMock:
    """Metadata with a mock flow whose metadata dict holds the given record."""
    meta = MagicMock()
    meta.flow = MagicMock()
    meta.flow.metadata = {InspectorMeta.RECORD: record}
    return meta


class TestContentviewProperties:
    def test_name(self) -> None:
        cv = ClientRequestContentview()
        assert cv.name == "Client-Request"

    def test_syntax_highlight(self) -> None:
        cv = ClientRequestContentview()
        assert cv.syntax_highlight == "yaml"

    def test_render_priority(self) -> None:
        cv = ClientRequestContentview()
        meta = MagicMock()
        assert cv.render_priority(b"", meta) == -1


class TestContentviewPrettify:
    def test_no_flow_returns_fallback(self) -> None:
        cv = ClientRequestContentview()
        meta = MagicMock()
        meta.flow = None
        assert cv.prettify(b"", meta) == "(no flow context)"

    def test_no_record_returns_fallback(self) -> None:
        cv = ClientRequestContentview()
        meta = _make_metadata(record=None)
        assert cv.prettify(b"", meta) == "(no client request snapshot)"

    def test_no_client_request_returns_fallback(self) -> None:
        cv = ClientRequestContentview()
        record = FlowRecord(direction="inbound", client_request=None)
        meta = _make_metadata(record=record)
        assert cv.prettify(b"", meta) == "(no client request snapshot)"

    def test_first_line_format(self) -> None:
        cv = ClientRequestContentview()
        cr = _make_cr(method="GET", url="http://localhost:8080/health")
        meta = _make_metadata(FlowRecord(direction="inbound", client_request=cr))
        result = cv.prettify(b"", meta)
        assert result.startswith("GET http://localhost:8080/health")

    def test_headers_rendered(self) -> None:
        cv = ClientRequestContentview()
        cr = _make_cr(headers={"x-api-key": "secret", "content-type": "application/json"})
        meta = _make_metadata(FlowRecord(direction="inbound", client_request=cr))
        result = cv.prettify(b"", meta)
        assert "  x-api-key: secret" in result
        assert "  content-type: application/json" in result

    def test_empty_body_marker(self) -> None:
        cv = ClientRequestContentview()
        cr = _make_cr(body=b"")
        meta = _make_metadata(FlowRecord(direction="inbound", client_request=cr))
        result = cv.prettify(b"", meta)
        assert "--- Body ---" in result
        assert "(empty)" in result

    def test_valid_json_body_pretty_printed(self) -> None:
        cv = ClientRequestContentview()
        payload = {"model": "claude-sonnet", "messages": [{"role": "user", "content": "hi"}]}
        cr = _make_cr(body=json.dumps(payload).encode())
        meta = _make_metadata(FlowRecord(direction="inbound", client_request=cr))
        result = cv.prettify(b"", meta)
        assert '"model": "claude-sonnet"' in result
        assert '"role": "user"' in result

    def test_non_json_body_decoded_as_utf8(self) -> None:
        cv = ClientRequestContentview()
        cr = _make_cr(body=b"plain text body")
        meta = _make_metadata(FlowRecord(direction="inbound", client_request=cr))
        result = cv.prettify(b"", meta)
        assert "plain text body" in result

    def test_invalid_utf8_bytes_replaced(self) -> None:
        cv = ClientRequestContentview()
        cr = _make_cr(body=b"data-\xff-end")  # \xff is invalid UTF-8
        meta = _make_metadata(FlowRecord(direction="inbound", client_request=cr))
        result = cv.prettify(b"", meta)
        # Should contain the replacement character
        assert "data-" in result
        assert "-end" in result

    def test_sections_structure(self) -> None:
        cv = ClientRequestContentview()
        cr = _make_cr(headers={"h": "v"}, body=b'{"k": 1}')
        meta = _make_metadata(FlowRecord(direction="inbound", client_request=cr))
        result = cv.prettify(b"", meta)
        assert "--- Headers ---" in result
        assert "--- Body ---" in result


class TestProviderResponseContentview:
    def test_name(self) -> None:
        cv = ProviderResponseContentview()
        assert cv.name == "Provider-Response"

    def test_no_flow_returns_fallback(self) -> None:
        cv = ProviderResponseContentview()
        meta = MagicMock()
        meta.flow = None
        assert cv.prettify(b"", meta) == "(no flow context)"

    def test_no_provider_response_returns_fallback(self) -> None:
        cv = ProviderResponseContentview()
        record = FlowRecord(direction="inbound")
        meta = _make_metadata(record=record)
        assert cv.prettify(b"", meta) == "(no provider response snapshot)"

    def test_status_code_rendered(self) -> None:
        cv = ProviderResponseContentview()
        pr = HttpSnapshot(
            headers={"content-type": "application/json"},
            body=b'{"id": "msg_123"}',
            status_code=200,
        )
        record = FlowRecord(direction="inbound", provider_response=pr)
        meta = _make_metadata(record=record)
        result = cv.prettify(b"", meta)
        assert result.startswith("HTTP 200")

    def test_json_body_pretty_printed(self) -> None:
        cv = ProviderResponseContentview()
        pr = HttpSnapshot(
            headers={},
            body=b'{"choices": [{"text": "hello"}]}',
            status_code=200,
        )
        record = FlowRecord(direction="inbound", provider_response=pr)
        meta = _make_metadata(record=record)
        result = cv.prettify(b"", meta)
        assert '"choices"' in result
