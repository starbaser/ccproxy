"""Duck-type stub for litellm's Logging class.

BaseConfig.transform_response() takes a ``logging_obj`` typed as ``Any``.
The only method called is ``post_call()``.
"""

from __future__ import annotations

from typing import Any


class NoopLogging:
    model_call_details: dict[str, Any]
    """Stub for LiteLLM's model call tracking dict."""

    optional_params: dict[str, Any]
    """Optional params forwarded to response iterators."""

    def __init__(self, optional_params: dict[str, Any] | None = None) -> None:
        self.model_call_details = {}
        self.optional_params = optional_params or {}

    def pre_call(self, *a: Any, **kw: Any) -> None: ...
    def post_call(self, *a: Any, **kw: Any) -> None: ...
