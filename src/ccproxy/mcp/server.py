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
        body = client.get_response_body(flow_id)
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


def _pplx_session() -> tuple[str, dict[str, str]]:
    """Resolve Perplexity session cookie + standard API headers.

    Returns ``(base_url, headers)``. Raises ``RuntimeError`` when the
    ``perplexity_pro`` provider isn't configured or has no token on disk —
    surfaced to the MCP client as a tool execution error.
    """
    from ccproxy.config import get_config
    from ccproxy.lightllm.pplx import (
        PERPLEXITY_BROWSER_UA,
        PERPLEXITY_PROVIDER_NAME,
        PERPLEXITY_SESSION_COOKIE,
        PERPLEXITY_URL_BASE,
    )

    cfg = get_config()
    if PERPLEXITY_PROVIDER_NAME not in cfg.providers:
        raise RuntimeError(
            f"provider {PERPLEXITY_PROVIDER_NAME!r} not configured in ccproxy.yaml"
        )
    token = cfg.resolve_oauth_token(PERPLEXITY_PROVIDER_NAME)
    if not token:
        raise RuntimeError(
            f"no session cookie resolved for {PERPLEXITY_PROVIDER_NAME!r}"
        )
    headers = {
        "Cookie": f"{PERPLEXITY_SESSION_COOKIE}={token}",
        "User-Agent": PERPLEXITY_BROWSER_UA,
        "Origin": PERPLEXITY_URL_BASE,
        "Referer": f"{PERPLEXITY_URL_BASE}/",
        "Accept": "application/json",
        "x-app-apiclient": "default",
        "x-app-apiversion": "2.18",
        "x-perplexity-request-reason": "perplexity-query-state-provider",
    }
    return PERPLEXITY_URL_BASE, headers


@mcp.tool()
def list_pplx_threads(
    search_term: str = "",
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """List the authenticated user's Perplexity threads (``/rest/thread/list_ask_threads``).

    Each entry contains ``slug``, ``title``, ``context_uuid``,
    ``last_query_datetime``, etc. Use ``slug`` as the value of
    ``metadata.ccproxy_pplx_thread`` on the next chat-completions request
    to resume that thread, or pass to ``get_pplx_thread`` / ``import_pplx_thread``.
    """
    import httpx

    base, headers = _pplx_session()
    headers["Content-Type"] = "application/json"
    resp = httpx.post(
        f"{base}/rest/thread/list_ask_threads",
        headers=headers,
        json={
            "limit": limit,
            "offset": offset,
            "ascending": False,
            "search_term": search_term,
            "with_temporary_threads": False,
            "exclude_asi": False,
        },
        timeout=15.0,
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("entries"), list):
        return data["entries"]
    return []


@mcp.tool()
def get_pplx_thread(slug_or_uuid: str) -> dict[str, Any]:
    """Fetch a Perplexity thread by URL slug or context UUID (``/rest/thread/{slug}``)."""
    import httpx

    from ccproxy.lightllm.pplx import PERPLEXITY_BLOCK_USE_CASES

    base, headers = _pplx_session()
    params: list[tuple[str, str]] = [
        ("version", "2.18"),
        ("source", "default"),
        ("limit", "100"),
        ("offset", "0"),
        ("from_first", "true"),
        ("with_parent_info", "true"),
        ("with_schematized_response", "true"),
    ]
    params.extend(("supported_block_use_cases", uc) for uc in PERPLEXITY_BLOCK_USE_CASES)
    headers["x-perplexity-request-endpoint"] = f"{base}/rest/thread/{slug_or_uuid}"
    resp = httpx.get(
        f"{base}/rest/thread/{slug_or_uuid}",
        params=params,
        headers=headers,
        timeout=15.0,
    )
    resp.raise_for_status()
    return resp.json()


@mcp.tool()
def import_pplx_thread(
    slug_or_uuid: str,
    citation_mode: str | None = None,
    include_reasoning: bool = False,
) -> dict[str, Any]:
    """Convert a Perplexity thread into a kit for next-turn resume.

    Returns ``{messages: [...], metadata: {ccproxy_pplx_thread: slug}, thread_info: {...}}``.
    The caller assembles the next OpenAI chat-completions request as:

        {"messages": [...returned, new_user_turn], "metadata": {ccproxy_pplx_thread: slug}}

    ccproxy's ``pplx_thread_inject`` hook then resolves the metadata slug
    to the thread's latest identifiers and routes the new turn as a
    Perplexity ``followup`` against the existing thread.
    """
    from ccproxy.config import get_config
    from ccproxy.lightllm.pplx import _thread_to_openai_messages

    mode = citation_mode or get_config().pplx.thread.citation_mode
    thread = get_pplx_thread(slug_or_uuid=slug_or_uuid)
    messages = _thread_to_openai_messages(thread, citation_mode=mode, include_reasoning=include_reasoning)

    thread_meta = thread.get("thread") if isinstance(thread.get("thread"), dict) else {}
    entries = thread.get("entries") if isinstance(thread.get("entries"), list) else []

    return {
        "messages": messages,
        "metadata": {"ccproxy_pplx_thread": slug_or_uuid},
        "thread_info": {
            "slug": (thread_meta.get("slug") if thread_meta else None) or slug_or_uuid,
            "context_uuid": thread_meta.get("context_uuid") if thread_meta else None,
            "title": thread_meta.get("title") if thread_meta else None,
            "entry_count": len(entries),
        },
    }


@mcp.tool()
def delete_pplx_thread(entry_uuid: str, read_write_token: str) -> dict[str, Any]:
    """Delete a Perplexity thread by entry UUID + read_write_token.

    Both identifiers come from a prior SSE response (captured by ccproxy
    on the response side) or from a ``get_pplx_thread`` call.
    """
    import httpx

    base, headers = _pplx_session()
    headers["Content-Type"] = "application/json"
    resp = httpx.request(
        "DELETE",
        f"{base}/rest/thread/delete_thread_by_entry_uuid",
        headers=headers,
        json={"entry_uuid": entry_uuid, "read_write_token": read_write_token},
        timeout=15.0,
    )
    resp.raise_for_status()
    try:
        return resp.json()
    except Exception:
        return {"status": "ok"}


@mcp.tool()
def export_pplx_thread(entry_uuid: str, format: str = "md") -> dict[str, Any]:
    """Export a single thread entry. Format is ``"pdf"``, ``"md"``, or ``"docx"``.

    Returns ``{filename, file_content_64}`` per ``threads-history.md:369-394``;
    base64-decode on the client side.
    """
    import httpx

    base, headers = _pplx_session()
    headers["Content-Type"] = "application/json"
    resp = httpx.post(
        f"{base}/rest/entry/export",
        headers=headers,
        json={"entry_uuid": entry_uuid, "format": format},
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()


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
