"""Apply learned compliance profile to outbound requests.

Runs last in the outbound pipeline. For reverse proxy flows that have
been transformed by lightllm, loads the best compliance profile for the
destination provider and merges it onto the request.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from mitmproxy.proxy.mode_specs import ReverseMode

from ccproxy.compliance.merger import resolve_merger_class
from ccproxy.compliance.store import get_store
from ccproxy.inspector.flow_store import InspectorMeta
from ccproxy.pipeline.hook import hook

if TYPE_CHECKING:
    from ccproxy.pipeline.context import Context

logger = logging.getLogger(__name__)


def _get_provider_ua_hint(provider: str) -> str | None:
    """Get the user_agent from OAuthSource config for profile selection."""
    try:
        from ccproxy.config import get_config

        return get_config().get_auth_provider_ua(provider)
    except Exception:
        return None


def apply_compliance_guard(ctx: Context) -> bool:
    """Guard: run on reverse proxy flows with a completed transform."""
    if not isinstance(ctx.flow.client_conn.proxy_mode, ReverseMode):
        return False

    record = ctx.flow.metadata.get(InspectorMeta.RECORD)
    return record is not None and getattr(record, "transform", None) is not None


@hook(
    reads=["system", "metadata"],
    writes=["system", "metadata"],
)
def apply_compliance(ctx: Context, params: dict[str, Any]) -> Context:
    """Apply the compliance profile for the destination provider."""
    record = ctx.flow.metadata.get(InspectorMeta.RECORD)
    transform = getattr(record, "transform", None)
    if transform is None:
        return ctx

    provider = transform.provider
    store = get_store()

    if store.is_degraded:
        logger.warning(
            "Compliance store is degraded (format version mismatch). "
            "Compliance headers will NOT be applied until profiles are re-learned. "
            "Delete the compliance_profiles.json file to force a fresh start."
        )

    ua_hint = _get_provider_ua_hint(provider)
    profile = store.get_profile(provider, ua_hint=ua_hint)

    if profile is None:
        logger.debug("No compliance profile for provider %s", provider)
        return ctx

    logger.info(
        "Applying compliance profile for %s (ua=%s, %d headers, %d body fields)",
        provider,
        profile.user_agent,
        len(profile.headers),
        len(profile.body_fields),
    )

    from ccproxy.config import get_config

    merger_cls = resolve_merger_class(get_config().compliance.merger_class)
    merger_cls(ctx, profile).merge()
    return ctx
