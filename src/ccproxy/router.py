"""Model routing component for mapping classification labels to models."""

import logging
import threading
from typing import Any, cast

logger = logging.getLogger(__name__)


class ModelRouter:
    """Routes classification labels to model configurations.

    Maps classification labels (e.g., 'default', 'background', 'think') to specific
    model configurations defined in the LiteLLM proxy YAML config. Models are lazy-loaded
    on first request. All public methods are thread-safe.
    """

    def __init__(self) -> None:
        """Initialize the model router."""
        self._lock = threading.RLock()
        self._model_map: dict[str, dict[str, Any]] = {}
        self._model_list: list[dict[str, Any]] = []
        self._model_group_alias: dict[str, list[str]] = {}
        self._available_models: set[str] = set()
        self._models_loaded = False

    def _ensure_models_loaded(self) -> None:
        """Ensure models are loaded on first request when proxy is ready."""
        if self._models_loaded:
            return

        with self._lock:
            # Double-check pattern
            if self._models_loaded:
                return

            self._load_model_mapping()

            # Mark as loaded regardless of success - models should be available by now
            # If no models are found, it's likely a configuration issue
            self._models_loaded = True

            if self._available_models:
                logger.info(
                    f"Successfully loaded {len(self._available_models)} models: {sorted(self._available_models)}"
                )
            else:
                logger.error("No models were loaded from LiteLLM proxy - check configuration")

    def _load_model_mapping(self) -> None:
        """Load and parse model mapping from LiteLLM proxy config."""
        with self._lock:
            self._model_map.clear()
            self._model_list.clear()
            self._model_group_alias.clear()
            self._available_models.clear()

            from litellm.proxy import proxy_server

            if proxy_server and hasattr(proxy_server, "llm_router") and proxy_server.llm_router:
                model_list = cast(list[dict[str, Any]], proxy_server.llm_router.get_model_list() or [])
                logger.debug(f"Loaded {len(model_list)} models from LiteLLM proxy server")
            else:
                model_list = []
                logger.warning("LiteLLM proxy server or llm_router not available - no models loaded")

            for model_entry in model_list:
                model_name = model_entry.get("model_name")
                if not model_name:
                    continue

                self._model_list.append(model_entry.copy())
                self._available_models.add(model_name)
                self._model_map[model_name] = model_entry.copy()

                litellm_params = model_entry.get("litellm_params", {})
                if isinstance(litellm_params, dict):
                    underlying_model = litellm_params.get("model")
                    if underlying_model:
                        if underlying_model not in self._model_group_alias:
                            self._model_group_alias[underlying_model] = []
                        self._model_group_alias[underlying_model].append(model_name)

    def get_model_for_label(self, model_name: str) -> dict[str, Any] | None:
        """Get model configuration for a classification label, falling back to 'default'."""
        self._ensure_models_loaded()

        model_name_str = model_name

        with self._lock:
            model = self._model_map.get(model_name_str)
            if model is not None:
                return model
            return self._model_map.get("default")

    def get_model_list(self) -> list[dict[str, Any]]:
        """Get the complete list of available model configurations."""
        self._ensure_models_loaded()

        with self._lock:
            return self._model_list.copy()

    @property
    def model_list(self) -> list[dict[str, Any]]:
        """Property access to model list for LiteLLM compatibility."""
        return self.get_model_list()

    @property
    def model_group_alias(self) -> dict[str, list[str]]:
        """Get model group aliases (underlying model name -> list of alias names)."""
        self._ensure_models_loaded()

        with self._lock:
            return self._model_group_alias.copy()

    def get_available_models(self) -> list[str]:
        """Get sorted list of available model alias names."""
        self._ensure_models_loaded()

        with self._lock:
            return sorted(self._available_models)

    def is_model_available(self, model_name: str) -> bool:
        """Check if a model alias is available in the configuration."""
        self._ensure_models_loaded()

        with self._lock:
            return model_name in self._available_models

    def reload_models(self) -> None:
        """Force reload model configuration from LiteLLM proxy.

        This can be used to refresh model configuration if it changes
        during runtime.
        """
        with self._lock:
            self._models_loaded = False
            self._ensure_models_loaded()


# Global router instance
_router_instance: ModelRouter | None = None


def get_router() -> ModelRouter:
    """Get the global ModelRouter instance."""
    global _router_instance

    if _router_instance is None:
        _router_instance = ModelRouter()

    return _router_instance


def clear_router() -> None:
    """Clear the global router instance (for testing)."""
    global _router_instance
    _router_instance = None
