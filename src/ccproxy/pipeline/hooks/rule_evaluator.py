"""Rule evaluator hook for request classification.

Evaluates classification rules to determine request routing label.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ccproxy.pipeline.hook import hook

if TYPE_CHECKING:
    from ccproxy.classifier import RequestClassifier
    from ccproxy.pipeline.context import Context

logger = logging.getLogger(__name__)


def rule_evaluator_guard(ctx: Context) -> bool:
    """Guard: Always run rule evaluation."""
    return True


@hook(reads=[], writes=["ccproxy_model_name", "ccproxy_alias_model"])
def rule_evaluator(ctx: Context, params: dict[str, Any]) -> Context:
    """Evaluate classification rules to determine request routing label.

    Runs the RequestClassifier against the request data. The classifier evaluates
    rules in configured order (first match wins) and returns a label like "thinking",
    "haiku", or "default".

    Args:
        ctx: Pipeline context
        params: Must contain 'classifier' (RequestClassifier instance)

    Returns:
        Modified context with metadata fields set:
        - ccproxy_alias_model: Original model from request
        - ccproxy_model_name: Classification label for routing
    """
    classifier: RequestClassifier | None = params.get("classifier")
    if classifier is None:
        logger.warning("Classifier not found in rule_evaluator params")
        return ctx

    # Store original model
    ctx.ccproxy_alias_model = ctx.model

    # Classify the request using raw data for compatibility
    data = ctx.to_litellm_data()
    ctx.ccproxy_model_name = classifier.classify(data)

    logger.debug(
        "Rule evaluation: %s -> %s",
        ctx.ccproxy_alias_model,
        ctx.ccproxy_model_name,
    )

    return ctx
