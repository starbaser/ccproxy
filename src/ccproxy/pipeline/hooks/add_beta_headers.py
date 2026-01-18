"""Add beta headers hook for Claude Code impersonation.

Adds anthropic-beta headers required for OAuth authentication.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from litellm.litellm_core_utils.get_llm_provider_logic import get_llm_provider

from ccproxy.hooks import ANTHROPIC_BETA_HEADERS
from ccproxy.pipeline.guards import routes_to_anthropic_provider
from ccproxy.pipeline.hook import hook

if TYPE_CHECKING:
    from ccproxy.pipeline.context import Context

logger = logging.getLogger(__name__)


def add_beta_headers_guard(ctx: Context) -> bool:
    """Guard: Run if routing to Anthropic-type provider."""
    if not ctx.ccproxy_litellm_model:
        return False

    # Check if routing to Anthropic-compatible API
    return routes_to_anthropic_provider(ctx)


@hook(
    reads=["ccproxy_litellm_model", "ccproxy_model_config"],
    writes=["anthropic-beta", "anthropic-version", "provider_specific_header", "extra_headers"],
)
def add_beta_headers(ctx: Context, params: dict[str, Any]) -> Context:
    """Add anthropic-beta headers for Claude Code impersonation.

    When routing to Anthropic-type API, adds required beta headers that allow
    Claude Max OAuth tokens to be accepted.

    Args:
        ctx: Pipeline context
        params: Additional parameters (unused)

    Returns:
        Modified context with anthropic-beta and anthropic-version headers
    """
    routed_model = ctx.ccproxy_litellm_model
    if not routed_model:
        return ctx

    # Detect provider
    model_config = ctx.ccproxy_model_config or {}
    litellm_params = model_config.get("litellm_params", {})
    api_base = litellm_params.get("api_base")
    custom_provider = litellm_params.get("custom_llm_provider")

    provider_name = _detect_provider(routed_model, custom_provider, api_base)
    if provider_name != "anthropic":
        return ctx

    # Build merged beta headers
    existing = ""
    if "extra_headers" in ctx.provider_headers:
        existing = ctx.provider_headers["extra_headers"].get("anthropic-beta", "")
    elif "extra_headers" in ctx._raw_data:
        existing = ctx._raw_data["extra_headers"].get("anthropic-beta", "")

    existing_list = [b.strip() for b in existing.split(",") if b.strip()]
    merged = list(dict.fromkeys(ANTHROPIC_BETA_HEADERS + existing_list))
    merged_str = ",".join(merged)

    # Method 1: provider_specific_header (for proxy router)
    if "custom_llm_provider" not in ctx.provider_headers:
        ctx.provider_headers["custom_llm_provider"] = "anthropic"
    if "extra_headers" not in ctx.provider_headers:
        ctx.provider_headers["extra_headers"] = {}

    ctx.provider_headers["extra_headers"]["anthropic-beta"] = merged_str
    ctx.provider_headers["extra_headers"]["anthropic-version"] = "2023-06-01"

    # Method 2: extra_headers (direct to completion call)
    if "extra_headers" not in ctx._raw_data:
        ctx._raw_data["extra_headers"] = {}
    ctx._raw_data["extra_headers"]["anthropic-beta"] = merged_str
    ctx._raw_data["extra_headers"]["anthropic-version"] = "2023-06-01"

    logger.info(
        "Added anthropic-beta headers for Claude Code impersonation",
        extra={"event": "beta_headers_added", "model": routed_model},
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
        # Fallback: check if this is Anthropic-type API
        if api_base and ("anthropic.com" in api_base or "z.ai" in api_base):
            return "anthropic"
        if "claude" in routed_model.lower():
            return "anthropic"
        return None
