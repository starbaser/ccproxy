"""Tests for ccproxy.mcp.server (FastMCP stdio server tools)."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ccproxy.mcp import server


@pytest.fixture
def fake_flows() -> list[dict[str, Any]]:
    return [
        {
            "id": "flow-a",
            "request": {
                "host": "api.anthropic.com",
                "method": "POST",
                "path": "/v1/messages",
            },
            "metadata": {"ccproxy.conversation_id": "abc123def456"},
        },
        {
            "id": "flow-b",
            "request": {
                "host": "api.anthropic.com",
                "method": "POST",
                "path": "/v1/messages",
            },
            "metadata": {"ccproxy.conversation_id": "abc123def456"},
        },
        {
            "id": "flow-c",
            "request": {
                "host": "cloudcode-pa.googleapis.com",
                "method": "POST",
                "path": "/v1internal:generateContent",
            },
            "metadata": {"ccproxy.conversation_id": "999zzz000111"},
        },
    ]


@pytest.fixture
def mock_client(fake_flows: list[dict[str, Any]]) -> Any:
    """A MitmwebClient mock pre-configured with ``fake_flows``."""
    client = MagicMock()
    client.list_flows.return_value = fake_flows
    client.get_request_body.return_value = b'{"messages": [{"role": "user", "content": "hi"}]}'
    client.dump_har.return_value = '{"log": {"version": "1.2", "entries": []}}'
    client.save_shape.return_value = {"saved": 1, "provider": "anthropic"}
    client.__enter__.return_value = client
    client.__exit__.return_value = None
    return client


def _patch_make_client(mock_client: Any) -> Any:
    """Patch ``ccproxy.mcp.server._make_client`` to return ``mock_client``."""
    return patch("ccproxy.mcp.server._make_client", return_value=mock_client)


def _registered_tool_fn(name: str) -> Any:
    """Locate a FastMCP-registered tool by name and return its underlying callable."""
    tool = server.mcp._tool_manager.get_tool(name)  # type: ignore[attr-defined]
    assert tool is not None, f"tool {name!r} not registered"
    return tool.fn


def test_list_flows_returns_all_when_no_filter(mock_client: Any, fake_flows: list[dict[str, Any]]) -> None:
    with _patch_make_client(mock_client):
        result = _registered_tool_fn("list_flows")()
    assert result == fake_flows


def test_list_flows_applies_jq_filter(mock_client: Any) -> None:
    with _patch_make_client(mock_client):
        result = _registered_tool_fn("list_flows")(
            jq_filter='map(select(.request.host == "api.anthropic.com"))',
        )
    assert len(result) == 2
    assert all(f["request"]["host"] == "api.anthropic.com" for f in result)


def test_get_flow_returns_match(mock_client: Any) -> None:
    with _patch_make_client(mock_client):
        result = _registered_tool_fn("get_flow")(flow_id="flow-b")
    assert result is not None
    assert result["id"] == "flow-b"


def test_get_flow_returns_none_for_missing_id(mock_client: Any) -> None:
    with _patch_make_client(mock_client):
        result = _registered_tool_fn("get_flow")(flow_id="nope")
    assert result is None


def test_dump_har_passes_through_client(mock_client: Any) -> None:
    with _patch_make_client(mock_client):
        result = _registered_tool_fn("dump_har")(flow_ids=["flow-a", "flow-b"])
    assert "log" in json.loads(result)
    mock_client.dump_har.assert_called_once_with(["flow-a", "flow-b"])


def test_get_request_body_decodes_utf8(mock_client: Any) -> None:
    with _patch_make_client(mock_client):
        body = _registered_tool_fn("get_request_body")(flow_id="flow-a")
    assert body == '{"messages": [{"role": "user", "content": "hi"}]}'


def test_get_response_body_decodes_utf8(mock_client: Any) -> None:
    inner = MagicMock()
    inner.get.return_value.content = b'{"id": "msg-1"}'
    inner.get.return_value.raise_for_status.return_value = None
    mock_client._client = inner
    with _patch_make_client(mock_client):
        body = _registered_tool_fn("get_response_body")(flow_id="flow-a")
    assert body == '{"id": "msg-1"}'


def test_diff_flows_emits_unified_diff(mock_client: Any) -> None:
    bodies = [b"first body line\n", b"second body line\n"]
    mock_client.get_request_body.side_effect = bodies
    with _patch_make_client(mock_client):
        diff = _registered_tool_fn("diff_flows")(flow_ids=["flow-a", "flow-b"])
    assert "--- flow-a" in diff
    assert "+++ flow-b" in diff
    assert "-first body line" in diff
    assert "+second body line" in diff


def test_diff_flows_requires_two_ids(mock_client: Any) -> None:
    with _patch_make_client(mock_client), pytest.raises(ValueError, match="at least two"):
        _registered_tool_fn("diff_flows")(flow_ids=["only-one"])


def test_compare_flow_includes_diff(mock_client: Any) -> None:
    mock_client.get_request_body.return_value = b'{"client": "true"}'
    with _patch_make_client(mock_client):
        result = _registered_tool_fn("compare_flow")(flow_id="flow-a")
    assert "client_request" in result
    assert "forwarded_request" in result
    assert "diff" in result
    assert isinstance(result["diff"], str)


def test_compare_flow_raises_for_missing_flow(mock_client: Any) -> None:
    with _patch_make_client(mock_client), pytest.raises(ValueError, match="flow not found"):
        _registered_tool_fn("compare_flow")(flow_id="missing")


def test_clear_flows_with_filter_calls_delete_per_match(
    mock_client: Any, fake_flows: list[dict[str, Any]]
) -> None:
    with _patch_make_client(mock_client):
        count = _registered_tool_fn("clear_flows")(
            jq_filter='map(select(.request.host == "api.anthropic.com"))',
        )
    assert count == 2
    assert mock_client.delete_flow.call_count == 2


def test_clear_flows_without_filter_calls_clear(mock_client: Any, fake_flows: list[dict[str, Any]]) -> None:
    with _patch_make_client(mock_client):
        count = _registered_tool_fn("clear_flows")()
    assert count == len(fake_flows)
    mock_client.clear.assert_called_once()


def test_capture_shape_passes_to_client(mock_client: Any) -> None:
    with _patch_make_client(mock_client):
        result = _registered_tool_fn("capture_shape")(flow_id="flow-a", provider="anthropic")
    mock_client.save_shape.assert_called_once_with(["flow-a"], "anthropic")
    assert result == {"saved": 1, "provider": "anthropic"}


def test_list_shapes_uses_shape_store() -> None:
    with patch("ccproxy.mcp.server.get_store") as get_store_mock:
        get_store_mock.return_value.list_providers.return_value = ["anthropic", "gemini"]
        result = _registered_tool_fn("list_shapes")()
    assert result == ["anthropic", "gemini"]


def test_list_conversations_groups_by_metadata_key(mock_client: Any, fake_flows: list[dict[str, Any]]) -> None:
    with _patch_make_client(mock_client):
        groups = _registered_tool_fn("list_conversations")()
    assert groups == {
        "abc123def456": ["flow-a", "flow-b"],
        "999zzz000111": ["flow-c"],
    }


def test_list_models_returns_static_floor() -> None:
    result = _registered_tool_fn("list_models")()
    assert result["object"] == "list"
    assert any(entry["id"] == "claude-opus-4-7" for entry in result["data"])


def test_resource_status_when_mitmweb_unreachable() -> None:
    """``proxy://status`` reports connected=False rather than raising."""
    with patch("ccproxy.mcp.server._make_client", side_effect=ConnectionError("nope")), \
         patch("ccproxy.mcp.server.get_store") as get_store_mock:
        get_store_mock.return_value.list_providers.return_value = []
        # Resource handlers store the function on the resource object.
        resource = server.mcp._resource_manager._resources["proxy://status"]  # type: ignore[attr-defined]
        text = resource.fn()
    payload = json.loads(text)
    assert payload["connected"] is False
    assert payload["flow_count"] == 0


def test_resource_requests_returns_json_array(mock_client: Any, fake_flows: list[dict[str, Any]]) -> None:
    with _patch_make_client(mock_client):
        resource = server.mcp._resource_manager._resources["proxy://requests"]  # type: ignore[attr-defined]
        text = resource.fn()
    parsed = json.loads(text)
    assert isinstance(parsed, list)
    assert len(parsed) == len(fake_flows)


def test_main_invokes_mcp_run() -> None:
    """``main()`` is the console script entry point — it just calls ``mcp.run()``."""
    with patch.object(server.mcp, "run") as run:
        server.main()
    run.assert_called_once_with()


def test_expected_tool_set_registered() -> None:
    """All 12 documented tools are registered on the FastMCP instance."""
    expected = {
        "list_flows",
        "get_flow",
        "dump_har",
        "get_request_body",
        "get_response_body",
        "diff_flows",
        "compare_flow",
        "clear_flows",
        "capture_shape",
        "list_shapes",
        "list_conversations",
        "list_models",
    }
    registered = {tool.name for tool in server.mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]
    assert expected.issubset(registered)
