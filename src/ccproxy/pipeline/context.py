"""Context dataclass for pipeline execution.

Wraps a mitmproxy HTTPFlow as a first-class member. Body fields
(model, messages, system, metadata) are read from the parsed JSON body
and flushed back via commit(). Header mutations are live — they hit the
flow immediately.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mitmproxy.http import HTTPFlow


@dataclass
class Context:
    """Typed context for hook pipeline execution.

    The flow is the source of truth. Body fields are parsed once on
    construction and flushed back to the flow via commit().
    """

    flow: HTTPFlow
    _body: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_flow(cls, flow: HTTPFlow) -> Context:
        """Build Context from a mitmproxy HTTPFlow."""
        try:
            body = json.loads(flow.request.content or b"{}")
        except (json.JSONDecodeError, TypeError):
            body = {}
        return cls(flow=flow, _body=body)

    # --- Body fields ---

    @property
    def model(self) -> str:
        return str(self._body.get("model", ""))

    @model.setter
    def model(self, value: str) -> None:
        self._body["model"] = value

    @property
    def messages(self) -> list[dict[str, Any]]:
        return self._body.get("messages", [])  # type: ignore[no-any-return]

    @messages.setter
    def messages(self, value: list[dict[str, Any]]) -> None:
        self._body["messages"] = value

    @property
    def system(self) -> str | list[dict[str, Any]] | None:
        return self._body.get("system")

    @system.setter
    def system(self, value: str | list[dict[str, Any]] | None) -> None:
        if value is None:
            self._body.pop("system", None)
        else:
            self._body["system"] = value

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
        return {k.lower(): v for k, v in self.flow.request.headers.items()}  # type: ignore[union-attr, no-untyped-call]

    def get_header(self, name: str, default: str = "") -> str:
        """Get header value (case-insensitive)."""
        return self.flow.request.headers.get(name, default)  # type: ignore[union-attr, no-any-return]

    def set_header(self, name: str, value: str) -> None:
        """Set or remove a header on the flow."""
        if value == "":
            self.flow.request.headers.pop(name, None)  # type: ignore[union-attr]
        else:
            self.flow.request.headers[name] = value  # type: ignore[index]

    @property
    def authorization(self) -> str:
        return self.get_header("authorization")

    @property
    def x_api_key(self) -> str:
        return self.get_header("x-api-key")

    @property
    def flow_id(self) -> str:
        return self.flow.id

    # --- Metadata convenience properties ---

    @property
    def ccproxy_oauth_provider(self) -> str:
        return str(self.metadata.get("ccproxy_oauth_provider", ""))

    @ccproxy_oauth_provider.setter
    def ccproxy_oauth_provider(self, value: str) -> None:
        self.metadata["ccproxy_oauth_provider"] = value

    @property
    def session_id(self) -> str:
        return str(self.metadata.get("session_id", ""))

    @session_id.setter
    def session_id(self, value: str) -> None:
        self.metadata["session_id"] = value

    # --- Commit ---

    def commit(self) -> None:
        """Flush body mutations back to flow.request.content.

        Strips empty ``metadata`` dicts injected by property access —
        upstream APIs reject unknown fields (e.g. Google: "Unknown name
        metadata").
        """
        body = self._body
        if "metadata" in body and isinstance(body["metadata"], dict) and not body["metadata"]:
            del body["metadata"]
        self.flow.request.content = json.dumps(body).encode()
