"""Runtime shape type and application.

A shape is a working copy of a captured request template.
Prepare functions strip the shape; fill functions inhabit it;
``apply_shape`` stamps it onto the outbound flow.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from mitmproxy import http

if TYPE_CHECKING:
    from ccproxy.pipeline.context import Context


Shape = http.Request


_PRESERVE_HEADERS: frozenset[str] = frozenset(
    {
        "authorization",
        "x-api-key",
        "x-goog-api-key",
        "host",
    }
)


def apply_shape(shape: Shape, ctx: Context) -> None:
    """Stamp the shape's headers and body onto the outbound flow.

    Preserves transport routing (host/port/scheme/path) already set by
    the redirect/transform handler, and preserves auth headers already
    injected by the inbound pipeline. Only stamps shaping-relevant
    headers and body content from the shape.
    """
    target = ctx.flow.request

    preserved = {
        name: target.headers[name]
        for name in _PRESERVE_HEADERS
        if name in target.headers
    }

    target.headers.clear()
    for name, value in shape.headers.items():  # type: ignore[no-untyped-call]
        target.headers[name] = value
    for name, value in preserved.items():
        target.headers[name] = value

    target.content = shape.content

    try:
        parsed = json.loads(shape.content or b"{}")
    except (json.JSONDecodeError, TypeError):
        parsed = {}
    ctx._body = parsed if isinstance(parsed, dict) else {}
