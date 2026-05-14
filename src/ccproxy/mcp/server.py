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
You are connected to ccproxy, a transparent interceptor for Perplexity Pro.

MANDATORY RULES:

1. For ordinary Perplexity queries (search, chat, deep research): use your
   standard chat/completions endpoint pointed at this proxy. DO NOT use any
   MCP tool to perform a search — the chat endpoint is faster, supports
   streaming, and has the full 22-model catalog and multimodal pipeline.

2. Use MCP tools ONLY for: thread library curation (list, get, rename,
   share, delete, export, import), and quota checking (`pplx_usage`).

3. COST MODEL: every chat/completions call costs one Pro Search query
   (weekly quota). Deep Research (`model="perplexity/deep-research"`) consumes
   scarce monthly quota. Call `pplx_usage` once per session before scheduling
   expensive queries.

4. RESUME PROTOCOL: chat/completions responses carry `pplx_thread_url_slug`
   (top-level body field on non-streaming responses, on the final chunk for
   streaming, plus an `X-CCProxy-Perplexity-Thread-Slug` header). Round-trip
   that slug via `extra_body={"metadata": {"ccproxy_pplx_thread": slug}}` on
   the next chat/completions request to continue the same thread.
"""

_USAGE_CACHE_TTL_SECONDS: float = 60.0
_USAGE_CACHE: dict[str, Any] = {"expires_at": 0.0, "data": None}


def clear_usage_cache() -> None:
    """Reset the pplx_usage TTL cache. Called from the autouse test fixture."""
    _USAGE_CACHE["expires_at"] = 0.0
    _USAGE_CACHE["data"] = None


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
        raise RuntimeError(f"provider {PERPLEXITY_PROVIDER_NAME!r} not configured in ccproxy.yaml")
    token = cfg.resolve_oauth_token(PERPLEXITY_PROVIDER_NAME)
    if not token:
        raise RuntimeError(f"no session cookie resolved for {PERPLEXITY_PROVIDER_NAME!r}")
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
async def pplx_usage(ctx: Context, refresh: bool = False) -> dict[str, Any]:
    """Check current Perplexity quota (Pro Search weekly, Deep Research monthly, Labs, etc.).

    Wraps ``GET /rest/rate-limit/all``. Returns the raw payload — typically
    includes ``remaining_pro``, ``remaining_research``, ``remaining_labs``,
    ``remaining_agentic_research``, ``model_specific_limits``, and a
    ``sources.source_to_limit`` map of per-source monthly limits.

    Cached for 60 seconds. Aggressive polling by calling LLMs risks a
    shadow-ban on the session cookie; the cache makes calling this tool at
    the start of every turn cheap. ``refresh=True`` bypasses the cache for
    a forced re-fetch.
    """
    import httpx

    if (
        not refresh
        and _USAGE_CACHE["data"] is not None
        and time.monotonic() < _USAGE_CACHE["expires_at"]
    ):
        return cast(dict[str, Any], _USAGE_CACHE["data"])

    base, headers = _pplx_session()
    await ctx.info(f"fetching perplexity quota (refresh={refresh})")

    def _do() -> Any:
        return httpx.get(
            f"{base}/rest/rate-limit/all",
            headers=headers,
            timeout=15.0,
        )

    resp = await asyncio.to_thread(_do)
    resp.raise_for_status()
    data = cast(dict[str, Any], resp.json())
    _USAGE_CACHE["data"] = data
    _USAGE_CACHE["expires_at"] = time.monotonic() + _USAGE_CACHE_TTL_SECONDS
    return data


@mcp.tool()
async def list_pplx_threads(
    ctx: Context,
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
    await ctx.info(f"listing perplexity threads (limit={limit}, offset={offset})")

    def _do() -> Any:
        return httpx.post(
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

    resp = await asyncio.to_thread(_do)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, list):
        return cast(list[dict[str, Any]], data)
    if isinstance(data, dict) and isinstance(data.get("entries"), list):
        return cast(list[dict[str, Any]], data["entries"])
    return []


@mcp.tool()
async def list_pplx_recent_threads(ctx: Context, exclude_asi: bool = False) -> list[dict[str, Any]]:
    """List the user's most recent Perplexity threads (``GET /rest/thread/list_recent``).

    Lighter than ``list_pplx_threads`` — no pagination, no search; returns
    only the latest entries with fewer fields per record. Use for "show me
    my last few threads" workflows.

    Args:
        exclude_asi: When ``True``, omits Deep Research / ASI threads from
            the response.
    """
    import httpx

    base, headers = _pplx_session()
    headers["x-perplexity-request-reason"] = "home-sidebar"
    await ctx.info(f"listing recent perplexity threads (exclude_asi={exclude_asi})")

    def _do() -> Any:
        return httpx.get(
            f"{base}/rest/thread/list_recent",
            headers=headers,
            params={
                "version": "2.18",
                "source": "default",
                "exclude_asi": "true" if exclude_asi else "false",
            },
            timeout=15.0,
        )

    resp = await asyncio.to_thread(_do)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, list):
        return cast(list[dict[str, Any]], data)
    if isinstance(data, dict) and isinstance(data.get("entries"), list):
        return cast(list[dict[str, Any]], data["entries"])
    return []


def _fetch_pplx_thread(slug_or_uuid: str) -> dict[str, Any]:
    """Synchronous Perplexity thread fetch. Shared by the async tool and the
    ``import_pplx_thread`` helper which composes it."""
    import httpx

    from ccproxy.lightllm.pplx import PERPLEXITY_BLOCK_USE_CASES

    base, headers = _pplx_session()
    params: list[tuple[str, str | int | float | None]] = [
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
    return cast(dict[str, Any], resp.json())


@mcp.tool()
async def get_pplx_thread(slug_or_uuid: str, ctx: Context) -> dict[str, Any]:
    """Fetch a Perplexity thread by URL slug or context UUID (``/rest/thread/{slug}``)."""
    await ctx.info(f"fetching perplexity thread {slug_or_uuid}")
    return await asyncio.to_thread(_fetch_pplx_thread, slug_or_uuid)


@mcp.tool()
async def import_pplx_thread(
    slug_or_uuid: str,
    ctx: Context,
    citation_mode: str | None = None,
    include_reasoning: bool = False,
) -> dict[str, Any]:
    """Convert a Perplexity thread into a kit for next-turn resume.

    Returns ``{messages: [...], metadata: {ccproxy_pplx_thread: slug}, thread_info: {...}}``.

    The returned ``metadata.ccproxy_pplx_thread`` is the canonical resume
    handle — drop it into ``extra_body={"metadata": {...}}`` on the next
    chat/completions request and ccproxy's ``pplx_thread_inject`` hook
    resolves the slug to the thread's latest identifiers and routes the new
    turn as a Perplexity follow-up.

    The caller assembles the next chat/completions request as:

        {"messages": [...returned, new_user_turn], "metadata": {ccproxy_pplx_thread: slug}}

    Args:
        slug_or_uuid: Thread URL slug or ``context_uuid`` from
            ``list_pplx_threads`` / ``list_pplx_recent_threads``.
        citation_mode: ``"markdown"`` (default) embeds URLs as ``[N](url)``;
            ``"default"`` preserves ``[N]`` markers verbatim; ``"clean"``
            strips them entirely.
        include_reasoning: When ``True``, appends each turn's
            ``plan_block.goals[].description`` strings as a Reasoning footnote.
    """
    from ccproxy.config import get_config
    from ccproxy.lightllm.pplx import _thread_to_openai_messages

    mode = citation_mode or get_config().pplx.thread.citation_mode

    await ctx.info(f"importing perplexity thread {slug_or_uuid} (citation_mode={mode})")
    thread = await asyncio.to_thread(_fetch_pplx_thread, slug_or_uuid)
    messages = _thread_to_openai_messages(thread, citation_mode=mode, include_reasoning=include_reasoning)

    thread_meta_raw = thread.get("thread")
    thread_meta: dict[str, Any] = thread_meta_raw if isinstance(thread_meta_raw, dict) else {}
    entries_raw = thread.get("entries")
    entries: list[Any] = entries_raw if isinstance(entries_raw, list) else []

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


def _resolve_thread_ids(slug: str) -> dict[str, str]:
    """Resolve a slug to ``{entry_uuid, context_uuid, read_write_token}``.

    Used by every slug-first library-curation tool (``set_pplx_thread_title``,
    ``delete_pplx_thread``, ``export_pplx_thread``, ``update_pplx_thread_access``,
    ``bulk_delete_pplx_threads``). Fetches the thread and pulls the latest entry's
    identifiers — ``read_write_token`` is set once per thread on the first entry
    that carries it; we walk forward from the last entry to find it.
    """
    thread = _fetch_pplx_thread(slug)
    entries_raw = thread.get("entries")
    entries: list[Any] = entries_raw if isinstance(entries_raw, list) else []
    if not entries:
        raise ValueError(f"Perplexity thread {slug!r} has no entries (deleted or inaccessible?)")
    latest = entries[-1]
    if not isinstance(latest, dict):
        raise ValueError(f"Perplexity thread {slug!r} has malformed entries")

    entry_uuid = latest.get("uuid") or latest.get("backend_uuid")
    context_uuid = latest.get("context_uuid")
    thread_block = thread.get("thread")
    if not context_uuid and isinstance(thread_block, dict):
        context_uuid = thread_block.get("context_uuid")

    read_write_token: str | None = None
    for entry in reversed(entries):
        if isinstance(entry, dict):
            rwt = entry.get("read_write_token")
            if isinstance(rwt, str) and rwt:
                read_write_token = rwt
                break

    if not isinstance(entry_uuid, str) or not isinstance(context_uuid, str) or not read_write_token:
        raise ValueError(
            f"Perplexity thread {slug!r} missing required identifiers (entry_uuid/context_uuid/read_write_token)"
        )
    return {
        "entry_uuid": entry_uuid,
        "context_uuid": context_uuid,
        "read_write_token": read_write_token,
    }


@mcp.tool()
async def set_pplx_thread_title(slug: str, title: str, ctx: Context) -> dict[str, Any]:
    """Set a custom title for a Perplexity thread (``POST /rest/thread/set_thread_title``).

    Slug-first: ccproxy resolves the slug to the thread's ``context_uuid`` and
    ``read_write_token`` internally — callers don't need to surface either.
    """
    import httpx

    await ctx.info(f"resolving thread {slug!r} for rename")
    ids = await asyncio.to_thread(_resolve_thread_ids, slug)

    base, headers = _pplx_session()
    headers["Content-Type"] = "application/json"
    headers["x-perplexity-request-reason"] = "home-sidebar"
    await ctx.info(f"renaming perplexity thread {slug!r} to {title!r}")

    def _do() -> Any:
        return httpx.post(
            f"{base}/rest/thread/set_thread_title",
            headers=headers,
            json={
                "context_uuid": ids["context_uuid"],
                "title": title,
                "read_write_token": ids["read_write_token"],
            },
            timeout=15.0,
        )

    resp = await asyncio.to_thread(_do)
    resp.raise_for_status()
    try:
        return cast(dict[str, Any], resp.json())
    except Exception:
        return {"status": "ok", "slug": slug, "title": title}


@mcp.tool()
async def update_pplx_thread_access(slug: str, public: bool, ctx: Context) -> dict[str, Any]:
    """Share or unshare a Perplexity thread (``POST /rest/thread/update_thread_access``).

    Sets ``updated_access=2`` for ``public=True`` (shareable), ``1`` for
    ``public=False`` (private). When making a thread public, the response
    includes the shareable URL at ``https://www.perplexity.ai/search/{slug}``.

    Slug-first: ccproxy resolves the slug to ``context_uuid`` and
    ``read_write_token`` internally.
    """
    import httpx

    await ctx.info(f"resolving thread {slug!r} for access update")
    ids = await asyncio.to_thread(_resolve_thread_ids, slug)

    base, headers = _pplx_session()
    headers["Content-Type"] = "application/json"
    headers["x-perplexity-request-reason"] = "home-sidebar"
    target_access = 2 if public else 1
    await ctx.info(f"updating perplexity thread {slug!r} access -> {'public' if public else 'private'}")

    def _do() -> Any:
        return httpx.post(
            f"{base}/rest/thread/update_thread_access",
            headers=headers,
            json={
                "context_uuid": ids["context_uuid"],
                "updated_access": target_access,
                "read_write_token": ids["read_write_token"],
            },
            timeout=15.0,
        )

    resp = await asyncio.to_thread(_do)
    resp.raise_for_status()
    try:
        body = cast(dict[str, Any], resp.json())
    except Exception:
        body = {"status": "ok", "access": target_access}

    if public:
        body["share_url"] = f"{base}/search/{slug}"
    return body


@mcp.tool()
async def delete_pplx_thread(slug: str, ctx: Context) -> dict[str, Any]:
    """Delete a Perplexity thread (``DELETE /rest/thread/delete_thread_by_entry_uuid``).

    Slug-first: resolves the slug to the thread's latest ``entry_uuid`` and
    ``read_write_token`` internally. Deletes the entire thread (all turns).
    """
    import httpx

    await ctx.info(f"resolving thread {slug!r} for deletion")
    ids = await asyncio.to_thread(_resolve_thread_ids, slug)

    base, headers = _pplx_session()
    headers["Content-Type"] = "application/json"
    headers["x-perplexity-request-reason"] = "home-sidebar"
    await ctx.info(f"deleting perplexity thread {slug!r} (entry={ids['entry_uuid'][:8]}...)")

    def _do() -> Any:
        return httpx.request(
            "DELETE",
            f"{base}/rest/thread/delete_thread_by_entry_uuid",
            headers=headers,
            json={
                "entry_uuid": ids["entry_uuid"],
                "read_write_token": ids["read_write_token"],
            },
            timeout=15.0,
        )

    resp = await asyncio.to_thread(_do)
    resp.raise_for_status()
    try:
        return cast(dict[str, Any], resp.json())
    except Exception:
        return {"status": "ok"}


@mcp.tool()
async def bulk_delete_pplx_threads(slugs: list[str], ctx: Context) -> dict[str, Any]:
    """Delete multiple Perplexity threads in one call (``DELETE /rest/thread``).

    Resolves each slug to its latest ``entry_uuid``; sends them together with a
    single ``read_write_token`` (token authority spans the user's library, so
    any one thread's token authenticates the whole batch).

    Returns ``{deleted: list[str], failed: list[{slug, error}], response: <upstream>}``.
    Per-slug resolution failures are collected, not raised — partial success is the
    expected outcome for cleanup workflows.
    """
    import httpx

    if not slugs:
        raise ValueError("bulk_delete_pplx_threads: slugs must be non-empty")

    await ctx.info(f"resolving {len(slugs)} thread(s) for bulk delete")

    def _resolve_all() -> tuple[list[tuple[str, str]], list[dict[str, str]], str | None]:
        resolved: list[tuple[str, str]] = []
        failed: list[dict[str, str]] = []
        token: str | None = None
        for slug in slugs:
            try:
                ids = _resolve_thread_ids(slug)
            except Exception as exc:
                failed.append({"slug": slug, "error": str(exc)})
                continue
            resolved.append((slug, ids["entry_uuid"]))
            if token is None:
                token = ids["read_write_token"]
        return resolved, failed, token

    resolved, failed, token = await asyncio.to_thread(_resolve_all)
    if not resolved or token is None:
        return {"deleted": [], "failed": failed, "response": None}

    base, headers = _pplx_session()
    headers["Content-Type"] = "application/json"
    headers["x-perplexity-request-reason"] = "home-sidebar"
    await ctx.info(f"bulk deleting {len(resolved)} thread(s)")

    def _do() -> Any:
        return httpx.request(
            "DELETE",
            f"{base}/rest/thread",
            headers=headers,
            json={
                "entry_uuids": [entry for _, entry in resolved],
                "read_write_token": token,
            },
            timeout=30.0,
        )

    resp = await asyncio.to_thread(_do)
    resp.raise_for_status()
    try:
        upstream: Any = resp.json()
    except Exception:
        upstream = None
    return {
        "deleted": [slug for slug, _ in resolved],
        "failed": failed,
        "response": upstream,
    }


@mcp.tool()
async def export_pplx_thread(slug: str, ctx: Context, format: str = "md") -> dict[str, Any]:
    """Export the latest entry of a Perplexity thread (``POST /rest/entry/export``).

    Slug-first: defaults to exporting the thread's most recent entry. Format
    is ``"pdf"``, ``"md"``, or ``"docx"``. Returns ``{filename, file_content_64}``
    per ``threads-history.md:369-394``; base64-decode on the client side.
    """
    import httpx

    await ctx.info(f"resolving thread {slug!r} for export")
    ids = await asyncio.to_thread(_resolve_thread_ids, slug)

    base, headers = _pplx_session()
    headers["Content-Type"] = "application/json"
    headers["x-perplexity-request-reason"] = "entry-export"
    await ctx.info(f"exporting perplexity thread {slug!r} as {format!r}")

    def _do() -> Any:
        return httpx.post(
            f"{base}/rest/entry/export",
            headers=headers,
            json={"entry_uuid": ids["entry_uuid"], "format": format},
            timeout=30.0,
        )

    resp = await asyncio.to_thread(_do)
    resp.raise_for_status()
    return cast(dict[str, Any], resp.json())


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
