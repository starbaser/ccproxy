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


def routes_to_anthropic_provider(ctx: Context) -> bool:
    """Check if request routes to Anthropic-compatible API (api_base, not model name).

    Handles api.anthropic.com, api.z.ai, and other Anthropic-compatible endpoints.
    """
    config = ctx.ccproxy_model_config
    litellm_params = config.get("litellm_params", {})
    api_base = litellm_params.get("api_base", "")

    anthropic_hosts = [
        "anthropic.com",
        "z.ai",
    ]

    return any(host in api_base for host in anthropic_hosts)
