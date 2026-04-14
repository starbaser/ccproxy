"""Tests for MitmwebClient in ccproxy.tools.flows."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from ccproxy.tools.flows import (
    Flows,
    MitmwebClient,
    _do_diff,
    _do_inspect,
    _do_list,
    _format_body,
    _format_headers_table,
    _header_value,
    _make_client,
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
    """Tests for MitmwebClient.get_client_request."""

    def test_parses_contentview_list_format(self) -> None:
        """contentview returns [[label, text], ...] — first entry's text is returned."""
        content_text = json.dumps({"method": "POST", "url": "https://example.com"})
        mock_resp = MagicMock()
        mock_resp.json.return_value = [["Client-Request", content_text]]
        mock_resp.raise_for_status = MagicMock()

        client = MitmwebClient(host="localhost", port=8084, token="tok")  # noqa: S106
        client._client = MagicMock()
        client._client.get.return_value = mock_resp

        result = client.get_client_request("flow-id-3")

        client._client.get.assert_called_once_with(
            "/flows/flow-id-3/request/content/client-request"
        )
        assert result == content_text

    def test_falls_back_to_text_on_non_list_response(self) -> None:
        """If contentview returns a non-list, fall back to resp.text."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = "plain text response"
        mock_resp.text = "plain text response"
        mock_resp.raise_for_status = MagicMock()

        client = MitmwebClient(host="localhost", port=8084, token="tok")  # noqa: S106
        client._client = MagicMock()
        client._client.get.return_value = mock_resp

        result = client.get_client_request("flow-id-4")
        assert result == "plain text response"

    def test_returns_text_for_empty_list(self) -> None:
        """Empty list response falls back to resp.text."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = []
        mock_resp.text = ""
        mock_resp.raise_for_status = MagicMock()

        client = MitmwebClient(host="localhost", port=8084, token="tok")  # noqa: S106
        client._client = MagicMock()
        client._client.get.return_value = mock_resp

        result = client.get_client_request("flow-id-5")
        assert result == ""

    def test_handles_string_entry_in_list(self) -> None:
        """List entry that is a plain string (not a nested list) is stringified."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = ["some string"]
        mock_resp.raise_for_status = MagicMock()

        client = MitmwebClient(host="localhost", port=8084, token="tok")  # noqa: S106
        client._client = MagicMock()
        client._client.get.return_value = mock_resp

        result = client.get_client_request("flow-id-6")
        assert result == "some string"

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


class TestFormatBody:
    def test_valid_json_returns_syntax(self) -> None:
        from rich.syntax import Syntax
        result = _format_body(b'{"key": "value"}')
        assert isinstance(result, Syntax)

    def test_invalid_json_returns_string(self) -> None:
        result = _format_body(b"plain text")
        assert result == "plain text"

    def test_empty_body_returns_empty_marker(self) -> None:
        result = _format_body(b"")
        assert result == "(empty)"


class TestFormatHeadersTable:
    def test_creates_table_with_headers(self) -> None:
        from rich.table import Table
        headers = [["Content-Type", "application/json"], ["X-Api-Key", "secret"]]
        result = _format_headers_table(headers)
        assert isinstance(result, Table)


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
            },
            "response": {
                "status_code": 200,
                "reason": "OK",
                "headers": [["content-type", "application/json"]],
            },
        }

    def test_inspect_request(self) -> None:
        console = MagicMock()
        client = MagicMock()
        client.resolve_id.return_value = "full-flow-id-123"
        client.list_flows.return_value = [self._make_flow_data()]
        client.get_request_body.return_value = b'{"model": "claude"}'

        _do_inspect(console, client, action="req", id_prefix="full")

        client.resolve_id.assert_called_once_with("full")
        assert console.print.call_count >= 1

    def test_inspect_response(self) -> None:
        console = MagicMock()
        client = MagicMock()
        client.resolve_id.return_value = "full-flow-id-123"
        client.list_flows.return_value = [self._make_flow_data()]
        client.get_response_body.return_value = b'{"content": "hello"}'

        _do_inspect(console, client, action="res", id_prefix="full")

        assert console.print.call_count >= 1

    def test_inspect_client_request(self) -> None:
        console = MagicMock()
        client = MagicMock()
        client.resolve_id.return_value = "full-flow-id-123"
        client.list_flows.return_value = [self._make_flow_data()]
        client.get_client_request.return_value = "GET https://example.com"

        _do_inspect(console, client, action="client", id_prefix="full")

        client.get_client_request.assert_called_once()
        assert console.print.call_count >= 1

    def test_inspect_response_no_response(self) -> None:
        console = MagicMock()
        client = MagicMock()
        flow_data = self._make_flow_data()
        flow_data["response"] = None
        client.resolve_id.return_value = "full-flow-id-123"
        client.list_flows.return_value = [flow_data]

        _do_inspect(console, client, action="res", id_prefix="full")

        assert "No response" in str(console.print.call_args)

    def test_inspect_flow_not_found(self) -> None:
        console = MagicMock()
        client = MagicMock()
        client.resolve_id.return_value = "not-in-list"
        client.list_flows.return_value = []

        with pytest.raises(SystemExit):
            _do_inspect(console, client, action="req", id_prefix="not")


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
