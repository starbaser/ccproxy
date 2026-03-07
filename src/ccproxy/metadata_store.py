"""Global request metadata store for cross-callback data passing.

LiteLLM doesn't preserve custom metadata from async_pre_call_hook to logging
callbacks — only internal fields like user_id and hidden_params survive. This
module provides a thread-safe TTL store keyed by litellm_call_id to bridge
that gap.
"""

import threading
import time
from typing import Any

_request_metadata_store: dict[str, tuple[dict[str, Any], float]] = {}
_store_lock = threading.Lock()
_STORE_TTL = 60.0  # Clean up entries older than 60 seconds


def store_request_metadata(call_id: str, metadata: dict[str, Any]) -> None:
    """Store metadata for a request by its call ID."""
    with _store_lock:
        _request_metadata_store[call_id] = (metadata, time.time())
        # Clean up old entries
        now = time.time()
        expired = [k for k, (_, ts) in _request_metadata_store.items() if now - ts > _STORE_TTL]
        for k in expired:
            del _request_metadata_store[k]


def get_request_metadata(call_id: str) -> dict[str, Any]:
    """Retrieve metadata for a request by its call ID."""
    with _store_lock:
        entry = _request_metadata_store.get(call_id)
        if entry:
            metadata, _ = entry
            return metadata
        return {}
