"""Compliance profile learning and application system.

Passively learns the compliance contract from legitimate CLI traffic
(via WireGuard observation) and applies it to non-compliant SDK
requests (via outbound pipeline hook).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from mitmproxy.proxy.mode_specs import WireGuardMode

from ccproxy.compliance.extractor import extract_observation
from ccproxy.compliance.store import get_store

if TYPE_CHECKING:
    from mitmproxy.http import HTTPFlow

    from ccproxy.inspector.flow_store import HttpSnapshot

logger = logging.getLogger(__name__)


def observe_flow(flow: HTTPFlow, client_request: HttpSnapshot) -> None:
    """Observe a flow for compliance profile learning.

    Called from InspectorAddon.request() after the ClientRequest
    snapshot is created. Only processes WireGuard flows (or flows
    matching configured reference UA patterns).
    """
    if not _should_observe(flow, client_request):
        return

    host: str = urlparse(client_request.url or "").hostname or ""
    provider = _resolve_provider(host)
    if not provider:
        logger.debug("Compliance: no provider for host %s, skipping observation", host)
        return

    extra_headers: frozenset[str] = frozenset()
    extra_fields: frozenset[str] = frozenset()
    try:
        from ccproxy.config import get_config

        cfg = get_config()
        extra_headers = frozenset(h.lower() for h in cfg.compliance.additional_header_exclusions)
        extra_fields = frozenset(cfg.compliance.additional_body_content_fields)
    except Exception:
        logger.debug("Failed to load classifier config additions", exc_info=True)

    bundle = extract_observation(
        client_request,
        provider,
        additional_header_exclusions=extra_headers,
        additional_body_content_fields=extra_fields,
    )

    try:
        store = get_store()
        store.submit_observation(bundle)
    except Exception:
        logger.exception("Compliance: failed to submit observation for %s", provider)


def _should_observe(flow: HTTPFlow, client_request: HttpSnapshot) -> bool:
    """Determine if this flow should be observed as reference traffic."""
    if isinstance(flow.client_conn.proxy_mode, WireGuardMode):
        return True

    # Check configured reference UA patterns
    try:
        from ccproxy.config import get_config

        config = get_config()
        if config.compliance.reference_user_agents:
            ua = client_request.headers.get("user-agent", "")
            return any(pattern in ua for pattern in config.compliance.reference_user_agents)
    except Exception:
        logger.debug("Failed to check reference UA patterns", exc_info=True)

    return False


def _resolve_provider(host: str) -> str | None:
    """Resolve a hostname to a provider name.

    Checks oat_sources.*.destinations first, then inspector.provider_map.
    """
    try:
        from ccproxy.config import get_config

        config = get_config()

        # Check oat_sources destinations
        provider = config.get_provider_for_destination(host)
        if provider:
            return provider

        # Fall back to inspector.provider_map
        return config.inspector.provider_map.get(host)
    except Exception:
        logger.exception("Compliance: failed to resolve provider for %s", host)
        return None
