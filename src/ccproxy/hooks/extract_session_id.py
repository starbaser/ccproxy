"""Extract session ID from Claude Code's metadata.user_id field.

Parses session_id from either JSON object or legacy compound string
format and stores it in ``ctx.metadata["session_id"]``. Also forwards
transparent metadata from the request body.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ccproxy.pipeline.hook import hook
from ccproxy.utils import parse_session_id

if TYPE_CHECKING:
    from ccproxy.pipeline.context import Context

logger = logging.getLogger(__name__)


def extract_session_id_guard(ctx: Context) -> bool:
    """Guard: run if the body has metadata with a user_id field."""
    metadata = ctx.metadata
    return bool(metadata.get("user_id"))


@hook(
    reads=["metadata"],
    writes=["session_id"],
)
def extract_session_id(ctx: Context, params: dict[str, Any]) -> Context:
    """Extract session_id from metadata.user_id and forward transparent metadata."""
    metadata = ctx.metadata

    # Forward transparent metadata (skip protected namespace)
    for key, value in list(metadata.items()):
        if key.startswith("ccproxy_") or key == "user_id":
            continue
        # Don't overwrite existing values
        if key not in ctx.metadata:
            ctx.metadata[key] = value

    # Parse user_id for session information
    user_id = str(metadata.get("user_id", ""))
    if not user_id:
        return ctx

    session_id = parse_session_id(user_id)
    if session_id:
        ctx.session_id = session_id
        logger.debug("Extracted session_id: %s", session_id)

    return ctx
