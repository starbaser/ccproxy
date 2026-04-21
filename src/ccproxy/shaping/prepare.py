"""Default prepare functions — strip the shape's original content.

Each function takes a ``mitmproxy.http.Request`` shape and mutates it to
remove content that must be replaced by incoming request data.
Users compose their own prepare lists via the ``husk`` hook's ``prepare``
param; these are shipped as minimal examples.
"""

from __future__ import annotations

from typing import Any

from mitmproxy import http

from ccproxy.shaping.body import mutate_body

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


def strip_request_content(shape: http.Request) -> None:
    """Remove top-level body fields that carry the incoming request's intent."""

    def _strip(body: dict[str, Any]) -> None:
        for key in _CONTENT_BODY_FIELDS:
            body.pop(key, None)

    mutate_body(shape, _strip)


def strip_auth_headers(shape: http.Request) -> None:
    """Remove auth headers — the auth pipeline stage owns them."""
    for name in _AUTH_HEADERS:
        shape.headers.pop(name, None)


def strip_transport_headers(shape: http.Request) -> None:
    """Remove transport headers that would desync on replay."""
    for name in _TRANSPORT_HEADERS:
        shape.headers.pop(name, None)


def strip_system_blocks(shape: http.Request, keep: str = "") -> None:
    """Slice the system block list using Python range syntax.

    ``keep`` is a Python slice string applied to ``body["system"]``.
    Examples: ``":1"`` (keep first), ``"1:"`` (drop first), ``""`` (remove all).
    """

    def _strip(body: dict[str, Any]) -> None:
        system = body.get("system")
        if not isinstance(system, list):
            return
        if not keep:
            del body["system"]
        else:
            body["system"] = system[_parse_slice(keep)]

    mutate_body(shape, _strip)


def _parse_slice(s: str) -> slice:
    parts = s.split(":")
    if len(parts) == 1:
        i = int(parts[0])
        return slice(i, i + 1)
    args = [int(p) if p else None for p in parts]
    return slice(*args)
