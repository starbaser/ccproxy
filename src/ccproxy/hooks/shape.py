"""Shape hook — pick a saved shape, inject content, apply it.

Runs last in the outbound pipeline. For reverse proxy or OAuth-injected
flows with a completed transform, loads the most recent shape for the
destination provider, strips auth/transport headers, injects content
fields from the incoming request per the provider's shaping profile,
runs shape hooks via an inner DAG, and applies the shape to the outbound flow.
"""

from __future__ import annotations

import logging
from typing import Any

from mitmproxy import http
from mitmproxy.proxy.mode_specs import ReverseMode

from ccproxy.config import ProviderShapingConfig, get_config
from ccproxy.flows.store import InspectorMeta
from ccproxy.pipeline.context import Context
from ccproxy.pipeline.hook import hook
from ccproxy.shaping.executor import execute_shape_hooks
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

    if _ua_matches(ctx, captured.request):
        logger.debug("Incoming UA matches shape UA, skipping shaping")
        return ctx

    working: Shape = http.Request.from_state(captured.request.get_state())  # type: ignore[no-untyped-call]
    shape_ctx = Context.from_request(working)

    strip_headers(shape_ctx, profile.strip_headers)

    _inject_content(shape_ctx, ctx, profile)

    shape_ctx = execute_shape_hooks(shape_ctx, ctx, profile.shape_hooks)

    shape_ctx.commit()
    apply_shape(working, ctx, profile.preserve_headers)
    logger.info("Applied shape from %s for provider %s", captured.id, provider)
    return ctx


def _ua_family(ua: str) -> str:
    """Extract the user-agent family prefix before the first ``/``."""
    return ua.split("/", 1)[0].strip().lower()


def _ua_matches(ctx: Context, shape_request: http.Request) -> bool:
    """True if the incoming UA shares the same family as the shape's UA."""
    incoming_ua = ctx.get_header("user-agent")
    shape_ua = shape_request.headers.get("user-agent", "")
    if not incoming_ua or not shape_ua:
        return False
    return _ua_family(incoming_ua) == _ua_family(shape_ua)


def _parse_strategy(raw: str) -> tuple[str, int | None]:
    """Parse ``"prepend_shape:2"`` into ``("prepend_shape", 2)``."""
    if ":" in raw:
        name, _, param = raw.partition(":")
        return name, int(param)
    return raw, None


def _inject_content(
    shape_ctx: Context,
    incoming_ctx: Context,
    profile: ProviderShapingConfig,
) -> None:
    """Strip content fields from shape, then fill from incoming per merge strategy."""
    # Snapshot shape values needed for non-replace strategies before stripping
    shape_originals: dict[str, Any] = {}
    for key in profile.content_fields:
        strategy, _ = _parse_strategy(profile.merge_strategies.get(key, "replace"))
        if strategy in ("prepend_shape", "append_shape") and key in shape_ctx._body:
            shape_originals[key] = shape_ctx._body[key]
        shape_ctx._body.pop(key, None)

    # Fill from incoming with merge strategy
    for key in profile.content_fields:
        strategy, slice_n = _parse_strategy(profile.merge_strategies.get(key, "replace"))
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
            if slice_n is not None:
                shape_val = shape_val[:slice_n]
            shape_ctx._body[key] = [*shape_val, *incoming_val]
        elif strategy == "append_shape":
            incoming_val = incoming_ctx._body.get(key) or []
            shape_val = shape_originals.get(key) or []
            if isinstance(shape_val, str):
                shape_val = [{"type": "text", "text": shape_val}]
            if isinstance(incoming_val, str):
                incoming_val = [{"type": "text", "text": incoming_val}]
            if slice_n is not None:
                shape_val = shape_val[:slice_n]
            shape_ctx._body[key] = [*incoming_val, *shape_val]
        elif strategy == "drop":
            pass  # already popped
