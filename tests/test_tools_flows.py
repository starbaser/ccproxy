"""Tests for MitmwebClient and the flows CLI subcommands in ccproxy.flows."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from ccproxy.flows import (
    FlowsClear,
    FlowsCompare,
    FlowsDiff,
    FlowsDump,
    FlowsList,
    MitmwebClient,
    _do_compare,
    _do_diff,
    _do_dump,
    _do_list,
    _format_body,
    _git_diff,
    _header_value,
    _make_client,
    _run_jq,
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
    """Tests for MitmwebClient.dump_har — takes list[str], comma-joins for RPC."""

    def test_dump_har_single_id(self) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"value": '{"log": {}}'}
        mock_resp.raise_for_status = MagicMock()

        client = MitmwebClient(host="localhost", port=8084, token="tok")  # noqa: S106
        client._client = MagicMock()
        client._client.cookies = MagicMock()
        client._client.post.return_value = mock_resp

        client.dump_har(["flow-id-123"])

        call_args = client._client.post.call_args
        assert call_args.args[0] == "/commands/ccproxy.dump"
        assert call_args.kwargs["json"] == {"arguments": ["flow-id-123"]}

    def test_dump_har_multi_id_comma_joined(self) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"value": '{"log": {}}'}
        mock_resp.raise_for_status = MagicMock()

        client = MitmwebClient(host="localhost", port=8084, token="tok")  # noqa: S106
        client._client = MagicMock()
        client._client.cookies = MagicMock()
        client._client.post.return_value = mock_resp

        client.dump_har(["id-a", "id-b", "id-c"])

        call_args = client._client.post.call_args
        assert call_args.kwargs["json"] == {"arguments": ["id-a,id-b,id-c"]}

    def test_dump_har_returns_value_field(self) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"value": '{"log": {"version": "1.2"}}'}
        mock_resp.raise_for_status = MagicMock()

        client = MitmwebClient(host="localhost", port=8084, token="tok")  # noqa: S106
        client._client = MagicMock()
        client._client.cookies = MagicMock()
        client._client.post.return_value = mock_resp

        result = client.dump_har(["abc"])
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
            client.dump_har(["abc"])

    def test_dump_har_empty_list_raises_value_error(self) -> None:
        client = MitmwebClient(host="localhost", port=8084, token="tok")  # noqa: S106
        with pytest.raises(ValueError, match="non-empty"):
            client.dump_har([])

    def test_dump_har_raises_on_http_error(self) -> None:
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError("500", request=MagicMock(), response=MagicMock())

        client = MitmwebClient(host="localhost", port=8084, token="tok")  # noqa: S106
        client._client = MagicMock()
        client._client.cookies = MagicMock()
        client._client.post.return_value = mock_resp

        with pytest.raises(httpx.HTTPStatusError):
            client.dump_har(["abc"])


class TestMitmwebClientDeleteFlow:
    """Tests for MitmwebClient.delete_flow."""

    def test_delete_flow_calls_delete_endpoint(self) -> None:
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()

        client = MitmwebClient(host="localhost", port=8084, token="tok")  # noqa: S106
        client._client = MagicMock()
        client._client.delete.return_value = mock_resp

        client.delete_flow("flow-id-1")

        args, kwargs = client._client.delete.call_args
        assert args == ("/flows/flow-id-1",)
        assert "X-XSRFToken" in kwargs["headers"]
        mock_resp.raise_for_status.assert_called_once()

    def test_delete_flow_raises_on_http_error(self) -> None:
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError("404", request=MagicMock(), response=MagicMock())

        client = MitmwebClient(host="localhost", port=8084, token="tok")  # noqa: S106
        client._client = MagicMock()
        client._client.delete.return_value = mock_resp

        with pytest.raises(httpx.HTTPStatusError):
            client.delete_flow("missing-id")


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


class TestFormatBody:
    """Tests for _format_body helper."""

    def test_json_body_pretty_printed(self) -> None:
        result = _format_body('{"a":1}')
        assert '"a": 1' in result

    def test_non_json_body_returned_as_is(self) -> None:
        assert _format_body("plain text") == "plain text"

    def test_none_returns_empty(self) -> None:
        assert _format_body(None) == ""


class TestGitDiff:
    """Tests for _git_diff — uses git diff --no-index."""

    @patch("subprocess.run")
    def test_invokes_git_diff_no_index(self, mock_run: MagicMock) -> None:
        _git_diff("aaa", "bbb", "left", "right")

        mock_run.assert_called_once()
        cmd = mock_run.call_args.args[0]
        assert cmd[:2] == ["git", "--no-pager"]
        assert "--no-index" in cmd
        assert "--color=auto" in cmd

    @patch("subprocess.run")
    def test_passes_label_prefixes(self, mock_run: MagicMock) -> None:
        _git_diff("a", "b", "client:abc", "fwd:abc")

        cmd = mock_run.call_args.args[0]
        assert "--src-prefix=client:abc/" in cmd
        assert "--dst-prefix=fwd:abc/" in cmd


class TestRunJq:
    """Tests for _run_jq — shells out to jq binary (available in devShell)."""

    def test_identity_filter_roundtrip(self) -> None:
        flows = [{"id": "a"}, {"id": "b"}]
        result = _run_jq(flows, ".")
        assert result == flows

    def test_map_select_filter(self) -> None:
        flows = [{"id": "a", "x": 1}, {"id": "b", "x": 2}]
        result = _run_jq(flows, "map(select(.x == 1))")
        assert result == [{"id": "a", "x": 1}]

    def test_chained_filters_via_pipe(self) -> None:
        flows = [{"id": "a", "x": 1}, {"id": "b", "x": 2}, {"id": "c", "x": 1}]
        result = _run_jq(flows, "map(select(.x == 1)) | map(.id)")
        assert result == ["a", "c"]

    def test_invalid_filter_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="jq filter failed"):
            _run_jq([{"id": "a"}], "invalid(((filter")

    def test_non_array_output_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="JSON array"):
            _run_jq([{"id": "a"}], ".[0]")

    def test_empty_input_returns_empty(self) -> None:
        assert _run_jq([], ".") == []


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
        flow_set = [self._make_mock_flow()]

        _do_list(console, flow_set)

        console.print.assert_called_once()

    def test_list_empty_shows_message(self) -> None:
        console = MagicMock()

        _do_list(console, [])

        console.print.assert_called_once()
        assert "No flows" in str(console.print.call_args)

    def test_list_json_output(self, capsys: pytest.CaptureFixture[str]) -> None:
        console = MagicMock()
        flow_set = [self._make_mock_flow()]

        _do_list(console, flow_set, json_output=True)

        captured = capsys.readouterr()
        assert '"id"' in captured.out
        console.print.assert_not_called()

    def test_list_flow_no_response(self) -> None:
        console = MagicMock()
        flow = self._make_mock_flow()
        flow["response"] = None

        _do_list(console, [flow])
        console.print.assert_called_once()


class TestDoDump:
    """Tests for _do_dump — takes a flow set, dumps multi-page HAR."""

    def test_dump_calls_dump_har_with_all_ids(self) -> None:
        client = MagicMock()
        client.dump_har.return_value = '{"log": {"version": "1.2"}}'
        flow_set = [{"id": "id-1"}, {"id": "id-2"}]

        _do_dump(client, flow_set)

        client.dump_har.assert_called_once_with(["id-1", "id-2"])

    def test_dump_empty_set_exits(self) -> None:
        client = MagicMock()

        with pytest.raises(SystemExit):
            _do_dump(client, [])


class TestDoDiff:
    """Tests for _do_diff — sliding window over the flow set."""

    @patch("ccproxy.flows._git_diff")
    def test_two_flows_one_diff(self, mock_gd: MagicMock) -> None:
        client = MagicMock()
        client.get_request_body.side_effect = [
            b'{"model": "claude"}',
            b'{"model": "gpt-4o"}',
        ]
        flow_set = [{"id": "aaa"}, {"id": "bbb"}]

        _do_diff(client, flow_set)

        assert client.get_request_body.call_count == 2
        mock_gd.assert_called_once()

    @patch("ccproxy.flows._git_diff")
    def test_three_flows_two_diffs(self, mock_gd: MagicMock) -> None:
        client = MagicMock()
        client.get_request_body.side_effect = [
            b'{"v": 1}',
            b'{"v": 2}',
            b'{"v": 2}',
            b'{"v": 3}',
        ]
        flow_set = [{"id": "a"}, {"id": "b"}, {"id": "c"}]

        _do_diff(client, flow_set)

        assert client.get_request_body.call_count == 4
        assert mock_gd.call_count == 2

    @patch("ccproxy.flows._git_diff")
    def test_identical_bodies_delegates_to_git(self, mock_gd: MagicMock) -> None:
        client = MagicMock()
        body = b'{"model": "claude"}'
        client.get_request_body.return_value = body
        flow_set = [{"id": "a"}, {"id": "b"}]

        _do_diff(client, flow_set)

        mock_gd.assert_called_once()

    def test_single_flow_exits(self) -> None:
        client = MagicMock()

        with pytest.raises(SystemExit):
            _do_diff(client, [{"id": "a"}])

    def test_empty_set_exits(self) -> None:
        client = MagicMock()

        with pytest.raises(SystemExit):
            _do_diff(client, [])


class TestDoCompare:
    """Tests for _do_compare — per-flow client-vs-forwarded diff."""

    def _make_har_json(self, flows: list[dict]) -> str:
        """Build a minimal HAR JSON string for compare testing."""
        import json

        entries = []
        pages = []
        for f in flows:
            pages.append({"id": f["id"]})
            fwd = {"url": f["fwd_url"], "postData": {"text": f.get("fwd_body", "")}}
            cli = {"url": f["cli_url"], "postData": {"text": f.get("cli_body", "")}}
            entries.append({"request": fwd, "response": {}})
            entries.append({"request": cli, "response": {}})
        return json.dumps({"log": {"pages": pages, "entries": entries}})

    @patch("ccproxy.flows._git_diff")
    def test_single_flow_shows_diff(self, mock_gd: MagicMock) -> None:
        client = MagicMock()
        client.dump_har.return_value = self._make_har_json(
            [
                {
                    "id": "abc",
                    "fwd_url": "https://fwd.example/v1",
                    "cli_url": "http://localhost:1/v1",
                    "fwd_body": '{"model":"haiku"}',
                    "cli_body": '{"model":"opus"}',
                },
            ]
        )

        _do_compare(client, [{"id": "abc"}])

        client.dump_har.assert_called_once_with(["abc"])
        mock_gd.assert_called()

    @patch("ccproxy.flows._git_diff")
    def test_url_change_shown(self, mock_gd: MagicMock, capsys: pytest.CaptureFixture[str]) -> None:
        client = MagicMock()
        client.dump_har.return_value = self._make_har_json(
            [
                {
                    "id": "abc",
                    "fwd_url": "https://api.anthropic.com/v1",
                    "cli_url": "http://localhost:1/v1",
                    "fwd_body": "{}",
                    "cli_body": "{}",
                },
            ]
        )

        _do_compare(client, [{"id": "abc"}])

        captured = capsys.readouterr()
        assert "URL change" in captured.out

    @patch("ccproxy.flows._git_diff")
    def test_multiple_flows_shows_one_diff_per_flow(self, mock_gd: MagicMock) -> None:
        client = MagicMock()
        client.dump_har.return_value = self._make_har_json(
            [
                {
                    "id": "f1",
                    "fwd_url": "https://a/v1",
                    "cli_url": "https://a/v1",
                    "fwd_body": '{"a":1}',
                    "cli_body": '{"a":2}',
                },
                {
                    "id": "f2",
                    "fwd_url": "https://b/v1",
                    "cli_url": "https://b/v1",
                    "fwd_body": '{"b":1}',
                    "cli_body": '{"b":2}',
                },
            ]
        )

        _do_compare(client, [{"id": "f1"}, {"id": "f2"}])

        client.dump_har.assert_called_once_with(["f1", "f2"])

    def test_empty_set_exits(self) -> None:
        client = MagicMock()

        with pytest.raises(SystemExit):
            _do_compare(client, [])


class TestDoClear:
    """Tests for _do_clear."""

    def test_clear_all_bypasses_pipeline(self) -> None:
        console = MagicMock()
        client = MagicMock()

        from ccproxy.flows import _do_clear

        _do_clear(console, client, [{"id": "a"}], clear_all=True)

        client.clear.assert_called_once()
        client.delete_flow.assert_not_called()

    def test_clear_filtered_set_deletes_each(self) -> None:
        console = MagicMock()
        client = MagicMock()

        from ccproxy.flows import _do_clear

        _do_clear(console, client, [{"id": "a"}, {"id": "b"}], clear_all=False)

        assert client.delete_flow.call_count == 2
        client.delete_flow.assert_any_call("a")
        client.delete_flow.assert_any_call("b")
        client.clear.assert_not_called()

    def test_clear_empty_set(self) -> None:
        console = MagicMock()
        client = MagicMock()

        from ccproxy.flows import _do_clear

        _do_clear(console, client, [], clear_all=False)

        client.delete_flow.assert_not_called()
        client.clear.assert_not_called()


class TestHandleFlows:
    """Tests for the handle_flows dispatcher — one test per subcommand class."""

    @patch("ccproxy.config.get_config")
    @patch("ccproxy.flows._make_client")
    @patch("ccproxy.flows._resolve_flow_set")
    @patch("ccproxy.flows._do_list")
    def test_list_subcommand(
        self,
        mock_list: MagicMock,
        mock_resolve: MagicMock,
        mock_client: MagicMock,
        mock_config: MagicMock,
    ) -> None:
        mock_ctx = MagicMock()
        mock_client.return_value.__enter__ = MagicMock(return_value=mock_ctx)
        mock_client.return_value.__exit__ = MagicMock(return_value=False)
        mock_resolve.return_value = [{"id": "a"}]

        handle_flows(FlowsList(), Path("/tmp"))  # noqa: S108

        mock_list.assert_called_once()
        assert mock_list.call_args.kwargs.get("json_output") is False

    @patch("ccproxy.config.get_config")
    @patch("ccproxy.flows._make_client")
    @patch("ccproxy.flows._resolve_flow_set")
    @patch("ccproxy.flows._do_dump")
    def test_dump_subcommand(
        self,
        mock_dump: MagicMock,
        mock_resolve: MagicMock,
        mock_client: MagicMock,
        mock_config: MagicMock,
    ) -> None:
        mock_ctx = MagicMock()
        mock_client.return_value.__enter__ = MagicMock(return_value=mock_ctx)
        mock_client.return_value.__exit__ = MagicMock(return_value=False)
        flow_set = [{"id": "a"}, {"id": "b"}]
        mock_resolve.return_value = flow_set

        handle_flows(FlowsDump(), Path("/tmp"))  # noqa: S108

        mock_dump.assert_called_once()
        assert mock_dump.call_args.args[1] == flow_set

    @patch("ccproxy.config.get_config")
    @patch("ccproxy.flows._make_client")
    @patch("ccproxy.flows._resolve_flow_set")
    @patch("ccproxy.flows._do_diff")
    def test_diff_subcommand(
        self,
        mock_diff: MagicMock,
        mock_resolve: MagicMock,
        mock_client: MagicMock,
        mock_config: MagicMock,
    ) -> None:
        mock_ctx = MagicMock()
        mock_client.return_value.__enter__ = MagicMock(return_value=mock_ctx)
        mock_client.return_value.__exit__ = MagicMock(return_value=False)
        flow_set = [{"id": "a"}, {"id": "b"}]
        mock_resolve.return_value = flow_set

        handle_flows(FlowsDiff(), Path("/tmp"))  # noqa: S108

        mock_diff.assert_called_once()
        assert mock_diff.call_args.args[1] == flow_set

    @patch("ccproxy.config.get_config")
    @patch("ccproxy.flows._make_client")
    @patch("ccproxy.flows._resolve_flow_set")
    @patch("ccproxy.flows._do_compare")
    def test_compare_subcommand(
        self,
        mock_compare: MagicMock,
        mock_resolve: MagicMock,
        mock_client: MagicMock,
        mock_config: MagicMock,
    ) -> None:
        mock_ctx = MagicMock()
        mock_client.return_value.__enter__ = MagicMock(return_value=mock_ctx)
        mock_client.return_value.__exit__ = MagicMock(return_value=False)
        flow_set = [{"id": "a"}]
        mock_resolve.return_value = flow_set

        handle_flows(FlowsCompare(), Path("/tmp"))  # noqa: S108

        mock_compare.assert_called_once()
        assert mock_compare.call_args.args[1] == flow_set

    @patch("ccproxy.config.get_config")
    @patch("ccproxy.flows._make_client")
    @patch("ccproxy.flows._resolve_flow_set")
    @patch("ccproxy.flows._do_clear")
    def test_clear_subcommand(
        self,
        mock_clear: MagicMock,
        mock_resolve: MagicMock,
        mock_client: MagicMock,
        mock_config: MagicMock,
    ) -> None:
        mock_ctx = MagicMock()
        mock_client.return_value.__enter__ = MagicMock(return_value=mock_ctx)
        mock_client.return_value.__exit__ = MagicMock(return_value=False)
        mock_resolve.return_value = [{"id": "a"}]

        handle_flows(FlowsClear(), Path("/tmp"))  # noqa: S108

        mock_clear.assert_called_once()
        assert mock_clear.call_args.kwargs["clear_all"] is False

    @patch("ccproxy.config.get_config")
    @patch("ccproxy.flows._make_client")
    @patch("ccproxy.flows._resolve_flow_set")
    @patch("ccproxy.flows._do_clear")
    def test_clear_all_flag(
        self,
        mock_clear: MagicMock,
        mock_resolve: MagicMock,
        mock_client: MagicMock,
        mock_config: MagicMock,
    ) -> None:
        mock_ctx = MagicMock()
        mock_client.return_value.__enter__ = MagicMock(return_value=mock_ctx)
        mock_client.return_value.__exit__ = MagicMock(return_value=False)
        mock_resolve.return_value = []

        handle_flows(FlowsClear(all=True), Path("/tmp"))  # noqa: S108

        mock_clear.assert_called_once()
        assert mock_clear.call_args.kwargs["clear_all"] is True

    @patch("ccproxy.config.get_config")
    @patch("ccproxy.flows._make_client")
    def test_connect_error_exits(self, mock_client: MagicMock, mock_config: MagicMock) -> None:
        mock_client.return_value.__enter__ = MagicMock(side_effect=httpx.ConnectError("refused"))
        mock_client.return_value.__exit__ = MagicMock(return_value=False)

        with pytest.raises(SystemExit):
            handle_flows(FlowsList(), Path("/tmp"))  # noqa: S108

    @patch("ccproxy.config.get_config")
    @patch("ccproxy.flows._make_client")
    @patch("ccproxy.flows._resolve_flow_set")
    def test_value_error_exits(
        self,
        mock_resolve: MagicMock,
        mock_client: MagicMock,
        mock_config: MagicMock,
    ) -> None:
        mock_ctx = MagicMock()
        mock_resolve.side_effect = ValueError("jq filter failed")
        mock_client.return_value.__enter__ = MagicMock(return_value=mock_ctx)
        mock_client.return_value.__exit__ = MagicMock(return_value=False)

        with pytest.raises(SystemExit):
            handle_flows(FlowsList(), Path("/tmp"))  # noqa: S108


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
