"""Feature classification for compliance profile extraction.

Determines which headers and body fields are "envelope" (compliance)
vs "content" (user intent) vs "dynamic" (per-request, excluded).
"""

from __future__ import annotations

# Body fields that carry user intent — never profiled
BODY_CONTENT_FIELDS = frozenset(
    {
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
    }
)

# Headers excluded from profiling (auth tokens, transport, internal)
HEADER_EXCLUSIONS = frozenset(
    {
        "authorization",
        "x-api-key",
        "x-goog-api-key",
        "cookie",
        "content-length",
        "transfer-encoding",
        "host",
        "connection",
        "accept-encoding",
        "x-ccproxy-flow-id",
        "x-ccproxy-hooks",
    }
)


def should_skip_header(
    name: str,
    additional_exclusions: frozenset[str] = frozenset(),
) -> bool:
    """Return True if this header should NOT be included in profiles."""
    lc = name.lower()
    return lc in HEADER_EXCLUSIONS or lc in additional_exclusions


def should_skip_body_field(
    key: str,
    additional_content_fields: frozenset[str] = frozenset(),
) -> bool:
    """Return True if this top-level body field is content, not envelope."""
    return key in BODY_CONTENT_FIELDS or key in additional_content_fields
