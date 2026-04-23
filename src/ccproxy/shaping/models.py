"""Runtime shape type and application.

A shape is a working copy of a captured request template.
Prepare functions strip the shape; fill functions inhabit it;
``apply_shape`` stamps it onto the outbound flow.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import TYPE_CHECKING

from mitmproxy import http

if TYPE_CHECKING:
    from ccproxy.pipeline.context import Context


Shape = http.Request


def apply_shape(shape: Shape, ctx: Context, preserve_headers: Sequence[str]) -> None:
    """Stamp the shape's headers and body onto the outbound flow.

    Preserves transport routing (host/port/scheme/path) already set by
    the redirect/transform handler, and preserves auth headers already
    injected by the inbound pipeline. Only stamps shaping-relevant
    headers and body content from the shape.
    """
    assert ctx.flow is not None
    target = ctx.flow.request

    preserved = {
        name: target.headers[name]
        for name in preserve_headers
        if name in target.headers
    }

    target.headers.clear()
    for name, value in shape.headers.items():  # type: ignore[no-untyped-call]
        target.headers[name] = value
    for name, value in preserved.items():
        target.headers[name] = value

    # Merge query parameters from the shape (e.g. ?beta=true)
    for key, value in shape.query.items():  # type: ignore[no-untyped-call]
        target.query[key] = value

    target.content = shape.content

    try:
        parsed = json.loads(shape.content or b"{}")
    except (json.JSONDecodeError, TypeError):
        parsed = {}
    ctx._body = parsed if isinstance(parsed, dict) else {}
