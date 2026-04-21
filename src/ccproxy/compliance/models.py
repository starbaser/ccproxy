"""Runtime husk type and application.

A husk is a working copy of a seed's captured ``mitmproxy.http.Request``.
Prepare functions mutate the husk to strip the seed's original request
content; fill functions inhabit the husk with the incoming request's
content; ``apply_husk`` field-copies the husk onto the outbound flow.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from mitmproxy import http

if TYPE_CHECKING:
    from ccproxy.pipeline.context import Context


Husk = http.Request


_PRESERVE_HEADERS: frozenset[str] = frozenset(
    {
        "authorization",
        "x-api-key",
        "x-goog-api-key",
        "host",
    }
)


def apply_husk(husk: Husk, ctx: Context) -> None:
    """Stamp the husk's headers and body onto the outbound flow.

    Preserves transport routing (host/port/scheme/path) already set by
    the redirect/transform handler, and preserves auth headers already
    injected by the inbound pipeline. Only stamps compliance-relevant
    headers and body content from the husk.
    """
    target = ctx.flow.request

    preserved = {
        name: target.headers[name]
        for name in _PRESERVE_HEADERS
        if name in target.headers
    }

    target.headers.clear()
    for name, value in husk.headers.items():  # type: ignore[no-untyped-call]
        target.headers[name] = value
    for name, value in preserved.items():
        target.headers[name] = value

    target.content = husk.content

    try:
        parsed = json.loads(husk.content or b"{}")
    except (json.JSONDecodeError, TypeError):
        parsed = {}
    ctx._body = parsed if isinstance(parsed, dict) else {}
