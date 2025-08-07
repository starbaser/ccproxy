import logging
import uuid
from typing import Any

from ccproxy.classifier import RequestClassifier
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

    # Get model_name with safe default
    model_name = data.get("metadata", {}).get("ccproxy_model_name", "default")
    if not model_name:
        logger.warning("No ccproxy_model_name found, using default")
        model_name = "default"

    # Get model for model_name from router (includes fallback to 'default' model_name)
    model_config = router.get_model_for_label(model_name)

    if model_config is not None:
        routed_model = model_config.get("litellm_params", {}).get("model")
        if routed_model:
            data["model"] = routed_model
        else:
            logger.warning(f"No model found in config for model_name: {model_name}")
        data["metadata"]["ccproxy_litellm_model"] = routed_model
        data["metadata"]["ccproxy_model_config"] = model_config
    else:
        # No model config found (not even default)
        # This should only happen if no 'default' model is configured
        raise ValueError(
            f"No model configured for model_name '{model_name}' and no 'default' model available as fallback"
        )

    # Generate request ID if not present
    if "request_id" not in data["metadata"]:
        data["metadata"]["request_id"] = str(uuid.uuid4())
    return data


def forward_oauth_hook(data: dict[str, Any], user_api_key_dict: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
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
    routed_model = metadata.get("ccproxy_litellm_model", "")
    model_config = metadata.get("ccproxy_model_config", {})
    litellm_params = model_config.get("litellm_params", {})

    api_base = litellm_params.get("api_base", "")
    custom_provider = litellm_params.get("custom_llm_provider", "")

    # Check if this is going to Anthropic's API directly
    from urllib.parse import urlparse

    # Parse hostname properly to prevent subdomain attacks
    if api_base:
        try:
            parsed_url = urlparse(api_base)
            hostname = parsed_url.hostname or ""
            # Check for exact domain match
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
        # Default provider for anthropic/ prefix or claude models is Anthropic
        is_anthropic_provider = True
    else:
        is_anthropic_provider = False

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
                    "request_id": data["metadata"].get("request_id", None),
                    "auth_present": bool(auth_header),  # Just indicate if auth is present
                },
            )

    return data
