"""ccproxy handler - Main LiteLLM CustomLogger implementation."""

import asyncio
import logging
from datetime import datetime
from typing import Any, TypedDict

import litellm
from fastapi import HTTPException
from litellm.integrations.custom_logger import CustomLogger
from litellm.litellm_core_utils.get_llm_provider_logic import get_llm_provider
from rich import print

from ccproxy.classifier import RequestClassifier
from ccproxy.config import get_config

# Pipeline imports (new architecture)
from ccproxy.pipeline import PipelineExecutor
from ccproxy.pipeline.hook import get_registry
from ccproxy.router import get_router
from ccproxy.utils import calculate_duration_ms

# Check interval for TTL-based refresh (30 minutes)
_OAUTH_REFRESH_CHECK_INTERVAL = 1800

# Maximum retry attempts for 401 errors
_MAX_401_RETRY_ATTEMPTS = 1

# Set up structured logging
logger = logging.getLogger(__name__)


class RequestData(TypedDict, total=False):
    """Type definition for LiteLLM request data."""

    model: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None
    metadata: dict[str, Any] | None


class CCProxyHandler(CustomLogger):
    """Main module of ccproxy, an instance of CCProxyHandler is instantiated in the LiteLLM callback python script"""

    _last_status: dict[str, Any] | None = None  # Class-level state
    _oauth_refresh_task: asyncio.Task | None = None  # Background refresh task

    def __init__(self) -> None:
        super().__init__()
        self.classifier = RequestClassifier()
        self.router = get_router()
        self._langfuse_client = None
        self._pipeline: PipelineExecutor | None = None

        config = get_config()
        if config.debug:
            logger.setLevel(logging.DEBUG)

        # Initialize pipeline executor with DAG-based hook ordering
        self._init_pipeline()

        # Register custom routes with LiteLLM proxy (for statusline integration)
        self._register_routes()

    _routes_registered: bool = False  # Class-level flag to prevent duplicate registration

    def _init_pipeline(self) -> None:
        """Initialize the pipeline executor with registered hooks.

        Imports and registers all pipeline hooks, then creates the executor
        with DAG-based dependency ordering.
        """
        # Import pipeline hooks to register them with the global registry
        # These imports have side effects (hook registration)
        from ccproxy.pipeline.hooks import (  # noqa: F401
            add_beta_headers,
            capture_headers,
            extract_session_id,
            forward_oauth,
            inject_claude_code_identity,
            model_router,
            rule_evaluator,
        )

        # Get registered hooks from registry
        registry = get_registry()
        all_specs = registry.get_all_specs()

        if not all_specs:
            logger.warning("No hooks registered in pipeline registry")
            return

        # Build list of HookSpec in registration order
        # (DAG will reorder based on dependencies)
        hook_specs = list(all_specs.values())

        # Create executor with classifier and router as extra params
        self._pipeline = PipelineExecutor(
            hooks=hook_specs,
            extra_params={
                "classifier": self.classifier,
                "router": self.router,
            },
        )

        config = get_config()
        if config.debug:
            logger.debug(
                "Pipeline initialized with %d hooks: %s",
                len(hook_specs),
                " → ".join(self._pipeline.get_execution_order()),
            )

    def _register_routes(self) -> None:
        """Register custom routes with LiteLLM proxy for statusline integration."""
        if CCProxyHandler._routes_registered:
            return

        try:
            from litellm.proxy.proxy_server import app

            from ccproxy.routes import router as ccproxy_router

            # Check if router already registered (by checking for our endpoint)
            existing_routes = [r.path for r in app.routes]
            if "/ccproxy/status" not in existing_routes:
                app.include_router(ccproxy_router)
                logger.debug("Registered ccproxy custom routes")

            CCProxyHandler._routes_registered = True
        except ImportError:
            logger.debug("LiteLLM proxy server not available for route registration")
        except Exception as e:
            logger.debug(f"Could not register custom routes: {e}")

    @property
    def langfuse(self):
        """Lazy-loaded Langfuse client."""
        if self._langfuse_client is None:
            try:
                from langfuse import Langfuse

                self._langfuse_client = Langfuse()
            except Exception:
                pass
        return self._langfuse_client

    @classmethod
    def get_status(cls) -> dict[str, Any] | None:
        """Get the last routing status for statusline widget."""
        return cls._last_status

    def _is_auth_error(self, response_obj: Any) -> bool:
        """Check if response indicates authentication failure (401).

        Args:
            response_obj: LiteLLM response/error object

        Returns:
            True if response indicates a 401 authentication error
        """
        if hasattr(response_obj, "status_code") and response_obj.status_code == 401:
            return True
        if hasattr(response_obj, "message"):
            msg = str(response_obj.message).lower()
            return "401" in msg or "unauthorized" in msg or "authentication" in msg
        return False

    def _is_auth_exception(self, exception: Exception) -> bool:
        """Check if exception indicates authentication failure (401).

        Args:
            exception: The exception to check

        Returns:
            True if exception indicates a 401 authentication error
        """
        # Check for LiteLLM AuthenticationError
        if isinstance(exception, litellm.AuthenticationError):
            return True

        # Check status_code attribute
        if hasattr(exception, "status_code") and exception.status_code == 401:
            return True

        # Check exception message
        exc_str = str(exception).lower()
        return "401" in exc_str or "unauthorized" in exc_str or "authentication" in exc_str

    def _extract_provider_from_metadata(self, kwargs: dict) -> str | None:
        """Extract provider name from request metadata.

        Args:
            kwargs: Request kwargs containing metadata

        Returns:
            Provider name (e.g., "anthropic", "openai") or None if not determinable
        """
        metadata = kwargs.get("metadata", {})
        model = metadata.get("ccproxy_litellm_model", "") or kwargs.get("model", "")
        model_lower = model.lower()
        if "claude" in model_lower or "anthropic" in model_lower:
            return "anthropic"
        if "gpt" in model_lower or "openai" in model_lower:
            return "openai"
        if "gemini" in model_lower or "google" in model_lower:
            return "gemini"
        return None

    def _extract_provider_from_request_data(self, request_data: dict) -> str | None:
        """Extract provider name from request data (used in failure hooks).

        Uses multiple strategies to determine the provider:
        1. Check ccproxy metadata for model config with api_base
        2. Check model name in request_data
        3. Use LiteLLM's provider detection

        Args:
            request_data: Request data dict from failure hook

        Returns:
            Provider name (e.g., "anthropic", "openai") or None if not determinable
        """
        config = get_config()
        metadata = request_data.get("metadata", {})

        # Strategy 1: Check ccproxy model config for api_base
        model_config = metadata.get("ccproxy_model_config", {})
        if model_config:
            litellm_params = model_config.get("litellm_params", {})
            api_base = litellm_params.get("api_base")
            if api_base:
                # Check destination-based matching
                dest_provider = config.get_provider_for_destination(api_base)
                if dest_provider:
                    return dest_provider

        # Strategy 2: Get model name
        model = metadata.get("ccproxy_litellm_model") or request_data.get("model", "")
        if not model:
            return None

        # Strategy 3: Try LiteLLM provider detection
        try:
            _, provider_name, _, _ = get_llm_provider(model=model)
            if provider_name:
                return provider_name
        except Exception:
            pass

        # Strategy 4: Fallback to model name-based detection
        model_lower = model.lower()
        if "claude" in model_lower or "anthropic" in model_lower:
            return "anthropic"
        if "gpt" in model_lower or "openai" in model_lower:
            return "openai"
        if "gemini" in model_lower or "google" in model_lower:
            return "gemini"

        return None

    async def _start_oauth_refresh_task(self) -> None:
        """Start background task for TTL-based token refresh if not already running."""
        if CCProxyHandler._oauth_refresh_task is not None and not CCProxyHandler._oauth_refresh_task.done():
            return
        CCProxyHandler._oauth_refresh_task = asyncio.create_task(self._oauth_refresh_loop())
        logger.debug("Started OAuth background refresh task")

    async def _oauth_refresh_loop(self) -> None:
        """Background loop to refresh OAuth tokens before expiration."""
        while True:
            try:
                await asyncio.sleep(_OAUTH_REFRESH_CHECK_INTERVAL)
                config = get_config()
                for provider in config.oat_sources:
                    if config.is_token_expired(provider):
                        new_token = config.refresh_oauth_token(provider)
                        if new_token:
                            logger.info(f"TTL refresh: renewed OAuth token for {provider}")
                        else:
                            logger.warning(f"TTL refresh: failed to renew OAuth token for {provider}")
            except asyncio.CancelledError:
                logger.debug("OAuth refresh loop cancelled")
                break
            except Exception as e:
                logger.warning(f"Error in OAuth refresh loop: {e}")

    async def async_pre_call_hook(
        self,
        data: dict[str, Any],
        user_api_key_dict: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        # Start background OAuth refresh task if not already running
        await self._start_oauth_refresh_task()

        # Skip custom routing for LiteLLM internal health checks
        # Health checks need to validate actual configured models, not routed ones
        metadata = data.get("metadata", {})
        tags = metadata.get("tags", [])
        if "litellm-internal-health-check" in tags:
            logger.debug("Skipping hooks for health check request")
            return data

        # Debug: Print thinking parameters if present
        thinking_params = data.get("thinking")
        if thinking_params is not None:
            print(f"🧠 Thinking parameters: {thinking_params}")

        # Debug: Log cache_control in system messages
        config = get_config()
        if config.debug:
            print(f"[CACHE DEBUG] REQUEST DATA KEYS: {list(data.keys())}")
            # Check messages
            messages = data.get("messages", [])
            print(f"[CACHE DEBUG] Messages count: {len(messages)}")
            for i, msg in enumerate(messages[:2]):  # First 2 messages
                if isinstance(msg, dict):
                    print(f"[CACHE DEBUG] Message {i}: role={msg.get('role')}, content_type={type(msg.get('content'))}")
                    content = msg.get("content", [])
                    if isinstance(content, list):
                        for j, block in enumerate(content[:2]):
                            if isinstance(block, dict):
                                print(f"[CACHE DEBUG]   Block {j} keys: {list(block.keys())}")
            # Check top-level system field
            top_system = data.get("system", [])
            if top_system:
                print(f"[CACHE DEBUG] Top-level system present: {len(top_system)} blocks")
                for i, block in enumerate(top_system[:2]):
                    if isinstance(block, dict):
                        print(f"[CACHE DEBUG]   System block {i} keys: {list(block.keys())}")
                        if "cache_control" in block:
                            print(f"[CACHE DEBUG]   cache_control: {block['cache_control']}")

        # Run hooks through pipeline with DAG-ordered execution
        if self._pipeline is not None:
            data = self._pipeline.execute(data, user_api_key_dict)
        else:
            logger.error("Pipeline not initialized - hooks will not be executed")

        # Log routing decision with structured logging
        metadata = data.get("metadata", {})
        self._log_routing_decision(
            model_name=metadata.get("ccproxy_model_name", None),
            original_model=metadata.get("ccproxy_alias_model", None),
            routed_model=metadata.get("ccproxy_litellm_model", None),
            model_config=metadata.get("ccproxy_model_config"),
            is_passthrough=metadata.get("ccproxy_is_passthrough", False),
        )

        # Update status for statusline widget
        CCProxyHandler._last_status = {
            "rule": metadata.get("ccproxy_model_name"),
            "model": metadata.get("ccproxy_litellm_model") or data.get("model"),
            "original_model": metadata.get("ccproxy_alias_model"),
            "is_passthrough": metadata.get("ccproxy_is_passthrough", False),
            "timestamp": datetime.now().isoformat(),
        }

        return data

    def _log_routing_decision(
        self,
        model_name: str,
        original_model: str,
        routed_model: str,
        model_config: dict[str, Any] | None,
        is_passthrough: bool = False,
    ) -> None:
        """Log routing decision with structured logging.

        Args:
            model_name: Classification model_name
            original_model: Original model requested
            routed_model: Model after routing
            model_config: Model configuration from router (None if fallback or passthrough)
            is_passthrough: Whether this was a passthrough decision (no rule applied + passthrough enabled)
        """
        # Get config to check debug mode
        config = get_config()

        # Only display colored routing decision when debug is enabled
        if config.debug:
            from rich.console import Console
            from rich.panel import Panel
            from rich.text import Text

            # Create console with 80 char width limit
            console = Console(width=80)

            # Color scheme based on routing
            if is_passthrough:
                # Passthrough (no rule applied, passthrough enabled) - dim
                color = "dim"
                routing_type = "PASSTHROUGH"
            elif original_model == routed_model:
                # No change but rule was applied - blue
                color = "blue"
                routing_type = "NO CHANGE"
            else:
                # Routed - green
                color = "green"
                routing_type = "ROUTED"

            # Helper function to truncate and wrap long model names
            def format_model_name(name: str | None, max_width: int = 60) -> str:
                """Format model name to fit within max width."""
                if name is None:
                    return "<none>"
                if len(name) <= max_width:
                    return name
                # Truncate with ellipsis
                return name[: max_width - 3] + "..."

            # Create the routing message
            routing_text = Text()
            routing_text.append("[ccproxy] Request Routed\n", style="bold cyan")
            routing_text.append("├─ Type: ", style="dim")
            routing_text.append(f"{routing_type}\n", style=f"bold {color}")
            routing_text.append("├─ Model Name: ", style="dim")
            routing_text.append(f"{format_model_name(model_name)}\n", style="magenta")
            routing_text.append("├─ Original: ", style="dim")
            routing_text.append(f"{format_model_name(original_model)}\n", style="blue")
            routing_text.append("└─ Routed to: ", style="dim")
            routing_text.append(f"{format_model_name(routed_model)}", style=f"bold {color}")

            # Print the panel with width constraint
            console.print(Panel(routing_text, border_style=color, padding=(0, 1), width=78))

        log_data = {
            "event": "ccproxy_routing",
            "model_name": model_name,
            "original_model": original_model,
            "routed_model": routed_model,
            "is_passthrough": is_passthrough,
        }

        # Add model info if available (excluding sensitive data)
        if model_config and "model_info" in model_config:
            model_info = model_config["model_info"]
            # Only include non-sensitive metadata
            safe_info = {}
            for key, value in model_info.items():
                if key not in ("api_key", "secret", "token", "password"):
                    safe_info[key] = value

            if safe_info:
                log_data["model_info"] = safe_info

        logger.info("ccproxy routing decision", extra=log_data)

    async def async_log_success_event(
        self,
        kwargs: dict[str, Any],
        response_obj: Any,
        start_time: float,
        end_time: float,
    ) -> None:
        """Log successful completion of a request.

        Args:
            kwargs: Request arguments
            response_obj: LiteLLM response object
            start_time: Request start timestamp
            end_time: Request completion timestamp
        """
        # Retrieve stored metadata and update Langfuse trace
        from ccproxy.hooks import get_request_metadata

        call_id = kwargs.get("litellm_call_id")
        litellm_params = kwargs.get("litellm_params", {})
        if not call_id:
            call_id = litellm_params.get("litellm_call_id")
        stored = get_request_metadata(call_id) if call_id else {}

        if stored and self.langfuse:
            standard_logging_obj = kwargs.get("standard_logging_object")
            if standard_logging_obj:
                trace_id = standard_logging_obj.get("trace_id")
                if trace_id:
                    try:
                        # Update trace with stored metadata
                        trace_metadata = stored.get("trace_metadata", {})
                        if trace_metadata:
                            self.langfuse.trace(id=trace_id, metadata=trace_metadata)
                            self.langfuse.flush()
                    except Exception as e:
                        logger.debug(f"Failed to update Langfuse trace: {e}")

        metadata = kwargs.get("metadata", {})
        model_name = metadata.get("ccproxy_model_name", "unknown")

        # Calculate duration using utility function
        duration_ms = calculate_duration_ms(start_time, end_time)

        log_data = {
            "event": "ccproxy_success",
            "model_name": model_name,
            "duration_ms": round(duration_ms, 2),
            "model": kwargs.get("model", "unknown"),
        }

        # Add usage stats if available (non-sensitive)
        if hasattr(response_obj, "usage") and response_obj.usage:
            usage = response_obj.usage
            log_data["usage"] = {
                "input_tokens": getattr(usage, "prompt_tokens", 0),
                "output_tokens": getattr(usage, "completion_tokens", 0),
                "total_tokens": getattr(usage, "total_tokens", 0),
            }

        logger.info("ccproxy request completed", extra=log_data)

    async def async_log_failure_event(
        self,
        kwargs: dict[str, Any],
        response_obj: Any,
        start_time: float,
        end_time: float,
    ) -> None:
        """Log failed request.

        Args:
            kwargs: Request arguments
            response_obj: LiteLLM response object (error)
            start_time: Request start timestamp
            end_time: Request completion timestamp
        """
        metadata = kwargs.get("metadata", {})
        model_name = metadata.get("ccproxy_model_name", "unknown")

        # Calculate duration using utility function
        duration_ms = calculate_duration_ms(start_time, end_time)

        log_data = {
            "event": "ccproxy_failure",
            "model_name": model_name,
            "duration_ms": round(duration_ms, 2),
            "model": kwargs.get("model", "unknown"),
            "error_type": type(response_obj).__name__,
        }

        # Add error message if available
        if hasattr(response_obj, "message"):
            error_message = str(response_obj.message)
            log_data["error_message"] = error_message[:500]  # Truncate long messages

        logger.error("ccproxy request failed", extra=log_data)

        # Trigger OAuth token refresh on 401 authentication errors
        if self._is_auth_error(response_obj):
            provider = self._extract_provider_from_metadata(kwargs)
            if provider:
                config = get_config()
                if provider in config.oat_sources:
                    new_token = config.refresh_oauth_token(provider)
                    if new_token:
                        logger.info(f"401 refresh: renewed OAuth token for {provider}")
                    else:
                        logger.warning(f"401 refresh: failed to renew OAuth token for {provider}")

    async def async_log_stream_event(
        self,
        kwargs: dict[str, Any],
        response_obj: Any,
        start_time: float,
        end_time: float,
    ) -> None:
        """Log streaming request completion.

        Args:
            kwargs: Request arguments
            response_obj: LiteLLM streaming response object
            start_time: Request start timestamp
            end_time: Request completion timestamp
        """
        metadata = kwargs.get("metadata", {})
        model_name = metadata.get("ccproxy_model_name", "unknown")

        # Calculate duration using utility function
        duration_ms = calculate_duration_ms(start_time, end_time)

        log_data = {
            "event": "ccproxy_stream_complete",
            "model_name": model_name,
            "duration_ms": round(duration_ms, 2),
            "model": kwargs.get("model", "unknown"),
            "streaming": True,
        }

        logger.info("ccproxy streaming request completed", extra=log_data)

    async def async_post_call_failure_hook(
        self,
        request_data: dict,
        original_exception: Exception,
        user_api_key_dict: Any,
        traceback_str: str | None = None,
    ) -> HTTPException | None:
        """Handle failed API calls with OAuth token refresh and retry.

        When a 401 authentication error occurs and OAuth is configured for the
        provider, this hook:
        1. Refreshes the OAuth token
        2. Retries the request with the new token via litellm.acompletion
        3. If successful, raises a special exception containing the response
           (LiteLLM will handle this appropriately)

        Args:
            request_data: Original request data dict
            original_exception: The exception that caused the failure
            user_api_key_dict: User API key authentication info
            traceback_str: Optional traceback string

        Returns:
            HTTPException to replace the original error, or None to use original
        """
        # Only handle 401 authentication errors
        if not self._is_auth_exception(original_exception):
            return None

        # Check if we've already retried (prevent infinite loops)
        metadata = request_data.get("metadata", {})
        retry_count = metadata.get("_ccproxy_401_retry_count", 0)
        if retry_count >= _MAX_401_RETRY_ATTEMPTS:
            logger.warning(
                "401 retry: Max retry attempts (%d) reached, not retrying",
                _MAX_401_RETRY_ATTEMPTS,
            )
            return None

        # Determine provider
        provider = self._extract_provider_from_request_data(request_data)
        if not provider:
            logger.debug("401 retry: Could not determine provider from request data")
            return None

        # Check if OAuth is configured for this provider
        config = get_config()
        if provider not in config.oat_sources:
            logger.debug("401 retry: No OAuth configured for provider '%s'", provider)
            return None

        # Refresh the OAuth token
        new_token = config.refresh_oauth_token(provider)
        if not new_token:
            logger.warning("401 retry: Failed to refresh OAuth token for provider '%s'", provider)
            return None

        logger.info(
            "401 retry: Refreshed OAuth token for provider '%s', attempting retry",
            provider,
            extra={
                "event": "oauth_401_retry",
                "provider": provider,
                "retry_count": retry_count + 1,
            },
        )

        # Prepare retry request data
        retry_data = request_data.copy()
        retry_metadata = retry_data.get("metadata", {}).copy()
        retry_metadata["_ccproxy_401_retry_count"] = retry_count + 1
        retry_data["metadata"] = retry_metadata

        # Inject the new OAuth token
        # We need to set it in a way that the hooks will pick it up
        if "proxy_server_request" not in retry_data:
            retry_data["proxy_server_request"] = {}
        if "headers" not in retry_data["proxy_server_request"]:
            retry_data["proxy_server_request"]["headers"] = {}

        # Set authorization header with new token
        retry_data["proxy_server_request"]["headers"]["authorization"] = f"Bearer {new_token}"

        try:
            # Make the retry call
            model = retry_data.get("model", "")
            messages = retry_data.get("messages", [])

            # Build kwargs for acompletion
            completion_kwargs: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "metadata": retry_metadata,
            }

            # Copy over other relevant parameters
            for key in ["temperature", "max_tokens", "stream", "tools", "tool_choice", "thinking"]:
                if key in retry_data:
                    completion_kwargs[key] = retry_data[key]

            # Add OAuth token via extra headers
            completion_kwargs["extra_headers"] = {
                "authorization": f"Bearer {new_token}",
                "x-api-key": "",  # Clear x-api-key for OAuth
            }

            logger.debug("401 retry: Calling litellm.acompletion with refreshed token")
            response = await litellm.acompletion(**completion_kwargs)

            logger.info(
                "401 retry: Request succeeded after OAuth token refresh",
                extra={
                    "event": "oauth_401_retry_success",
                    "provider": provider,
                    "model": model,
                },
            )

            # Convert response to JSON-serializable dict
            # LiteLLM ModelResponse has a model_dump() method
            if hasattr(response, "model_dump"):
                response_dict = response.model_dump()
            elif hasattr(response, "dict"):
                response_dict = response.dict()
            else:
                response_dict = dict(response) if hasattr(response, "__iter__") else {"response": str(response)}

        except Exception as retry_error:
            logger.warning(
                "401 retry: Retry attempt failed: %s",
                str(retry_error),
                extra={
                    "event": "oauth_401_retry_failed",
                    "provider": provider,
                    "error": str(retry_error),
                },
            )
            # Return None to let the original exception propagate
            return None

        # Retry succeeded - return successful response via HTTPException mechanism
        # This is a workaround since async_post_call_failure_hook can only
        # return HTTPException or None. We return an HTTPException with 200 status
        # which LiteLLM's proxy will send to the client as a successful response.
        #
        # NOTE: This approach may not work with all LiteLLM versions as it
        # depends on how the proxy handles HTTPExceptions with 2xx status codes.
        # If it doesn't work, the token is still refreshed and subsequent
        # requests will succeed.
        return HTTPException(
            status_code=200,
            detail=response_dict,
        )
