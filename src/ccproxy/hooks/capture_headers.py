"""Capture headers hook for LangFuse observability.

Captures HTTP headers as trace_metadata with sensitive value redaction.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlparse

from ccproxy.constants import SENSITIVE_PATTERNS
from ccproxy.metadata_store import store_request_metadata
from ccproxy.pipeline.hook import hook

if TYPE_CHECKING:
    from ccproxy.pipeline.context import Context

logger = logging.getLogger(__name__)


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
    return bool(ctx._raw_data.get("proxy_server_request"))  # pyright: ignore[reportPrivateUsage]


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
    trace_metadata: dict[str, Any] = cast(dict[str, Any], ctx.metadata["trace_metadata"])

    # Get optional headers filter from params
    headers_filter: list[str] | None = params.get("headers")

    request = ctx._raw_data.get("proxy_server_request", {})  # pyright: ignore[reportPrivateUsage]
    headers = request.get("headers", {})

    # Merge with raw headers (has auth info)
    all_headers = {**headers, **ctx.raw_headers}

    for name, value in all_headers.items():
        if not value:
            continue
        name_lower = name.lower()

        # Filter headers if a filter list is provided
        if headers_filter is not None and name_lower not in [h.lower() for h in headers_filter]:
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
        path: str = urlparse(str(url)).path
        if path:
            trace_metadata["http_path"] = path

    # Store in global store for retrieval in success callback
    call_id = ctx.litellm_call_id
    if not call_id:
        import uuid

        call_id = str(uuid.uuid4())
        ctx.litellm_call_id = call_id
        ctx._raw_data["litellm_call_id"] = call_id  # pyright: ignore[reportPrivateUsage]

    store_request_metadata(call_id, {"trace_metadata": trace_metadata.copy()})

    return ctx
