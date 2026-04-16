"""Reroute Gemini SDK traffic to cloudcode-pa.googleapis.com.

Detects WireGuard flows targeting ``generativelanguage.googleapis.com``,
wraps the standard Gemini API body in the ``v1internal`` envelope, and
redirects the flow to ``cloudcode-pa.googleapis.com``.

The ``v1internal`` endpoint requires a different body schema::

    Standard:    {contents, generationConfig, ...}
    v1internal:  {model, project, request: {contents, generationConfig, ...}}

The ``project`` field (Google Cloud AI Companion project ID) is resolved
once via ``loadCodeAssist`` and cached for the process lifetime.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import TYPE_CHECKING, Any

from mitmproxy.connection import Server
from mitmproxy.proxy.mode_specs import ReverseMode

from ccproxy.inspector.flow_store import InspectorMeta, TransformMeta
from ccproxy.pipeline.hook import hook

if TYPE_CHECKING:
    from ccproxy.pipeline.context import Context

logger = logging.getLogger(__name__)

_GEMINI_API_HOST = "generativelanguage.googleapis.com"
_CLOUDCODE_HOST = "cloudcode-pa.googleapis.com"
_MODEL_RE = re.compile(r"/models/([^/:]+)")
_ACTION_RE = re.compile(r":(\w+)$")

_cached_project: str | None = None


def _get_flow_host(ctx: Context) -> str:
    """Resolve the target hostname from the flow."""
    host = ctx.flow.request.headers.get("host", "")
    if host:
        return host.split(":")[0]
    return ctx.flow.request.pretty_host


def reroute_gemini_guard(ctx: Context) -> bool:
    """Guard: only run for WireGuard flows targeting generativelanguage.googleapis.com."""
    if isinstance(ctx.flow.client_conn.proxy_mode, ReverseMode):
        return False
    return _get_flow_host(ctx) == _GEMINI_API_HOST


def _resolve_project(auth_header: str) -> str | None:
    """Resolve the cloudaicompanion project ID via loadCodeAssist."""
    global _cached_project
    if _cached_project is not None:
        return _cached_project

    import httpx

    try:
        resp = httpx.post(
            f"https://{_CLOUDCODE_HOST}/v1internal:loadCodeAssist",
            headers={
                "Authorization": auth_header,
                "Content-Type": "application/json",
            },
            json={},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            project = data.get("cloudaicompanionProject")
            if project:
                _cached_project = project
                logger.info("Resolved cloudaicompanion project: %s", project)
                return project
        logger.warning("loadCodeAssist returned %d", resp.status_code)
    except Exception:
        logger.warning("Failed to resolve cloudaicompanion project", exc_info=True)
    return None


@hook(
    reads=["authorization", "x-goog-api-key"],
    writes=[],
)
def reroute_gemini(ctx: Context, _: dict[str, Any]) -> Context:
    """Reroute Gemini SDK traffic to cloudcode-pa v1internal endpoint."""
    flow = ctx.flow
    path = flow.request.path.split("?")[0]

    # Extract model from path: /v1beta/models/{model}:action
    model_match = _MODEL_RE.search(path)
    model = model_match.group(1) if model_match else ""

    # Extract action: :generateContent, :streamGenerateContent, etc.
    action_match = _ACTION_RE.search(path)
    if not action_match:
        logger.warning("reroute_gemini: no action in path %s, passing through", path)
        return ctx

    action = action_match.group(1)
    is_streaming = action == "streamGenerateContent"

    # Resolve project ID from loadCodeAssist
    auth = ctx.authorization
    project = _resolve_project(auth) if auth else None

    # Wrap body in v1internal envelope.
    # Must replace ctx._body (not flow.request.content) because
    # ctx.commit() at pipeline end serializes _body back to the flow.
    envelope: dict[str, Any] = {
        "model": model,
        "request": dict(ctx._body),
    }
    if project:
        envelope["project"] = project
    envelope["user_prompt_id"] = str(uuid.uuid4())

    ctx._body = envelope

    # Set transform metadata so the response phase can unwrap the v1internal envelope
    record = flow.metadata.get(InspectorMeta.RECORD)
    if record is not None:
        record.transform = TransformMeta(
            provider="gemini",
            model=model,
            request_data=dict(ctx._body),
            is_streaming=is_streaming,
        )

    # Rewrite destination
    new_path = f"/v1internal:{action}"
    if is_streaming:
        new_path += "?alt=sse"

    flow.request.host = _CLOUDCODE_HOST
    flow.request.port = 443
    flow.request.scheme = "https"
    flow.request.path = new_path
    flow.request.headers["host"] = _CLOUDCODE_HOST
    flow.server_conn = Server(address=(_CLOUDCODE_HOST, 443))

    # Strip x-goog-api-key if present (sentinel already resolved by forward_oauth)
    if flow.request.headers.get("x-goog-api-key"):
        del flow.request.headers["x-goog-api-key"]

    flow.comment = f"reroute gemini → {_CLOUDCODE_HOST} ({model})"
    logger.info(
        "reroute_gemini: %s %s → %s%s",
        model,
        _GEMINI_API_HOST,
        _CLOUDCODE_HOST,
        new_path,
    )

    return ctx
