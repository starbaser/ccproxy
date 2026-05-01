"""Strip values at glom paths from the request body."""

from __future__ import annotations

import logging
from typing import Any

from glom import GlomError, delete
from pydantic import BaseModel

from ccproxy.pipeline.context import Context
from ccproxy.pipeline.hook import hook

logger = logging.getLogger(__name__)


class StripParams(BaseModel):
    paths: list[str]
    """Glom dot-paths to delete. Wildcards supported: 'system.*.cache_control'"""


@hook(
    reads=["system", "tools", "messages"],
    writes=["system", "tools", "messages"],
    model=StripParams,
)
def strip(ctx: Context, params: dict[str, Any]) -> Context:
    """Strip values at the given glom paths."""
    for path in params.get("paths", []):
        try:
            delete(ctx._body, path, ignore_missing=True)
        except GlomError as exc:
            logger.debug("strip: path %s skipped: %s", path, exc)
    return ctx
