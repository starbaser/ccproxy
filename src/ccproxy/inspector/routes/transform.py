"""Transform route — provider-to-provider request transformation at the mitmproxy layer.

Intercepts inbound flows matching configured transform rules, rewrites the
request body from one provider format to another using lightllm, and redirects
the flow to the destination provider.

Three modes:
  - ``transform``: rewrite request body via lightllm dispatch (cross-format)
  - ``redirect``: rewrite destination host but preserve body (same-format)
  - ``passthrough``: forward to the original destination unchanged

Unmatched flows: WireGuard flows pass through to their original destination;
reverse proxy flows get a 501 error (no default upstream).
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from mitmproxy.connection import Server
from mitmproxy.proxy.mode_specs import ReverseMode

from ccproxy.config import get_config
from ccproxy.flows.store import InspectorMeta, TransformMeta

if TYPE_CHECKING:
    from mitmproxy.http import HTTPFlow

    from ccproxy.config import TransformRoute
    from ccproxy.inspector.router import InspectorRouter

logger = logging.getLogger(__name__)


def _get_flow_hosts(flow: HTTPFlow) -> set[str]:
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
    if target.dest_api_key_ref is None:
        return None

    config = get_config()
    token = config.get_oauth_token(target.dest_api_key_ref)
    if token:
        return token

    return os.environ.get(target.dest_api_key_ref)


# Gemini SDK path → cloudcode-pa path mapping
# /v1beta/models/{model}:generateContent → /v1internal:generateContent
# /v1beta/models/{model}:streamGenerateContent → /v1internal:streamGenerateContent?alt=sse
_GEMINI_ACTION_RE = re.compile(r":(\w+)$")


def _rewrite_path(stripped: str, target: TransformRoute) -> str | None:
    """Rewrite a prefix-stripped path for the destination host.

    For Gemini: maps standard SDK paths to cloudcode-pa's /v1internal endpoint.
    """
    if target.dest_provider != "gemini":
        return None
    m = _GEMINI_ACTION_RE.search(stripped.split("?")[0])
    if not m:
        return None
    action = m.group(1)
    if action == "streamGenerateContent":
        return f"/v1internal:{action}?alt=sse"
    return f"/v1internal:{action}"


def _handle_passthrough(flow: HTTPFlow) -> None:
    logger.info("lightllm passthrough: → %s:%d%s", flow.request.host, flow.request.port, flow.request.path)


def _handle_redirect(flow: HTTPFlow, target: TransformRoute, body: dict[str, object]) -> None:
    """Redirect to destination host without transforming the body.

    For same-format flows (e.g. Anthropic → Anthropic, Gemini → Gemini)
    where the request body is already in the correct provider format.
    """
    dest_host = target.dest_host
    if not dest_host:
        logger.error("redirect mode requires dest_host, falling back to passthrough")
        return

    is_streaming = bool(body.get("stream", False))

    # Resolve model from config, body, or path
    model = target.dest_model or str(body.get("model", ""))
    if not model:
        match = re.search(r"/models/([^/:]+)", flow.request.path)
        if match:
            model = match.group(1)

    # Persist transform context for shape hook
    record = flow.metadata.get(InspectorMeta.RECORD)
    if record is not None:
        record.transform = TransformMeta(
            provider=target.dest_provider,
            model=model,
            request_data={**body},
            is_streaming=is_streaming,
        )

    flow.request.host = dest_host
    flow.request.port = 443
    flow.request.scheme = "https"
    if target.dest_path:
        flow.request.path = target.dest_path
    elif target.match_path and target.match_path != "/":
        # Strip the routing prefix and rewrite the path for the destination
        prefix = target.match_path.rstrip("/")
        if flow.request.path.startswith(prefix):
            stripped = flow.request.path[len(prefix) :] or "/"
            flow.request.path = _rewrite_path(stripped, target) or stripped
    flow.server_conn = Server(address=(dest_host, 443))

    # Inject auth from oat_sources if configured
    api_key = _resolve_api_key(target)
    if api_key:
        flow.request.headers["authorization"] = f"Bearer {api_key}"

    flow.comment = f"redirect → {target.dest_provider}/{dest_host}"

    logger.info("redirect: → %s %s%s", target.dest_provider, dest_host, flow.request.path)


_GEMINI_PROVIDERS = {"gemini", "vertex_ai", "vertex_ai_beta"}


def _handle_transform(flow: HTTPFlow, target: TransformRoute, body: dict[str, object]) -> None:
    # deferred: heavy LiteLLM transform chain
    from ccproxy.lightllm import transform_to_provider

    is_streaming = bool(body.get("stream", False))
    api_key = _resolve_api_key(target)
    messages: list[object] = body.get("messages", [])  # type: ignore[assignment]
    optional_params = {k: v for k, v in body.items() if k != "messages"}
    cached_content: str | None = None

    if target.dest_provider in _GEMINI_PROVIDERS:
        from ccproxy.lightllm.context_cache import resolve_cached_content

        try:
            messages, optional_params, cached_content = resolve_cached_content(
                messages=messages,  # type: ignore[arg-type]
                model=target.dest_model,
                provider=target.dest_provider,  # type: ignore[arg-type]
                optional_params=optional_params,
                api_key=api_key,
                vertex_project=target.dest_vertex_project,
                vertex_location=target.dest_vertex_location,
            )
        except Exception:
            logger.warning("Context cache resolution failed, proceeding without", exc_info=True)

    model = target.dest_model or str(body.get("model", ""))
    url, headers, new_body = transform_to_provider(
        model=model,
        provider=target.dest_provider,
        messages=messages,  # type: ignore[arg-type]
        optional_params=optional_params,
        api_key=api_key,
        stream=is_streaming,
        cached_content=cached_content,
    )

    # Persist transform context for response phase
    record = flow.metadata.get(InspectorMeta.RECORD)
    if record is not None:
        record.transform = TransformMeta(
            provider=target.dest_provider,
            model=target.dest_model,
            request_data={**body},
            is_streaming=is_streaming,
            mode="transform",
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

    flow.comment = f"{body.get('model', '?')} → {target.dest_provider}/{target.dest_model}"

    log_url = url.split("?")[0]
    logger.info(
        "lightllm transform: %s → %s %s",
        body.get("model", "?"),
        target.dest_provider,
        log_url,
    )


def register_transform_routes(router: InspectorRouter) -> None:
    from ccproxy.inspector.router import RouteType

    @router.route("/{path}", rtype=RouteType.REQUEST)
    def handle_transform(flow: HTTPFlow, **kwargs: object) -> None:  # pyright: ignore[reportUnusedFunction]
        if flow.metadata.get(InspectorMeta.DIRECTION) != "inbound":
            return

        try:
            body = json.loads(flow.request.content or b"{}")
        except (json.JSONDecodeError, TypeError):
            body = {}

        target = _resolve_transform_target(flow, body)

        if target is None:
            if isinstance(flow.client_conn.proxy_mode, ReverseMode):
                # deferred: heavy mitmproxy Response import
                from mitmproxy.http import Response

                flow.response = Response.make(
                    501,
                    b'{"error": "no transform rule configured for this destination"}',
                    {"Content-Type": "application/json"},
                )
            return

        if target.mode == "passthrough":
            _handle_passthrough(flow)
        elif isinstance(flow.client_conn.proxy_mode, ReverseMode):
            # Transform and redirect only apply to reverse proxy flows.
            # WireGuard flows already have the correct destination.
            if target.mode == "redirect":
                _handle_redirect(flow, target, body)
            else:
                _handle_transform(flow, target, body)
        else:
            _handle_passthrough(flow)

    @router.route("/{path}", rtype=RouteType.RESPONSE)
    def handle_transform_response(flow: HTTPFlow, **kwargs: object) -> None:  # pyright: ignore[reportUnusedFunction]
        record = flow.metadata.get(InspectorMeta.RECORD)
        if record is None or getattr(record, "transform", None) is None:
            return

        meta = record.transform
        if meta.mode != "transform":
            return
        if not flow.response or flow.response.status_code >= 400:
            return

        if meta.is_streaming:
            return

        try:
            # deferred: heavy LiteLLM transform chain
            from ccproxy.lightllm import MitmResponseShim, transform_to_openai

            shim = MitmResponseShim(flow.response)
            messages = meta.request_data.get("messages", [])
            request_data = {k: v for k, v in meta.request_data.items() if k != "messages"}

            model_response = transform_to_openai(
                model=meta.model,
                provider=meta.provider,
                raw_response=shim,
                request_data=request_data,
                messages=messages,
            )

            flow.response.content = json.dumps(model_response.model_dump()).encode()  # type: ignore[no-untyped-call]
            flow.response.headers["content-type"] = "application/json"
            flow.response.headers.pop("content-encoding", None)  # type: ignore[no-untyped-call]

            logger.info(
                "lightllm response transform: %s %s → OpenAI format",
                meta.provider,
                meta.model,
            )
        except Exception:
            logger.warning("Response transform failed, passing through raw response", exc_info=True)
