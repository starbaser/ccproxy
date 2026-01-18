"""Forward OAuth hook for Bearer token forwarding.

Forwards OAuth Bearer tokens to LLM providers with proper header handling.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from litellm.litellm_core_utils.get_llm_provider_logic import get_llm_provider

from ccproxy.config import get_config
from ccproxy.hooks import OAUTH_SENTINEL_PREFIX
from ccproxy.pipeline.hook import hook

if TYPE_CHECKING:
    from ccproxy.pipeline.context import Context

logger = logging.getLogger(__name__)


def forward_oauth_guard(ctx: Context) -> bool:
    """Guard: Run if OAuth token present and model routing complete."""
    # Need routed model to determine provider
    if not ctx.ccproxy_litellm_model:
        return False

    # Run if we have OAuth token or sentinel key
    auth = ctx.authorization
    if auth.lower().startswith("bearer "):
        return True

    # Also run if we might need to inject cached OAuth token
    return True


@hook(
    reads=["ccproxy_litellm_model", "ccproxy_model_config", "authorization", "secret_fields"],
    writes=["authorization", "x-api-key", "api_key", "provider_specific_header"],
)
def forward_oauth(ctx: Context, params: dict[str, Any]) -> Context:
    """Forward OAuth token to provider if configured.

    Detects the target provider from routing metadata and forwards the OAuth
    Bearer token. For Anthropic-type APIs, also clears x-api-key (required
    for OAuth auth) and sets custom User-Agent if configured.

    Args:
        ctx: Pipeline context
        params: Additional parameters (unused)

    Returns:
        Modified context with authorization headers set
    """
    routed_model = ctx.ccproxy_litellm_model
    if not routed_model:
        logger.warning("forward_oauth: No routed_model in metadata, skipping")
        return ctx

    model_config = ctx.ccproxy_model_config or {}
    litellm_params = model_config.get("litellm_params", {})
    api_base = litellm_params.get("api_base")
    custom_provider = litellm_params.get("custom_llm_provider")

    # Get auth header from raw headers
    auth_header = ctx.authorization

    # Detect provider
    provider_name = _detect_provider(routed_model, custom_provider, api_base)
    logger.debug("forward_oauth: Detected provider '%s' for model '%s'", provider_name, routed_model)

    if not provider_name:
        logger.warning("forward_oauth: No provider detected for model %s", routed_model)
        return ctx

    # Handle sentinel key substitution
    auth_header = _handle_sentinel_key(auth_header, provider_name)

    # Fallback to cached OAuth token if no auth header
    if not auth_header:
        config = get_config()
        oauth_token = config.get_oauth_token(provider_name)
        if oauth_token:
            logger.debug("No authorization header, using cached OAuth token for '%s'", provider_name)
            auth_header = f"Bearer {oauth_token}" if not oauth_token.startswith("Bearer ") else oauth_token
        else:
            return ctx

    # Set up provider headers
    _setup_provider_headers(ctx, provider_name, auth_header)

    # Log OAuth forwarding
    user_agent = ctx.headers.get("user-agent", "")
    is_claude_cli = user_agent and "claude-cli" in user_agent
    log_msg = (
        "Forwarding request with Claude Code OAuth authentication"
        if is_claude_cli
        else f"Forwarding request with OAuth authentication for provider '{provider_name}'"
    )

    config = get_config()
    custom_user_agent = config.get_oauth_user_agent(provider_name)

    logger.info(
        log_msg,
        extra={
            "event": "oauth_forwarding",
            "provider": provider_name,
            "user_agent": custom_user_agent or user_agent,
            "model": routed_model,
            "auth_present": bool(auth_header),
            "custom_user_agent": bool(custom_user_agent),
        },
    )

    return ctx


def _detect_provider(
    routed_model: str,
    custom_provider: str | None,
    api_base: str | None,
) -> str | None:
    """Detect provider from model/api_base."""
    try:
        _, provider_name, _, _ = get_llm_provider(
            model=routed_model,
            custom_llm_provider=custom_provider,
            api_base=api_base,
        )
        return provider_name
    except Exception:
        # Fallback to name-based detection
        model_lower = routed_model.lower()
        if "claude" in model_lower:
            return "anthropic"
        elif "gemini" in model_lower or "palm" in model_lower:
            return "gemini"
        elif "gpt" in model_lower:
            return "openai"
        return None


def _handle_sentinel_key(auth_header: str, provider_name: str) -> str:
    """Handle sentinel key substitution."""
    sentinel_token = auth_header.removeprefix("Bearer ").strip()
    if not sentinel_token.startswith(OAUTH_SENTINEL_PREFIX):
        return auth_header

    sentinel_provider = sentinel_token[len(OAUTH_SENTINEL_PREFIX) :]
    config = get_config()
    oauth_token = config.get_oauth_token(sentinel_provider)

    if oauth_token:
        logger.info(
            "Sentinel key detected, substituting OAuth token for provider '%s'",
            sentinel_provider,
            extra={"event": "oauth_sentinel_substitution", "provider": sentinel_provider},
        )
        return f"Bearer {oauth_token}"
    else:
        logger.warning(
            "Sentinel key for provider '%s' but no OAuth token configured in oat_sources",
            sentinel_provider,
        )
        return ""


def _setup_provider_headers(ctx: Context, provider_name: str, auth_header: str) -> None:
    """Set up provider-specific headers."""
    # Ensure provider_specific_header structure exists
    if "custom_llm_provider" not in ctx.provider_headers:
        ctx.provider_headers["custom_llm_provider"] = provider_name
    if "extra_headers" not in ctx.provider_headers:
        ctx.provider_headers["extra_headers"] = {}

    extra = ctx.provider_headers["extra_headers"]

    # Set authorization header
    extra["authorization"] = auth_header

    # Clear x-api-key when using OAuth Bearer (Anthropic requires empty x-api-key with OAuth)
    extra["x-api-key"] = ""

    # Set api_key for LiteLLM internal handling
    if auth_header.startswith("Bearer "):
        oauth_token = auth_header[7:]  # Strip "Bearer " prefix
        ctx.api_key = oauth_token
        # LiteLLM requires model_group in metadata for api_key handling
        if "model_group" not in ctx.metadata:
            ctx.metadata["model_group"] = ctx.model or "default"

    # Set custom User-Agent if configured
    config = get_config()
    custom_user_agent = config.get_oauth_user_agent(provider_name)
    if custom_user_agent:
        extra["user-agent"] = custom_user_agent
        logger.debug("Setting custom User-Agent for provider '%s': %s", provider_name, custom_user_agent)
