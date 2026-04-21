"""Default prepare functions — husk out the seed's original content.

Each function takes a ``mitmproxy.http.Request`` husk and mutates it to
remove seed content that must be replaced by incoming request data.
Users compose their own prepare lists via the ``husk`` hook's ``prepare``
param; these are shipped as minimal examples.
"""

from __future__ import annotations

from typing import Any

from mitmproxy import http

from ccproxy.compliance.body import mutate_body

_CONTENT_BODY_FIELDS: frozenset[str] = frozenset(
    {
        "messages",
        "contents",
        "tools",
        "toolConfig",
        "tool_choice",
        "model",
        "prompt",
        "input",
        "stream",
        "thinking",
        "output_config",
        "context_management",
    }
)

_AUTH_HEADERS: tuple[str, ...] = (
    "authorization",
    "x-api-key",
    "x-goog-api-key",
)

_TRANSPORT_HEADERS: tuple[str, ...] = (
    "content-length",
    "host",
    "transfer-encoding",
    "connection",
)


def strip_request_content(husk: http.Request) -> None:
    """Remove top-level body fields that carry the incoming request's intent."""

    def _strip(body: dict[str, Any]) -> None:
        for key in _CONTENT_BODY_FIELDS:
            body.pop(key, None)

    mutate_body(husk, _strip)


def strip_auth_headers(husk: http.Request) -> None:
    """Remove auth headers — the auth pipeline stage owns them."""
    for name in _AUTH_HEADERS:
        husk.headers.pop(name, None)


def strip_transport_headers(husk: http.Request) -> None:
    """Remove transport headers that would desync on replay."""
    for name in _TRANSPORT_HEADERS:
        husk.headers.pop(name, None)


def strip_system_blocks_except_first(husk: http.Request) -> None:
    """Keep only the first system block; drops seed-specific follow-ons."""

    def _strip(body: dict[str, Any]) -> None:
        system = body.get("system")
        if isinstance(system, list) and system:
            body["system"] = [system[0]]

    mutate_body(husk, _strip)
