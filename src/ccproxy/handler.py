"""ccproxy handler - Main LiteLLM CustomLogger implementation."""

import logging
import os
from typing import Any, TypedDict

from litellm.integrations.custom_logger import CustomLogger
from rich import print, inspect

from ccproxy.classifier import RequestClassifier
from ccproxy.config import get_config
from ccproxy.router import get_router
from ccproxy.utils import calculate_duration_ms

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

    def __init__(self) -> None:
        super().__init__()
        self.classifier = RequestClassifier()
        self.router = get_router()

        config = get_config()
        if config.debug:
            logger.setLevel(logging.DEBUG)

        # Load hooks from configuration
        self.hooks = config.load_hooks()
        if config.debug and self.hooks:
            hook_names = [f"{h.__module__}.{h.__name__}" for h in self.hooks]
            logger.debug(f"Loaded {len(self.hooks)} hooks: {', '.join(hook_names)}")

        # Validate Langfuse configuration
        self._check_langfuse_config()

    async def async_pre_call_hook(
        self,
        data: dict[str, Any],
        user_api_key_dict: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        # Debug: Print thinking parameters if present
        thinking_params = data.get("thinking")
        if thinking_params is not None:
            print(f"🧠 Thinking parameters: {thinking_params}")

        # Run all processors in sequence with error handling
        for hook in self.hooks:
            try:
                data = hook(data, user_api_key_dict, classifier=self.classifier, router=self.router)
            except Exception as e:
                logger.error(
                    f"Hook {hook.__name__} failed with error: {e}",
                    extra={
                        "hook_name": hook.__name__,
                        "error_type": type(e).__name__,
                        "error_message": str(e),
                    },
                    exc_info=True,
                )
                # Continue with other hooks even if one fails
                # The request will proceed with partial processing

        # Log routing decision with structured logging
        metadata = data.get("metadata", {})
        self._log_routing_decision(
            model_name=metadata.get("ccproxy_model_name", None),
            original_model=metadata.get("ccproxy_alias_model", None),
            routed_model=metadata.get("ccproxy_litellm_model", None),
            model_config=metadata.get("ccproxy_model_config"),
            is_passthrough=metadata.get("ccproxy_is_passthrough", False),
        )

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
            def format_model_name(name: str, max_width: int = 60) -> str:
                """Format model name to fit within max width."""
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
