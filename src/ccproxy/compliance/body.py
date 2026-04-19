"""JSON body helpers for ``mitmproxy.http.Request``.

Prepare and fill functions access the husk's JSON body through these
helpers instead of hand-rolling ``json.loads``/``json.dumps``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from mitmproxy import http


def get_body(req: http.Request) -> dict[str, Any]:
    """Return the request's JSON body as a dict. Returns ``{}`` on non-JSON."""
    try:
        data = json.loads(req.content or b"{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def set_body(req: http.Request, body: dict[str, Any]) -> None:
    """Serialize the dict back onto ``req.content``."""
    req.content = json.dumps(body).encode()


def mutate_body(req: http.Request, fn: Callable[[dict[str, Any]], None]) -> None:
    """Read-modify-write: ``fn`` mutates the parsed body dict in place."""
    body = get_body(req)
    fn(body)
    set_body(req, body)
