"""Tests for the in-daemon FastMCP streamable-HTTP server.

Mirrors the lifecycle pattern from ``tests/test_transport_sidecar.py`` —
boots a real ``uvicorn.Server`` on a kernel-picked port via
``asyncio.create_task`` and tears it down via ``should_exit``. Uses the
official MCP ``ClientSession`` + ``streamable_http_client`` to exercise the
``initialize`` / ``tools/list`` round-trip over the wire.

These tests intentionally do not configure auth — the in-daemon server
permits unauthenticated access when ``mcp.http.auth`` is ``None``, and that's
what we exercise here. Auth wiring is exercised separately via configure_auth
unit tests.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from contextlib import suppress

import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from ccproxy.mcp import server as mcp_server


def _pick_port() -> int:
    """Find an available TCP port by binding to 0."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
async def running_mcp_http() -> AsyncIterator[str]:
    """Start the in-daemon FastMCP HTTP server on a fresh port; yield the URL.

    ``StreamableHTTPSessionManager.run()`` is one-shot per instance — once a
    lifespan has entered/exited, the manager refuses to start again. FastMCP
    lazily caches the session manager on the FastMCP singleton; reset it
    before each test so ``streamable_http_app()`` constructs a fresh one.
    """
    mcp_server.mcp._session_manager = None
    port = _pick_port()
    config = uvicorn.Config(
        app=mcp_server.mcp.streamable_http_app(),
        host="127.0.0.1",
        port=port,
        log_level="warning",
        log_config=None,
        lifespan="on",
        access_log=False,
        ws="websockets-sansio",
        timeout_graceful_shutdown=2,
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(), name="test-mcp-http")

    deadline = asyncio.get_running_loop().time() + 5.0
    while not server.started:
        if asyncio.get_running_loop().time() > deadline:
            raise RuntimeError("MCP HTTP test server failed to bind within 5s")
        if task.done():
            raise RuntimeError(f"serve() exited prematurely: {task.exception()!r}")
        await asyncio.sleep(0.01)

    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        with suppress(asyncio.CancelledError, asyncio.TimeoutError):
            await asyncio.wait_for(task, timeout=5.0)


class TestMcpHttpLifecycle:
    """Server starts and stops cleanly."""

    async def test_server_binds_port(self, running_mcp_http: str) -> None:
        assert running_mcp_http.startswith("http://127.0.0.1:")
        assert running_mcp_http.endswith("/mcp")

    async def test_unmounted_path_returns_404(self, running_mcp_http: str) -> None:
        import httpx

        base = running_mcp_http.rsplit("/mcp", 1)[0]
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{base}/nonexistent", timeout=5.0)
        assert resp.status_code == 404


class TestMcpToolsList:
    """The server exposes the expected ccproxy tool surface."""

    EXPECTED_TOOLS = frozenset(
        {
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
    )

    async def test_tools_list_returns_full_surface(self, running_mcp_http: str) -> None:
        async with (
            streamable_http_client(url=running_mcp_http) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.list_tools()
        tool_names = {tool.name for tool in result.tools}
        missing = self.EXPECTED_TOOLS - tool_names
        assert not missing, f"missing expected tools: {sorted(missing)}"

    async def test_tools_list_excludes_ctx_param_from_schema(self, running_mcp_http: str) -> None:
        """The injected ``ctx: Context`` must not surface in the published JSON schema."""
        async with (
            streamable_http_client(url=running_mcp_http) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.list_tools()

        retrofit_tools = [
            tool
            for tool in result.tools
            if tool.name in {"dump_har", "diff_flows", "compare_flow", "capture_shape", "import_pplx_thread"}
        ]
        assert retrofit_tools, "expected to find at least one ctx-retrofit tool"

        for tool in retrofit_tools:
            properties = (tool.inputSchema or {}).get("properties", {})
            assert "ctx" not in properties, (
                f"tool {tool.name!r} leaked the injected ctx parameter to clients: {sorted(properties)}"
            )


class TestMcpToolCall:
    """Round-trip tool execution over streamable HTTP."""

    async def test_list_shapes_returns_list(self, running_mcp_http: str) -> None:
        async with (
            streamable_http_client(url=running_mcp_http) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.call_tool("list_shapes", arguments={})

        # list_shapes returns list[str]; the SDK wraps that in a structured content
        # block (text content with JSON-stringified payload).
        assert not result.isError, f"list_shapes errored: {result.content!r}"
        assert result.content, "list_shapes returned no content blocks"


class TestConfigureAuth:
    """Unit-level coverage of the auth configurator."""

    def test_configure_auth_sets_settings(self) -> None:
        # Save/restore so subsequent tests aren't affected by this state mutation.
        prev_auth = mcp_server.mcp.settings.auth
        prev_verifier = mcp_server.mcp._token_verifier
        try:
            mcp_server.configure_auth("test-token-xyz", "http://127.0.0.1:9999/mcp")
            assert mcp_server.mcp.settings.auth is not None
            assert mcp_server.mcp._token_verifier is not None
        finally:
            mcp_server.mcp.settings.auth = prev_auth
            mcp_server.mcp._token_verifier = prev_verifier

    async def test_static_verifier_accepts_expected_token(self) -> None:
        verifier = mcp_server._StaticTokenVerifier("expected-token")
        token = await verifier.verify_token("expected-token")
        assert token is not None
        assert token.token == "expected-token"  # noqa: S105
        assert token.client_id == "ccproxy"

    async def test_static_verifier_rejects_wrong_token(self) -> None:
        verifier = mcp_server._StaticTokenVerifier("expected-token")
        assert await verifier.verify_token("wrong-token") is None

    async def test_static_verifier_rejects_empty_token(self) -> None:
        verifier = mcp_server._StaticTokenVerifier("expected-token")
        assert await verifier.verify_token("") is None
