"""Capture headers hook for LangFuse observability.

Captures HTTP headers as trace_metadata with sensitive value redaction.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from ccproxy.pipeline.hook import hook

if TYPE_CHECKING:
    from ccproxy.pipeline.context import Context

logger = logging.getLogger(__name__)

# Global storage for request metadata, keyed by litellm_call_id
# Required because LiteLLM doesn't preserve custom metadata through its internal flow
_request_metadata_store: dict[str, tuple[dict[str, Any], float]] = {}
_store_lock = threading.Lock()
_STORE_TTL = 60.0


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


# Regex patterns for detecting sensitive header values to redact
SENSITIVE_PATTERNS = {
    "authorization": r"^(Bearer sk-[a-z]+-|Bearer |sk-[a-z]+-)",
    "x-api-key": r"^(sk-[a-z]+-)",
    "cookie": None,  # Fully redact
}


def _redact_value(header: str, value: str) -> str:
    """Redact sensitive header values while preserving identifying prefix and suffix."""
    header_lower = header.lower()
    if header_lower in SENSITIVE_PATTERNS:
        pattern = SENSITIVE_PATTERNS[header_lower]
        if pattern is None:
            return "[REDACTED]"
        match = re.match(pattern, value)
        prefix = match.group(0) if match else ""
        suffix = value[-4:] if len(value) > 8 else ""
        return f"{prefix}...{suffix}"
    return str(value)[:200]


def capture_headers_guard(ctx: Context) -> bool:
    """Guard: Run if proxy_server_request exists."""
    return bool(ctx._raw_data.get("proxy_server_request"))


@hook(
    reads=["proxy_server_request", "secret_fields"],
    writes=["trace_metadata"],
)
def capture_headers(ctx: Context, params: dict[str, Any]) -> Context:
    """Capture HTTP headers as LangFuse trace_metadata with sensitive value redaction.

    Headers are added to metadata["trace_metadata"] which flows to LangFuse.

    Args:
        ctx: Pipeline context
        params: Optional 'headers' list to filter which headers to capture

    Returns:
        Modified context with trace_metadata populated
    """
    if "trace_metadata" not in ctx.metadata:
        ctx.metadata["trace_metadata"] = {}
    trace_metadata = ctx.metadata["trace_metadata"]

    # Get optional headers filter from params
    headers_filter: list[str] | None = params.get("headers")

    request = ctx._raw_data.get("proxy_server_request", {})
    headers = request.get("headers", {})

    # Merge with raw headers (has auth info)
    all_headers = {**headers, **ctx.raw_headers}

    for name, value in all_headers.items():
        if not value:
            continue
        name_lower = name.lower()

        # Filter headers if a filter list is provided
        if headers_filter is not None:
            if name_lower not in [h.lower() for h in headers_filter]:
                continue

        # Add to trace_metadata with header_ prefix
        redacted_value = _redact_value(name, str(value))
        trace_metadata[f"header_{name_lower}"] = redacted_value

    # Add HTTP method and path
    http_method = request.get("method", "")
    if http_method:
        trace_metadata["http_method"] = http_method

    url = request.get("url", "")
    if url:
        path = urlparse(url).path
        if path:
            trace_metadata["http_path"] = path

    # Store in global store for retrieval in success callback
    call_id = ctx.litellm_call_id
    if not call_id:
        import uuid

        call_id = str(uuid.uuid4())
        ctx.litellm_call_id = call_id
        ctx._raw_data["litellm_call_id"] = call_id

    store_request_metadata(call_id, {"trace_metadata": trace_metadata.copy()})

    return ctx
