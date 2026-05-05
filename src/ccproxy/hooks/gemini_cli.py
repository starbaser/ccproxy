"""Convert Gemini-bound traffic into the v1internal envelope cloudcode-pa speaks.

Triggered when ``forward_oauth`` resolved the Gemini sentinel key
(``flow.metadata["ccproxy.oauth_provider"] == "gemini"``). Single hook,
three responsibilities:

    1. Header masquerade  ── user-agent + x-goog-api-client → Gemini CLI fingerprint
    2. Body envelope wrap ── {contents, ...} → {model, project, request: {...}}
    3. Path/host rewrite  ── /v1beta/models/{m}:action → /v1internal:action[?alt=sse]

Idempotent on already-wrapped bodies (Glass-style clients pass through unchanged).
Sets ``record.transform`` so the addon's response phase unwraps the v1internal
envelope on the way back. Streaming responses get the envelope unwrapped
chunk-by-chunk via :class:`EnvelopeUnwrapStream`.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import TYPE_CHECKING, Any

import httpx
from glom import delete as glom_delete
from mitmproxy.connection import Server

from ccproxy.config import get_config
from ccproxy.flows.store import InspectorMeta, TransformMeta
from ccproxy.hooks.gemini_envelope import EnvelopeUnwrapStream
from ccproxy.pipeline.hook import hook

if TYPE_CHECKING:
    from ccproxy.pipeline.context import Context

__all__ = ["EnvelopeUnwrapStream", "gemini_cli", "gemini_cli_guard", "prewarm_project", "reset_cache"]

logger = logging.getLogger(__name__)

_CLOUDCODE_HOST = "cloudcode-pa.googleapis.com"
_MODEL_RE = re.compile(r"/models/([^/:]+)")
_KNOWN_GEMINI_ACTIONS = ("generateContent", "streamGenerateContent", "countTokens")
_ACTION_RE = re.compile(rf":({'|'.join(_KNOWN_GEMINI_ACTIONS)})$")
_SDK_UA_RE = re.compile(r"google-genai-sdk/")

_CLI_VERSION = "0.36.0"
_NODE_CLIENT_VERSION = "9.15.1"
_NODE_VERSION = "22.22.2"

_cached_project: str | None = None


def prewarm_project() -> None:
    """Resolve the cloudaicompanion project ID at startup.

    Called once after readiness if ``providers.gemini`` is configured.
    Calls ``loadCodeAssist`` with the Gemini OAuth token, caches the
    resulting ``cloudaicompanionProject`` for the process lifetime. On
    failure logs a warning but does not block startup — the hook will
    omit the ``project`` field at request time.
    """
    global _cached_project
    if _cached_project is not None:
        return

    config = get_config()
    if "gemini" not in config.providers:
        return

    token = config.get_oauth_token("gemini")
    if not token:
        logger.warning("gemini_cli: providers.gemini configured but token is empty; project resolution skipped")
        return

    try:
        resp = httpx.post(
            f"https://{_CLOUDCODE_HOST}/v1internal:loadCodeAssist",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={},
            timeout=10,
        )
        if resp.status_code == 200:
            project = resp.json().get("cloudaicompanionProject")
            if project:
                _cached_project = str(project)
                logger.info("gemini_cli: resolved cloudaicompanion project: %s", _cached_project)
                return
        logger.warning("gemini_cli: loadCodeAssist returned %d; project field will be omitted", resp.status_code)
    except Exception:
        logger.warning("gemini_cli: failed to resolve cloudaicompanion project", exc_info=True)


def reset_cache() -> None:
    """Clear the cached project ID (for tests)."""
    global _cached_project
    _cached_project = None


def gemini_cli_guard(ctx: Context) -> bool:
    """Run when forward_oauth resolved the Gemini sentinel key."""
    assert ctx.flow is not None
    return ctx.flow.metadata.get("ccproxy.oauth_provider") == "gemini"


@hook(
    reads=["authorization", "x-goog-api-key", "user-agent"],
    writes=["user-agent", "x-goog-api-client"],
)
def gemini_cli(ctx: Context, _: dict[str, Any]) -> Context:
    """Wrap Gemini traffic in v1internal envelope and route to cloudcode-pa."""
    assert ctx.flow is not None
    flow = ctx.flow
    path = flow.request.path.split("?")[0]

    action_match = _ACTION_RE.search(path)
    if not action_match:
        logger.debug(
            "gemini_cli: no known cloudcode-pa action %s in path %s, passing through",
            _KNOWN_GEMINI_ACTIONS,
            path,
        )
        return ctx
    action = action_match.group(1)
    is_streaming = action == "streamGenerateContent"

    body = ctx._body if isinstance(ctx._body, dict) else {}

    model_match = _MODEL_RE.search(path)
    if model_match:
        model = model_match.group(1)
    elif "model" in body:
        model = str(body["model"])
    else:
        inner = body.get("request") if isinstance(body.get("request"), dict) else None
        model = str(body.get("model", "")) if inner is None else str(inner.get("model", ""))

    # UA masquerade is intentionally conditional. cloudcode-pa rate-limits per
    # (token, project, user-agent) bucket; forcing every Gemini-sentinel client
    # to look like the CLI puts third-party tools (e.g. Glass on urllib) into
    # the same bucket as the user's interactive CLI session and exhausts shared
    # quota. Only masquerade when the caller is the google-genai SDK — that's
    # the case the original gemini_cli_compat hook covered.
    original_ua = ctx.get_header("user-agent", "")
    if _SDK_UA_RE.search(original_ua):
        cli_ua = (
            f"GeminiCLI/{_CLI_VERSION}/{model} (linux; x64; terminal) google-api-nodejs-client/{_NODE_CLIENT_VERSION}"
        )
        ctx.set_header("user-agent", cli_ua)
        ctx.set_header("x-goog-api-client", f"gl-node/{_NODE_VERSION}")

    already_wrapped = "request" in body and "contents" not in body
    if already_wrapped:
        logger.debug("gemini_cli: body already wrapped (Glass-style), skipping envelope")
    else:
        request_body = dict(body)
        glom_delete(request_body, "metadata", ignore_missing=True)

        envelope: dict[str, Any] = {
            "model": model,
            "request": request_body,
        }
        if _cached_project:
            envelope["project"] = _cached_project
        envelope["user_prompt_id"] = str(uuid.uuid4())
        ctx._body = envelope

    new_path = f"/v1internal:{action}"
    if is_streaming:
        new_path += "?alt=sse"
    flow.request.path = new_path

    flow.request.host = _CLOUDCODE_HOST
    flow.request.port = 443
    flow.request.scheme = "https"
    flow.request.headers["host"] = _CLOUDCODE_HOST
    flow.server_conn = Server(address=(_CLOUDCODE_HOST, 443))

    if flow.request.headers.get("x-goog-api-key"):
        del flow.request.headers["x-goog-api-key"]

    record = flow.metadata.get(InspectorMeta.RECORD)
    if record is not None:
        record.transform = TransformMeta(
            provider="gemini",
            model=model,
            request_data=dict(ctx._body) if isinstance(ctx._body, dict) else {},
            is_streaming=is_streaming,
        )

    flow.comment = f"gemini_cli → {_CLOUDCODE_HOST} ({model})"
    logger.info(
        "gemini_cli: %s → %s%s (wrapped=%s)",
        model,
        _CLOUDCODE_HOST,
        new_path,
        not already_wrapped,
    )
    return ctx
