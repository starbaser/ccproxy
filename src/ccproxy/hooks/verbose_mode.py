"""Verbose mode hook — enables full thinking block output.

Strips ``redact-thinking-*`` from the ``anthropic-beta`` header so
thinking blocks arrive unredacted in API responses.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ccproxy.pipeline.guards import is_anthropic_destination
from ccproxy.pipeline.hook import hook

if TYPE_CHECKING:
    from ccproxy.pipeline.context import Context

logger = logging.getLogger(__name__)

_STRIP_PREFIX = "redact-thinking-"


def verbose_mode_guard(ctx: Context) -> bool:
    """Guard: run if targeting an Anthropic endpoint."""
    return is_anthropic_destination(ctx)


@hook(reads=["anthropic-beta"], writes=[])
def verbose_mode(ctx: Context, params: dict[str, Any]) -> Context:
    """Remove redact-thinking-* from anthropic-beta header."""
    beta = ctx.get_header("anthropic-beta")
    if not beta:
        return ctx

    filtered = ",".join(b.strip() for b in beta.split(",") if not b.strip().startswith(_STRIP_PREFIX))
    if filtered != beta:
        ctx.set_header("anthropic-beta", filtered)
        logger.info("Verbose mode: stripped redact-thinking beta header")

    return ctx
