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
    writes=[],
)
def extract_session_id(ctx: Context, params: dict[str, Any]) -> Context:
    """Extract session_id from metadata.user_id into flow metadata.

    Stores session_id on ``flow.metadata`` (mitmproxy per-flow dict), NOT
    on the body's metadata dict — writing into the body would inject fields
    that upstream APIs reject (e.g. Anthropic: "metadata.session_id: Extra
    inputs are not permitted").
    """
    metadata = ctx.metadata

    user_id = str(metadata.get("user_id", ""))
    if not user_id:
        return ctx

    session_id = parse_session_id(user_id)
    if session_id:
        ctx.flow.metadata["ccproxy.session_id"] = session_id
        logger.debug("Extracted session_id: %s", session_id)

    return ctx
