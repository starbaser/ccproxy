"""Response-side OAuth orchestration.

Detects 401 responses on flows where the request-side ``forward_oauth`` hook
injected an OAuth token, refreshes the token, and transparently replays the
request. The actual refresh primitives live in ``ccproxy/oauth/``; this addon
owns only the response-side detect/replay loop.
"""

from __future__ import annotations

import logging

from mitmproxy import http

from ccproxy import transport
from ccproxy.config import get_config

logger = logging.getLogger(__name__)


class OAuthAddon:
    """mitmproxy addon: 401-detect → refresh → replay.

    Trigger contract: ``forward_oauth`` stamps
    ``flow.metadata["ccproxy.oauth_injected"]`` and
    ``flow.metadata["ccproxy.oauth_provider"]``. ``response()`` reads those and
    replays the request when it sees a 401 on a flow ccproxy injected.
    """

    async def response(self, flow: http.HTTPFlow) -> None:
        response = flow.response
        if not response or response.status_code != 401:
            return
        if not flow.metadata.get("ccproxy.oauth_injected"):
            return

        try:
            await self._retry_with_refreshed_token(flow)
        except Exception:
            logger.error("OAuth retry failed", exc_info=True)

    async def _retry_with_refreshed_token(self, flow: http.HTTPFlow) -> bool:
        provider = flow.metadata.get("ccproxy.oauth_provider", "")
        if not provider:
            return False

        config = get_config()
        new_token = config.resolve_oauth_token(provider)
        if not new_token:
            logger.warning("OAuth 401 for provider '%s' — no token available, not retrying", provider)
            return False

        target_header = (config.get_auth_header(provider) or "authorization").lower()
        new_value = f"Bearer {new_token}" if target_header == "authorization" else new_token
        flow.request.headers[target_header] = new_value

        logger.info("OAuth 401 for provider '%s' — token refreshed, retrying request", provider)

        headers = dict(flow.request.headers)
        headers.pop("x-ccproxy-oauth-injected", None)

        profile = flow.metadata.get("ccproxy.fingerprint_profile") or transport.DEFAULT_PROFILE
        client = await transport.get_client(host=flow.request.pretty_host, profile=profile)
        retry_resp = await client.request(
            method=flow.request.method,
            url=flow.request.pretty_url,
            headers=headers,
            content=flow.request.content,
            timeout=config.provider_timeout,
        )
        flow.metadata["ccproxy.retry_transport"] = "curl_cffi"
        flow.metadata["ccproxy.retry_profile"] = profile

        assert flow.response is not None
        flow.response.status_code = retry_resp.status_code
        flow.response.headers.clear()
        for key, value in retry_resp.headers.multi_items():
            flow.response.headers.add(key, value)
        flow.response.content = retry_resp.content
        return True
