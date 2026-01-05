import logging
import re
import threading
import time
from typing import Any

from litellm.litellm_core_utils.get_llm_provider_logic import get_llm_provider

from ccproxy.classifier import RequestClassifier
from ccproxy.config import get_config
from ccproxy.router import ModelRouter

# Set up structured logging
logger = logging.getLogger(__name__)

# Global storage for request metadata, keyed by litellm_call_id
# Required because LiteLLM doesn't preserve custom metadata from async_pre_call_hook
# to logging callbacks - only internal fields like user_id and hidden_params survive.
_request_metadata_store: dict[str, tuple[dict[str, Any], float]] = {}
_store_lock = threading.Lock()
_STORE_TTL = 60.0  # Clean up entries older than 60 seconds


def store_request_metadata(call_id: str, metadata: dict[str, Any]) -> None:
    """Store metadata for a request by its call ID."""
    with _store_lock:
        _request_metadata_store[call_id] = (metadata, time.time())
        # Clean up old entries
        now = time.time()
        expired = [k for k, (_, ts) in _request_metadata_store.items() if now - ts > _STORE_TTL]
        for k in expired:
            del _request_metadata_store[k]


def get_request_metadata(call_id: str) -> dict[str, Any]:
    """Retrieve metadata for a request by its call ID."""
    with _store_lock:
        entry = _request_metadata_store.get(call_id)
        if entry:
            metadata, _ = entry
            return metadata
        return {}


# Beta headers required for Claude Code impersonation (Claude Max OAuth support)
ANTHROPIC_BETA_HEADERS = [
    "oauth-2025-04-20",
    "claude-code-20250219",
    "interleaved-thinking-2025-05-14",
    "fine-grained-tool-streaming-2025-05-14",
]

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


def extract_session_id(data: dict[str, Any], user_api_key_dict: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Extract session_id from Claude Code's user_id field for LangFuse session tracking.

    Claude Code embeds session info in the metadata.user_id field with format:
    user_{hash}_account_{uuid}_session_{uuid}

    This hook extracts the session_id and sets it on metadata["session_id"] for LangFuse.
    """
    if "metadata" not in data:
        data["metadata"] = {}

    # Get user_id from request body metadata
    request = data.get("proxy_server_request", {})
    body = request.get("body", {})
    if isinstance(body, dict):
        body_metadata = body.get("metadata", {})
        user_id = body_metadata.get("user_id", "")

        if user_id and "_session_" in user_id:
            # Parse: user_{hash}_account_{uuid}_session_{uuid}
            parts = user_id.split("_session_")
            if len(parts) == 2:
                session_id = parts[1]
                data["metadata"]["session_id"] = session_id
                logger.debug(f"Extracted session_id: {session_id}")

                # Also extract user and account for trace_metadata
                prefix = parts[0]
                if "_account_" in prefix:
                    user_account = prefix.split("_account_")
                    if len(user_account) == 2:
                        user_hash = user_account[0].replace("user_", "")
                        account_id = user_account[1]
                        if "trace_metadata" not in data["metadata"]:
                            data["metadata"]["trace_metadata"] = {}
                        data["metadata"]["trace_metadata"]["claude_user_hash"] = user_hash
                        data["metadata"]["trace_metadata"]["claude_account_id"] = account_id

    return data


def capture_headers(data: dict[str, Any], user_api_key_dict: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Capture HTTP headers as LangFuse trace_metadata with sensitive value redaction.

    Headers are added to metadata["trace_metadata"] which flows to LangFuse trace metadata.
    This is the proper mechanism for structured key-value data (tags are for categorization only).

    Args:
        data: Request data from LiteLLM
        user_api_key_dict: User API key dictionary
        **kwargs: Additional keyword arguments including:
            - headers: Optional list of header names to capture (captures all if not specified)
    """
    if "metadata" not in data:
        data["metadata"] = {}
    if "trace_metadata" not in data["metadata"]:
        data["metadata"]["trace_metadata"] = {}

    trace_metadata = data["metadata"]["trace_metadata"]

    # Get optional headers filter from params
    headers_filter: list[str] | None = kwargs.get("headers")

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

    for name, value in all_headers.items():
        if not value:
            continue
        name_lower = name.lower()
        # Filter headers if a filter list is provided
        if headers_filter is not None:
            if name_lower not in [h.lower() for h in headers_filter]:
                continue
        # Add to trace_metadata with header_ prefix
        redacted_value = _redact_value(name, str(value))
        trace_metadata[f"header_{name_lower}"] = redacted_value

    # Add HTTP method and path
    http_method = request.get("method", "")
    if http_method:
        trace_metadata["http_method"] = http_method

    url = request.get("url", "")
    if url:
        from urllib.parse import urlparse

        path = urlparse(url).path
        if path:
            trace_metadata["http_path"] = path

    # Store in global store for retrieval in success callback
    # LiteLLM doesn't preserve custom metadata through its internal flow
    call_id = data.get("litellm_call_id")
    if not call_id:
        import uuid

        call_id = str(uuid.uuid4())
        data["litellm_call_id"] = call_id
    store_request_metadata(call_id, {"trace_metadata": trace_metadata.copy()})

    return data


def forward_oauth(data: dict[str, Any], user_api_key_dict: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Forward OAuth token to provider if configured.

    This hook checks if the request is going to a provider that has an OAuth token
    configured in oat_sources, and if so, forwards that token in the authorization header.
    """
    request = data.get("proxy_server_request")
    if request is None:
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
        logger.warning(f"forward_oauth: No routed_model in metadata, skipping. metadata={metadata}")
        return data

    # Detect provider - try LiteLLM first, then fallback to simple name matching
    provider_name = None
    try:
        _, provider_name, _, _ = get_llm_provider(
            model=routed_model,
            custom_llm_provider=custom_provider,
            api_base=api_base,
        )
    except Exception:
        # Fallback: simple name-based detection
        if "claude" in routed_model.lower():
            provider_name = "anthropic"
        elif "gemini" in routed_model.lower() or "palm" in routed_model.lower():
            provider_name = "gemini"
        elif "gpt" in routed_model.lower():
            provider_name = "openai"

    logger.debug(f"forward_oauth: Detected provider '{provider_name}' for model '{routed_model}'")
    if not provider_name:
        # Cannot determine provider, skip OAuth forwarding
        logger.warning(f"forward_oauth: No provider_name detected for model {routed_model}")
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
        # LiteLLM requires custom_llm_provider when this dict is present
        if "provider_specific_header" not in data:
            data["provider_specific_header"] = {"custom_llm_provider": provider_name}
        elif "custom_llm_provider" not in data["provider_specific_header"]:
            data["provider_specific_header"]["custom_llm_provider"] = provider_name
        if "extra_headers" not in data["provider_specific_header"]:
            data["provider_specific_header"]["extra_headers"] = {}

        # Set the authorization header
        data["provider_specific_header"]["extra_headers"]["authorization"] = auth_header
        # Clear x-api-key when using OAuth Bearer (Anthropic requires empty x-api-key with OAuth)
        data["provider_specific_header"]["extra_headers"]["x-api-key"] = ""

        # Also set api_key for LiteLLM's internal handling
        if auth_header.startswith("Bearer "):
            oauth_token = auth_header[7:]  # Strip "Bearer " prefix
            data["api_key"] = oauth_token
            # LiteLLM's clientside credential handler requires model_group in metadata
            # when api_key is set dynamically (used for deployment ID generation)
            if "metadata" not in data:
                data["metadata"] = {}
            if "model_group" not in data["metadata"]:
                data["metadata"]["model_group"] = data.get("model", "default")

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


def add_beta_headers(data: dict[str, Any], user_api_key_dict: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Add anthropic-beta headers for Claude Code impersonation.

    When routing to Anthropic, adds the required beta headers that allow
    Claude Max OAuth tokens to be accepted by Anthropic's API.
    """
    metadata = data.get("metadata", {})
    routed_model = metadata.get("ccproxy_litellm_model", "")
    model_config = metadata.get("ccproxy_model_config") or {}

    if not routed_model:
        return data

    # Detect provider using same logic as forward_oauth
    litellm_params = model_config.get("litellm_params", {})
    api_base = litellm_params.get("api_base")
    custom_provider = litellm_params.get("custom_llm_provider")

    # Detect provider - try LiteLLM first, then fallback to simple name matching
    provider_name = None
    try:
        _, provider_name, _, _ = get_llm_provider(
            model=routed_model,
            custom_llm_provider=custom_provider,
            api_base=api_base,
        )
    except Exception:
        # Fallback: simple name-based detection
        if "claude" in routed_model.lower():
            provider_name = "anthropic"

    if provider_name != "anthropic":
        return data

    # Build the merged beta headers
    existing = ""
    if "provider_specific_header" in data and "extra_headers" in data["provider_specific_header"]:
        existing = data["provider_specific_header"]["extra_headers"].get("anthropic-beta", "")
    elif "extra_headers" in data:
        existing = data["extra_headers"].get("anthropic-beta", "")
    existing_list = [b.strip() for b in existing.split(",") if b.strip()]
    merged = list(dict.fromkeys(ANTHROPIC_BETA_HEADERS + existing_list))
    merged_str = ",".join(merged)

    # Method 1: provider_specific_header (for proxy router)
    # LiteLLM requires custom_llm_provider when this dict is present
    if "provider_specific_header" not in data:
        data["provider_specific_header"] = {"custom_llm_provider": "anthropic"}
    elif "custom_llm_provider" not in data["provider_specific_header"]:
        data["provider_specific_header"]["custom_llm_provider"] = "anthropic"
    if "extra_headers" not in data["provider_specific_header"]:
        data["provider_specific_header"]["extra_headers"] = {}
    data["provider_specific_header"]["extra_headers"]["anthropic-beta"] = merged_str
    data["provider_specific_header"]["extra_headers"]["anthropic-version"] = "2023-06-01"

    # Method 2: extra_headers (direct to completion call)
    if "extra_headers" not in data:
        data["extra_headers"] = {}
    data["extra_headers"]["anthropic-beta"] = merged_str
    data["extra_headers"]["anthropic-version"] = "2023-06-01"

    logger.info(
        "Added anthropic-beta headers for Claude Code impersonation",
        extra={"event": "beta_headers_added", "model": routed_model},
    )

    return data


# Required system message prefix for Claude Code OAuth tokens
CLAUDE_CODE_SYSTEM_PREFIX = "You are Claude Code, Anthropic's official CLI for Claude."


def inject_claude_code_identity(
    data: dict[str, Any], user_api_key_dict: dict[str, Any], **kwargs: Any
) -> dict[str, Any]:
    """Inject Claude Code identity into system message for OAuth authentication.

    Anthropic's OAuth tokens are restricted to Claude Code. To use them, the API
    request must include a system message that starts with "You are Claude Code".
    This hook prepends that required prefix to the system message when OAuth is detected.
    """
    # Check if this is an OAuth request by looking at the authorization header
    secret_fields = data.get("secret_fields") or {}
    raw_headers = secret_fields.get("raw_headers") or {}
    auth_header = raw_headers.get("authorization", "")

    # Only inject for OAuth Bearer tokens (sk-ant-oat prefix)
    if not auth_header.lower().startswith("bearer sk-ant-oat"):
        return data

    # Detect provider - only inject for Anthropic
    metadata = data.get("metadata", {})
    routed_model = metadata.get("ccproxy_litellm_model", "")

    if not routed_model or "claude" not in routed_model.lower():
        return data

    # Check if system message already contains the required prefix
    messages = data.get("messages", [])

    # Handle system message - can be string or in messages array
    system_msg = data.get("system")
    if system_msg is not None:
        # System is a separate field (Anthropic native format)
        if isinstance(system_msg, str):
            if CLAUDE_CODE_SYSTEM_PREFIX not in system_msg:
                data["system"] = f"{CLAUDE_CODE_SYSTEM_PREFIX}\n\n{system_msg}"
        elif isinstance(system_msg, list):
            # System is array of content blocks
            has_prefix = any(
                isinstance(block, dict) and
                block.get("type") == "text" and
                CLAUDE_CODE_SYSTEM_PREFIX in block.get("text", "")
                for block in system_msg
            )
            if not has_prefix:
                prefix_block = {"type": "text", "text": CLAUDE_CODE_SYSTEM_PREFIX}
                data["system"] = [prefix_block] + system_msg
    else:
        # No system message - add one
        data["system"] = CLAUDE_CODE_SYSTEM_PREFIX

    logger.info(
        "Injected Claude Code identity for OAuth authentication",
        extra={"event": "claude_code_identity_injected", "model": routed_model},
    )

    return data
