"""Inject Claude Code identity hook.

Injects required system message for OAuth authentication with Anthropic.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ccproxy.hooks import CLAUDE_CODE_SYSTEM_PREFIX
from ccproxy.pipeline.guards import (
    is_oauth_request,
    routes_to_anthropic_provider,
)
from ccproxy.pipeline.hook import hook

if TYPE_CHECKING:
    from ccproxy.pipeline.context import Context

logger = logging.getLogger(__name__)


def inject_claude_code_identity_guard(ctx: Context) -> bool:
    """Guard: Run if OAuth request to Anthropic-type provider.

    Detects OAuth via:
    1. Original Authorization: Bearer header (client-provided OAuth)
    2. Metadata flag set by forward_oauth (cached OAuth token injection)
    """
    has_oauth = is_oauth_request(ctx) or bool(ctx.metadata.get("ccproxy_oauth_provider"))
    if not has_oauth:
        return False
    return routes_to_anthropic_provider(ctx)


@hook(
    reads=["authorization", "ccproxy_litellm_model", "ccproxy_model_config", "ccproxy_oauth_provider", "system"],
    writes=["system"],
)
def inject_claude_code_identity(ctx: Context, params: dict[str, Any]) -> Context:
    """Inject Claude Code identity into system message for OAuth authentication.

    Anthropic's OAuth tokens are restricted to Claude Code. To use them, the API
    request must include a system message that starts with "You are Claude Code".
    This hook prepends that required prefix to the system message.

    Only injects for requests going to api.anthropic.com - other Anthropic-compatible
    APIs like ZAI don't require this identity prefix.

    Args:
        ctx: Pipeline context
        params: Additional parameters (unused)

    Returns:
        Modified context with system message containing required prefix
    """
    # Check if model has its own api_key - if so, don't inject identity
    model_config = ctx.ccproxy_model_config or {}
    litellm_params = model_config.get("litellm_params", {})
    configured_api_key = litellm_params.get("api_key")
    if configured_api_key:
        logger.debug(
            "inject_claude_code_identity: Model has configured api_key, skipping identity injection"
        )
        return ctx

    # Check if this is going to api.anthropic.com vs other Anthropic-compatible APIs
    api_base = litellm_params.get("api_base", "")
    if api_base and "anthropic.com" not in api_base.lower():
        logger.debug(
            "inject_claude_code_identity: Skipping for api_base '%s' (not api.anthropic.com)",
            api_base,
        )
        return ctx

    system_msg = ctx.system

    if system_msg is not None:
        if isinstance(system_msg, str):
            # String system message
            if CLAUDE_CODE_SYSTEM_PREFIX not in system_msg:
                ctx.system = f"{CLAUDE_CODE_SYSTEM_PREFIX}\n\n{system_msg}"
        elif isinstance(system_msg, list):
            # Array of content blocks
            has_prefix = any(
                isinstance(block, dict)
                and block.get("type") == "text"
                and CLAUDE_CODE_SYSTEM_PREFIX in block.get("text", "")
                for block in system_msg
            )
            if not has_prefix:
                prefix_block = {"type": "text", "text": CLAUDE_CODE_SYSTEM_PREFIX}
                ctx.system = [prefix_block] + list(system_msg)
    else:
        # No system message - add one
        ctx.system = CLAUDE_CODE_SYSTEM_PREFIX

    routed_model = ctx.ccproxy_litellm_model
    logger.info(
        "Injected Claude Code identity for OAuth authentication",
        extra={"event": "claude_code_identity_injected", "model": routed_model},
    )

    return ctx
