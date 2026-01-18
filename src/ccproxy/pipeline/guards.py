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

    Detection by header presence, not token format.
    This allows any OAuth provider (Anthropic, ZAI, etc.) to work.

    Args:
        ctx: Pipeline context

    Returns:
        True if Authorization: Bearer is present
    """
    auth_header = ctx.authorization.lower()
    return auth_header.startswith("bearer ")


def is_anthropic_type_request(ctx: Context) -> bool:
    """Check if request is Anthropic-style OAuth.

    Detection criteria:
    - Has Bearer token (Authorization: Bearer ...)
    - Does NOT have x-api-key (which would indicate API key auth)

    This handles the case where LiteLLM converts Bearer → x-api-key
    for Anthropic provider, but we want to preserve OAuth flow.

    Args:
        ctx: Pipeline context

    Returns:
        True if request should be handled as Anthropic OAuth
    """
    has_bearer = ctx.authorization.lower().startswith("bearer ")
    has_api_key = bool(ctx.x_api_key)
    return has_bearer and not has_api_key


def is_anthropic_oauth_token(ctx: Context) -> bool:
    """Check if request has Anthropic OAuth token (sk-ant-oat).

    This is the legacy check that only matches Anthropic's token format.
    Prefer is_oauth_request() for universal detection.

    Args:
        ctx: Pipeline context

    Returns:
        True if Authorization header has Anthropic OAuth token
    """
    auth_header = ctx.authorization.lower()
    return auth_header.startswith("bearer sk-ant-oat")


def is_sentinel_key(ctx: Context) -> bool:
    """Check if request uses OAuth sentinel key.

    Sentinel keys have format: sk-ant-oat-ccproxy-{provider}
    They trigger OAuth token substitution from oat_sources config.

    Args:
        ctx: Pipeline context

    Returns:
        True if using sentinel key
    """
    from ccproxy.hooks import OAUTH_SENTINEL_PREFIX

    auth_header = ctx.authorization
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()  # Remove "Bearer " prefix
        return token.startswith(OAUTH_SENTINEL_PREFIX)
    return False


def routes_to_anthropic_provider(ctx: Context) -> bool:
    """Check if request routes to Anthropic-compatible API.

    Checks api_base, not just model name. This handles:
    - api.anthropic.com (official)
    - api.z.ai (ZAI)
    - Other Anthropic-compatible endpoints

    Args:
        ctx: Pipeline context

    Returns:
        True if routing to Anthropic-type API
    """
    config = ctx.ccproxy_model_config
    litellm_params = config.get("litellm_params", {})
    api_base = litellm_params.get("api_base", "")

    anthropic_hosts = [
        "anthropic.com",
        "z.ai",
    ]

    return any(host in api_base for host in anthropic_hosts)


def routes_to_claude_model(ctx: Context) -> bool:
    """Check if request routes to a Claude model.

    Args:
        ctx: Pipeline context

    Returns:
        True if routed model contains 'claude'
    """
    routed_model = ctx.ccproxy_litellm_model.lower()
    return "claude" in routed_model


def has_model_routing(ctx: Context) -> bool:
    """Check if model routing has been completed.

    Args:
        ctx: Pipeline context

    Returns:
        True if ccproxy_litellm_model is set in metadata
    """
    return bool(ctx.ccproxy_litellm_model)


def has_model_config(ctx: Context) -> bool:
    """Check if model configuration has been set.

    Args:
        ctx: Pipeline context

    Returns:
        True if ccproxy_model_config is set in metadata
    """
    return bool(ctx.ccproxy_model_config)


def is_health_check(ctx: Context) -> bool:
    """Check if request is a health check.

    LiteLLM uses internal health checks with a specific tag.

    Args:
        ctx: Pipeline context

    Returns:
        True if this is a health check request
    """
    tags = ctx.metadata.get("tags", [])
    return "litellm-internal-health-check" in tags


def needs_beta_headers(ctx: Context) -> bool:
    """Check if request needs Anthropic beta headers.

    Required for Claude Code emulation on Anthropic-type APIs.

    Args:
        ctx: Pipeline context

    Returns:
        True if beta headers should be added
    """
    if not has_model_config(ctx):
        return False

    # Need beta headers for Anthropic-type APIs
    return routes_to_anthropic_provider(ctx)


def needs_identity_injection(ctx: Context) -> bool:
    """Check if request needs Claude Code identity injection.

    Required when:
    - Using OAuth (not API key)
    - Routing to Anthropic-type API

    Args:
        ctx: Pipeline context

    Returns:
        True if identity should be injected
    """
    return is_oauth_request(ctx) and routes_to_anthropic_provider(ctx)
