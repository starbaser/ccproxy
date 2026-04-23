"""Shape hook — pick a saved shape, inject content, apply it.

Runs last in the outbound pipeline. For reverse proxy or OAuth-injected
flows with a completed transform, loads the most recent shape for the
destination provider, strips auth/transport headers, injects content
fields from the incoming request per the provider's shaping profile,
runs callbacks, and applies the shape to the outbound flow.
"""

from __future__ import annotations

import functools
import importlib
import inspect
import logging
from collections.abc import Callable
from typing import Any

from mitmproxy import http
from mitmproxy.proxy.mode_specs import ReverseMode

from ccproxy.config import ProviderShapingConfig, get_config
from ccproxy.flows.store import InspectorMeta
from ccproxy.pipeline.context import Context
from ccproxy.pipeline.hook import hook
from ccproxy.shaping.models import Shape, apply_shape
from ccproxy.shaping.prepare import strip_headers
from ccproxy.shaping.store import get_store

logger = logging.getLogger(__name__)


def shape_guard(ctx: Context) -> bool:
    """Run on reverse proxy or OAuth-injected flows with a completed transform."""
    assert ctx.flow is not None
    is_reverse = isinstance(ctx.flow.client_conn.proxy_mode, ReverseMode)
    is_oauth = ctx.flow.metadata.get("ccproxy.oauth_injected", False)
    if not (is_reverse or is_oauth):
        return False

    record = ctx.flow.metadata.get(InspectorMeta.RECORD)
    return record is not None and getattr(record, "transform", None) is not None


@hook(
    reads=["messages", "system", "metadata"],
    writes=["messages", "system", "metadata"],
)
def shape(ctx: Context, params: dict[str, Any]) -> Context:
    """Pick a shape, inject content from the incoming request, apply to the outbound flow."""
    assert ctx.flow is not None
    record = ctx.flow.metadata.get(InspectorMeta.RECORD)
    transform = getattr(record, "transform", None)
    if transform is None:
        return ctx

    provider = transform.provider
    config = get_config()
    profile = config.shaping.providers.get(provider)
    if profile is None:
        logger.debug("No shaping profile for provider %s", provider)
        return ctx

    store = get_store()
    captured = store.pick(provider)
    if captured is None or captured.request is None:
        logger.debug("No shape available for provider %s", provider)
        return ctx

    working: Shape = http.Request.from_state(captured.request.get_state())  # type: ignore[no-untyped-call]
    shape_ctx = Context.from_request(working)

    strip_headers(shape_ctx, profile.strip_headers)

    _inject_content(shape_ctx, ctx, profile)

    for entry in profile.callbacks:
        _resolve_entry(entry)(shape_ctx, ctx)

    shape_ctx.commit()
    apply_shape(working, ctx, profile.preserve_headers)
    logger.info("Applied shape from %s for provider %s", captured.id, provider)
    return ctx


def _inject_content(
    shape_ctx: Context,
    incoming_ctx: Context,
    profile: ProviderShapingConfig,
) -> None:
    """Strip content fields from shape, then fill from incoming per merge strategy."""
    # Snapshot shape values needed for non-replace strategies before stripping
    shape_originals: dict[str, Any] = {}
    for key in profile.content_fields:
        strategy = profile.merge_strategies.get(key, "replace")
        if strategy in ("prepend_shape", "append_shape") and key in shape_ctx._body:
            shape_originals[key] = shape_ctx._body[key]
        shape_ctx._body.pop(key, None)

    # Fill from incoming with merge strategy
    for key in profile.content_fields:
        strategy = profile.merge_strategies.get(key, "replace")
        if strategy == "replace":
            if key in incoming_ctx._body:
                shape_ctx._body[key] = incoming_ctx._body[key]
        elif strategy == "prepend_shape":
            incoming_val = incoming_ctx._body.get(key) or []
            shape_val = shape_originals.get(key) or []
            if isinstance(shape_val, str):
                shape_val = [{"type": "text", "text": shape_val}]
            if isinstance(incoming_val, str):
                incoming_val = [{"type": "text", "text": incoming_val}]
            shape_ctx._body[key] = [*shape_val, *incoming_val]
        elif strategy == "append_shape":
            incoming_val = incoming_ctx._body.get(key) or []
            shape_val = shape_originals.get(key) or []
            if isinstance(shape_val, str):
                shape_val = [{"type": "text", "text": shape_val}]
            if isinstance(incoming_val, str):
                incoming_val = [{"type": "text", "text": incoming_val}]
            shape_ctx._body[key] = [*incoming_val, *shape_val]
        elif strategy == "drop":
            pass  # already popped


def _resolve_entry(entry: str) -> Callable[..., Any]:
    """Resolve ``"mod.fn"`` or ``"mod.fn(arg)"`` into a callable.

    The parenthesized arg binds to the function's first parameter that
    has a default value, preserving the leading positional parameters
    (``shape``, ``ctx``) for the caller.
    """
    if "(" in entry:
        path, _, arg = entry.partition("(")
        arg = arg.rstrip(")")
        fn = _import_dotted(path)
        kwarg = _first_defaulted_param(fn)
        return functools.partial(fn, **{kwarg: arg})
    return _import_dotted(entry)


def _first_defaulted_param(fn: Callable[..., Any]) -> str:
    """Return the name of ``fn``'s first parameter that has a default value."""
    for p in inspect.signature(fn).parameters.values():
        if p.default is not inspect.Parameter.empty:
            return p.name
    raise ValueError(f"{fn.__qualname__} has no parameter with a default value")


def _import_dotted(dotted: str) -> Callable[..., Any]:
    module_path, _, name = dotted.rpartition(".")
    if not module_path:
        raise ValueError(f"invalid dotted path: {dotted!r}")
    return getattr(importlib.import_module(module_path), name)  # type: ignore[no-any-return]
