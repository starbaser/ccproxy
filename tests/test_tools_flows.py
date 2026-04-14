"""Tests for MitmwebClient and the flows CLI subcommands in ccproxy.tools.flows."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from ccproxy.tools.flows import (
    FlowsClear,
    FlowsDiff,
    FlowsDump,
    FlowsList,
    MitmwebClient,
    _do_diff,
    _do_dump,
    _do_list,
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
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError("403", request=MagicMock(), response=MagicMock())

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
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError("404", request=MagicMock(), response=MagicMock())

        client = MitmwebClient(host="localhost", port=8084, token="tok")  # noqa: S106
        client._client = MagicMock()
        client._client.get.return_value = mock_resp

        with pytest.raises(httpx.HTTPStatusError):
            client.get_request_body("missing-id")


class TestMitmwebClientPost:
    """Tests for MitmwebClient._post (XSRF token pair generation + optional JSON body)."""

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

    def test_post_forwards_json_body(self) -> None:
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()

        client = MitmwebClient(host="localhost", port=8084, token="tok")  # noqa: S106
        client._client = MagicMock()
        client._client.cookies = MagicMock()
        client._client.post.return_value = mock_resp

        body = {"arguments": ["abc"]}
        client._post("/commands/ccproxy.dump", json_body=body)

        call_kwargs = client._client.post.call_args
        assert call_kwargs.kwargs["json"] == body

    def test_post_raises_on_http_error(self) -> None:
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError("403", request=MagicMock(), response=MagicMock())

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
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError("500", request=MagicMock(), response=MagicMock())

        client = MitmwebClient(host="localhost", port=8084, token="tok")  # noqa: S106
        client._client = MagicMock()
        client._client.cookies = MagicMock()
        client._client.post.return_value = mock_resp

        with pytest.raises(httpx.HTTPStatusError):
            client.clear()


class TestMitmwebClientDumpHar:
    """Tests for MitmwebClient.dump_har — invokes the ccproxy.dump RPC endpoint."""

    def test_dump_har_posts_command_endpoint(self) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"value": '{"log": {}}'}
        mock_resp.raise_for_status = MagicMock()

        client = MitmwebClient(host="localhost", port=8084, token="tok")  # noqa: S106
        client._client = MagicMock()
        client._client.cookies = MagicMock()
        client._client.post.return_value = mock_resp

        client.dump_har("flow-id-123")

        call_args = client._client.post.call_args
        assert call_args.args[0] == "/commands/ccproxy.dump"
        assert call_args.kwargs["json"] == {"arguments": ["flow-id-123"]}
        assert call_args.kwargs["headers"]["X-XSRFToken"] == client._xsrf

    def test_dump_har_returns_value_field(self) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"value": '{"log": {"version": "1.2"}}'}
        mock_resp.raise_for_status = MagicMock()

        client = MitmwebClient(host="localhost", port=8084, token="tok")  # noqa: S106
        client._client = MagicMock()
        client._client.cookies = MagicMock()
        client._client.post.return_value = mock_resp

        result = client.dump_har("abc")
        assert result == '{"log": {"version": "1.2"}}'

    def test_dump_har_raises_on_error_field(self) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"error": "no flow with id abc"}
        mock_resp.raise_for_status = MagicMock()

        client = MitmwebClient(host="localhost", port=8084, token="tok")  # noqa: S106
        client._client = MagicMock()
        client._client.cookies = MagicMock()
        client._client.post.return_value = mock_resp

        with pytest.raises(ValueError, match="no flow with id abc"):
            client.dump_har("abc")

    def test_dump_har_raises_on_http_error(self) -> None:
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError("500", request=MagicMock(), response=MagicMock())

        client = MitmwebClient(host="localhost", port=8084, token="tok")  # noqa: S106
        client._client = MagicMock()
        client._client.cookies = MagicMock()
        client._client.post.return_value = mock_resp

        with pytest.raises(httpx.HTTPStatusError):
            client.dump_har("abc")


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

        with pytest.raises(ValueError, match="No flow matching"):
            client.resolve_id("zzz")


class TestMitmwebClientContextManager:
    """Tests for MitmwebClient context manager protocol."""

    def test_enter_returns_self(self) -> None:
        client = MitmwebClient(host="localhost", port=8084, token="tok")  # noqa: S106
        client._client = MagicMock()

        with client as entered:
            assert entered is client

    def test_exit_closes_client(self) -> None:
        client = MitmwebClient(host="localhost", port=8084, token="tok")  # noqa: S106
        client._client = MagicMock()

        with client:
            pass

        client._client.close.assert_called_once()


class TestMakeClient:
    """Tests for _make_client factory."""

    def test_make_client_uses_config_values(self) -> None:
        mock_config = MagicMock()
        mock_config.inspector.mitmproxy.web_host = "localhost"
        mock_config.inspector.port = 8084
        mock_config.inspector.mitmproxy.web_password = "test-token"  # noqa: S105

        with patch("ccproxy.config.get_config", return_value=mock_config):
            client = _make_client()
            assert client._base == "http://localhost:8084"


class TestHeaderValue:
    """Tests for _header_value helper."""

    def test_finds_header_case_insensitive(self) -> None:
        headers = [["Content-Type", "application/json"], ["User-Agent", "test"]]
        assert _header_value(headers, "content-type") == "application/json"
        assert _header_value(headers, "USER-AGENT") == "test"

    def test_returns_empty_string_when_missing(self) -> None:
        headers = [["Content-Type", "application/json"]]
        assert _header_value(headers, "x-missing") == ""

    def test_empty_headers(self) -> None:
        assert _header_value([], "any") == ""


class TestDoList:
    def _make_mock_flow(
        self,
        id: str = "abc123def",
        host: str = "api.openai.com",
        path: str = "/v1/chat/completions",
        method: str = "POST",
        status_code: int = 200,
    ) -> dict:
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

        console.print.assert_called_once()

    def test_list_flow_no_response(self) -> None:
        console = MagicMock()
        client = MagicMock()
        flow = self._make_mock_flow()
        flow["response"] = None
        client.list_flows.return_value = [flow]

        _do_list(console, client)
        console.print.assert_called_once()


class TestDoDump:
    """Tests for _do_dump — resolve_id → dump_har → stdout."""

    def test_resolve_and_dump(self, capsys: pytest.CaptureFixture) -> None:
        client = MagicMock()
        client.resolve_id.return_value = "full-flow-id-abc"
        client.dump_har.return_value = '{"log": {"version": "1.2"}}'

        _do_dump(client, id_prefix="abc")

        client.resolve_id.assert_called_once_with("abc")
        client.dump_har.assert_called_once_with("full-flow-id-abc")

        captured = capsys.readouterr()
        assert "1.2" in captured.out

    def test_propagates_value_error_from_resolve(self) -> None:
        client = MagicMock()
        client.resolve_id.side_effect = ValueError("No flow matching 'xyz'")

        with pytest.raises(ValueError, match="No flow matching"):
            _do_dump(client, id_prefix="xyz")


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
    """Tests for the handle_flows dispatcher — one test per subcommand class."""

    @patch("ccproxy.tools.flows._make_client")
    @patch("ccproxy.tools.flows._do_list")
    def test_list_subcommand(self, mock_list: MagicMock, mock_client: MagicMock) -> None:
        mock_ctx = MagicMock()
        mock_client.return_value.__enter__ = MagicMock(return_value=mock_ctx)
        mock_client.return_value.__exit__ = MagicMock(return_value=False)

        handle_flows(FlowsList(), Path("/tmp"))  # noqa: S108

        mock_list.assert_called_once()
        assert mock_list.call_args.kwargs.get("json_output") is False
        assert mock_list.call_args.kwargs.get("filter_pat") is None

    @patch("ccproxy.tools.flows._make_client")
    @patch("ccproxy.tools.flows._do_list")
    def test_list_subcommand_with_options(self, mock_list: MagicMock, mock_client: MagicMock) -> None:
        mock_ctx = MagicMock()
        mock_client.return_value.__enter__ = MagicMock(return_value=mock_ctx)
        mock_client.return_value.__exit__ = MagicMock(return_value=False)

        handle_flows(
            FlowsList(json_output=True, filter="anthropic"),
            Path("/tmp"),  # noqa: S108
        )

        mock_list.assert_called_once()
        assert mock_list.call_args.kwargs.get("json_output") is True
        assert mock_list.call_args.kwargs.get("filter_pat") == "anthropic"

    @patch("ccproxy.tools.flows._make_client")
    @patch("ccproxy.tools.flows._do_dump")
    def test_dump_subcommand(self, mock_dump: MagicMock, mock_client: MagicMock) -> None:
        mock_ctx = MagicMock()
        mock_client.return_value.__enter__ = MagicMock(return_value=mock_ctx)
        mock_client.return_value.__exit__ = MagicMock(return_value=False)

        handle_flows(FlowsDump(id_prefix="abc"), Path("/tmp"))  # noqa: S108

        mock_dump.assert_called_once()
        assert mock_dump.call_args.kwargs["id_prefix"] == "abc"

    @patch("ccproxy.tools.flows._make_client")
    @patch("ccproxy.tools.flows._do_diff")
    def test_diff_subcommand(self, mock_diff: MagicMock, mock_client: MagicMock) -> None:
        mock_ctx = MagicMock()
        mock_client.return_value.__enter__ = MagicMock(return_value=mock_ctx)
        mock_client.return_value.__exit__ = MagicMock(return_value=False)

        handle_flows(FlowsDiff(id_a="a1", id_b="b2"), Path("/tmp"))  # noqa: S108

        mock_diff.assert_called_once()
        call_args = mock_diff.call_args
        # _do_diff(console, client, id_a, id_b) — positional
        assert call_args.args[2] == "a1"
        assert call_args.args[3] == "b2"

    @patch("ccproxy.tools.flows._make_client")
    def test_clear_subcommand(self, mock_client: MagicMock) -> None:
        mock_ctx = MagicMock()
        mock_client.return_value.__enter__ = MagicMock(return_value=mock_ctx)
        mock_client.return_value.__exit__ = MagicMock(return_value=False)

        handle_flows(FlowsClear(), Path("/tmp"))  # noqa: S108

        mock_ctx.clear.assert_called_once()

    @patch("ccproxy.tools.flows._make_client")
    def test_connect_error_exits(self, mock_client: MagicMock) -> None:
        mock_client.return_value.__enter__ = MagicMock(side_effect=httpx.ConnectError("refused"))
        mock_client.return_value.__exit__ = MagicMock(return_value=False)

        with pytest.raises(SystemExit):
            handle_flows(FlowsList(), Path("/tmp"))  # noqa: S108

    @patch("ccproxy.tools.flows._make_client")
    def test_http_status_error_exits(self, mock_client: MagicMock) -> None:
        mock_ctx = MagicMock()
        resp = MagicMock()
        resp.status_code = 403
        resp.text = "Forbidden"
        mock_ctx.list_flows.side_effect = httpx.HTTPStatusError("403", request=MagicMock(), response=resp)
        mock_client.return_value.__enter__ = MagicMock(return_value=mock_ctx)
        mock_client.return_value.__exit__ = MagicMock(return_value=False)

        with pytest.raises(SystemExit):
            handle_flows(FlowsList(), Path("/tmp"))  # noqa: S108

    @patch("ccproxy.tools.flows._make_client")
    def test_value_error_exits(self, mock_client: MagicMock) -> None:
        mock_ctx = MagicMock()
        mock_ctx.list_flows.side_effect = ValueError("no flow matching 'xyz'")
        mock_client.return_value.__enter__ = MagicMock(return_value=mock_ctx)
        mock_client.return_value.__exit__ = MagicMock(return_value=False)

        with pytest.raises(SystemExit):
            handle_flows(FlowsList(), Path("/tmp"))  # noqa: S108

    @patch("ccproxy.tools.flows._make_client")
    def test_clear_error_exits(self, mock_client: MagicMock) -> None:
        mock_ctx = MagicMock()
        mock_ctx.clear.side_effect = httpx.ConnectError("refused")
        mock_client.return_value.__enter__ = MagicMock(return_value=mock_ctx)
        mock_client.return_value.__exit__ = MagicMock(return_value=False)

        with pytest.raises(SystemExit):
            handle_flows(FlowsClear(), Path("/tmp"))  # noqa: S108


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

        with (
            patch("ccproxy.config.get_config", return_value=mock_config),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="pass123")
            client = _make_client()

        assert client._base == "http://127.0.0.1:8084"
