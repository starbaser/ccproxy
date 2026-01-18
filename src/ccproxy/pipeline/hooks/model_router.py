"""Model router hook for request routing.

Routes request to actual LiteLLM model based on classification label.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ccproxy.config import get_config
from ccproxy.pipeline.hook import hook

if TYPE_CHECKING:
    from ccproxy.pipeline.context import Context
    from ccproxy.router import ModelRouter as Router

logger = logging.getLogger(__name__)


def model_router_guard(ctx: Context) -> bool:
    """Guard: Run if classification label is present."""
    return bool(ctx.ccproxy_model_name) or bool(ctx.model)


@hook(
    reads=["ccproxy_model_name", "ccproxy_alias_model"],
    writes=["model", "ccproxy_litellm_model", "ccproxy_model_config", "ccproxy_is_passthrough"],
)
def model_router(ctx: Context, params: dict[str, Any]) -> Context:
    """Route request to actual LiteLLM model based on classification label.

    Takes the ccproxy_model_name from rule_evaluator and looks up the corresponding
    model configuration from the ModelRouter. Supports passthrough mode where
    "default" classification keeps the original requested model.

    Args:
        ctx: Pipeline context (must have ccproxy_model_name in metadata)
        params: Must contain 'router' (ModelRouter instance)

    Returns:
        Modified context with:
        - model: Updated to routed model name
        - ccproxy_litellm_model: The model being used
        - ccproxy_model_config: Full model config dict
        - ccproxy_is_passthrough: True if using passthrough mode

    Raises:
        ValueError: If no model configured for label and no default fallback
    """
    router: Router | None = params.get("router")
    if router is None:
        logger.warning("Router not found in model_router params")
        return ctx

    # Get model_name with safe default
    model_name = ctx.ccproxy_model_name or "default"
    if not model_name:
        logger.warning("No ccproxy_model_name found, using default")
        model_name = "default"

    # Check if we should pass through the original model for "default" routing
    config = get_config()
    if model_name == "default" and config.default_model_passthrough:
        original_model = ctx.ccproxy_alias_model
        if original_model:
            # Keep the original model - no routing needed
            ctx.ccproxy_litellm_model = original_model
            ctx.ccproxy_model_config = {}
            ctx.ccproxy_is_passthrough = True
            logger.debug(
                "Using passthrough mode for default routing: keeping original model %s",
                original_model,
            )
            return ctx
        else:
            logger.warning("No original model found for passthrough mode, falling back to routing")

    # Standard routing logic - get model for model_name from router
    model_config = router.get_model_for_label(model_name)

    if model_config is not None:
        routed_model = model_config.get("litellm_params", {}).get("model")
        if routed_model:
            ctx.model = routed_model
        else:
            logger.warning("No model found in config for model_name: %s", model_name)
        ctx.ccproxy_litellm_model = routed_model or ""
        ctx.ccproxy_model_config = model_config
        ctx.ccproxy_is_passthrough = False
    else:
        # No model config found - try reload
        logger.warning("No model configured for model_name '%s' and no 'default' available", model_name)
        router.reload_models()
        model_config = router.get_model_for_label(model_name)

        if model_config is not None:
            routed_model = model_config.get("litellm_params", {}).get("model")
            if routed_model:
                ctx.model = routed_model
            ctx.ccproxy_litellm_model = routed_model or ""
            ctx.ccproxy_model_config = model_config
            ctx.ccproxy_is_passthrough = False
            logger.info("Successfully routed after model reload: %s -> %s", model_name, routed_model)
        else:
            raise ValueError(f"No model configured for model_name '{model_name}' and no 'default' available")

    return ctx
