"""MCP stdio server exposing ccproxy's flow inspection surface as tools.

Launched via the ``ccproxy_mcp`` console script (or ``ccproxy mcp`` CLI
subcommand). Wraps ``MitmwebClient`` and ``ShapeStore`` so MCP-aware
clients (e.g. Claude Code with an MCP server config) can list captured
HTTP flows, fetch bodies, dump HAR, group by conversation, and capture
shape templates without spawning the ccproxy CLI per call.

Tools mirror the ``ccproxy flows`` CLI surface plus a few extras for
shape capture and conversation grouping.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from mcp.server.fastmcp import FastMCP

from ccproxy.flows import MitmwebClient, _make_client, _run_jq
from ccproxy.shaping.store import get_store
from ccproxy.specs.model_catalog import build_catalog

logger = logging.getLogger(__name__)

mcp = FastMCP("ccproxy")


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
def dump_har(flow_ids: list[str]) -> str:
    """Render the given flow ids as a multi-page HAR 1.2 JSON string."""
    with _make_client() as client:
        return client.dump_har(flow_ids)


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
        path = f"/flows/{flow_id}/response/content.data"
        resp = client._client.get(path)  # type: ignore[attr-defined]
        resp.raise_for_status()
        body = resp.content
    return body.decode("utf-8", errors="replace")


@mcp.tool()
def diff_flows(flow_ids: list[str]) -> str:
    """Return a sliding-window unified diff of request bodies across the given flows.

    Requires at least two ids. Returns the concatenated diff text.
    """
    if len(flow_ids) < 2:
        raise ValueError("diff_flows: need at least two flow ids")
    import difflib

    with _make_client() as client:
        bodies = [client.get_request_body(fid).decode("utf-8", errors="replace") for fid in flow_ids]

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
def compare_flow(flow_id: str) -> dict[str, Any]:
    """Diff client-request vs forwarded-request for a single flow.

    Returns ``{client_request, forwarded_request, diff}`` where ``diff`` is
    a unified diff text. Both bodies decoded best-effort as UTF-8.
    """
    import difflib

    with _make_client() as client:
        client_body = client.get_request_body(flow_id).decode("utf-8", errors="replace")
        flow_obj = next((f for f in client.list_flows() if f.get("id") == flow_id), None)

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
def capture_shape(flow_id: str, provider: str) -> dict[str, Any]:
    """Save a captured flow as a shape template under ``provider``."""
    with _make_client() as client:
        return client.save_shape([flow_id], provider)


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
def list_models(refresh: bool = False) -> dict[str, Any]:
    """Return ccproxy's OpenAI-shaped model catalog. ``refresh=True`` queries upstream providers."""
    return build_catalog(refresh=refresh)


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


def main() -> None:
    """Entry point for the ``ccproxy_mcp`` console script."""
    mcp.run()


if __name__ == "__main__":
    main()
