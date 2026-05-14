"""Tests for ccproxy.mcp.server (FastMCP streamable-HTTP server tool surface).

The stdio transport and the ``main()`` console-script entry point have been
removed; the FastMCP singleton is now exercised over streamable HTTP by
``tests/test_mcp_http_server.py``. The tests here cover the tool callables
directly via the registered FastMCP ``tool.fn`` handles — fast unit tests
that don't need to boot a uvicorn instance.

Retrofitted async tools take a ``ctx: Context`` parameter for progress/log
notifications. The tests pass an ``AsyncMock`` for ``ctx`` and assert the
expected ``info()`` calls.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

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


def _mock_ctx() -> AsyncMock:
    """Build a ``Context`` mock with async info/report_progress/debug stubs."""
    ctx = AsyncMock()
    ctx.info = AsyncMock()
    ctx.debug = AsyncMock()
    ctx.warning = AsyncMock()
    ctx.error = AsyncMock()
    ctx.report_progress = AsyncMock()
    return ctx


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


async def test_dump_har_passes_through_client(mock_client: Any) -> None:
    ctx = _mock_ctx()
    with _patch_make_client(mock_client):
        result = await _registered_tool_fn("dump_har")(flow_ids=["flow-a", "flow-b"], ctx=ctx)
    assert "log" in json.loads(result)
    mock_client.dump_har.assert_called_once_with(["flow-a", "flow-b"])
    ctx.info.assert_awaited_once()


def test_get_request_body_decodes_utf8(mock_client: Any) -> None:
    with _patch_make_client(mock_client):
        body = _registered_tool_fn("get_request_body")(flow_id="flow-a")
    assert body == '{"messages": [{"role": "user", "content": "hi"}]}'


def test_get_response_body_decodes_utf8(mock_client: Any) -> None:
    mock_client.get_response_body.return_value = b'{"id": "msg-1"}'
    with _patch_make_client(mock_client):
        body = _registered_tool_fn("get_response_body")(flow_id="flow-a")
    mock_client.get_response_body.assert_called_once_with("flow-a")
    assert body == '{"id": "msg-1"}'


async def test_diff_flows_emits_unified_diff(mock_client: Any) -> None:
    ctx = _mock_ctx()
    bodies = [b"first body line\n", b"second body line\n"]
    mock_client.get_request_body.side_effect = bodies
    with _patch_make_client(mock_client):
        diff = await _registered_tool_fn("diff_flows")(flow_ids=["flow-a", "flow-b"], ctx=ctx)
    assert "--- flow-a" in diff
    assert "+++ flow-b" in diff
    assert "-first body line" in diff
    assert "+second body line" in diff
    ctx.info.assert_awaited_once()


async def test_diff_flows_requires_two_ids(mock_client: Any) -> None:
    ctx = _mock_ctx()
    with _patch_make_client(mock_client), pytest.raises(ValueError, match="at least two"):
        await _registered_tool_fn("diff_flows")(flow_ids=["only-one"], ctx=ctx)


async def test_compare_flow_includes_diff(mock_client: Any) -> None:
    ctx = _mock_ctx()
    mock_client.get_request_body.return_value = b'{"client": "true"}'
    with _patch_make_client(mock_client):
        result = await _registered_tool_fn("compare_flow")(flow_id="flow-a", ctx=ctx)
    assert "client_request" in result
    assert "forwarded_request" in result
    assert "diff" in result
    assert isinstance(result["diff"], str)
    ctx.info.assert_awaited_once()


async def test_compare_flow_raises_for_missing_flow(mock_client: Any) -> None:
    ctx = _mock_ctx()
    with _patch_make_client(mock_client), pytest.raises(ValueError, match="flow not found"):
        await _registered_tool_fn("compare_flow")(flow_id="missing", ctx=ctx)


def test_clear_flows_with_filter_calls_delete_per_match(mock_client: Any, fake_flows: list[dict[str, Any]]) -> None:
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


async def test_capture_shape_passes_to_client(mock_client: Any) -> None:
    ctx = _mock_ctx()
    with _patch_make_client(mock_client):
        result = await _registered_tool_fn("capture_shape")(flow_id="flow-a", provider="anthropic", ctx=ctx)
    mock_client.save_shape.assert_called_once_with(["flow-a"], "anthropic")
    assert result == {"saved": 1, "provider": "anthropic"}
    ctx.info.assert_awaited_once()


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


async def test_list_models_returns_static_floor() -> None:
    ctx = _mock_ctx()
    result = await _registered_tool_fn("list_models")(ctx=ctx)
    assert result["object"] == "list"
    assert any(entry["id"] == "claude-opus-4-7" for entry in result["data"])


async def test_list_models_refresh_emits_info() -> None:
    ctx = _mock_ctx()
    with patch("ccproxy.mcp.server.build_catalog", return_value={"object": "list", "data": []}):
        await _registered_tool_fn("list_models")(ctx=ctx, refresh=True)
    ctx.info.assert_awaited_once()


def test_resource_status_when_mitmweb_unreachable() -> None:
    """``proxy://status`` reports connected=False rather than raising."""
    with (
        patch("ccproxy.mcp.server._make_client", side_effect=ConnectionError("nope")),
        patch("ccproxy.mcp.server.get_store") as get_store_mock,
    ):
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


def test_expected_tool_set_registered() -> None:
    """All documented tools are registered on the FastMCP instance."""
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
        "pplx_usage",
        "list_pplx_threads",
        "list_pplx_recent_threads",
        "get_pplx_thread",
        "import_pplx_thread",
        "set_pplx_thread_title",
        "update_pplx_thread_access",
        "delete_pplx_thread",
        "bulk_delete_pplx_threads",
        "export_pplx_thread",
    }
    registered = {tool.name for tool in server.mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]
    assert expected.issubset(registered)


def test_fastmcp_instructions_block_configured() -> None:
    """The FastMCP server advertises ccproxy-specific guidance to calling LLMs."""
    instructions = getattr(server.mcp, "instructions", "") or ""
    assert "ccproxy" in instructions
    assert "chat/completions" in instructions or "chat-completions" in instructions
    assert "pplx_usage" in instructions
    assert "ccproxy_pplx_thread" in instructions


def test_stateless_http_set_on_singleton() -> None:
    """The MCP server is constructed with ``stateless_http=True`` — the SDK default
    is ``False``; we want the streamable-HTTP transport to skip the GET-SSE
    long-poll route and the per-session manager bookkeeping."""
    assert server.mcp.settings.stateless_http is True


def _pplx_response(payload: Any, *, status: int = 200) -> Any:
    """Build a mock httpx-style response object."""
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return resp


def _patch_pplx_session() -> Any:
    return patch("ccproxy.mcp.server._pplx_session", return_value=("https://pplx.test", {}))


async def test_list_pplx_threads_returns_entries_payload() -> None:
    ctx = _mock_ctx()
    payload = {"entries": [{"slug": "abc", "title": "Test thread"}]}
    with _patch_pplx_session(), patch("httpx.post", return_value=_pplx_response(payload)) as mock_post:
        result = await _registered_tool_fn("list_pplx_threads")(ctx=ctx, search_term="", limit=10, offset=0)
    assert result == payload["entries"]
    assert mock_post.call_count == 1
    ctx.info.assert_awaited_once()


async def test_list_pplx_threads_returns_list_payload() -> None:
    ctx = _mock_ctx()
    direct_list = [{"slug": "abc"}, {"slug": "def"}]
    with _patch_pplx_session(), patch("httpx.post", return_value=_pplx_response(direct_list)):
        result = await _registered_tool_fn("list_pplx_threads")(ctx=ctx)
    assert result == direct_list


async def test_get_pplx_thread_returns_response_json() -> None:
    ctx = _mock_ctx()
    payload = {"thread": {"slug": "abc", "context_uuid": "uuid-1"}, "entries": []}
    with _patch_pplx_session(), patch("httpx.get", return_value=_pplx_response(payload)):
        result = await _registered_tool_fn("get_pplx_thread")(slug_or_uuid="abc", ctx=ctx)
    assert result == payload
    ctx.info.assert_awaited_once()


async def test_import_pplx_thread_assembles_resume_kit() -> None:
    ctx = _mock_ctx()
    thread_payload = {
        "thread": {"slug": "abc", "context_uuid": "uuid-1", "title": "T"},
        "entries": [{"foo": 1}, {"foo": 2}],
    }
    converted = [{"role": "assistant", "content": "hi"}]
    with (
        _patch_pplx_session(),
        patch("httpx.get", return_value=_pplx_response(thread_payload)),
        patch("ccproxy.lightllm.pplx._thread_to_openai_messages", return_value=converted),
    ):
        result = await _registered_tool_fn("import_pplx_thread")(
            slug_or_uuid="abc", ctx=ctx, citation_mode="markdown", include_reasoning=False
        )
    assert result["messages"] == [{"role": "assistant", "content": "hi"}]
    assert result["metadata"] == {"ccproxy_pplx_thread": "abc"}
    assert result["thread_info"]["slug"] == "abc"
    assert result["thread_info"]["entry_count"] == 2


_FAKE_THREAD_FOR_RESOLVE = {
    "thread": {"slug": "abc", "context_uuid": "ctx-uuid-1"},
    "entries": [
        {
            "uuid": "ent-1",
            "backend_uuid": "ent-1",
            "context_uuid": "ctx-uuid-1",
            "read_write_token": "rw-1",
        },
    ],
}


async def test_delete_pplx_thread_resolves_slug_to_ids() -> None:
    ctx = _mock_ctx()
    with (
        _patch_pplx_session(),
        patch(
            "ccproxy.mcp.server._fetch_pplx_thread",
            return_value=_FAKE_THREAD_FOR_RESOLVE,
        ),
        patch("httpx.request", return_value=_pplx_response({"status": "ok"})) as mock_req,
    ):
        result = await _registered_tool_fn("delete_pplx_thread")(slug="abc", ctx=ctx)
    assert result == {"status": "ok"}
    call = mock_req.call_args
    assert call.args[0] == "DELETE"
    body = call.kwargs["json"]
    assert body["entry_uuid"] == "ent-1"
    assert body["read_write_token"] == "rw-1"  # noqa: S105


async def test_delete_pplx_thread_raises_when_entries_empty() -> None:
    ctx = _mock_ctx()
    empty = {"thread": {"slug": "abc"}, "entries": []}
    with (
        _patch_pplx_session(),
        patch("ccproxy.mcp.server._fetch_pplx_thread", return_value=empty),
        pytest.raises(ValueError, match="no entries"),
    ):
        await _registered_tool_fn("delete_pplx_thread")(slug="abc", ctx=ctx)


async def test_export_pplx_thread_resolves_slug_then_exports() -> None:
    ctx = _mock_ctx()
    payload = {"filename": "export.md", "file_content_64": "ZGF0YQ=="}
    with (
        _patch_pplx_session(),
        patch(
            "ccproxy.mcp.server._fetch_pplx_thread",
            return_value=_FAKE_THREAD_FOR_RESOLVE,
        ),
        patch("httpx.post", return_value=_pplx_response(payload)) as mock_post,
    ):
        result = await _registered_tool_fn("export_pplx_thread")(slug="abc", ctx=ctx, format="md")
    assert result == payload
    assert "/rest/entry/export" in mock_post.call_args.args[0]
    assert mock_post.call_args.kwargs["json"] == {"entry_uuid": "ent-1", "format": "md"}


async def test_set_pplx_thread_title_resolves_slug_to_context_uuid() -> None:
    ctx = _mock_ctx()
    with (
        _patch_pplx_session(),
        patch(
            "ccproxy.mcp.server._fetch_pplx_thread",
            return_value=_FAKE_THREAD_FOR_RESOLVE,
        ),
        patch("httpx.post", return_value=_pplx_response({"status": "ok"})) as mock_post,
    ):
        result = await _registered_tool_fn("set_pplx_thread_title")(slug="abc", title="renamed", ctx=ctx)
    assert result == {"status": "ok"}
    body = mock_post.call_args.kwargs["json"]
    assert body == {
        "context_uuid": "ctx-uuid-1",
        "title": "renamed",
        "read_write_token": "rw-1",
    }
    assert "/rest/thread/set_thread_title" in mock_post.call_args.args[0]


async def test_set_pplx_thread_title_returns_synth_payload_on_empty_body() -> None:
    """When the upstream response isn't JSON, return a synthesized success dict."""
    ctx = _mock_ctx()
    bad_resp = MagicMock()
    bad_resp.status_code = 200
    bad_resp.raise_for_status.return_value = None
    bad_resp.json.side_effect = ValueError("not JSON")
    with (
        _patch_pplx_session(),
        patch(
            "ccproxy.mcp.server._fetch_pplx_thread",
            return_value=_FAKE_THREAD_FOR_RESOLVE,
        ),
        patch("httpx.post", return_value=bad_resp),
    ):
        result = await _registered_tool_fn("set_pplx_thread_title")(slug="abc", title="renamed", ctx=ctx)
    assert result == {"status": "ok", "slug": "abc", "title": "renamed"}


async def test_pplx_usage_caches_for_60s() -> None:
    """Second call within the TTL window returns the cached payload (no second HTTP call)."""
    ctx = _mock_ctx()
    payload = {"pro_search_left": 297, "deep_research_left": 4}
    with (
        _patch_pplx_session(),
        patch("httpx.get", return_value=_pplx_response(payload)) as mock_get,
    ):
        first = await _registered_tool_fn("pplx_usage")(ctx=ctx)
        second = await _registered_tool_fn("pplx_usage")(ctx=ctx)
    assert first == payload
    assert second == payload
    assert mock_get.call_count == 1


async def test_pplx_usage_refresh_bypasses_cache() -> None:
    """refresh=True forces a fresh fetch even when the cache is warm."""
    ctx = _mock_ctx()
    payload = {"pro_search_left": 297}
    with (
        _patch_pplx_session(),
        patch("httpx.get", return_value=_pplx_response(payload)) as mock_get,
    ):
        await _registered_tool_fn("pplx_usage")(ctx=ctx)
        await _registered_tool_fn("pplx_usage")(ctx=ctx, refresh=True)
    assert mock_get.call_count == 2


async def test_pplx_usage_emits_info_only_on_network_fetch() -> None:
    """The ctx.info() call accompanies network fetches, not cache hits."""
    ctx = _mock_ctx()
    payload = {"pro_search_left": 297}
    with (
        _patch_pplx_session(),
        patch("httpx.get", return_value=_pplx_response(payload)),
    ):
        await _registered_tool_fn("pplx_usage")(ctx=ctx)
        await _registered_tool_fn("pplx_usage")(ctx=ctx)
    assert ctx.info.await_count == 1


async def test_list_pplx_recent_threads_hits_correct_endpoint() -> None:
    ctx = _mock_ctx()
    payload = [{"slug": "abc", "title": "T1"}, {"slug": "def", "title": "T2"}]
    with (
        _patch_pplx_session(),
        patch("httpx.get", return_value=_pplx_response(payload)) as mock_get,
    ):
        result = await _registered_tool_fn("list_pplx_recent_threads")(ctx=ctx)
    assert result == payload
    args, kwargs = mock_get.call_args.args, mock_get.call_args.kwargs
    assert "/rest/thread/list_recent" in args[0]
    assert kwargs["params"]["exclude_asi"] == "false"


async def test_list_pplx_recent_threads_unwraps_entries_dict() -> None:
    ctx = _mock_ctx()
    payload = {"entries": [{"slug": "abc"}]}
    with (
        _patch_pplx_session(),
        patch("httpx.get", return_value=_pplx_response(payload)),
    ):
        result = await _registered_tool_fn("list_pplx_recent_threads")(ctx=ctx)
    assert result == payload["entries"]


async def test_list_pplx_recent_threads_exclude_asi_true() -> None:
    ctx = _mock_ctx()
    with (
        _patch_pplx_session(),
        patch("httpx.get", return_value=_pplx_response([])) as mock_get,
    ):
        await _registered_tool_fn("list_pplx_recent_threads")(ctx=ctx, exclude_asi=True)
    assert mock_get.call_args.kwargs["params"]["exclude_asi"] == "true"


async def test_update_pplx_thread_access_public_returns_share_url() -> None:
    ctx = _mock_ctx()
    with (
        _patch_pplx_session(),
        patch(
            "ccproxy.mcp.server._fetch_pplx_thread",
            return_value=_FAKE_THREAD_FOR_RESOLVE,
        ),
        patch("httpx.post", return_value=_pplx_response({"status": "success", "access": 2})) as mock_post,
    ):
        result = await _registered_tool_fn("update_pplx_thread_access")(slug="abc", public=True, ctx=ctx)
    body = mock_post.call_args.kwargs["json"]
    assert body == {
        "context_uuid": "ctx-uuid-1",
        "updated_access": 2,
        "read_write_token": "rw-1",
    }
    assert result["status"] == "success"
    assert result["share_url"] == "https://pplx.test/search/abc"


async def test_update_pplx_thread_access_private_has_no_share_url() -> None:
    ctx = _mock_ctx()
    with (
        _patch_pplx_session(),
        patch(
            "ccproxy.mcp.server._fetch_pplx_thread",
            return_value=_FAKE_THREAD_FOR_RESOLVE,
        ),
        patch("httpx.post", return_value=_pplx_response({"status": "success", "access": 1})) as mock_post,
    ):
        result = await _registered_tool_fn("update_pplx_thread_access")(slug="abc", public=False, ctx=ctx)
    assert mock_post.call_args.kwargs["json"]["updated_access"] == 1
    assert "share_url" not in result


async def test_bulk_delete_pplx_threads_resolves_each_slug() -> None:
    ctx = _mock_ctx()
    thread_a = {
        "thread": {"slug": "abc", "context_uuid": "ctx-a"},
        "entries": [{"uuid": "ent-a", "context_uuid": "ctx-a", "read_write_token": "rw-a"}],
    }
    thread_b = {
        "thread": {"slug": "def", "context_uuid": "ctx-b"},
        "entries": [{"uuid": "ent-b", "context_uuid": "ctx-b", "read_write_token": "rw-b"}],
    }
    with (
        _patch_pplx_session(),
        patch(
            "ccproxy.mcp.server._fetch_pplx_thread",
            side_effect=[thread_a, thread_b],
        ),
        patch("httpx.request", return_value=_pplx_response({})) as mock_req,
    ):
        result = await _registered_tool_fn("bulk_delete_pplx_threads")(slugs=["abc", "def"], ctx=ctx)
    body = mock_req.call_args.kwargs["json"]
    assert body["entry_uuids"] == ["ent-a", "ent-b"]
    assert body["read_write_token"] == "rw-a"  # noqa: S105 — first slug's token wins
    assert result["deleted"] == ["abc", "def"]
    assert result["failed"] == []


async def test_bulk_delete_pplx_threads_collects_partial_failures() -> None:
    ctx = _mock_ctx()

    def side_effect(slug: str) -> dict[str, Any]:
        if slug == "good":
            return _FAKE_THREAD_FOR_RESOLVE
        raise ValueError(f"Perplexity thread {slug!r} not found")

    with (
        _patch_pplx_session(),
        patch("ccproxy.mcp.server._fetch_pplx_thread", side_effect=side_effect),
        patch("httpx.request", return_value=_pplx_response({})),
    ):
        result = await _registered_tool_fn("bulk_delete_pplx_threads")(slugs=["good", "missing"], ctx=ctx)
    assert result["deleted"] == ["good"]
    assert len(result["failed"]) == 1
    assert result["failed"][0]["slug"] == "missing"


async def test_bulk_delete_pplx_threads_empty_slug_list_raises() -> None:
    ctx = _mock_ctx()
    with _patch_pplx_session(), pytest.raises(ValueError, match="non-empty"):
        await _registered_tool_fn("bulk_delete_pplx_threads")(slugs=[], ctx=ctx)


async def test_bulk_delete_pplx_threads_skips_upstream_when_all_resolve_fail() -> None:
    """No upstream DELETE fires if every slug fails to resolve."""
    ctx = _mock_ctx()
    with (
        _patch_pplx_session(),
        patch(
            "ccproxy.mcp.server._fetch_pplx_thread",
            side_effect=ValueError("not found"),
        ),
        patch("httpx.request") as mock_req,
    ):
        result = await _registered_tool_fn("bulk_delete_pplx_threads")(slugs=["missing-1", "missing-2"], ctx=ctx)
    mock_req.assert_not_called()
    assert result["deleted"] == []
    assert len(result["failed"]) == 2


def test_pplx_session_raises_when_provider_missing() -> None:
    """``_pplx_session`` raises ``RuntimeError`` when ``perplexity_pro`` isn't configured."""
    fake_cfg = MagicMock()
    fake_cfg.providers = {}
    with patch("ccproxy.config.get_config", return_value=fake_cfg), pytest.raises(RuntimeError, match="not configured"):
        server._pplx_session()


def test_pplx_session_raises_when_token_unresolvable() -> None:
    """``_pplx_session`` raises ``RuntimeError`` when the cookie source resolves empty."""
    from ccproxy.lightllm.pplx import PERPLEXITY_PROVIDER_NAME

    fake_cfg = MagicMock()
    fake_cfg.providers = {PERPLEXITY_PROVIDER_NAME: object()}
    fake_cfg.resolve_oauth_token.return_value = None
    with (
        patch("ccproxy.config.get_config", return_value=fake_cfg),
        pytest.raises(RuntimeError, match="no session cookie"),
    ):
        server._pplx_session()
