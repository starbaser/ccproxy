"""Extract session ID hook for LangFuse tracking.

Extracts session_id from Claude Code's user_id field format,
with fallback to metadata.session_id for other clients (e.g. talkstream).

For /v1/messages (Anthropic) routes, LiteLLM's validate_anthropic_api_metadata
strips non-user_id keys from data["metadata"] before Langfuse reads it.
Langfuse-relevant keys are injected as langfuse_* headers into
proxy_server_request, which Langfuse recovers via add_metadata_from_header.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ccproxy.pipeline.hook import hook

if TYPE_CHECKING:
    from ccproxy.pipeline.context import Context

logger = logging.getLogger(__name__)

# Langfuse metadata keys read from litellm_params["metadata"] that get stripped
# by validate_anthropic_api_metadata on /v1/messages routes.  Injecting them as
# langfuse_* headers lets Langfuse's add_metadata_from_header recover them.
_LANGFUSE_HEADER_KEYS = frozenset(
    {
        "session_id",
        "trace_name",
        "generation_name",
        "trace_id",
        "existing_trace_id",
        "trace_user_id",
    }
)


def extract_session_id_guard(ctx: Context) -> bool:
    """Guard: Run if proxy_server_request exists."""
    return bool(ctx._raw_data.get("proxy_server_request"))


@hook(reads=["proxy_server_request"], writes=["session_id", "trace_metadata"])
def extract_session_id(ctx: Context, params: dict[str, Any]) -> Context:
    """Forward client body metadata and extract session_id for Langfuse.

    Transparently forwards all client body metadata keys to ctx.metadata so
    Langfuse-native fields (session_id, trace_name, generation_name,
    trace_user_id, tags, etc.) pass through to LiteLLM's Langfuse callback.

    Additionally parses Claude Code's compound user_id format
    (user_{hash}_account_{uuid}_session_{uuid}) to extract session_id.
    """
    request = ctx._raw_data.get("proxy_server_request", {})
    body = request.get("body", {})
    if not isinstance(body, dict):
        return ctx

    body_metadata = body.get("metadata", {})

    # Forward all body metadata to ctx.metadata (transparent proxy).
    # Internal ccproxy keys (ccproxy_*) and already-set keys are not overwritten.
    for key, value in body_metadata.items():
        if key.startswith("ccproxy_") or key in ctx.metadata:
            continue
        ctx.metadata[key] = value

    user_id = body_metadata.get("user_id", "")

    # Claude Code user_id format: user_{hash}_account_{uuid}_session_{uuid}
    if user_id and "_session_" in user_id:
        parts = user_id.split("_session_")
        if len(parts) == 2:
            session_id = parts[1]
            ctx.metadata["session_id"] = session_id
            logger.debug("Extracted session_id from user_id: %s", session_id)

            prefix = parts[0]
            if "_account_" in prefix:
                user_account = prefix.split("_account_")
                if len(user_account) == 2:
                    user_hash = user_account[0].replace("user_", "")
                    account_id = user_account[1]
                    ctx.metadata["trace_user_id"] = user_hash
                    if "trace_metadata" not in ctx.metadata:
                        ctx.metadata["trace_metadata"] = {}
                    ctx.metadata["trace_metadata"]["claude_account_id"] = account_id

    # Inject langfuse_* headers so values survive LiteLLM's
    # validate_anthropic_api_metadata stripping on /v1/messages routes.
    _inject_langfuse_headers(request, ctx.metadata)

    return ctx


def _inject_langfuse_headers(request: dict[str, Any], metadata: dict[str, Any]) -> None:
    """Inject langfuse_* headers into proxy_server_request for Langfuse recovery.

    LiteLLM's Langfuse integration reads headers prefixed with ``langfuse_``
    from ``proxy_server_request`` and strips the prefix before merging into
    the metadata dict that Langfuse uses for trace/session grouping.
    """
    headers = request.get("headers")
    if not isinstance(headers, dict):
        return

    for key in _LANGFUSE_HEADER_KEYS:
        value = metadata.get(key)
        if not value or not isinstance(value, str):
            continue
        header_key = f"langfuse_{key}"
        if header_key not in headers:
            headers[header_key] = value
