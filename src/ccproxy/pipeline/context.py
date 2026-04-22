"""Context dataclass for pipeline execution.

Wraps a mitmproxy HTTPFlow (or bare http.Request for shapes) as a
first-class member. Content fields (messages, system, tools) are
lazy-parsed into Pydantic AI typed objects and flushed back via
commit(). Header mutations are live — they hit the flow immediately.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic_ai.messages import ModelMessage, SystemPromptPart
from pydantic_ai.tools import ToolDefinition

from ccproxy.pipeline.wire import (
    parse_messages,
    parse_system,
    parse_tools,
    serialize_messages,
    serialize_system,
    serialize_tools,
)

if TYPE_CHECKING:
    from mitmproxy import http
    from mitmproxy.http import HTTPFlow


@dataclass
class Context:
    """Typed context for hook pipeline execution.

    The flow (or bare request) is the source of truth. Body fields are
    parsed once on first access and flushed back via commit().
    """

    flow: HTTPFlow | None
    """Mitmproxy flow (None for shape-only contexts)."""

    _body: dict[str, Any] = field(default_factory=dict, repr=False)
    """Parsed JSON request body, flushed back via commit()."""

    _request: http.Request | None = field(default=None, repr=False)
    """Bare request for shape contexts (no flow)."""

    _cached_messages: list[ModelMessage] | None = field(default=None, repr=False)
    """Lazy-parsed typed messages, populated on first access."""

    _cached_system: list[SystemPromptPart] | None = field(default=None, repr=False)
    """Lazy-parsed typed system prompts, populated on first access."""

    _cached_tools: list[ToolDefinition] | None = field(default=None, repr=False)
    """Lazy-parsed typed tool definitions, populated on first access."""

    @classmethod
    def from_flow(cls, flow: HTTPFlow) -> Context:
        """Build Context from a mitmproxy HTTPFlow."""
        try:
            body = json.loads(flow.request.content or b"{}")
        except (json.JSONDecodeError, TypeError):
            body = {}
        return cls(flow=flow, _body=body)

    @classmethod
    def from_request(cls, req: http.Request) -> Context:
        """Build Context from a bare http.Request (for shapes, no flow)."""
        try:
            body = json.loads(req.content or b"{}")
        except (json.JSONDecodeError, TypeError):
            body = {}
        return cls(flow=None, _body=body, _request=req)

    # --- Typed content properties ---

    @property
    def messages(self) -> list[ModelMessage]:
        if self._cached_messages is None:
            self._cached_messages = parse_messages(self._body.get("messages", []))
        return self._cached_messages

    @messages.setter
    def messages(self, value: list[ModelMessage]) -> None:
        self._cached_messages = value
        self._body["messages"] = serialize_messages(value)

    @property
    def system(self) -> list[SystemPromptPart]:
        if self._cached_system is None:
            self._cached_system = parse_system(self._body.get("system"))
        return self._cached_system

    @system.setter
    def system(self, value: list[SystemPromptPart]) -> None:
        self._cached_system = value
        self._body["system"] = serialize_system(value)

    @property
    def tools(self) -> list[ToolDefinition]:
        if self._cached_tools is None:
            self._cached_tools = parse_tools(self._body.get("tools", []))
        return self._cached_tools

    @tools.setter
    def tools(self, value: list[ToolDefinition]) -> None:
        self._cached_tools = value
        self._body["tools"] = serialize_tools(value)

    @property
    def model(self) -> str:
        return str(self._body.get("model", ""))

    @model.setter
    def model(self, value: str) -> None:
        self._body["model"] = value

    @property
    def stream(self) -> bool:
        """Whether the request uses SSE streaming."""
        return bool(self._body.get("stream", False))

    @stream.setter
    def stream(self, value: bool) -> None:
        self._body["stream"] = value

    @property
    def tool_choice(self) -> Any:
        """Tool choice configuration from the request body."""
        return self._body.get("tool_choice")

    @tool_choice.setter
    def tool_choice(self, value: Any) -> None:
        self._body["tool_choice"] = value

    # --- Body metadata ---

    @property
    def metadata(self) -> dict[str, Any]:
        return self._body.setdefault("metadata", {})  # type: ignore[no-any-return]

    @metadata.setter
    def metadata(self, value: dict[str, Any]) -> None:
        self._body["metadata"] = value

    # --- Headers (read/write flow.request.headers directly) ---

    @property
    def headers(self) -> dict[str, str]:
        """Snapshot of flow headers, lowercased keys."""
        req = self._resolve_request()
        if req is None:
            return {}
        return {k.lower(): v for k, v in req.headers.items()}  # type: ignore[no-untyped-call]

    def get_header(self, name: str, default: str = "") -> str:
        """Get header value (case-insensitive)."""
        req = self._resolve_request()
        if req is None:
            return default
        return req.headers.get(name, default)  # type: ignore[no-any-return]

    def set_header(self, name: str, value: str) -> None:
        """Set or remove a header on the flow."""
        req = self._resolve_request()
        if req is None:
            return
        if value == "":
            req.headers.pop(name, None)
        else:
            req.headers[name] = value

    @property
    def authorization(self) -> str:
        return self.get_header("authorization")

    @property
    def x_api_key(self) -> str:
        return self.get_header("x-api-key")

    @property
    def flow_id(self) -> str:
        if self.flow is not None:
            return self.flow.id
        return ""

    # --- Metadata convenience properties ---

    @property
    def ccproxy_oauth_provider(self) -> str:
        return str(self.metadata.get("ccproxy_oauth_provider", ""))

    @ccproxy_oauth_provider.setter
    def ccproxy_oauth_provider(self, value: str) -> None:
        self.metadata["ccproxy_oauth_provider"] = value

    # --- Commit ---

    def commit(self) -> None:
        """Flush body mutations back to the underlying request content.

        Strips empty ``metadata`` dicts injected by property access —
        upstream APIs reject unknown fields (e.g. Google: "Unknown name
        metadata").
        """
        body = self._body
        if "metadata" in body and isinstance(body["metadata"], dict) and not body["metadata"]:
            del body["metadata"]
        encoded = json.dumps(body).encode()

        if self.flow is not None:
            self.flow.request.content = encoded
        elif self._request is not None:
            self._request.content = encoded

    # --- Internal ---

    def _resolve_request(self) -> http.Request | None:
        """Return the underlying http.Request, from flow or direct."""
        if self.flow is not None:
            return self.flow.request  # type: ignore[return-value]
        return self._request
