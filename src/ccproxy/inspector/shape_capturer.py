"""Shape capturer addon.

Registers ``ccproxy.shape``: a mitmproxy command that saves the specified
flows as shapes to the provider's shape store on disk.
"""

from __future__ import annotations

import json
import logging
import re

from mitmproxy import command, ctx, http

from ccproxy.config import get_config
from ccproxy.shaping.store import get_store

logger = logging.getLogger(__name__)

_CCPROXY_META_PREFIX = "ccproxy."


class ShapeCapturer:
    """Addon exposing ``ccproxy.shape`` — save raw flows as provider shapes."""

    @command.command("ccproxy.shape")  # type: ignore[untyped-decorator]
    def ccproxy_shape(self, flow_ids: str, provider: str) -> str:
        """Save the listed flows as shapes into the provider's shape store.

        ``flow_ids`` is a comma-separated list of mitmproxy flow ids.
        ``provider`` is the target provider name (e.g. ``anthropic``).
        Returns a JSON summary of the save operation.
        """
        ids = [fid.strip() for fid in flow_ids.split(",") if fid.strip()]
        if not ids:
            raise ValueError("no flow ids provided")

        store = get_store()
        saved = 0
        missing: list[str] = []

        config = get_config()
        profile = config.shaping.providers.get(provider)

        for fid in ids:
            flow = self._find_http_flow(fid)
            if flow is None:
                logger.warning("ccproxy.shape: no flow with id %s, skipping", fid)
                missing.append(fid)
                continue
            if not _validate_flow(flow, provider, profile):
                missing.append(fid)
                continue
            clean = _strip_runtime_metadata(flow)
            store.add(provider, clean)
            saved += 1

        summary: dict[str, object] = {
            "status": "ok" if saved else "empty",
            "provider": provider,
            "flows_saved": saved,
            "missing": missing,
        }

        logger.info(
            "Shaped %d flow(s) under provider %s (%d missing)",
            saved,
            provider,
            len(missing),
        )
        return json.dumps(summary)

    @staticmethod
    def _find_http_flow(flow_id: str) -> http.HTTPFlow | None:
        view = ctx.master.addons.get("view")  # type: ignore[no-untyped-call]
        if view is None:
            return None
        found = view.get_by_id(flow_id)
        return found if isinstance(found, http.HTTPFlow) else None


def _validate_flow(
    flow: http.HTTPFlow,
    provider: str,
    profile: object | None,
) -> bool:
    """Check that a flow is a valid API request suitable for shaping."""
    from ccproxy.config import ProviderShapingConfig

    if flow.request.method != "POST":
        logger.warning(
            "ccproxy.shape: flow %s is %s not POST, skipping",
            flow.id, flow.request.method,
        )
        return False
    ct = flow.request.headers.get("content-type", "")
    if not ct.startswith("application/json"):
        logger.warning(
            "ccproxy.shape: flow %s content-type %r not JSON, skipping",
            flow.id, ct,
        )
        return False
    if isinstance(profile, ProviderShapingConfig) and profile.capture.path_pattern:
        if not re.search(profile.capture.path_pattern, flow.request.path):
            logger.warning(
                "ccproxy.shape: flow %s path %s doesn't match %s, skipping",
                flow.id, flow.request.path, profile.capture.path_pattern,
            )
            return False
    return True


def _strip_runtime_metadata(flow: http.HTTPFlow) -> http.HTTPFlow:
    """Deep-copy the flow and strip non-serializable metadata.

    Removes ccproxy runtime keys and any non-string metadata keys
    (e.g. mitmproxy 12's FlowMeta enum members) that FlowWriter
    cannot serialize.
    """
    clone = flow.copy()
    keys_to_remove = [
        k
        for k in clone.metadata
        if not isinstance(k, str) or k.startswith(_CCPROXY_META_PREFIX)
    ]
    for k in keys_to_remove:
        del clone.metadata[k]
    return clone
