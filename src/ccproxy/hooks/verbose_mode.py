"""Verbose mode hook — enables full thinking block output."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ccproxy.pipeline.guards import routes_to_anthropic_provider
from ccproxy.pipeline.hook import hook

if TYPE_CHECKING:
    from ccproxy.pipeline.context import Context

logger = logging.getLogger(__name__)

_STRIP_PREFIX = "redact-thinking-"


def verbose_mode_guard(ctx: Context) -> bool:
    """Guard: Run if routing to Anthropic-type provider."""
    return routes_to_anthropic_provider(ctx)


@hook(reads=["extra_headers"], writes=[])
def verbose_mode(ctx: Context, params: dict[str, Any]) -> Context:
    """Remove redact-thinking-* from anthropic-beta header.

    Enables full thinking block content in API responses.
    """
    for headers_dict in (
        ctx.provider_headers.get("extra_headers"),
        ctx._raw_data.get("extra_headers"),
    ):
        if not isinstance(headers_dict, dict):
            continue
        beta = headers_dict.get("anthropic-beta", "")
        if not beta:
            continue
        filtered = ",".join(b.strip() for b in beta.split(",") if not b.strip().startswith(_STRIP_PREFIX))
        if filtered != beta:
            headers_dict["anthropic-beta"] = filtered
            logger.info("Verbose mode: stripped redact-thinking beta header")

    return ctx
