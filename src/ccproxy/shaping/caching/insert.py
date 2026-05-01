"""Insert a value at a glom path in the request body."""

from __future__ import annotations

import logging
from typing import Any

from glom import GlomError, assign
from pydantic import BaseModel

from ccproxy.pipeline.context import Context
from ccproxy.pipeline.hook import hook

logger = logging.getLogger(__name__)


class InsertParams(BaseModel):
    path: str
    """Glom dot-path target. e.g. 'system.-1.cache_control'"""

    value: Any = {"type": "ephemeral"}
    """Value to set at the path."""


@hook(
    reads=["system", "tools", "messages"],
    writes=["system", "tools", "messages"],
    model=InsertParams,
)
def insert(ctx: Context, params: dict[str, Any]) -> Context:
    """Set a value at the given glom path."""
    try:
        assign(ctx._body, params.get("path", ""), params.get("value", {"type": "ephemeral"}))
    except GlomError as exc:
        logger.debug("insert: path %s skipped: %s", params.get("path"), exc)
    return ctx
