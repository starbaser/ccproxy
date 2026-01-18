"""Forward API key hook.

Forwards x-api-key header from incoming request to proxied request.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ccproxy.pipeline.hook import hook

if TYPE_CHECKING:
    from ccproxy.pipeline.context import Context

logger = logging.getLogger(__name__)


def forward_apikey_guard(ctx: Context) -> bool:
    """Guard: Run if x-api-key header is present."""
    return bool(ctx.x_api_key)


@hook(
    reads=["secret_fields"],
    writes=["x-api-key", "provider_specific_header"],
)
def forward_apikey(ctx: Context, params: dict[str, Any]) -> Context:
    """Forward x-api-key header from incoming request to proxied request.

    Args:
        ctx: Pipeline context
        params: Additional parameters (unused)

    Returns:
        Modified context with x-api-key header forwarded
    """
    api_key = ctx.x_api_key
    if not api_key:
        return ctx

    # Ensure provider_specific_header structure exists
    if "extra_headers" not in ctx.provider_headers:
        ctx.provider_headers["extra_headers"] = {}

    # Set the x-api-key header
    ctx.provider_headers["extra_headers"]["x-api-key"] = api_key

    logger.info(
        "Forwarding request with x-api-key header",
        extra={"event": "apikey_forwarding", "api_key_present": True},
    )

    return ctx
