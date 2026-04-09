"""Inspector addon for HTTP/HTTPS traffic capture with ccproxy

Captures all HTTP traffic flowing through reverse, forward, and WireGuard
proxy listeners. Mode is detected per-flow via mitmproxy's multi-mode
``flow.client_conn.proxy_mode`` attribute using ``isinstance`` checks
against the concrete mode dataclasses.
"""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING, Any, Literal, cast

from mitmproxy import http
from mitmproxy.proxy.mode_specs import ReverseMode, WireGuardMode

from ccproxy.config import InspectorConfig
from ccproxy.inspector.flow_store import (
    FLOW_ID_HEADER,
    InspectorMeta,
    create_flow_record,
    get_flow_record,
)

if TYPE_CHECKING:
    from ccproxy.inspector.telemetry import InspectorTracer

logger = logging.getLogger(__name__)

Direction = Literal["inbound", "outbound"]


class InspectorAddon:
    """Inspector addon for HTTP/HTTPS traffic capture and tracing."""

    def __init__(
        self,
        config: InspectorConfig,
        traffic_source: str | None = None,
        wg_cli_port: int | None = None,
        wg_gateway_port: int | None = None,
    ) -> None:
        self.config = config
        self.traffic_source = traffic_source
        self.tracer: InspectorTracer | None = None
        self._forward_domains: set[str] = set(config.forward_domains)
        self._wg_cli_port = wg_cli_port
        self._wg_gateway_port = wg_gateway_port

    def set_tracer(self, tracer: InspectorTracer) -> None:
        self.tracer = tracer

    def _get_direction(self, flow: http.HTTPFlow) -> Direction | None:
        """Detect traffic direction from the proxy mode that accepted this flow."""
        mode = flow.client_conn.proxy_mode

        if isinstance(mode, ReverseMode):
            return "inbound"

        if isinstance(mode, WireGuardMode):
            port = mode.custom_listen_port
            if port is not None and port == self._wg_gateway_port:
                return "outbound"
            return "inbound"

        return None

    def _truncate_body(self, body: bytes | None) -> bytes | None:
        if not body:
            return None
        if self.config.max_body_size > 0 and len(body) > self.config.max_body_size:
            return body[: self.config.max_body_size]
        return body

    def _serialize_headers(self, headers: Any) -> dict[str, str]:
        return {str(k): str(v) for k, v in headers.items()}

    def _extract_session_id(self, request: http.Request) -> str | None:
        """Extract session_id from Claude Code's metadata.user_id field."""
        if not request.content:
            return None

        try:
            body = json.loads(request.content)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

        metadata = body.get("metadata", {})
        if not isinstance(metadata, dict):
            return None

        user_id = str(metadata.get("user_id", ""))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        if not user_id:
            return None

        if user_id.startswith("{"):
            try:
                user_id_obj = json.loads(user_id)
                if isinstance(user_id_obj, dict) and user_id_obj.get("session_id"):  # pyright: ignore[reportUnknownMemberType]
                    return cast(str, user_id_obj["session_id"])
            except (json.JSONDecodeError, TypeError):
                pass

        if "_session_" in user_id:
            parts = user_id.split("_session_")
            if len(parts) == 2:
                return parts[1]

        return None

    def _maybe_forward(self, flow: http.HTTPFlow, direction: Direction, host: str) -> None:
        """Forward CLI WireGuard LLM API traffic to LiteLLM.

        Only applies to inbound WireGuard flows (WIREGUARD_CLI) whose host is
        in the configured forward_domains list. Reverse proxy flows are already
        targeting LiteLLM. Outbound flows must not be forwarded (infinite loop).
        """
        if direction != "inbound" or host not in self._forward_domains:
            return
        if not isinstance(flow.client_conn.proxy_mode, WireGuardMode):
            return
        litellm_port = int(os.environ.get("CCPROXY_LITELLM_PORT", "4000"))
        flow.request.headers["X-Forwarded-Host"] = host
        flow.request.host = "localhost"
        flow.request.port = litellm_port
        flow.request.scheme = "http"
        logger.info("Forwarding %s → localhost:%d", host, litellm_port)

    async def request(self, flow: http.HTTPFlow) -> None:
        direction = self._get_direction(flow)
        if direction is None:
            return

        headers = cast("dict[str, Any]", flow.request.headers)
        record = get_flow_record(headers.get(FLOW_ID_HEADER))

        if record is None:
            flow_id, record = create_flow_record(direction)
            flow.request.headers[FLOW_ID_HEADER] = flow_id
            record.original_headers = self._serialize_headers(flow.request.headers)

        flow.metadata[InspectorMeta.DIRECTION] = direction
        flow.metadata[InspectorMeta.RECORD] = record

        host = flow.request.pretty_host
        self._maybe_forward(flow, direction, host)

        try:
            session_id = self._extract_session_id(flow.request)

            if self.tracer:
                self.tracer.start_span(flow, direction, host, flow.request.method, session_id)

            logger.debug(
                "Captured request: %s %s (trace_id: %s, direction: %s, session: %s)",
                flow.request.method,
                flow.request.pretty_url,
                flow.id,
                direction,
                session_id or "none",
            )

        except Exception as e:
            logger.error("Error capturing request: %s", e, exc_info=True)

    async def response(self, flow: http.HTTPFlow) -> None:
        try:
            response = flow.response
            if not response:
                return

            started = flow.request.timestamp_start
            ended = response.timestamp_end
            duration_ms = (ended - started) * 1000 if started and ended else None

            if self.tracer:
                self.tracer.finish_span(flow, response.status_code, duration_ms)

            logger.debug(
                "Captured response: %s (status: %d, duration: %.2fms, trace_id: %s)",
                flow.request.pretty_url,
                response.status_code,
                duration_ms or 0.0,
                flow.id,
            )

        except Exception as e:
            logger.error("Error capturing response: %s", e, exc_info=True)

    async def error(self, flow: http.HTTPFlow) -> None:
        try:
            error = flow.error
            if not error:
                return

            if self.tracer:
                self.tracer.finish_span_error(flow, str(error))

            logger.warning("Request error: %s (trace_id: %s)", error, flow.id)

        except Exception as e:
            logger.error("Error handling flow error: %s", e, exc_info=True)
