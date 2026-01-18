"""Context dataclass for pipeline execution.

Provides a typed interface to LiteLLM's request data dict.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Context:
    """Typed context for hook pipeline execution.

    Attributes:
        model: Model being requested
        messages: Conversation messages
        metadata: Routing decisions and trace info
        system: System prompt (string or list of content blocks)
        headers: HTTP headers from proxy_server_request
        raw_headers: Sensitive headers from secret_fields
        provider_headers: Headers to forward to LLM provider
        litellm_call_id: Unique call identifier
        api_key: API key for LiteLLM
        _raw_data: Original data dict (for fields not explicitly modeled)
    """

    model: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    system: str | list[dict[str, Any]] | None = None
    headers: dict[str, str] = field(default_factory=dict)
    raw_headers: dict[str, str] = field(default_factory=dict)
    provider_headers: dict[str, Any] = field(default_factory=dict)
    litellm_call_id: str = ""
    api_key: str | None = None
    _raw_data: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_litellm_data(cls, data: dict[str, Any]) -> Context:
        """Create Context from LiteLLM's data dict.

        Args:
            data: LiteLLM request data dict with structure:
                - model: str
                - messages: list[dict]
                - metadata: dict
                - system: str | list | None
                - proxy_server_request: dict with headers, body, url, method
                - secret_fields: dict with raw_headers
                - provider_specific_header: dict with extra_headers
                - litellm_call_id: str
                - api_key: str | None

        Returns:
            Context instance with extracted fields
        """
        proxy_request = data.get("proxy_server_request", {})
        secret_fields = data.get("secret_fields", {})
        provider_specific = data.get("provider_specific_header", {})

        # Extract headers from proxy_server_request
        headers = {}
        raw_headers_data = proxy_request.get("headers", {})
        if isinstance(raw_headers_data, dict):
            headers = {k.lower(): v for k, v in raw_headers_data.items()}

        # Extract raw headers from secret_fields (contains sensitive data)
        raw_headers = {}
        secret_raw = secret_fields.get("raw_headers", {})
        if isinstance(secret_raw, dict):
            raw_headers = {k.lower(): v for k, v in secret_raw.items()}

        return cls(
            model=data.get("model", ""),
            messages=data.get("messages", []),
            metadata=data.get("metadata", {}),
            system=data.get("system"),
            headers=headers,
            raw_headers=raw_headers,
            provider_headers=provider_specific,
            litellm_call_id=data.get("litellm_call_id", ""),
            api_key=data.get("api_key"),
            _raw_data=data,
        )

    def to_litellm_data(self) -> dict[str, Any]:
        """Convert Context back to LiteLLM's data dict.

        Returns:
            Data dict suitable for LiteLLM processing
        """
        data = dict(self._raw_data)

        # Update modified fields
        data["model"] = self.model
        data["messages"] = self.messages
        data["metadata"] = self.metadata
        if self.system is not None:
            data["system"] = self.system
        elif "system" in data:
            del data["system"]

        data["provider_specific_header"] = self.provider_headers
        data["litellm_call_id"] = self.litellm_call_id

        if self.api_key is not None:
            data["api_key"] = self.api_key

        return data

    def get_header(self, name: str, default: str = "") -> str:
        """Get header value (case-insensitive).

        Checks raw_headers first (has auth tokens), then regular headers.

        Args:
            name: Header name (case-insensitive)
            default: Default value if not found

        Returns:
            Header value or default
        """
        name_lower = name.lower()
        return self.raw_headers.get(name_lower, self.headers.get(name_lower, default))

    def set_provider_header(self, name: str, value: str) -> None:
        """Set a header to forward to the LLM provider.

        Args:
            name: Header name
            value: Header value
        """
        if "extra_headers" not in self.provider_headers:
            self.provider_headers["extra_headers"] = {}
        self.provider_headers["extra_headers"][name] = value

    def get_provider_header(self, name: str, default: str = "") -> str:
        """Get a provider header value.

        Args:
            name: Header name
            default: Default value if not found

        Returns:
            Header value or default
        """
        extra = self.provider_headers.get("extra_headers", {})
        return extra.get(name, default)

    @property
    def authorization(self) -> str:
        """Get Authorization header value."""
        return self.get_header("authorization", "")

    @property
    def x_api_key(self) -> str:
        """Get x-api-key header value."""
        return self.get_header("x-api-key", "")

    @property
    def ccproxy_model_name(self) -> str:
        """Get classified model name from metadata."""
        return self.metadata.get("ccproxy_model_name", "")

    @ccproxy_model_name.setter
    def ccproxy_model_name(self, value: str) -> None:
        """Set classified model name in metadata."""
        self.metadata["ccproxy_model_name"] = value

    @property
    def ccproxy_alias_model(self) -> str:
        """Get original model alias from metadata."""
        return self.metadata.get("ccproxy_alias_model", "")

    @ccproxy_alias_model.setter
    def ccproxy_alias_model(self, value: str) -> None:
        """Set original model alias in metadata."""
        self.metadata["ccproxy_alias_model"] = value

    @property
    def ccproxy_litellm_model(self) -> str:
        """Get routed LiteLLM model from metadata."""
        return self.metadata.get("ccproxy_litellm_model", "")

    @ccproxy_litellm_model.setter
    def ccproxy_litellm_model(self, value: str) -> None:
        """Set routed LiteLLM model in metadata."""
        self.metadata["ccproxy_litellm_model"] = value

    @property
    def ccproxy_model_config(self) -> dict[str, Any]:
        """Get model configuration from metadata."""
        return self.metadata.get("ccproxy_model_config", {})

    @ccproxy_model_config.setter
    def ccproxy_model_config(self, value: dict[str, Any]) -> None:
        """Set model configuration in metadata."""
        self.metadata["ccproxy_model_config"] = value

    @property
    def ccproxy_is_passthrough(self) -> bool:
        """Check if request is in passthrough mode."""
        return self.metadata.get("ccproxy_is_passthrough", False)

    @ccproxy_is_passthrough.setter
    def ccproxy_is_passthrough(self, value: bool) -> None:
        """Set passthrough mode flag."""
        self.metadata["ccproxy_is_passthrough"] = value
