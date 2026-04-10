"""Transform route — provider-to-provider request transformation at the mitmproxy layer.

Intercepts inbound flows matching configured transform rules, rewrites the
request body from one provider format to another using lightllm, and redirects
the flow to the destination provider — optionally bypassing LiteLLM entirely.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from mitmproxy.connection import Server

from ccproxy.inspector.flow_store import InspectorMeta

if TYPE_CHECKING:
    from mitmproxy.http import HTTPFlow

    from ccproxy.config import TransformRoute
    from ccproxy.inspector.router import InspectorRouter

logger = logging.getLogger(__name__)


def _resolve_transform_target(flow: HTTPFlow) -> TransformRoute | None:
    """Match flow against configured transform rules (first match wins)."""
    from ccproxy.config import get_config

    config = get_config()
    transforms = config.inspector.transforms
    if not transforms:
        return None

    host = flow.request.pretty_host
    path = flow.request.path

    for rule in transforms:
        if rule.match_host != host:
            continue
        if not path.startswith(rule.match_path):
            continue
        return rule
    return None


def _resolve_api_key(target: TransformRoute) -> str | None:
    """Resolve API key for the destination provider."""
    if target.dest_api_key_ref is None:
        return None

    from ccproxy.config import get_config

    config = get_config()
    token = config.get_oauth_token(target.dest_api_key_ref)
    if token:
        return token

    import os
    return os.environ.get(target.dest_api_key_ref)


def register_transform_routes(router: InspectorRouter) -> None:
    """Register transform route handlers on the given router."""
    from ccproxy.inspector.router import RouteType
    from ccproxy.lightllm import transform_to_provider

    @router.route("/{path}", rtype=RouteType.REQUEST)
    def handle_transform(flow: HTTPFlow, **kwargs: object) -> None:  # pyright: ignore[reportUnusedFunction]
        if flow.metadata.get(InspectorMeta.DIRECTION) != "inbound":
            return

        target = _resolve_transform_target(flow)
        if target is None:
            return

        body = json.loads(flow.request.content or b"{}")

        url, headers, new_body = transform_to_provider(
            model=target.dest_model,
            provider=target.dest_provider,
            messages=body.get("messages", []),
            optional_params={k: v for k, v in body.items() if k != "messages"},
            api_key=_resolve_api_key(target),
            stream=body.get("stream", False),
        )

        parsed = urlparse(url)
        flow.request.host = parsed.hostname or flow.request.host
        flow.request.port = parsed.port or (443 if parsed.scheme == "https" else 80)
        flow.request.scheme = parsed.scheme or "https"
        flow.request.path = parsed.path or "/"
        flow.server_conn = Server(address=(flow.request.host, flow.request.port))
        for k, v in headers.items():
            flow.request.headers[k] = v
        flow.request.content = new_body

        logger.info(
            "lightllm transform: %s → %s %s",
            body.get("model", "?"),
            target.dest_provider,
            url,
        )
