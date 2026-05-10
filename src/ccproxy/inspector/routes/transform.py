"""Transform route — sentinel-driven Provider routing + optional override layer.

Routing precedence on every inbound request:

    1. ``inspector.transforms`` — first regex-matched override wins.
    2. ``flow.metadata["ccproxy.oauth_provider"]`` — set by ``forward_oauth``
       when a sentinel key resolved. Looks up :class:`CCProxyConfig.providers`.
    3. None — :class:`mitmproxy.proxy.mode_specs.ReverseMode` flows return
       OpenAI-shape 501; WireGuard flows pass through unchanged.

Three actions:

    - ``transform``: rewrite the request body via lightllm dispatch (cross-format).
    - ``redirect``: rewrite destination only, preserve body (same-format).
    - ``passthrough``: forward unchanged.

For sentinel-resolved Provider targets, the action is auto-derived: when
``_detect_incoming_format`` matches ``provider.provider.value`` it's redirect,
otherwise transform.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

from glom import glom
from litellm.types.utils import LlmProviders
from mitmproxy.connection import Server
from mitmproxy.proxy.mode_specs import ReverseMode

from ccproxy.config import Provider, TransformOverride, get_config
from ccproxy.flows.store import InspectorMeta, TransformMeta

if TYPE_CHECKING:
    from mitmproxy.http import HTTPFlow

    from ccproxy.inspector.router import InspectorRouter

logger = logging.getLogger(__name__)


_ACTION_RE = re.compile(r":(\w+)(?:$|\?)")
_MODEL_FROM_PATH_RE = re.compile(r"/models/([^/:]+)")

_FORMAT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^/v1/chat/completions(?:/|$)"), "openai"),
    (re.compile(r"^/(?:anthropic/)?v1/messages(?:/|$)"), "anthropic"),
    (re.compile(r"^/(?:gemini/)?v1beta/models/[^/]+:"), "gemini"),
    (re.compile(r"^/(?:gemini/)?v1alpha/models/[^/]+:"), "gemini"),
    (re.compile(r"^/v1internal:"), "gemini"),
)
"""URL-prefix patterns ccproxy recognises as a known wire format."""

_GEMINI_FORMATS: frozenset[str] = frozenset(
    {
        LlmProviders.GEMINI.value,
        LlmProviders.VERTEX_AI.value,
        LlmProviders.VERTEX_AI_BETA.value,
    }
)


def _openai_error(message: str, *, error_type: str, code: int) -> bytes:
    """Serialize an OpenAI-shape error envelope for synthetic responses."""
    return json.dumps(
        {
            "error": {"message": message, "type": error_type, "code": code},
        }
    ).encode()


def _detect_incoming_format(path: str) -> str | None:
    """Return the wire format ccproxy thinks the incoming request speaks.

    ``"openai"`` for OpenAI Chat Completions; ``"anthropic"`` for Messages
    (including DeepSeek's anthropic-compat endpoint); ``"gemini"`` for both
    v1beta and the cloudcode-pa v1internal envelope; ``None`` for unknown.
    """
    for pattern, name in _FORMAT_PATTERNS:
        if pattern.search(path):
            return name
    return None


def _flow_hosts(flow: HTTPFlow) -> set[str]:
    hosts: set[str] = {flow.request.pretty_host}
    for header in ("host", "x-forwarded-host"):
        value = flow.request.headers.get(header, "")
        if value:
            hosts.add(value.split(":")[0])
    return hosts


def _any_search(pattern: re.Pattern[str], values: set[str]) -> bool:
    return any(pattern.search(v) for v in values)


def _action_from_path(path: str) -> str | None:
    match = _ACTION_RE.search(path.split("?")[0])
    return match.group(1) if match else None


def _model_for_routing(body: dict[str, object], path: str) -> str:
    body_model = str(glom(body, "model", default=""))
    if body_model:
        return body_model
    match = _MODEL_FROM_PATH_RE.search(path)
    return match.group(1) if match else ""


def _apply_path_template(template: str, *, model: str, action: str | None) -> str:
    out = template
    if "{model}" in out:
        out = out.replace("{model}", model)
    if "{action}" in out:
        out = out.replace("{action}", action or "")
    return out


def _resolve_transform_target(
    flow: HTTPFlow,
    body: dict[str, object] | None = None,
) -> Provider | TransformOverride | None:
    """Pick the routing target. First match wins; None means no signal."""
    config = get_config()
    request_model = str(glom(body or {}, "model", default=""))

    for rule in config.inspector.transforms:
        if rule.match_host_re and not _any_search(rule.match_host_re, _flow_hosts(flow)):
            continue
        if not rule.match_path_re.search(flow.request.path):
            continue
        if rule.match_model_re and not rule.match_model_re.search(request_model):
            continue
        return rule

    oauth_provider = flow.metadata.get("ccproxy.oauth_provider")
    if oauth_provider:
        return config.providers.get(oauth_provider)

    return None


def _record_transform_meta(
    flow: HTTPFlow,
    *,
    provider: str,
    model: str,
    body: dict[str, object],
    is_streaming: bool,
    mode: str,
) -> None:
    record = flow.metadata.get(InspectorMeta.RECORD)
    if record is None:
        return
    record.transform = TransformMeta(
        provider=provider,
        model=model,
        request_data={**body},
        is_streaming=is_streaming,
        mode=mode,  # type: ignore[arg-type]
    )


def _apply_destination(flow: HTTPFlow, host: str, path: str) -> None:
    flow.request.host = host
    flow.request.port = 443
    flow.request.scheme = "https"
    flow.request.path = path
    flow.server_conn = Server(address=(host, 443))


def _handle_passthrough(flow: HTTPFlow) -> None:
    logger.info(
        "transform passthrough: → %s:%d%s",
        flow.request.host,
        flow.request.port,
        flow.request.path,
    )


def _handle_redirect(
    flow: HTTPFlow,
    target: Provider | TransformOverride,
    body: dict[str, object],
) -> None:
    """Same-format redirect: rewrite host/path, preserve body."""
    is_streaming = bool(glom(body, "stream", default=False))
    action = _action_from_path(flow.request.path)
    config = get_config()

    host: str
    path: str
    if isinstance(target, Provider):
        provider_str = target.provider
        model = _model_for_routing(body, flow.request.path)
        host = target.host
        path = _apply_path_template(target.path, model=model, action=action)
        api_key: str | None = None  # auth already stamped by forward_oauth
    else:
        bound = config.providers.get(target.dest_provider) if target.dest_provider else None
        resolved_host = target.dest_host or (bound.host if bound else None)
        if resolved_host is None:
            logger.error(
                "redirect override missing dest_host and no resolvable dest_provider; passthrough",
            )
            return
        host = resolved_host
        provider_str = (bound.provider if bound else target.dest_provider) or ""
        model = target.dest_model or _model_for_routing(body, flow.request.path)
        if target.dest_path:
            path = _apply_path_template(target.dest_path, model=model, action=action)
        elif bound is not None:
            path = _apply_path_template(bound.path, model=model, action=action)
        else:
            path = flow.request.path
        api_key = config.resolve_oauth_token(target.dest_provider) if target.dest_provider else None

    _record_transform_meta(
        flow,
        provider=provider_str,
        model=model,
        body=body,
        is_streaming=is_streaming,
        mode="redirect",
    )

    _apply_destination(flow, host, path)
    if api_key:
        flow.request.headers["authorization"] = f"Bearer {api_key}"

    flow.comment = f"redirect → {provider_str}/{host}"
    logger.info("redirect: → %s %s%s", provider_str, host, path)


def _handle_transform(
    flow: HTTPFlow,
    target: Provider | TransformOverride,
    body: dict[str, object],
) -> None:
    """Cross-format transform via lightllm: rewrite both body and destination."""
    from urllib.parse import urlparse

    # deferred: heavy LiteLLM transform chain
    from ccproxy.lightllm import transform_to_provider

    is_streaming = bool(glom(body, "stream", default=False))
    config = get_config()

    if isinstance(target, Provider):
        provider_str = target.provider
        oauth_provider = flow.metadata.get("ccproxy.oauth_provider")
        api_key = config.resolve_oauth_token(oauth_provider) if oauth_provider else None
        model = _model_for_routing(body, flow.request.path)
        vertex_project: str | None = None
        vertex_location: str | None = None
    else:
        if target.dest_provider is None:
            logger.error("transform override missing dest_provider; passthrough")
            return
        bound = config.providers.get(target.dest_provider)
        if bound is None:
            logger.error(
                "transform override dest_provider '%s' not in config.providers; passthrough",
                target.dest_provider,
            )
            return
        provider_str = bound.provider
        api_key = config.resolve_oauth_token(target.dest_provider)
        model = target.dest_model or _model_for_routing(body, flow.request.path)
        vertex_project = target.dest_vertex_project
        vertex_location = target.dest_vertex_location

    messages: list[object] = list(glom(body, "messages", default=[]))  # type: ignore[arg-type]
    optional_params = {k: v for k, v in body.items() if k != "messages"}
    cached_content: str | None = None

    if provider_str in _GEMINI_FORMATS:
        from ccproxy.lightllm.context_cache import resolve_cached_content

        try:
            messages, optional_params, cached_content = resolve_cached_content(
                messages=messages,  # type: ignore[arg-type]
                model=model,
                provider=provider_str,  # type: ignore[arg-type]
                optional_params=optional_params,
                api_key=api_key,
                vertex_project=vertex_project,
                vertex_location=vertex_location,
            )
        except Exception:
            logger.warning("Context cache resolution failed, proceeding without", exc_info=True)

    url, headers, new_body = transform_to_provider(
        model=model,
        provider=provider_str,
        messages=messages,  # type: ignore[arg-type]
        optional_params=optional_params,
        api_key=api_key,
        stream=is_streaming,
        cached_content=cached_content,
    )

    _record_transform_meta(
        flow,
        provider=provider_str,
        model=model,
        body=body,
        is_streaming=is_streaming,
        mode="transform",
    )

    parsed = urlparse(url)
    host = parsed.hostname or flow.request.host
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    flow.request.host = host
    flow.request.port = port
    flow.request.scheme = parsed.scheme or "https"
    flow.request.path = parsed.path or "/"
    flow.server_conn = Server(address=(host, port))
    for k, v in headers.items():
        flow.request.headers[k] = v
    # Cookie-auth providers (Perplexity Pro) ship without an Authorization
    # header. forward_oauth has already stamped one with the real token —
    # strip it so the upstream doesn't see two competing auth signals.
    if any(k.lower() == "cookie" for k in headers) and not any(
        k.lower() == "authorization" for k in headers
    ):
        flow.request.headers.pop("Authorization", None)
    flow.request.content = new_body

    incoming_model = str(glom(body, "model", default="?"))
    flow.comment = f"{incoming_model} → {provider_str}/{model}"
    logger.info(
        "transform: %s → %s %s",
        incoming_model,
        provider_str,
        url.split("?")[0],
    )


def register_transform_routes(router: InspectorRouter) -> None:
    from ccproxy.inspector.router import RouteType

    @router.route("/{path}", rtype=RouteType.REQUEST, catch_error=False)
    def handle_transform(flow: HTTPFlow, **kwargs: object) -> None:  # pyright: ignore[reportUnusedFunction]
        if flow.metadata.get(InspectorMeta.DIRECTION) != "inbound":
            return

        try:
            body = json.loads(flow.request.content or b"{}")
        except (json.JSONDecodeError, TypeError):
            body = {}

        target = _resolve_transform_target(flow, body)
        is_reverse = isinstance(flow.client_conn.proxy_mode, ReverseMode)

        if target is None:
            if is_reverse:
                # deferred: heavy mitmproxy Response import
                from mitmproxy.http import Response

                flow.response = Response.make(
                    501,
                    _openai_error(
                        "no provider or transform rule matched this request",
                        error_type="not_implemented_error",
                        code=501,
                    ),
                    {"Content-Type": "application/json"},
                )
            return

        action = target.action if isinstance(target, TransformOverride) else None

        if action == "passthrough":
            _handle_passthrough(flow)
        elif not is_reverse:
            # WireGuard flows already encode their destination.
            _handle_passthrough(flow)
        elif isinstance(target, Provider):
            incoming = _detect_incoming_format(flow.request.path)
            if incoming == target.provider:
                _handle_redirect(flow, target, body)
            else:
                _handle_transform(flow, target, body)
        elif action == "redirect":
            _handle_redirect(flow, target, body)
        else:  # action == "transform"
            _handle_transform(flow, target, body)

        if is_reverse and flow.response is None and flow.request.host == "localhost" and flow.request.port == 1:
            from mitmproxy.http import Response

            flow.response = Response.make(
                502,
                _openai_error(
                    f"transform failed to rewrite destination (path={flow.request.path})",
                    error_type="api_error",
                    code=502,
                ),
                {"Content-Type": "application/json"},
            )
            logger.error(
                "Safety net: flow still targeting localhost:1 after transform (path=%s)",
                flow.request.path,
            )

    @router.route("/{path}", rtype=RouteType.RESPONSE, catch_error=False)
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

            # GeminiAddon.response (which strips cloudcode-pa's {response: {...}}
            # envelope) runs AFTER this handler in the addon chain, so the body
            # is still wrapped at this point. Unwrap inline for Gemini-family
            # providers; unwrap_buffered is idempotent.
            if meta.provider in _GEMINI_FORMATS:
                from ccproxy.hooks.gemini_envelope import unwrap_buffered

                flow.response.content = unwrap_buffered(flow.response.content or b"")

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
