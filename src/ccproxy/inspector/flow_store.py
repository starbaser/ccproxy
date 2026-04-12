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


@dataclass
class AuthMeta:
    """Auth decision record — written during request phase, readable during response phase."""

    provider: str
    credential: str
    auth_header: str
    injected: bool = False
    original_key: str = ""


@dataclass
class OtelMeta:
    """OTel span lifecycle — per-flow, not cross-pass."""

    span: Any = None
    ended: bool = False


@dataclass
class ClientRequest:
    """Snapshot of the client request before the pipeline mutates it."""

    method: str
    scheme: str
    host: str
    port: int
    path: str
    headers: dict[str, str]
    body: bytes
    content_type: str


@dataclass
class TransformMeta:
    """Transform context stored during request phase, consumed by response phase."""

    provider: str
    model: str
    request_data: dict[str, Any]
    is_streaming: bool
    mode: Literal["redirect", "transform"] = "redirect"


@dataclass
class FlowRecord:
    """Cross-pass state for a single logical request through the inspector."""

    direction: Literal["inbound"]
    auth: AuthMeta | None = None
    otel: OtelMeta | None = None
    client_request: ClientRequest | None = None
    transform: TransformMeta | None = None


class InspectorMeta:
    """Flow metadata keys for ccproxy inspector — mirrors xepor's FlowMeta pattern.

    These are keys for mitmproxy's flow.metadata dict (per-flow, in-memory only).
    The RECORD key holds a reference to the FlowRecord from the flow store.
    """

    RECORD = "ccproxy.record"
    DIRECTION = "ccproxy.direction"


_flow_store: dict[str, tuple[FlowRecord, float]] = {}
_store_lock = threading.Lock()
_STORE_TTL = 120.0


def create_flow_record(direction: Literal["inbound"]) -> tuple[str, FlowRecord]:
    """Create a new FlowRecord and store it. Returns (flow_id, record)."""
    flow_id = str(uuid.uuid4())
    record = FlowRecord(direction=direction)
    with _store_lock:
        _flow_store[flow_id] = (record, time.time())
        _cleanup_expired()
    return flow_id, record


def get_flow_record(flow_id: str | None) -> FlowRecord | None:
    """Look up a FlowRecord by flow ID. Returns None if not found, expired, or ID is None."""
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
