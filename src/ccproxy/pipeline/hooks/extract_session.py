"""Extract session ID hook for LangFuse tracking.

Extracts session_id from Claude Code's user_id field format.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ccproxy.pipeline.hook import hook

if TYPE_CHECKING:
    from ccproxy.pipeline.context import Context

logger = logging.getLogger(__name__)


def extract_session_id_guard(ctx: Context) -> bool:
    """Guard: Run if proxy_server_request exists."""
    return bool(ctx._raw_data.get("proxy_server_request"))


@hook(reads=["proxy_server_request"], writes=["session_id", "trace_metadata"])
def extract_session_id(ctx: Context, params: dict[str, Any]) -> Context:
    """Extract session_id from Claude Code's user_id field for LangFuse.

    Claude Code embeds session info in the metadata.user_id field with format:
    user_{hash}_account_{uuid}_session_{uuid}

    This hook extracts the session_id and sets it on metadata["session_id"].

    Args:
        ctx: Pipeline context
        params: Additional parameters (unused)

    Returns:
        Modified context with session_id and trace_metadata set
    """
    # Get user_id from request body metadata
    request = ctx._raw_data.get("proxy_server_request", {})
    body = request.get("body", {})
    if not isinstance(body, dict):
        return ctx

    body_metadata = body.get("metadata", {})
    user_id = body_metadata.get("user_id", "")

    if not user_id or "_session_" not in user_id:
        return ctx

    # Parse: user_{hash}_account_{uuid}_session_{uuid}
    parts = user_id.split("_session_")
    if len(parts) != 2:
        return ctx

    session_id = parts[1]
    ctx.metadata["session_id"] = session_id
    logger.debug("Extracted session_id: %s", session_id)

    # Also extract user and account for trace_metadata
    prefix = parts[0]
    if "_account_" in prefix:
        user_account = prefix.split("_account_")
        if len(user_account) == 2:
            user_hash = user_account[0].replace("user_", "")
            account_id = user_account[1]
            if "trace_metadata" not in ctx.metadata:
                ctx.metadata["trace_metadata"] = {}
            ctx.metadata["trace_metadata"]["claude_user_hash"] = user_hash
            ctx.metadata["trace_metadata"]["claude_account_id"] = account_id

    return ctx
