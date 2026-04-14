"""Tests for MitmwebClient in ccproxy.tools.flows."""

import base64
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from ccproxy.tools.flows import (
    Flows,
    MitmwebClient,
    _body_to_har_text,
    _build_har,
    _build_har_entry,
    _build_har_request,
    _build_har_response,
    _build_timings,
    _do_diff,
    _do_inspect,
    _do_list,
    _header_value,
    _headers_to_har,
    _make_client,
    _ms_delta,
    _parse_client_request_text,
    _query_string,
    _safe_fetch,
    handle_flows,
)


class TestMitmwebClientListFlows:
    """Tests for MitmwebClient.list_flows."""

    def test_list_flows_returns_parsed_json(self) -> None:
        payload = [{"id": "abc123", "request": {"method": "POST"}}]
        mock_resp = MagicMock()
        mock_resp.json.return_value = payload
        mock_resp.raise_for_status = MagicMock()

        client = MitmwebClient(host="localhost", port=8084, token="tok")  # noqa: S106
        client._client = MagicMock()
        client._client.get.return_value = mock_resp

        result = client.list_flows()

        client._client.get.assert_called_once_with("/flows")
        mock_resp.raise_for_status.assert_called_once()
        assert result == payload

    def test_list_flows_raises_on_http_error(self) -> None:
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "403", request=MagicMock(), response=MagicMock()
        )

        client = MitmwebClient(host="localhost", port=8084, token="tok")  # noqa: S106
        client._client = MagicMock()
        client._client.get.return_value = mock_resp

        with pytest.raises(httpx.HTTPStatusError):
            client.list_flows()

    def test_list_flows_empty_list(self) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = []
        mock_resp.raise_for_status = MagicMock()

        client = MitmwebClient(host="localhost", port=8084, token="tok")  # noqa: S106
        client._client = MagicMock()
        client._client.get.return_value = mock_resp

        assert client.list_flows() == []


class TestMitmwebClientGetRequestBody:
    """Tests for MitmwebClient.get_request_body."""

    def test_returns_raw_bytes(self) -> None:
        mock_resp = MagicMock()
        mock_resp.content = b'{"model": "claude"}'
        mock_resp.raise_for_status = MagicMock()

        client = MitmwebClient(host="localhost", port=8084, token="tok")  # noqa: S106
        client._client = MagicMock()
        client._client.get.return_value = mock_resp

        result = client.get_request_body("flow-id-1")

        client._client.get.assert_called_once_with("/flows/flow-id-1/request/content.data")
        assert result == b'{"model": "claude"}'

    def test_raises_on_http_error(self) -> None:
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "404", request=MagicMock(), response=MagicMock()
        )

        client = MitmwebClient(host="localhost", port=8084, token="tok")  # noqa: S106
        client._client = MagicMock()
        client._client.get.return_value = mock_resp

        with pytest.raises(httpx.HTTPStatusError):
            client.get_request_body("missing-id")


class TestMitmwebClientGetResponseBody:
    """Tests for MitmwebClient.get_response_body."""

    def test_returns_raw_bytes(self) -> None:
        mock_resp = MagicMock()
        mock_resp.content = b'{"id": "msg-1"}'
        mock_resp.raise_for_status = MagicMock()

        client = MitmwebClient(host="localhost", port=8084, token="tok")  # noqa: S106
        client._client = MagicMock()
        client._client.get.return_value = mock_resp

        result = client.get_response_body("flow-id-2")

        client._client.get.assert_called_once_with("/flows/flow-id-2/response/content.data")
        assert result == b'{"id": "msg-1"}'

    def test_raises_on_http_error(self) -> None:
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "404", request=MagicMock(), response=MagicMock()
        )

        client = MitmwebClient(host="localhost", port=8084, token="tok")  # noqa: S106
        client._client = MagicMock()
        client._client.get.return_value = mock_resp

        with pytest.raises(httpx.HTTPStatusError):
            client.get_response_body("missing-id")


class TestMitmwebClientGetClientRequest:
    """Tests for MitmwebClient.get_client_request — returns structured dict."""

    _CONTENTVIEW_TEXT = (
        "POST https://api.anthropic.com:443/v1/messages\n"
        "\n"
        "--- Headers ---\n"
        "  content-type: application/json\n"
        "  user-agent: claude-code/1.0\n"
        "\n"
        "--- Body ---\n"
        '{"model": "claude-3-5-sonnet"}'
    )

    def test_parses_dict_text_field(self) -> None:
        """contentview returns {text: ..., view_name: ...} — text field is parsed."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "text": self._CONTENTVIEW_TEXT,
            "view_name": "Client-Request",
            "syntax_highlight": "yaml",
            "description": "",
        }
        mock_resp.raise_for_status = MagicMock()

        client = MitmwebClient(host="localhost", port=8084, token="tok")  # noqa: S106
        client._client = MagicMock()
        client._client.get.return_value = mock_resp

        result = client.get_client_request("flow-id-3")

        client._client.get.assert_called_once_with(
            "/flows/flow-id-3/request/content/client-request"
        )
        assert isinstance(result, dict)
        assert result["method"] == "POST"
        assert result["url"] == "https://api.anthropic.com:443/v1/messages"
        assert {"name": "content-type", "value": "application/json"} in result["headers"]
        assert result["body_text"] == '{"model": "claude-3-5-sonnet"}'

    def test_falls_back_to_list_format(self) -> None:
        """List format [[label, text]] — first entry's text element is parsed."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = [["Client-Request", self._CONTENTVIEW_TEXT]]
        mock_resp.raise_for_status = MagicMock()

        client = MitmwebClient(host="localhost", port=8084, token="tok")  # noqa: S106
        client._client = MagicMock()
        client._client.get.return_value = mock_resp

        result = client.get_client_request("flow-id-4")

        assert isinstance(result, dict)
        assert result["method"] == "POST"

    def test_falls_back_to_text_on_non_list_response(self) -> None:
        """If contentview returns a non-list non-dict, fall back to resp.text."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = "not a dict"
        mock_resp.text = self._CONTENTVIEW_TEXT
        mock_resp.raise_for_status = MagicMock()

        client = MitmwebClient(host="localhost", port=8084, token="tok")  # noqa: S106
        client._client = MagicMock()
        client._client.get.return_value = mock_resp

        result = client.get_client_request("flow-id-5")

        assert isinstance(result, dict)
        assert result["method"] == "POST"

    def test_returns_dict_for_empty_list(self) -> None:
        """Empty list response falls back to resp.text, parsed as dict."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = []
        mock_resp.text = ""
        mock_resp.raise_for_status = MagicMock()

        client = MitmwebClient(host="localhost", port=8084, token="tok")  # noqa: S106
        client._client = MagicMock()
        client._client.get.return_value = mock_resp

        result = client.get_client_request("flow-id-6")

        assert isinstance(result, dict)
        assert result["method"] == ""
        assert result["url"] == ""
        assert result["headers"] == []
        assert result["body_text"] == ""

    def test_raises_on_http_error(self) -> None:
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "404", request=MagicMock(), response=MagicMock()
        )

        client = MitmwebClient(host="localhost", port=8084, token="tok")  # noqa: S106
        client._client = MagicMock()
        client._client.get.return_value = mock_resp

        with pytest.raises(httpx.HTTPStatusError):
            client.get_client_request("missing-id")


class TestMitmwebClientPost:
    """Tests for MitmwebClient._post (XSRF token pair generation)."""

    def test_post_generates_xsrf_token_on_first_call(self) -> None:
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()

        client = MitmwebClient(host="localhost", port=8084, token="tok")  # noqa: S106
        client._client = MagicMock()
        client._client.post.return_value = mock_resp

        assert client._xsrf is None
        client._post("/clear")

        assert client._xsrf is not None
        assert len(client._xsrf) == 32  # secrets.token_hex(16) → 32 hex chars

    def test_post_reuses_existing_xsrf_token(self) -> None:
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()

        client = MitmwebClient(host="localhost", port=8084, token="tok")  # noqa: S106
        client._client = MagicMock()
        client._client.post.return_value = mock_resp
        client._xsrf = "presettoken1234"

        client._post("/some-path")

        assert client._xsrf == "presettoken1234"

    def test_post_sets_xsrf_cookie_and_header(self) -> None:
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()

        client = MitmwebClient(host="localhost", port=8084, token="tok")  # noqa: S106
        client._client = MagicMock()
        client._client.cookies = MagicMock()
        client._client.post.return_value = mock_resp

        client._post("/clear")

        client._client.cookies.set.assert_called_once_with("_xsrf", client._xsrf)
        call_kwargs = client._client.post.call_args
        assert call_kwargs.kwargs["headers"]["X-XSRFToken"] == client._xsrf

    def test_post_raises_on_http_error(self) -> None:
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "403", request=MagicMock(), response=MagicMock()
        )

        client = MitmwebClient(host="localhost", port=8084, token="tok")  # noqa: S106
        client._client = MagicMock()
        client._client.post.return_value = mock_resp

        with pytest.raises(httpx.HTTPStatusError):
            client._post("/clear")


class TestMitmwebClientClear:
    """Tests for MitmwebClient.clear."""

    def test_clear_calls_post_clear(self) -> None:
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()

        client = MitmwebClient(host="localhost", port=8084, token="tok")  # noqa: S106
        client._client = MagicMock()
        client._client.cookies = MagicMock()
        client._client.post.return_value = mock_resp

        client.clear()

        client._client.post.assert_called_once()
        call_args = client._client.post.call_args
        assert call_args.args[0] == "/clear"

    def test_clear_raises_on_http_error(self) -> None:
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500", request=MagicMock(), response=MagicMock()
        )

        client = MitmwebClient(host="localhost", port=8084, token="tok")  # noqa: S106
        client._client = MagicMock()
        client._client.cookies = MagicMock()
        client._client.post.return_value = mock_resp

        with pytest.raises(httpx.HTTPStatusError):
            client.clear()


class TestMitmwebClientResolveId:
    """Tests for MitmwebClient.resolve_id."""

    def test_finds_flow_by_prefix(self) -> None:
        flows = [
            {"id": "abcdef123456"},
            {"id": "xyz987654321"},
        ]
        mock_resp = MagicMock()
        mock_resp.json.return_value = flows
        mock_resp.raise_for_status = MagicMock()

        client = MitmwebClient(host="localhost", port=8084, token="tok")  # noqa: S106
        client._client = MagicMock()
        client._client.get.return_value = mock_resp

        result = client.resolve_id("abc")
        assert result == "abcdef123456"

    def test_raises_value_error_when_no_match(self) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = [{"id": "abcdef123456"}]
        mock_resp.raise_for_status = MagicMock()

        client = MitmwebClient(host="localhost", port=8084, token="tok")  # noqa: S106
        client._client = MagicMock()
        client._client.get.return_value = mock_resp

        with pytest.raises(ValueError, match="no-match"):
            client.resolve_id("no-match")


class TestMitmwebClientContextManager:
    """Tests for MitmwebClient context manager protocol."""

    def test_enter_returns_self(self) -> None:
        client = MitmwebClient(host="localhost", port=8084, token="tok")  # noqa: S106
        client._client = MagicMock()

        result = client.__enter__()
        assert result is client

    def test_exit_calls_close(self) -> None:
        client = MitmwebClient(host="localhost", port=8084, token="tok")  # noqa: S106
        client._client = MagicMock()

        client.__exit__(None, None, None)
        client._client.close.assert_called_once()


class TestMakeClient:
    """Tests for the _make_client factory function."""

    def test_builds_client_from_config(self) -> None:
        mock_config = MagicMock()
        mock_config.inspector.mitmproxy.web_host = "127.0.0.1"
        mock_config.inspector.port = 8084
        mock_config.inspector.mitmproxy.web_password = "secret-token"  # noqa: S105

        with patch("ccproxy.config.get_config", return_value=mock_config):
            client = _make_client()

        assert client._base == "http://127.0.0.1:8084"

    def test_builds_client_with_empty_token_when_password_is_none(self) -> None:
        mock_config = MagicMock()
        mock_config.inspector.mitmproxy.web_host = "localhost"
        mock_config.inspector.port = 8084
        mock_config.inspector.mitmproxy.web_password = None

        with patch("ccproxy.config.get_config", return_value=mock_config):
            client = _make_client()

        assert client._base == "http://localhost:8084"


class TestHeaderValue:
    def test_extracts_matching_header(self) -> None:
        headers = [["Content-Type", "application/json"], ["User-Agent", "claude"]]
        assert _header_value(headers, "user-agent") == "claude"

    def test_case_insensitive_match(self) -> None:
        headers = [["X-Api-Key", "secret"]]
        assert _header_value(headers, "x-api-key") == "secret"

    def test_missing_header_returns_empty(self) -> None:
        assert _header_value([], "missing") == ""
        assert _header_value([["other", "val"]], "missing") == ""


class TestParseClientRequestText:
    """Tests for _parse_client_request_text."""

    def test_empty_input(self) -> None:
        result = _parse_client_request_text("")
        assert result == {"method": "", "url": "", "headers": [], "body_text": ""}

    def test_well_formed_full_input(self) -> None:
        text = (
            "POST https://api.anthropic.com:443/v1/messages\n"
            "\n"
            "--- Headers ---\n"
            "  content-type: application/json\n"
            "  user-agent: claude-code/1.0\n"
            "\n"
            "--- Body ---\n"
            '{"model": "claude-3-5-sonnet"}'
        )
        result = _parse_client_request_text(text)
        assert result["method"] == "POST"
        assert result["url"] == "https://api.anthropic.com:443/v1/messages"
        assert {"name": "content-type", "value": "application/json"} in result["headers"]
        assert {"name": "user-agent", "value": "claude-code/1.0"} in result["headers"]
        assert result["body_text"] == '{"model": "claude-3-5-sonnet"}'

    def test_empty_body_marker(self) -> None:
        text = (
            "GET https://example.com/\n"
            "\n"
            "--- Headers ---\n"
            "  accept: */*\n"
            "\n"
            "--- Body ---\n"
            "(empty)"
        )
        result = _parse_client_request_text(text)
        assert result["body_text"] == ""

    def test_body_with_multiline_content(self) -> None:
        text = (
            "POST https://example.com/api\n"
            "\n"
            "--- Headers ---\n"
            "  content-type: application/json\n"
            "\n"
            "--- Body ---\n"
            "line one\n"
            "line two\n"
            "line three"
        )
        result = _parse_client_request_text(text)
        assert result["body_text"] == "line one\nline two\nline three"

    def test_malformed_first_line_no_space(self) -> None:
        text = "https://example.com/\n\n--- Headers ---\n"
        result = _parse_client_request_text(text)
        assert result["method"] == ""
        assert result["url"] == "https://example.com/"

    def test_header_value_with_colon(self) -> None:
        text = (
            "GET https://example.com/\n"
            "\n"
            "--- Headers ---\n"
            "  authorization: Bearer tok:extra:colons\n"
            "\n"
            "--- Body ---\n"
            "(empty)"
        )
        result = _parse_client_request_text(text)
        assert {"name": "authorization", "value": "Bearer tok:extra:colons"} in result["headers"]

    def test_no_headers_or_body_sections(self) -> None:
        text = "DELETE https://example.com/resource"
        result = _parse_client_request_text(text)
        assert result["method"] == "DELETE"
        assert result["url"] == "https://example.com/resource"
        assert result["headers"] == []
        assert result["body_text"] == ""


class TestSafeFetch:
    """Tests for _safe_fetch."""

    def test_success_returns_bytes(self) -> None:
        fetch = MagicMock(return_value=b"response body")
        result = _safe_fetch(fetch, "flow-id-1")
        assert result == b"response body"
        fetch.assert_called_once_with("flow-id-1")

    def test_http_status_error_returns_empty_bytes(self) -> None:
        fetch = MagicMock(
            side_effect=httpx.HTTPStatusError(
                "500", request=MagicMock(), response=MagicMock()
            )
        )
        result = _safe_fetch(fetch, "flow-id-2")
        assert result == b""

    def test_non_http_error_propagates(self) -> None:
        fetch = MagicMock(side_effect=ValueError("unexpected"))
        with pytest.raises(ValueError, match="unexpected"):
            _safe_fetch(fetch, "flow-id-3")


class TestHeadersToHar:
    """Tests for _headers_to_har."""

    def test_empty_list(self) -> None:
        assert _headers_to_har([]) == []

    def test_single_header(self) -> None:
        result = _headers_to_har([["Content-Type", "application/json"]])
        assert result == [{"name": "Content-Type", "value": "application/json"}]

    def test_multiple_headers(self) -> None:
        headers = [
            ["Content-Type", "application/json"],
            ["Authorization", "Bearer tok"],
        ]
        result = _headers_to_har(headers)
        assert result == [
            {"name": "Content-Type", "value": "application/json"},
            {"name": "Authorization", "value": "Bearer tok"},
        ]


class TestQueryString:
    """Tests for _query_string."""

    def test_no_query(self) -> None:
        assert _query_string("/v1/messages") == []

    def test_single_param(self) -> None:
        result = _query_string("/v1/messages?key=AIzaXXX")
        assert result == [{"name": "key", "value": "AIzaXXX"}]

    def test_multiple_params(self) -> None:
        result = _query_string("/search?q=hello&limit=10")
        assert result == [
            {"name": "q", "value": "hello"},
            {"name": "limit", "value": "10"},
        ]

    def test_param_with_no_value(self) -> None:
        result = _query_string("/api?flag")
        assert result == [{"name": "flag", "value": ""}]

    def test_full_url_with_query(self) -> None:
        result = _query_string("https://example.com/api?model=claude&stream=true")
        assert result == [
            {"name": "model", "value": "claude"},
            {"name": "stream", "value": "true"},
        ]


class TestBodyToHarText:
    """Tests for _body_to_har_text."""

    def test_utf8_text(self) -> None:
        raw = b'{"key": "value"}'
        text, encoding = _body_to_har_text(raw)
        assert text == '{"key": "value"}'
        assert encoding is None

    def test_binary_bytes(self) -> None:
        raw = bytes(range(256))
        text, encoding = _body_to_har_text(raw)
        assert encoding == "base64"
        assert text == base64.b64encode(raw).decode("ascii")

    def test_empty_bytes(self) -> None:
        text, encoding = _body_to_har_text(b"")
        assert text == ""
        assert encoding is None


class TestMsDelta:
    """Tests for _ms_delta."""

    def test_normal_delta(self) -> None:
        result = _ms_delta(1234567891.0, 1234567890.0)
        assert result == pytest.approx(1000.0)

    def test_none_earlier(self) -> None:
        assert _ms_delta(1234567891.0, None) == -1.0

    def test_none_later(self) -> None:
        assert _ms_delta(None, 1234567890.0) == -1.0

    def test_both_none(self) -> None:
        assert _ms_delta(None, None) == -1.0


class TestBuildTimings:
    """Tests for _build_timings."""

    def _make_req(self, start: float = 1234567890.0, end: float = 1234567890.1) -> dict:
        return {"timestamp_start": start, "timestamp_end": end}

    def _make_res(self, start: float = 1234567890.2, end: float = 1234567890.5) -> dict:
        return {
            "timestamp_start": start,
            "timestamp_end": end,
            "status_code": 200,
        }

    def _make_server_conn(
        self,
        start: float = 1234567889.8,
        tcp_setup: float = 1234567889.9,
        tls_setup: float = 1234567889.95,
    ) -> dict:
        return {
            "timestamp_start": start,
            "timestamp_tcp_setup": tcp_setup,
            "timestamp_tls_setup": tls_setup,
        }

    def test_full_timing_data(self) -> None:
        req = self._make_req()
        res = self._make_res()
        sc = self._make_server_conn()
        timings = _build_timings(req, res, sc)
        assert "connect" in timings
        assert "ssl" in timings
        assert "send" in timings
        assert "wait" in timings
        assert "receive" in timings
        assert timings["connect"] == pytest.approx(100.0, rel=1e-3)
        assert timings["ssl"] == pytest.approx(50.0, rel=1e-3)
        assert timings["send"] == pytest.approx(100.0, rel=1e-3)
        assert timings["receive"] == pytest.approx(300.0, rel=1e-3)

    def test_missing_response(self) -> None:
        req = self._make_req()
        sc = self._make_server_conn()
        timings = _build_timings(req, None, sc)
        assert timings["wait"] == 0.0
        assert timings["receive"] == 0.0

    def test_missing_server_conn_timestamps(self) -> None:
        req = self._make_req()
        res = self._make_res()
        sc: dict = {}
        timings = _build_timings(req, res, sc)
        assert timings["connect"] == -1.0
        assert timings["ssl"] == -1.0


class TestBuildHarRequest:
    """Tests for _build_har_request."""

    def _make_flow(self) -> dict:
        return {
            "id": "flow-123",
            "request": {
                "method": "POST",
                "scheme": "https",
                "pretty_host": "api.anthropic.com",
                "path": "/v1/messages",
                "headers": [["content-type", "application/json"]],
                "http_version": "HTTP/1.1",
                "timestamp_start": 1234567890.0,
                "timestamp_end": 1234567890.1,
            },
            "response": None,
            "server_conn": {},
        }

    def test_forwarded_request_with_body(self) -> None:
        flow = self._make_flow()
        body = b'{"model": "claude"}'
        result = _build_har_request(flow, body, client_req=None)
        assert result["method"] == "POST"
        assert result["url"] == "https://api.anthropic.com/v1/messages"
        assert result["postData"]["text"] == '{"model": "claude"}'
        assert result["bodySize"] == len(body)

    def test_forwarded_get_request_no_post_data(self) -> None:
        flow = self._make_flow()
        flow["request"]["method"] = "GET"
        flow["request"]["path"] = "/v1/models"
        result = _build_har_request(flow, b"", client_req=None)
        assert result["method"] == "GET"
        assert "postData" not in result

    def test_client_req_override(self) -> None:
        flow = self._make_flow()
        client_req = {
            "method": "POST",
            "url": "http://127.0.0.1:4000/v1/messages",
            "headers": [{"name": "content-type", "value": "application/json"}],
            "body_text": '{"model": "claude-3-5-sonnet"}',
        }
        result = _build_har_request(flow, b"", client_req=client_req)
        assert result["method"] == "POST"
        assert result["url"] == "http://127.0.0.1:4000/v1/messages"
        assert result["postData"]["text"] == '{"model": "claude-3-5-sonnet"}'


class TestBuildHarResponse:
    """Tests for _build_har_response."""

    def _make_flow_with_response(self) -> dict:
        return {
            "id": "flow-123",
            "request": {
                "method": "POST",
                "scheme": "https",
                "pretty_host": "api.anthropic.com",
                "path": "/v1/messages",
                "headers": [],
                "timestamp_start": 1234567890.0,
                "timestamp_end": 1234567890.1,
            },
            "response": {
                "status_code": 200,
                "reason": "OK",
                "headers": [["content-type", "application/json"]],
                "http_version": "HTTP/1.1",
                "timestamp_start": 1234567890.2,
                "timestamp_end": 1234567890.5,
            },
            "server_conn": {},
        }

    def test_with_response_and_body(self) -> None:
        flow = self._make_flow_with_response()
        body = b'{"id": "msg-1"}'
        result = _build_har_response(flow, body)
        assert result["status"] == 200
        assert result["statusText"] == "OK"
        assert result["content"]["text"] == '{"id": "msg-1"}'
        assert result["bodySize"] == len(body)

    def test_no_response_returns_stub(self) -> None:
        flow = self._make_flow_with_response()
        flow["response"] = None
        result = _build_har_response(flow, b"")
        assert result["status"] == 0
        assert result["statusText"] == ""
        assert result["content"]["size"] == 0

    def test_binary_body_base64_encoding(self) -> None:
        flow = self._make_flow_with_response()
        # bytes 0x80-0xFF are invalid UTF-8 start bytes - forces base64 encoding
        raw = bytes(range(128, 256))
        result = _build_har_response(flow, raw)
        assert result["content"]["encoding"] == "base64"
        assert result["content"]["text"] == base64.b64encode(raw).decode("ascii")


class TestBuildHarEntry:
    """Tests for _build_har_entry."""

    def _make_flow(self) -> dict:
        return {
            "id": "full-flow-id-123",
            "request": {
                "method": "POST",
                "scheme": "https",
                "pretty_host": "api.anthropic.com",
                "path": "/v1/messages",
                "headers": [["content-type", "application/json"]],
                "http_version": "HTTP/1.1",
                "timestamp_start": 1234567890.0,
                "timestamp_end": 1234567890.1,
            },
            "response": {
                "status_code": 200,
                "reason": "OK",
                "headers": [["content-type", "application/json"]],
                "http_version": "HTTP/1.1",
                "timestamp_start": 1234567890.2,
                "timestamp_end": 1234567890.5,
            },
            "server_conn": {
                "peername": None,
                "timestamp_start": 1234567889.8,
                "timestamp_tcp_setup": 1234567889.9,
                "timestamp_tls_setup": 1234567889.95,
            },
        }

    def test_full_happy_path(self) -> None:
        flow = self._make_flow()
        entry = _build_har_entry(flow, b'{"model": "claude"}', b'{"id": "msg-1"}')
        assert "startedDateTime" in entry
        assert entry["request"]["method"] == "POST"
        assert entry["response"]["status"] == 200
        assert "timings" in entry
        assert "cache" in entry

    def test_no_response(self) -> None:
        flow = self._make_flow()
        flow["response"] = None
        entry = _build_har_entry(flow, b"", b"")
        assert entry["response"]["status"] == 0

    def test_with_client_req(self) -> None:
        flow = self._make_flow()
        client_req = {
            "method": "POST",
            "url": "http://127.0.0.1:4000/v1/messages",
            "headers": [{"name": "content-type", "value": "application/json"}],
            "body_text": '{"model": "claude-3-5-sonnet"}',
        }
        entry = _build_har_entry(flow, b"", b"", client_req=client_req)
        assert entry["request"]["url"] == "http://127.0.0.1:4000/v1/messages"

    def test_with_peername(self) -> None:
        flow = self._make_flow()
        flow["server_conn"]["peername"] = ["192.168.1.1", 443]
        entry = _build_har_entry(flow, b"", b"")
        assert entry["serverIPAddress"] == "192.168.1.1"


class TestBuildHar:
    """Tests for _build_har."""

    def test_wraps_entry_in_har_log(self) -> None:
        entry = {"startedDateTime": "2024-01-01T00:00:00+00:00", "time": 100.0}
        har = _build_har(entry)
        assert har["log"]["version"] == "1.2"
        assert har["log"]["creator"]["name"] == "ccproxy"
        assert len(har["log"]["entries"]) == 1
        assert har["log"]["entries"][0] is entry

    def test_round_trip_json(self) -> None:
        entry = {"startedDateTime": "2024-01-01T00:00:00+00:00", "time": 42.0}
        har = _build_har(entry)
        serialized = json.dumps(har, indent=2)
        parsed = json.loads(serialized)
        assert parsed["log"]["version"] == "1.2"
        assert parsed["log"]["entries"][0]["time"] == 42.0


class TestDoList:
    def _make_mock_flow(self, id: str = "abc123def", host: str = "api.openai.com",
                        path: str = "/v1/chat/completions", method: str = "POST",
                        status_code: int = 200) -> dict:
        return {
            "id": id,
            "request": {
                "method": method,
                "pretty_host": host,
                "path": path,
                "scheme": "https",
                "headers": [["user-agent", "claude-code/1.0"]],
            },
            "response": {"status_code": status_code},
        }

    def test_list_renders_table(self) -> None:
        console = MagicMock()
        client = MagicMock()
        client.list_flows.return_value = [self._make_mock_flow()]

        _do_list(console, client)

        console.print.assert_called_once()

    def test_list_empty_shows_message(self) -> None:
        console = MagicMock()
        client = MagicMock()
        client.list_flows.return_value = []

        _do_list(console, client)

        console.print.assert_called_once()
        assert "No flows" in str(console.print.call_args)

    def test_list_json_output(self) -> None:
        console = MagicMock()
        client = MagicMock()
        client.list_flows.return_value = [self._make_mock_flow()]

        _do_list(console, client, json_output=True)

        console.print_json.assert_called_once()

    def test_list_filter_pattern(self) -> None:
        console = MagicMock()
        client = MagicMock()
        client.list_flows.return_value = [
            self._make_mock_flow(id="a1", host="api.openai.com"),
            self._make_mock_flow(id="b2", host="api.anthropic.com"),
        ]

        _do_list(console, client, filter_pat="anthropic")

        # Only one flow matches the filter, table still rendered
        console.print.assert_called_once()

    def test_list_flow_no_response(self) -> None:
        console = MagicMock()
        client = MagicMock()
        flow = self._make_mock_flow()
        flow["response"] = None
        client.list_flows.return_value = [flow]

        _do_list(console, client)
        console.print.assert_called_once()


class TestDoInspect:
    def _make_flow_data(self) -> dict:
        return {
            "id": "full-flow-id-123",
            "request": {
                "method": "POST",
                "scheme": "https",
                "pretty_host": "api.anthropic.com",
                "path": "/v1/messages",
                "headers": [["content-type", "application/json"]],
                "http_version": "HTTP/1.1",
                "timestamp_start": 1234567890.0,
                "timestamp_end": 1234567890.1,
            },
            "response": {
                "status_code": 200,
                "reason": "OK",
                "headers": [["content-type", "application/json"]],
                "http_version": "HTTP/1.1",
                "timestamp_start": 1234567890.2,
                "timestamp_end": 1234567890.5,
            },
            "server_conn": {
                "peername": None,
                "timestamp_start": 1234567889.8,
                "timestamp_tcp_setup": 1234567889.9,
                "timestamp_tls_setup": 1234567889.95,
            },
        }

    def test_inspect_request(self, capsys: pytest.CaptureFixture) -> None:
        client = MagicMock()
        client.resolve_id.return_value = "full-flow-id-123"
        client.list_flows.return_value = [self._make_flow_data()]
        client.get_request_body.return_value = b'{"model": "claude"}'
        client.get_response_body.return_value = b""

        _do_inspect(client, action="req", id_prefix="full")

        captured = capsys.readouterr()
        har = json.loads(captured.out)
        assert har["log"]["version"] == "1.2"
        assert har["log"]["entries"][0]["request"]["method"] == "POST"

    def test_inspect_response(self, capsys: pytest.CaptureFixture) -> None:
        client = MagicMock()
        client.resolve_id.return_value = "full-flow-id-123"
        client.list_flows.return_value = [self._make_flow_data()]
        client.get_request_body.return_value = b""
        client.get_response_body.return_value = b'{"content": "hello"}'

        _do_inspect(client, action="res", id_prefix="full")

        captured = capsys.readouterr()
        har = json.loads(captured.out)
        assert har["log"]["entries"][0]["response"]["status"] == 200

    def test_inspect_client_request(self, capsys: pytest.CaptureFixture) -> None:
        client = MagicMock()
        client.resolve_id.return_value = "full-flow-id-123"
        client.list_flows.return_value = [self._make_flow_data()]
        client.get_request_body.return_value = b""
        client.get_response_body.return_value = b""
        client.get_client_request.return_value = {
            "method": "POST",
            "url": "http://127.0.0.1:4000/v1/messages",
            "headers": [{"name": "content-type", "value": "application/json"}],
            "body_text": '{"model": "claude-3-5-sonnet"}',
        }

        _do_inspect(client, action="client", id_prefix="full")

        client.get_client_request.assert_called_once_with("full-flow-id-123")
        captured = capsys.readouterr()
        har = json.loads(captured.out)
        assert har["log"]["entries"][0]["request"]["url"] == "http://127.0.0.1:4000/v1/messages"

    def test_inspect_response_no_response(self, capsys: pytest.CaptureFixture) -> None:
        client = MagicMock()
        flow_data = self._make_flow_data()
        flow_data["response"] = None
        client.resolve_id.return_value = "full-flow-id-123"
        client.list_flows.return_value = [flow_data]
        client.get_request_body.return_value = b""
        client.get_response_body.return_value = b""

        _do_inspect(client, action="res", id_prefix="full")

        captured = capsys.readouterr()
        har = json.loads(captured.out)
        assert har["log"]["entries"][0]["response"]["status"] == 0

    def test_inspect_flow_not_found(self, capsys: pytest.CaptureFixture) -> None:
        client = MagicMock()
        client.resolve_id.return_value = "not-in-list"
        client.list_flows.return_value = []

        with pytest.raises(SystemExit):
            _do_inspect(client, action="req", id_prefix="not")

        captured = capsys.readouterr()
        assert "not found" in captured.err


class TestDoDiff:
    def test_identical_bodies(self) -> None:
        console = MagicMock()
        client = MagicMock()
        client.resolve_id.side_effect = lambda x: f"full-{x}"
        body = b'{"model": "claude"}'
        client.get_request_body.return_value = body

        _do_diff(console, client, "a", "b")

        assert "identical" in str(console.print.call_args).lower()

    def test_different_bodies(self) -> None:
        console = MagicMock()
        client = MagicMock()
        client.resolve_id.side_effect = lambda x: f"full-{x}"
        client.get_request_body.side_effect = [
            b'{"model": "claude"}',
            b'{"model": "gpt-4o"}',
        ]

        _do_diff(console, client, "a", "b")

        console.print.assert_called_once()

    def test_non_json_bodies_diff(self) -> None:
        console = MagicMock()
        client = MagicMock()
        client.resolve_id.side_effect = lambda x: f"full-{x}"
        client.get_request_body.side_effect = [b"text-a", b"text-b"]

        _do_diff(console, client, "a", "b")

        console.print.assert_called_once()


class TestHandleFlows:
    """Tests for the handle_flows dispatcher."""

    @patch("ccproxy.tools.flows._make_client")
    @patch("ccproxy.tools.flows._do_list")
    def test_default_action_calls_list(self, mock_list: MagicMock, mock_client: MagicMock) -> None:
        mock_ctx = MagicMock()
        mock_client.return_value.__enter__ = MagicMock(return_value=mock_ctx)
        mock_client.return_value.__exit__ = MagicMock(return_value=False)

        cmd = Flows(args=[])
        handle_flows(cmd, Path("/tmp"))  # noqa: S108

        mock_list.assert_called_once()

    @patch("ccproxy.tools.flows._make_client")
    @patch("ccproxy.tools.flows._do_list")
    def test_explicit_list_action(self, mock_list: MagicMock, mock_client: MagicMock) -> None:
        mock_ctx = MagicMock()
        mock_client.return_value.__enter__ = MagicMock(return_value=mock_ctx)
        mock_client.return_value.__exit__ = MagicMock(return_value=False)

        cmd = Flows(args=["list"], json=True, filter="anthropic")
        handle_flows(cmd, Path("/tmp"))  # noqa: S108

        mock_list.assert_called_once()
        call_kwargs = mock_list.call_args
        assert call_kwargs.kwargs.get("json_output") is True
        assert call_kwargs.kwargs.get("filter_pat") == "anthropic"

    @patch("ccproxy.tools.flows._make_client")
    @patch("ccproxy.tools.flows._do_inspect")
    def test_req_action(self, mock_inspect: MagicMock, mock_client: MagicMock) -> None:
        mock_ctx = MagicMock()
        mock_client.return_value.__enter__ = MagicMock(return_value=mock_ctx)
        mock_client.return_value.__exit__ = MagicMock(return_value=False)

        cmd = Flows(args=["req", "abc123"])
        handle_flows(cmd, Path("/tmp"))  # noqa: S108

        mock_inspect.assert_called_once()
        assert mock_inspect.call_args.kwargs["action"] == "req"
        assert mock_inspect.call_args.kwargs["id_prefix"] == "abc123"

    @patch("ccproxy.tools.flows._make_client")
    @patch("ccproxy.tools.flows._do_inspect")
    def test_client_action(self, mock_inspect: MagicMock, mock_client: MagicMock) -> None:
        mock_ctx = MagicMock()
        mock_client.return_value.__enter__ = MagicMock(return_value=mock_ctx)
        mock_client.return_value.__exit__ = MagicMock(return_value=False)

        cmd = Flows(args=["client", "abc"])
        handle_flows(cmd, Path("/tmp"))  # noqa: S108

        mock_inspect.assert_called_once()
        assert mock_inspect.call_args.kwargs["action"] == "client"

    @patch("ccproxy.tools.flows._make_client")
    @patch("ccproxy.tools.flows._do_diff")
    def test_diff_action(self, mock_diff: MagicMock, mock_client: MagicMock) -> None:
        mock_ctx = MagicMock()
        mock_client.return_value.__enter__ = MagicMock(return_value=mock_ctx)
        mock_client.return_value.__exit__ = MagicMock(return_value=False)

        cmd = Flows(args=["diff", "a1", "b2"])
        handle_flows(cmd, Path("/tmp"))  # noqa: S108

        mock_diff.assert_called_once()

    @patch("ccproxy.tools.flows._make_client")
    def test_req_without_id_exits(self, mock_client: MagicMock) -> None:
        mock_ctx = MagicMock()
        mock_client.return_value.__enter__ = MagicMock(return_value=mock_ctx)
        mock_client.return_value.__exit__ = MagicMock(return_value=False)

        cmd = Flows(args=["req"])
        with pytest.raises(SystemExit):
            handle_flows(cmd, Path("/tmp"))  # noqa: S108

    @patch("ccproxy.tools.flows._make_client")
    def test_diff_without_two_ids_exits(self, mock_client: MagicMock) -> None:
        mock_ctx = MagicMock()
        mock_client.return_value.__enter__ = MagicMock(return_value=mock_ctx)
        mock_client.return_value.__exit__ = MagicMock(return_value=False)

        cmd = Flows(args=["diff", "only-one"])
        with pytest.raises(SystemExit):
            handle_flows(cmd, Path("/tmp"))  # noqa: S108

    @patch("ccproxy.tools.flows._make_client")
    def test_unknown_action_exits(self, mock_client: MagicMock) -> None:
        mock_ctx = MagicMock()
        mock_client.return_value.__enter__ = MagicMock(return_value=mock_ctx)
        mock_client.return_value.__exit__ = MagicMock(return_value=False)

        cmd = Flows(args=["bogus"])
        with pytest.raises(SystemExit):
            handle_flows(cmd, Path("/tmp"))  # noqa: S108

    @patch("ccproxy.tools.flows._make_client")
    def test_clear_flag(self, mock_client: MagicMock) -> None:
        mock_ctx = MagicMock()
        mock_client.return_value.__enter__ = MagicMock(return_value=mock_ctx)
        mock_client.return_value.__exit__ = MagicMock(return_value=False)

        cmd = Flows(clear=True)
        handle_flows(cmd, Path("/tmp"))  # noqa: S108

        mock_ctx.clear.assert_called_once()

    @patch("ccproxy.tools.flows._make_client")
    def test_clear_error_exits(self, mock_client: MagicMock) -> None:
        mock_ctx = MagicMock()
        mock_ctx.clear.side_effect = httpx.HTTPError("clear failed")
        mock_client.return_value.__enter__ = MagicMock(return_value=mock_ctx)
        mock_client.return_value.__exit__ = MagicMock(return_value=False)

        cmd = Flows(clear=True)
        with pytest.raises(SystemExit):
            handle_flows(cmd, Path("/tmp"))  # noqa: S108

    @patch("ccproxy.tools.flows._make_client")
    @patch("ccproxy.tools.flows._do_list")
    def test_clear_then_list(self, mock_list: MagicMock, mock_client: MagicMock) -> None:
        mock_ctx = MagicMock()
        mock_client.return_value.__enter__ = MagicMock(return_value=mock_ctx)
        mock_client.return_value.__exit__ = MagicMock(return_value=False)

        cmd = Flows(args=["list"], clear=True)
        handle_flows(cmd, Path("/tmp"))  # noqa: S108

        mock_ctx.clear.assert_called_once()
        mock_list.assert_called_once()

    @patch("ccproxy.tools.flows._make_client")
    def test_connect_error_exits(self, mock_client: MagicMock) -> None:
        mock_client.return_value.__enter__ = MagicMock(side_effect=httpx.ConnectError("refused"))
        mock_client.return_value.__exit__ = MagicMock(return_value=False)

        cmd = Flows(args=["list"])
        with pytest.raises(SystemExit):
            handle_flows(cmd, Path("/tmp"))  # noqa: S108

    @patch("ccproxy.tools.flows._make_client")
    def test_http_status_error_exits(self, mock_client: MagicMock) -> None:
        mock_ctx = MagicMock()
        resp = MagicMock()
        resp.status_code = 403
        resp.text = "Forbidden"
        mock_ctx.list_flows.side_effect = httpx.HTTPStatusError("403", request=MagicMock(), response=resp)
        mock_client.return_value.__enter__ = MagicMock(return_value=mock_ctx)
        mock_client.return_value.__exit__ = MagicMock(return_value=False)

        cmd = Flows(args=["list"])
        with pytest.raises(SystemExit):
            handle_flows(cmd, Path("/tmp"))  # noqa: S108

    @patch("ccproxy.tools.flows._make_client")
    def test_value_error_exits(self, mock_client: MagicMock) -> None:
        mock_ctx = MagicMock()
        mock_ctx.list_flows.side_effect = ValueError("no flow matching 'xyz'")
        mock_client.return_value.__enter__ = MagicMock(return_value=mock_ctx)
        mock_client.return_value.__exit__ = MagicMock(return_value=False)

        cmd = Flows(args=["list"])
        with pytest.raises(SystemExit):
            handle_flows(cmd, Path("/tmp"))  # noqa: S108


class TestMakeClientCredentialSource:
    """Tests for _make_client with CredentialSource web_password."""

    def test_dict_form_web_password(self, tmp_path: Path) -> None:
        mock_config = MagicMock()
        mock_config.inspector.mitmproxy.web_host = "127.0.0.1"
        mock_config.inspector.port = 8084
        cred_file = tmp_path / "pass.txt"
        cred_file.write_text("file-password")
        mock_config.inspector.mitmproxy.web_password = {"file": str(cred_file)}

        with patch("ccproxy.config.get_config", return_value=mock_config):
            client = _make_client()

        assert client._base == "http://127.0.0.1:8084"

    def test_credential_source_object(self) -> None:
        from ccproxy.config import CredentialSource

        mock_config = MagicMock()
        mock_config.inspector.mitmproxy.web_host = "127.0.0.1"
        mock_config.inspector.port = 8084
        source = CredentialSource(command="echo pass123")
        mock_config.inspector.mitmproxy.web_password = source

        with patch("ccproxy.config.get_config", return_value=mock_config), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="pass123")
            client = _make_client()

        assert client._base == "http://127.0.0.1:8084"
