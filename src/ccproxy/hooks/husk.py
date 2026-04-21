"""Husk hook — pick a seed, husk it, fill it, apply it.

Runs last in the outbound pipeline. For reverse proxy or OAuth-injected
flows with a completed transform, loads the most recent seed for the
destination provider, runs the configured prepare functions to strip
seed content, then the configured fill functions to inhabit the husk
with incoming request data, and applies the husk to the outbound flow.
"""

from __future__ import annotations

import functools
import importlib
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from mitmproxy import http
from mitmproxy.proxy.mode_specs import ReverseMode
from pydantic import BaseModel, Field

from ccproxy.compliance.models import Husk, apply_husk
from ccproxy.compliance.store import get_store
from ccproxy.inspector.flow_store import InspectorMeta
from ccproxy.pipeline.hook import hook

if TYPE_CHECKING:
    from ccproxy.pipeline.context import Context

logger = logging.getLogger(__name__)


class HuskParams(BaseModel):
    """Dotted-path lists of prepare and fill callables.

    Entries are dotted paths, optionally with a parenthesized argument:
    ``"mod.fn"`` or ``"mod.fn(arg)"``.
    """

    prepare: list[str] = Field(default_factory=list)
    fill: list[str] = Field(default_factory=list)


def husk_guard(ctx: Context) -> bool:
    """Run on reverse proxy or OAuth-injected flows with a completed transform."""
    is_reverse = isinstance(ctx.flow.client_conn.proxy_mode, ReverseMode)
    is_oauth = ctx.flow.metadata.get("ccproxy.oauth_injected", False)
    if not (is_reverse or is_oauth):
        return False

    record = ctx.flow.metadata.get(InspectorMeta.RECORD)
    return record is not None and getattr(record, "transform", None) is not None


@hook(
    reads=["messages", "system", "metadata"],
    writes=["messages", "system", "metadata"],
    model=HuskParams,
)
def husk(ctx: Context, params: dict[str, Any]) -> Context:
    """Pick a seed, husk it via prepare functions, fill it via fill functions, apply to the outbound request."""
    record = ctx.flow.metadata.get(InspectorMeta.RECORD)
    transform = getattr(record, "transform", None)
    if transform is None:
        return ctx

    provider = transform.provider
    store = get_store()
    seed = store.pick(provider)
    if seed is None or seed.request is None:
        logger.debug("No seed available for provider %s", provider)
        return ctx

    working: Husk = http.Request.from_state(seed.request.get_state())  # type: ignore[no-untyped-call]

    for entry in params.get("prepare", []):
        _resolve_entry(entry)(working)

    for entry in params.get("fill", []):
        _resolve_entry(entry)(working, ctx)

    apply_husk(working, ctx)
    logger.info("Applied husk from seed %s for provider %s", seed.id, provider)
    return ctx


def _resolve_entry(entry: str) -> Callable[..., Any]:
    """Resolve ``"mod.fn"`` or ``"mod.fn(arg)"`` into a callable."""
    if "(" in entry:
        path, _, arg = entry.partition("(")
        arg = arg.rstrip(")")
        fn = _import_dotted(path)
        return functools.partial(fn, arg)
    return _import_dotted(entry)


def _import_dotted(dotted: str) -> Callable[..., Any]:
    module_path, _, name = dotted.rpartition(".")
    if not module_path:
        raise ValueError(f"invalid dotted path: {dotted!r}")
    return getattr(importlib.import_module(module_path), name)  # type: ignore[no-any-return]
