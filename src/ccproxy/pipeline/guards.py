"""Shared guard functions for pipeline hooks.

These guards use header presence (not token format) for universal
detection across different OAuth providers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ccproxy.pipeline.context import Context


def is_oauth_request(ctx: Context) -> bool:
    """Check if request uses OAuth Bearer token.

    Detection by header presence, not token format, so any OAuth provider works.
    """
    auth_header = ctx.authorization.lower()
    return auth_header.startswith("bearer ")


def is_anthropic_destination(ctx: Context) -> bool:
    """Check if the flow targets an Anthropic API endpoint.

    Detected by presence of the ``anthropic-version`` header, which is
    set by all Anthropic SDKs and by lightllm's transform.
    """
    return ctx.get_header("anthropic-version") != ""
