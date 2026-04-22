"""Thread-safe TTL store for cross-phase flow state in the inspector.

Bridges metadata between the request phase and response phase of a single
logical flow through the mitmproxy addon chain. A flow ID is propagated via
the ``x-ccproxy-flow-id`` header so that inbound auth decisions are readable
when the corresponding response phase fires.
"""

import threading
import time
import uuid
from dataclasses import dataclass
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
