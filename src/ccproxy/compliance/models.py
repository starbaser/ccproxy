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


def apply_husk(husk: Husk, ctx: Context) -> None:
    """Field-copy the husk onto ``ctx.flow.request`` and sync ``ctx._body``.

    Rewrites method, URL parts, headers, and content. Also updates the
    pipeline ``Context``'s parsed body so ``ctx.commit()`` (called by the
    executor after the hook returns) re-serializes the husk shape rather
    than reverting to the pre-husk body.
    """
    target = ctx.flow.request
    target.method = husk.method
    target.scheme = husk.scheme
    target.host = husk.host
    target.port = husk.port
    target.path = husk.path
    target.headers.clear()
    for name, value in husk.headers.items():  # type: ignore[no-untyped-call]
        target.headers[name] = value
    target.content = husk.content

    try:
        parsed = json.loads(husk.content or b"{}")
    except (json.JSONDecodeError, TypeError):
        parsed = {}
    ctx._body = parsed if isinstance(parsed, dict) else {}
