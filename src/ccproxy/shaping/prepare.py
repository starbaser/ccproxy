"""Prepare functions — strip headers from the shape before content injection.

Called directly by the shape hook with the provider's configured header list.
"""

from __future__ import annotations

from collections.abc import Sequence

from ccproxy.pipeline.context import Context


def strip_headers(shape_ctx: Context, headers: Sequence[str]) -> None:
    """Remove the listed headers from the shape context."""
    for name in headers:
        shape_ctx.set_header(name, "")
