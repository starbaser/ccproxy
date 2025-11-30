import logging
import re
from typing import Any

from litellm.litellm_core_utils.get_llm_provider_logic import get_llm_provider

from ccproxy.classifier import RequestClassifier
from ccproxy.config import get_config
from ccproxy.router import ModelRouter

# Set up structured logging
logger = logging.getLogger(__name__)

# Headers containing secrets - redact but show prefix/suffix for identification
SENSITIVE_PATTERNS = {
    "authorization": r"^(Bearer sk-[a-z]+-|Bearer |sk-[a-z]+-)",  # Keep "Bearer sk-ant-" or "Bearer " or "sk-ant-"
    "x-api-key": r"^(sk-[a-z]+-)",
    "cookie": None,  # Fully redact
}


def _redact_value(header: str, value: str) -> str:
    """Redact sensitive header values, keeping prefix and last 4 chars."""
    header_lower = header.lower()
    if header_lower in SENSITIVE_PATTERNS:
        pattern = SENSITIVE_PATTERNS[header_lower]
        if pattern is None:
            return "[REDACTED]"
        match = re.match(pattern, value)
        prefix = match.group(0) if match else ""
        suffix = value[-4:] if len(value) > 8 else ""
        return f"{prefix}...{suffix}"
    return str(value)[:200]


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


def capture_headers(data: dict[str, Any], user_api_key_dict: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Capture all HTTP headers for Langfuse with sensitive value redaction."""
    if "metadata" not in data:
        data["metadata"] = {}

    request = data.get("proxy_server_request", {})
    headers = request.get("headers", {})

    # Also get raw headers for auth info
    secret_fields = data.get("secret_fields")
    if secret_fields and hasattr(secret_fields, "raw_headers"):
        raw_headers = secret_fields.raw_headers or {}
    else:
        raw_headers = {}

    # Merge headers (raw has auth, cleaned has rest)
    all_headers = {**headers, **raw_headers}

    captured = {}
    for name, value in all_headers.items():
        if value:
            captured[name.lower()] = _redact_value(name, str(value))

    data["metadata"]["http_headers"] = captured
    data["metadata"]["http_method"] = request.get("method", "")

    url = request.get("url", "")
    if url:
        from urllib.parse import urlparse

        data["metadata"]["http_path"] = urlparse(url).path

    return data


def forward_oauth(data: dict[str, Any], user_api_key_dict: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Forward OAuth token to provider if configured.

    This hook checks if the request is going to a provider that has an OAuth token
    configured in oat_sources, and if so, forwards that token in the authorization header.
    """
    request = data.get("proxy_server_request")
    if request is None:
        # No proxy server request, skip OAuth forwarding
        return data

    headers = request.get("headers", {})
    user_agent = headers.get("user-agent", "")

    # Determine which provider this request is going to
    metadata = data.get("metadata", {})
    model_config = metadata.get("ccproxy_model_config", {})
    routed_model = metadata.get("ccproxy_litellm_model", "")

    # Handle case where model_config is None (passthrough mode)
    if model_config is None:
        model_config = {}

    litellm_params = model_config.get("litellm_params", {})
    api_base = litellm_params.get("api_base")
    custom_provider = litellm_params.get("custom_llm_provider")

    # Get the raw headers to check if auth is already present in the request
    secret_fields = data.get("secret_fields") or {}
    raw_headers = secret_fields.get("raw_headers") or {}
    auth_header = raw_headers.get("authorization", "")

    # If no routed model, skip OAuth forwarding
    # We only forward OAuth when we know the target model/provider from routing
    if not routed_model:
        return data

    # Use LiteLLM's official provider detection
    # Returns: (model, custom_llm_provider, dynamic_api_key, api_base)
    try:
        _, provider_name, _, _ = get_llm_provider(
            model=routed_model,
            custom_llm_provider=custom_provider,
            api_base=api_base,
        )
    except Exception as e:
        # If provider detection fails, skip OAuth forwarding
        logger.debug(f"Could not determine provider for model {routed_model}: {e}")
        return data

    if not provider_name:
        # Cannot determine provider, skip OAuth forwarding
        return data

    # If no auth header found in request, try to use cached OAuth token as fallback
    if not auth_header:
        config = get_config()
        oauth_token = config.get_oauth_token(provider_name)

        if oauth_token:
            logger.debug(f"No authorization header found, using cached OAuth token for provider '{provider_name}'")
            # Format as Bearer token if not already formatted
            if not oauth_token.startswith("Bearer "):
                auth_header = f"Bearer {oauth_token}"
            else:
                auth_header = oauth_token
        else:
            # No auth header in request and no cached OAuth token
            return data

    # Only forward if we have an auth header
    if auth_header:
        # Ensure the provider_specific_header structure exists
        if "provider_specific_header" not in data:
            data["provider_specific_header"] = {}
        if "extra_headers" not in data["provider_specific_header"]:
            data["provider_specific_header"]["extra_headers"] = {}

        # Set the authorization header
        data["provider_specific_header"]["extra_headers"]["authorization"] = auth_header

        # Set custom User-Agent if configured for this provider
        config = get_config()
        custom_user_agent = config.get_oauth_user_agent(provider_name)
        if custom_user_agent:
            data["provider_specific_header"]["extra_headers"]["user-agent"] = custom_user_agent
            logger.debug(f"Setting custom User-Agent for provider '{provider_name}': {custom_user_agent}")

        # Log OAuth forwarding (without exposing the token)
        # Check if this is from Claude CLI for backwards-compatible logging
        is_claude_cli = user_agent and "claude-cli" in user_agent
        log_msg = (
            "Forwarding request with Claude Code OAuth authentication"
            if is_claude_cli
            else f"Forwarding request with OAuth authentication for provider '{provider_name}'"
        )

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

    return data


def forward_apikey(data: dict[str, Any], user_api_key_dict: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Forward x-api-key header from incoming request to proxied request.

    This hook simply forwards the x-api-key header if it exists in the incoming request.

    Args:
        data: Request data from LiteLLM
        user_api_key_dict: User API key dictionary
        **kwargs: Additional keyword arguments

    Returns:
        Modified request data with x-api-key header forwarded (if present)
    """
    request = data.get("proxy_server_request")
    if request is None:
        # No proxy server request, skip API key forwarding
        return data

    # Get the x-api-key from incoming request headers
    secret_fields = data.get("secret_fields") or {}
    raw_headers = secret_fields.get("raw_headers") or {}
    api_key = raw_headers.get("x-api-key", "")

    # Only forward if we have an API key
    if api_key:
        # Ensure the provider_specific_header structure exists
        if "provider_specific_header" not in data:
            data["provider_specific_header"] = {}
        if "extra_headers" not in data["provider_specific_header"]:
            data["provider_specific_header"]["extra_headers"] = {}

        # Set the x-api-key header
        data["provider_specific_header"]["extra_headers"]["x-api-key"] = api_key

        # Log API key forwarding (without exposing the key)
        logger.info(
            "Forwarding request with x-api-key header",
            extra={
                "event": "apikey_forwarding",
                "api_key_present": True,
            },
        )

    return data
