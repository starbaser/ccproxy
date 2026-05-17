"""FastMCP streamable-HTTP server exposing ccproxy's flow inspection surface.

This is THE MCP surface for ccproxy. It is hosted inside the running ccproxy
daemon process — see :mod:`ccproxy.inspector.process` for the in-event-loop
``uvicorn`` integration. There is no stdio transport; clients connect to
``http://<host>:<port>/mcp`` with a bearer token (when auth is configured).

Tools mirror the ``ccproxy flows`` CLI surface plus extras for shape capture,
conversation grouping, and Perplexity Pro thread management.

Long-running tools accept a ``ctx: Context`` parameter (auto-injected by
FastMCP, excluded from the published JSON schema) and emit
``notifications/message`` events via ``ctx.info()`` interleaved into the
streaming POST response body.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, cast

from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import Context, FastMCP
from pydantic import AnyHttpUrl

from ccproxy.flows import MitmwebClient, _make_client, _run_jq
from ccproxy.shaping.store import get_store
from ccproxy.specs.model_catalog import build_catalog

logger = logging.getLogger(__name__)


class _StaticTokenVerifier(TokenVerifier):
    """Minimal ``TokenVerifier`` implementation for the ccproxy MCP server.

    The MCP SDK ships ``ProviderTokenVerifier`` which validates against an
    upstream OAuth introspection endpoint. We don't want that — ccproxy is a
    local daemon and the bearer token comes from an opnix-managed file or
    command source. This class wraps a single expected token string and
    rejects anything else.
    """

    def __init__(self, expected_token: str, *, client_id: str = "ccproxy") -> None:
        self._expected = expected_token
        self._client_id = client_id

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token or token != self._expected:
            return None
        return AccessToken(token=token, client_id=self._client_id, scopes=[])


_MCP_INSTRUCTIONS = """\
You are connected to ccproxy, a transparent LLM API interceptor.

MANDATORY RULES:

1. This MCP server provides flow inspection tools. Chat/completions requests
   should be sent directly to the proxy's HTTP endpoint — DO NOT route them
   through MCP tools.

2. Use MCP tools for: listing and comparing captured HTTP flows, inspecting
   request/response bodies, manipulating shapes, grouping conversations, and
   listing the model catalog.
"""


# Module-level FastMCP singleton. Tools register via ``@mcp.tool()`` decorators
# at import time. Auth is configured later via ``configure_auth()`` once
# CCProxyConfig is loaded — the SDK's ``streamable_http_app()`` reads
# ``self.settings.auth`` and ``self._token_verifier`` lazily, so post-import
# mutation is safe (and clearer than juggling factory + decorator scoping).
mcp: FastMCP = FastMCP("ccproxy", stateless_http=True, instructions=_MCP_INSTRUCTIONS)


def configure_auth(token: str, base_url: str) -> None:
    """Wire a static bearer token onto the MCP singleton.

    Called once during daemon startup from :func:`ccproxy.inspector.process.run_inspector`
    before ``mcp.streamable_http_app()`` is invoked. ``base_url`` is the MCP
    server's own externally-visible URL (e.g. ``http://127.0.0.1:4030/mcp``);
    it satisfies ``AuthSettings``'s required ``issuer_url`` /
    ``resource_server_url`` fields, which exist for OAuth discovery flows that
    static-token clients don't use.
    """
    mcp.settings.auth = AuthSettings(
        issuer_url=cast(AnyHttpUrl, base_url),
        resource_server_url=cast(AnyHttpUrl, base_url),
    )
    mcp._token_verifier = _StaticTokenVerifier(token)


def _flows_with_optional_filter(client: MitmwebClient, jq_filter: str | None) -> list[dict[str, Any]]:
    """Run the user's jq filter (if any) over the raw flow list."""
    raw = client.list_flows()
    if not jq_filter:
        return raw
    return _run_jq(raw, jq_filter)


@mcp.tool()
def list_flows(jq_filter: str | None = None) -> list[dict[str, Any]]:
    """List captured HTTP flows. Optional ``jq_filter`` consumes/produces a JSON array."""
    with _make_client() as client:
        return _flows_with_optional_filter(client, jq_filter)


@mcp.tool()
def get_flow(flow_id: str) -> dict[str, Any] | None:
    """Return a single flow by id, or None if not present."""
    with _make_client() as client:
        for flow in client.list_flows():
            if flow.get("id") == flow_id:
                return flow
    return None


@mcp.tool()
async def dump_har(flow_ids: list[str], ctx: Context) -> str:
    """Render the given flow ids as a multi-page HAR 1.2 JSON string."""
    await ctx.info(f"dumping HAR for {len(flow_ids)} flow(s)")

    def _do() -> str:
        with _make_client() as client:
            return client.dump_har(flow_ids)

    return await asyncio.to_thread(_do)


@mcp.tool()
def get_request_body(flow_id: str) -> str:
    """Return the request body for a single flow (UTF-8 decoded best-effort)."""
    with _make_client() as client:
        body = client.get_request_body(flow_id)
    return body.decode("utf-8", errors="replace")


@mcp.tool()
def get_response_body(flow_id: str) -> str:
    """Return the response body for a single flow (UTF-8 decoded best-effort)."""
    with _make_client() as client:
        body = client.get_response_body(flow_id)
    return body.decode("utf-8", errors="replace")


@mcp.tool()
async def diff_flows(flow_ids: list[str], ctx: Context) -> str:
    """Return a sliding-window unified diff of request bodies across the given flows.

    Requires at least two ids. Returns the concatenated diff text.
    """
    if len(flow_ids) < 2:
        raise ValueError("diff_flows: need at least two flow ids")
    import difflib

    await ctx.info(f"diffing {len(flow_ids)} flow body bodies")

    def _fetch_bodies() -> list[str]:
        with _make_client() as client:
            return [client.get_request_body(fid).decode("utf-8", errors="replace") for fid in flow_ids]

    bodies = await asyncio.to_thread(_fetch_bodies)

    chunks: list[str] = []
    for i in range(len(bodies) - 1):
        a, b = bodies[i], bodies[i + 1]
        diff = difflib.unified_diff(
            a.splitlines(keepends=True),
            b.splitlines(keepends=True),
            fromfile=flow_ids[i],
            tofile=flow_ids[i + 1],
            n=3,
        )
        chunks.append("".join(diff))
    return "\n".join(chunks)


@mcp.tool()
async def compare_flow(flow_id: str, ctx: Context) -> dict[str, Any]:
    """Diff client-request vs forwarded-request for a single flow.

    Returns ``{client_request, forwarded_request, diff}`` where ``diff`` is
    a unified diff text. Both bodies decoded best-effort as UTF-8.
    """
    import difflib

    await ctx.info(f"comparing client vs forwarded request for flow {flow_id}")

    def _fetch() -> tuple[str, dict[str, Any] | None]:
        with _make_client() as client:
            body = client.get_request_body(flow_id).decode("utf-8", errors="replace")
            obj = next((f for f in client.list_flows() if f.get("id") == flow_id), None)
        return body, obj

    client_body, flow_obj = await asyncio.to_thread(_fetch)

    if flow_obj is None:
        raise ValueError(f"flow not found: {flow_id}")

    forwarded = json.dumps(flow_obj.get("request", {}), indent=2, sort_keys=True)
    diff = "".join(
        difflib.unified_diff(
            forwarded.splitlines(keepends=True),
            client_body.splitlines(keepends=True),
            fromfile="forwarded",
            tofile="client",
            n=3,
        )
    )
    return {
        "client_request": client_body,
        "forwarded_request": forwarded,
        "diff": diff,
    }


@mcp.tool()
def clear_flows(jq_filter: str | None = None) -> int:
    """Delete flows matching ``jq_filter`` (or all if filter omitted). Returns the count deleted."""
    with _make_client() as client:
        if jq_filter is None:
            count = len(client.list_flows())
            client.clear()
            return count
        targets = _flows_with_optional_filter(client, jq_filter)
        for flow in targets:
            client.delete_flow(flow["id"])
        return len(targets)


@mcp.tool()
async def capture_shape(flow_id: str, provider: str, ctx: Context) -> dict[str, Any]:
    """Save a captured flow as a shape template under ``provider``."""
    await ctx.info(f"capturing shape {provider!r} from flow {flow_id!r}")

    def _do() -> dict[str, Any]:
        with _make_client() as client:
            return client.save_shape([flow_id], provider)

    return await asyncio.to_thread(_do)


@mcp.tool()
def list_shapes() -> list[str]:
    """Return providers that have at least one captured shape on disk."""
    return get_store().list_providers()


@mcp.tool()
def list_conversations() -> dict[str, list[str]]:
    """Group captured flows by ``conversation_id`` (first 12 hex of sha256(first user message text)).

    Returns ``{conversation_id: [flow_id, ...]}`` for flows whose metadata
    carries a ``ccproxy.conversation_id`` (set by the inspector addon).
    """
    grouped: dict[str, list[str]] = {}
    with _make_client() as client:
        flows = client.list_flows()
    for flow in flows:
        metadata = flow.get("metadata", {}) or {}
        conv_id = metadata.get("ccproxy.conversation_id")
        if not isinstance(conv_id, str):
            continue
        grouped.setdefault(conv_id, []).append(str(flow.get("id", "")))
    return grouped


@mcp.tool()
async def list_models(ctx: Context, refresh: bool = False) -> dict[str, Any]:
    """Return ccproxy's OpenAI-shaped model catalog. ``refresh=True`` queries upstream providers."""
    if refresh:
        await ctx.info("refreshing model catalog from upstream providers")
    return await asyncio.to_thread(lambda: build_catalog(refresh=refresh))



@mcp.resource("proxy://requests")
def resource_requests() -> str:
    """Resource view of the captured flow set (JSON list)."""
    with _make_client() as client:
        return json.dumps(client.list_flows())


@mcp.resource("proxy://status")
def resource_status() -> str:
    """Snapshot of ccproxy runtime state (uptime placeholder, flow count, shape providers)."""
    try:
        with _make_client() as client:
            flow_count = len(client.list_flows())
        connected = True
    except Exception as exc:
        flow_count = 0
        connected = False
        logger.warning("status resource: mitmweb not reachable: %s", exc)

    return json.dumps(
        {
            "connected": connected,
            "flow_count": flow_count,
            "shape_providers": get_store().list_providers(),
            "wall_clock": int(time.time()),
        }
    )
