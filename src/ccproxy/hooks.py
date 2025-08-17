import logging
from typing import Any
from urllib.parse import urlparse

from ccproxy.classifier import RequestClassifier
from ccproxy.config import get_config
from ccproxy.router import ModelRouter

# Set up structured logging
logger = logging.getLogger(__name__)


def rule_evaluator(data: dict[str, Any], user_api_key_dict: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    classifier = kwargs.get("classifier")
    if not isinstance(classifier, RequestClassifier):
        logger.warning("Classifier not found or invalid type in rule_evaluator")
        return data

    if "metadata" not in data:
        data["metadata"] = {}

    # Store original model
    data["metadata"]["ccproxy_alias_model"] = data.get("model")

    # Classify the request
    data["metadata"]["ccproxy_model_name"] = classifier.classify(data)
    return data


def model_router(data: dict[str, Any], user_api_key_dict: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    router = kwargs.get("router")
    if not isinstance(router, ModelRouter):
        logger.warning("Router not found or invalid type in model_router")
        return data

    # Ensure metadata exists
    if "metadata" not in data:
        data["metadata"] = {}

    # Get model_name with safe default
    model_name = data.get("metadata", {}).get("ccproxy_model_name", "default")
    if not model_name:
        logger.warning("No ccproxy_model_name found, using default")
        model_name = "default"

    # Check if we should pass through the original model for "default" routing
    config = get_config()
    if model_name == "default" and config.default_model_passthrough:
        # Use the original model that Claude Code requested
        original_model = data["metadata"].get("ccproxy_alias_model")
        if original_model:
            # Keep the original model - no routing needed
            data["metadata"]["ccproxy_litellm_model"] = original_model
            data["metadata"]["ccproxy_model_config"] = None  # No specific config since we're not routing
            data["metadata"]["ccproxy_is_passthrough"] = True  # Mark as passthrough decision
            logger.debug(f"Using passthrough mode for default routing: keeping original model {original_model}")
            # Skip the routing logic and go directly to request ID generation
        else:
            logger.warning("No original model found for passthrough mode, falling back to routing")
            # Continue with routing logic below
            model_config = router.get_model_for_label(model_name)
    else:
        # Standard routing logic - get model for model_name from router
        model_config = router.get_model_for_label(model_name)

    # Only process model_config if we didn't already handle passthrough above
    passthrough_handled = (
        model_name == "default" and config.default_model_passthrough and data["metadata"].get("ccproxy_litellm_model")
    )
    if not passthrough_handled:
        if model_config is not None:
            routed_model = model_config.get("litellm_params", {}).get("model")
            if routed_model:
                data["model"] = routed_model
            else:
                logger.warning(f"No model found in config for model_name: {model_name}")
            data["metadata"]["ccproxy_litellm_model"] = routed_model
            data["metadata"]["ccproxy_model_config"] = model_config
            data["metadata"]["ccproxy_is_passthrough"] = False  # Mark as routed decision
        else:
            # No model config found (not even default)
            # This can happen during startup when LiteLLM proxy is still initializing
            logger.warning(
                f"No model configured for model_name '{model_name}' and no 'default' model available as fallback"
            )

            # Try to reload models in case they weren't loaded properly
            router.reload_models()
            model_config = router.get_model_for_label(model_name)

            if model_config is not None:
                routed_model = model_config.get("litellm_params", {}).get("model")
                if routed_model:
                    data["model"] = routed_model
                data["metadata"]["ccproxy_litellm_model"] = routed_model
                data["metadata"]["ccproxy_model_config"] = model_config
                data["metadata"]["ccproxy_is_passthrough"] = False  # Mark as routed decision
                logger.info(f"Successfully routed after model reload: {model_name} -> {routed_model}")
            else:
                # Final fallback - still no models available, raise error
                raise ValueError(
                    f"No model configured for model_name '{model_name}' and no 'default' model available as fallback"
                )

    return data


def forward_oauth(data: dict[str, Any], user_api_key_dict: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    request = data.get("proxy_server_request")
    if request is None:
        # No proxy server request, skip OAuth forwarding
        return data

    headers = request.get("headers", {})
    user_agent = headers.get("user-agent", "")

    # Check if this is a claude-cli request and the routed model is going to Anthropic provider
    # Forward OAuth token only when the final destination is Anthropic's API directly
    # (not Vertex, Bedrock, or other providers hosting Anthropic models)
    metadata = data.get("metadata", {})
    is_anthropic_provider = False
    # Need to determine the final end destination of the request to
    model_config = metadata.get("ccproxy_model_config", {})
    routed_model = metadata.get("ccproxy_litellm_model", "")
    # Handle case where model_config is None (passthrough mode)
    if model_config is None:
        model_config = {}
    litellm_params = model_config.get("litellm_params", {})

    api_base = litellm_params.get("api_base", "")
    custom_provider = litellm_params.get("custom_llm_provider", "")

    # Check if this is going to Anthropic's API directly
    if api_base:
        try:
            parsed_url = urlparse(api_base)
            hostname = parsed_url.hostname or ""
            is_anthropic_provider = hostname in {"api.anthropic.com", "anthropic.com"}
        except Exception:
            is_anthropic_provider = False
    elif custom_provider == "anthropic":
        is_anthropic_provider = True
    elif (
        not api_base
        and not custom_provider
        and (routed_model.startswith("anthropic/") or routed_model.startswith("claude"))
    ):
        # provider for anthropic/ prefix or claude- prefix is always Anthropic
        is_anthropic_provider = True
    else:
        is_anthropic_provider = False

    # Forward the header iff claude code is the UA, the oauth token is present and the request is going to Anthropic
    if user_agent and "claude-cli" in user_agent and is_anthropic_provider:
        # Get the raw headers containing the OAuth token
        secret_fields = data.get("secret_fields") or {}
        raw_headers = secret_fields.get("raw_headers") or {}
        auth_header = raw_headers.get("authorization", "")

        # Only forward if we have an auth header
        if auth_header:
            # Ensure the provider_specific_header structure exists
            if "provider_specific_header" not in data:
                data["provider_specific_header"] = {}
            if "extra_headers" not in data["provider_specific_header"]:
                data["provider_specific_header"]["extra_headers"] = {}

            # Set the authorization header
            data["provider_specific_header"]["extra_headers"]["authorization"] = auth_header

            # Log OAuth forwarding (without exposing the token)
            logger.info(
                "Forwarding request with Claude Code OAuth authentication",
                extra={
                    "event": "oauth_forwarding",
                    "user_agent": user_agent,
                    "model": routed_model,
                    "auth_present": bool(auth_header),  # Just indicate if auth is present
                },
            )

    return data
