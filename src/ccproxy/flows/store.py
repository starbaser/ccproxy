"""Thread-safe TTL store for cross-phase flow state in the inspector.

Bridges metadata between the request phase and response phase of a single
logical flow through the mitmproxy addon chain. A flow ID is propagated via
the ``x-ccproxy-flow-id`` header so that inbound auth decisions are readable
when the corresponding response phase fires.
"""

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

FLOW_ID_HEADER = "x-ccproxy-flow-id"


@dataclass(frozen=True)
class AuthMeta:
    """Auth decision record."""

    provider: str
    """Provider name (e.g. 'anthropic', 'gemini')."""

    credential: str
    """Resolved credential value (token or API key)."""

    auth_header: str
    """HTTP header name used for authentication."""

    injected: bool = False
    """Whether the credential was injected by the OAuth hook."""

    original_key: str = ""
    """Original API key before sentinel substitution."""


@dataclass
class OtelMeta:
    """OTel span lifecycle."""

    span: Any = None
    """Active OpenTelemetry span for this flow."""

    ended: bool = False
    """Whether the span has been finished."""


@dataclass(frozen=True)
class HttpSnapshot:
    """Frozen copy of an HTTP message (request or response)."""

    headers: dict[str, str]
    """HTTP headers as a flat key-value mapping."""

    body: bytes
    """Raw HTTP body content."""

    method: str | None = None
    """HTTP method (request snapshots only)."""

    url: str | None = None
    """Full URL (request snapshots only)."""

    status_code: int | None = None
    """HTTP status code (response snapshots only)."""


ClientRequest = HttpSnapshot


@dataclass(frozen=True)
class TransformMeta:
    """Transform context for the response phase."""

    provider: str
    """Destination provider name for lightllm dispatch."""

    model: str
    """Destination model name."""

    request_data: dict[str, Any]
    """Stashed request body for response-phase transform."""

    is_streaming: bool
    """Whether the request uses SSE streaming."""

    mode: Literal["redirect", "transform"] = "redirect"
    """Transform mode: redirect preserves body, transform rewrites it."""


@dataclass
class FlowRecord:
    """Cross-pass state for a single logical request through the inspector."""

    direction: Literal["inbound"]
    """Traffic direction (always inbound)."""

    auth: AuthMeta | None = None
    """Auth decision from the OAuth hook, if any."""

    otel: OtelMeta | None = None
    """OTel span lifecycle state."""

    client_request: HttpSnapshot | None = None
    """Pre-pipeline client request snapshot."""

    provider_response: HttpSnapshot | None = None
    """Raw provider response before transforms."""

    transform: TransformMeta | None = None
    """Transform context bridging request to response phase."""

    conversation_id: str | None = None
    """First 12 hex chars of ``sha256(extract_first_user_text(messages))``.

    Stable across requests in the same conversation (same first user message),
    so MCP and CLI tools can group flows by logical session.
    """

    system_prompt_sha: str | None = None
    """First 12 hex chars of ``sha256(json.dumps(system, sort_keys=True))``.

    Identifies which system prompt was in effect for this request.
    """

    _parsed_request_body: dict[str, Any] | None = field(default=None, init=False, repr=False)
    """Parse-once cache of the JSON request body, populated lazily by
    ``parsed_request_body``."""

    _parse_attempted: bool = field(default=False, init=False, repr=False)
    """Sentinel ensuring the parse runs at most once per record (so a malformed
    body returning ``None`` doesn't trigger repeated re-parses)."""

    def parsed_request_body(self, content: bytes | None) -> dict[str, Any] | None:
        """Parse the JSON request body once and cache the result.

        Returns ``None`` on empty bodies, parse failures, or non-dict roots.
        Subsequent calls reuse the cached value (or cached ``None`` failure)
        without re-parsing.
        """
        if not self._parse_attempted:
            self._parse_attempted = True
            if content:
                try:
                    parsed = json.loads(content)
                    if isinstance(parsed, dict):
                        self._parsed_request_body = parsed
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass
        return self._parsed_request_body


class InspectorMeta:
    """Flow metadata keys for ccproxy inspector."""

    RECORD = "ccproxy.record"
    DIRECTION = "ccproxy.direction"


_flow_store: dict[str, tuple[FlowRecord, float]] = {}
_store_lock = threading.Lock()
_STORE_TTL = 3600


def create_flow_record(direction: Literal["inbound"]) -> tuple[str, FlowRecord]:
    flow_id = str(uuid.uuid4())
    record = FlowRecord(direction=direction)
    with _store_lock:
        _flow_store[flow_id] = (record, time.time())
        _cleanup_expired()
    return flow_id, record


def get_flow_record(flow_id: str | None) -> FlowRecord | None:
    if flow_id is None:
        return None
    with _store_lock:
        entry = _flow_store.get(flow_id)
        if entry:
            record, ts = entry
            if time.time() - ts <= _STORE_TTL:
                return record
            del _flow_store[flow_id]
    return None


def _cleanup_expired() -> None:
    """Remove expired entries. Must be called with _store_lock held."""
    now = time.time()
    expired = [k for k, (_, ts) in _flow_store.items() if now - ts > _STORE_TTL]
    for k in expired:
        del _flow_store[k]


def clear_flow_store() -> None:
    """Clear all entries. For testing."""
    with _store_lock:
        _flow_store.clear()
