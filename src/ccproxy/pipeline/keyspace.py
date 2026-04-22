"""Read-key vocabulary extraction for per-request DAG validation.

The pipeline executor uses this to seed the set of keys available for hook
reads at the start of each request. A hook declaring ``reads=["metadata"]``
or ``reads=["metadata.user_id"]`` resolves cleanly when the corresponding
body path exists; otherwise the executor emits a runtime warning with the
request path and trace id.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ccproxy.pipeline.context import Context


def extract_available_keys(ctx: Context) -> set[str]:
    """Compute the initial read-key vocabulary for a flow.

    Walks the parsed request body dict recursively, emitting dot-separated
    paths for every dict key (both intermediate and leaf). List contents are
    intentionally skipped — enumerating indices is not useful and body items
    like ``messages[*]`` would churn the set per request.

    Also emits lowercased header names so hooks reading from headers (e.g.
    ``reads=["authorization"]``) resolve cleanly.
    """
    keys: set[str] = set()
    _walk_dict(ctx._body, prefix="", out=keys)
    req = ctx._resolve_request()
    if req is not None:
        for name in req.headers:
            keys.add(name.lower())
    return keys


def _walk_dict(obj: Any, prefix: str, out: set[str]) -> None:
    if not isinstance(obj, dict):
        return
    for k, v in obj.items():
        path = f"{prefix}.{k}" if prefix else k
        out.add(path)
        if isinstance(v, dict):
            _walk_dict(v, path, out)
