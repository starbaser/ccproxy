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


def _get_flow_hosts(flow: HTTPFlow) -> set[str]:
    """Collect all host identifiers for this flow (pretty_host, Host header, X-Forwarded-Host)."""
    hosts: set[str] = set()
    hosts.add(flow.request.pretty_host)
    host_header = flow.request.headers.get("host", "")
    if host_header:
        hosts.add(host_header.split(":")[0])
    fwd_host = flow.request.headers.get("x-forwarded-host", "")
    if fwd_host:
        hosts.add(fwd_host.split(":")[0])
    return hosts


def _resolve_transform_target(flow: HTTPFlow, body: dict[str, object] | None = None) -> TransformRoute | None:
    """Match flow against configured transform rules (first match wins)."""
    from ccproxy.config import get_config

    config = get_config()
    transforms = config.inspector.transforms
    if not transforms:
        return None

    hosts = _get_flow_hosts(flow)
    path = flow.request.path
    request_model = (body or {}).get("model", "") if body is not None else ""

    for rule in transforms:
        if rule.match_host is not None and rule.match_host not in hosts:
            continue
        if not path.startswith(rule.match_path):
            continue
        if rule.match_model is not None and rule.match_model not in str(request_model):
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

        try:
            body = json.loads(flow.request.content or b"{}")
        except (json.JSONDecodeError, TypeError):
            return

        target = _resolve_transform_target(flow, body)
        if target is None:
            return

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

        # Strip query params (may contain API keys) from log output
        log_url = url.split("?")[0]
        logger.info(
            "lightllm transform: %s → %s %s",
            body.get("model", "?"),
            target.dest_provider,
            log_url,
        )
