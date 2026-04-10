"""Add Anthropic beta headers for Claude Code OAuth impersonation.

Merges required beta headers into the ``anthropic-beta`` header and
sets ``anthropic-version``. Fires on all flows targeting Anthropic APIs.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ccproxy.constants import ANTHROPIC_BETA_HEADERS
from ccproxy.pipeline.hook import hook

if TYPE_CHECKING:
    from ccproxy.pipeline.context import Context

logger = logging.getLogger(__name__)


def add_beta_headers_guard(ctx: Context) -> bool:
    """Guard: run if the flow targets an Anthropic endpoint."""
    return ctx.get_header("anthropic-version") != ""


@hook(
    reads=["anthropic-beta"],
    writes=["anthropic-beta", "anthropic-version"],
)
def add_beta_headers(ctx: Context, params: dict[str, Any]) -> Context:
    """Merge required Anthropic beta headers."""
    existing = ctx.get_header("anthropic-beta")
    existing_list = [h.strip() for h in existing.split(",") if h.strip()] if existing else []
    merged = list(dict.fromkeys(ANTHROPIC_BETA_HEADERS + existing_list))
    ctx.set_header("anthropic-beta", ",".join(merged))

    if not ctx.get_header("anthropic-version"):
        ctx.set_header("anthropic-version", "2023-06-01")

    return ctx
