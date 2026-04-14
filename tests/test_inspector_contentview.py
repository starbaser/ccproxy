"""Tests for ccproxy.inspector.contentview.ClientRequestContentview."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from ccproxy.inspector.contentview import ClientRequestContentview
from ccproxy.inspector.flow_store import ClientRequest, FlowRecord, InspectorMeta


def _make_cr(
    method: str = "POST",
    scheme: str = "https",
    host: str = "api.example.com",
    port: int = 443,
    path: str = "/v1/messages",
    headers: dict[str, str] | None = None,
    body: bytes = b"",
) -> ClientRequest:
    return ClientRequest(
        method=method,
        scheme=scheme,
        host=host,
        port=port,
        path=path,
        headers=headers or {},
        body=body,
        content_type="application/json",
    )


def _make_flow(record: FlowRecord | None) -> MagicMock:
    """Mock flow whose metadata dict holds the given record."""
    flow = MagicMock()
    flow.metadata = {InspectorMeta.RECORD: record}
    return flow


def _render(cv: ClientRequestContentview, flow: MagicMock | None) -> str:
    """Invoke the view and join its line generator back into a single string."""
    _desc, line_gen = cv(b"", flow=flow)
    return "\n".join("".join(piece for _, piece in line) for line in line_gen)


class TestContentviewProperties:
    def test_name(self) -> None:
        assert ClientRequestContentview.name == "Client-Request"

    def test_render_priority_returns_negative(self) -> None:
        cv = ClientRequestContentview()
        assert cv.render_priority(b"") == -1


class TestContentviewRender:
    def test_no_flow_returns_fallback(self) -> None:
        cv = ClientRequestContentview()
        assert _render(cv, None) == "(no flow context)"

    def test_no_record_returns_fallback(self) -> None:
        cv = ClientRequestContentview()
        assert _render(cv, _make_flow(None)) == "(no client request snapshot)"

    def test_no_client_request_returns_fallback(self) -> None:
        cv = ClientRequestContentview()
        record = FlowRecord(direction="inbound", client_request=None)
        assert _render(cv, _make_flow(record)) == "(no client request snapshot)"

    def test_first_line_format(self) -> None:
        cv = ClientRequestContentview()
        cr = _make_cr(method="GET", scheme="http", host="localhost", port=8080, path="/health")
        result = _render(cv, _make_flow(FlowRecord(direction="inbound", client_request=cr)))
        assert result.startswith("GET http://localhost:8080/health")

    def test_headers_rendered(self) -> None:
        cv = ClientRequestContentview()
        cr = _make_cr(headers={"x-api-key": "secret", "content-type": "application/json"})
        result = _render(cv, _make_flow(FlowRecord(direction="inbound", client_request=cr)))
        assert "  x-api-key: secret" in result
        assert "  content-type: application/json" in result

    def test_empty_body_marker(self) -> None:
        cv = ClientRequestContentview()
        cr = _make_cr(body=b"")
        result = _render(cv, _make_flow(FlowRecord(direction="inbound", client_request=cr)))
        assert "--- Body ---" in result
        assert "(empty)" in result

    def test_valid_json_body_pretty_printed(self) -> None:
        cv = ClientRequestContentview()
        payload = {"model": "claude-sonnet", "messages": [{"role": "user", "content": "hi"}]}
        cr = _make_cr(body=json.dumps(payload).encode())
        result = _render(cv, _make_flow(FlowRecord(direction="inbound", client_request=cr)))
        assert '"model": "claude-sonnet"' in result
        assert '"role": "user"' in result

    def test_non_json_body_decoded_as_utf8(self) -> None:
        cv = ClientRequestContentview()
        cr = _make_cr(body=b"plain text body")
        result = _render(cv, _make_flow(FlowRecord(direction="inbound", client_request=cr)))
        assert "plain text body" in result

    def test_invalid_utf8_bytes_replaced(self) -> None:
        cv = ClientRequestContentview()
        cr = _make_cr(body=b"data-\xff-end")  # \xff is invalid UTF-8
        result = _render(cv, _make_flow(FlowRecord(direction="inbound", client_request=cr)))
        assert "data-" in result
        assert "-end" in result

    def test_sections_structure(self) -> None:
        cv = ClientRequestContentview()
        cr = _make_cr(headers={"h": "v"}, body=b'{"k": 1}')
        result = _render(cv, _make_flow(FlowRecord(direction="inbound", client_request=cr)))
        assert "--- Headers ---" in result
        assert "--- Body ---" in result
