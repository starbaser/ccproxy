"""Feature classification for compliance profile extraction.

Determines which headers and body fields are "envelope" (compliance)
vs "content" (user intent) vs "dynamic" (per-request, excluded).
"""

from __future__ import annotations

# Body fields that carry user intent — never profiled
BODY_CONTENT_FIELDS = frozenset({
    "messages",
    "contents",
    "prompt",
    "tools",
    "tool_choice",
    "model",
    "stream",
    "max_tokens",
    "max_completion_tokens",
    "temperature",
    "top_p",
    "top_k",
    "stop",
    "n",
})

# Headers excluded from profiling (auth tokens, transport, internal)
HEADER_EXCLUSIONS = frozenset({
    "authorization",
    "x-api-key",
    "cookie",
    "content-length",
    "transfer-encoding",
    "host",
    "connection",
    "accept-encoding",
    "x-ccproxy-flow-id",
    "x-ccproxy-oauth-injected",
    "x-ccproxy-hooks",
})


def should_skip_header(name: str) -> bool:
    """Return True if this header should NOT be included in profiles."""
    return name.lower() in HEADER_EXCLUSIONS


def should_skip_body_field(key: str) -> bool:
    """Return True if this top-level body field is content, not envelope."""
    return key in BODY_CONTENT_FIELDS
