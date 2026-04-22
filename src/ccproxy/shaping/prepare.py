"""Default prepare functions — strip the shape's original content.

Each function takes a ``Context`` wrapping the shape and mutates it to
remove content that must be replaced by incoming request data.
"""

from __future__ import annotations

from ccproxy.pipeline.context import Context

_RAW_BODY_FIELDS: frozenset[str] = frozenset(
    {
        "contents",
        "toolConfig",
        "tool_choice",
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


def strip_request_content(shape_ctx: Context) -> None:
    """Remove content fields that carry the incoming request's intent."""
    shape_ctx.messages = []
    shape_ctx.tools = []
    shape_ctx._body.pop("model", None)
    for key in _RAW_BODY_FIELDS:
        shape_ctx._body.pop(key, None)


def strip_auth_headers(shape_ctx: Context) -> None:
    """Remove auth headers — the auth pipeline stage owns them."""
    for name in _AUTH_HEADERS:
        shape_ctx.set_header(name, "")


def strip_transport_headers(shape_ctx: Context) -> None:
    """Remove transport headers that would desync on replay."""
    for name in _TRANSPORT_HEADERS:
        shape_ctx.set_header(name, "")


def strip_system_blocks(shape_ctx: Context, keep: str = "") -> None:
    """Slice the system block list using Python range syntax.

    ``keep`` is a Python slice string applied to the system parts list.
    Examples: ``":1"`` (keep first), ``"1:"`` (drop first), ``""`` (remove all).
    """
    parts = shape_ctx.system
    if not parts:
        return
    if not keep:
        shape_ctx.system = []
    else:
        shape_ctx.system = parts[_parse_slice(keep)]


def _parse_slice(s: str) -> slice:
    parts = s.split(":")
    if len(parts) == 1:
        i = int(parts[0])
        return slice(i, i + 1)
    args = [int(p) if p else None for p in parts]
    return slice(*args)
