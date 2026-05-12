"""Rewrite ``flow.request`` to the in-process sidecar for impersonated outbound.

Selection is keyed on ``flow.metadata["ccproxy.oauth_provider"]`` (set by the
``forward_oauth`` inbound hook for sentinel-keyed flows). When the resolved
:class:`~ccproxy.config.Provider` declares a ``fingerprint_profile``, this
addon stashes the real target in ``X-CCProxy-Target-Url`` and the profile in
``X-CCProxy-Impersonate``, then rewrites destination to ``127.0.0.1:<sidecar>``.
mitmproxy's existing upstream pipeline does the rest — the sidecar makes the
actual upstream call via ``httpx-curl-cffi`` and streams the response back.
"""

from __future__ import annotations

import logging

from mitmproxy import http

from ccproxy.config import get_config
from ccproxy.flows.store import HttpSnapshot, InspectorMeta
from ccproxy.transport.sidecar import IMPERSONATE_HEADER, TARGET_URL_HEADER

logger = logging.getLogger(__name__)


class TransportOverrideAddon:
    """mitmproxy addon: redirect to the impersonating sidecar."""

    def __init__(self, sidecar_port: int) -> None:
        self._sidecar_port = sidecar_port

    async def request(self, flow: http.HTTPFlow) -> None:
        provider_name = flow.metadata.get("ccproxy.oauth_provider")
        if not provider_name:
            return

        provider = get_config().providers.get(provider_name)
        if provider is None or provider.fingerprint_profile is None:
            return

        profile = provider.fingerprint_profile
        target_url = flow.request.pretty_url

        record = flow.metadata.get(InspectorMeta.RECORD)
        if record is not None:
            record.forwarded_request = HttpSnapshot(
                headers=dict(flow.request.headers.items()),  # type: ignore[no-untyped-call]
                body=flow.request.content or b"",
                method=flow.request.method,
                url=target_url,
            )

        flow.request.headers[TARGET_URL_HEADER] = target_url
        flow.request.headers[IMPERSONATE_HEADER] = profile

        flow.request.host = "127.0.0.1"
        flow.request.port = self._sidecar_port
        flow.request.scheme = "http"
        flow.request.headers["host"] = f"127.0.0.1:{self._sidecar_port}"

        flow.metadata["ccproxy.transport_override"] = True
        flow.metadata["ccproxy.fingerprint_profile"] = profile

        logger.debug(
            "sidecar override: flow=%s provider=%s profile=%s target=%s",
            flow.id,
            provider_name,
            profile,
            target_url,
        )
