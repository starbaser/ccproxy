"""Inspector addon for HTTP/HTTPS traffic capture with ccproxy

Captures all HTTP traffic flowing through reverse and WireGuard proxy
listeners. All flows are treated as inbound — there is no outbound
direction concept. The three-stage addon chain (inbound → transform →
outbound) handles OAuth injection, lightllm routing, and last-mile
fixups respectively.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Literal, cast

from mitmproxy import http
from mitmproxy.proxy.mode_specs import ReverseMode, WireGuardMode

from ccproxy.inspector.flow_store import (
    FLOW_ID_HEADER,
    FlowRecord,
    InspectorMeta,
    create_flow_record,
    get_flow_record,
)
from ccproxy.utils import parse_session_id

if TYPE_CHECKING:
    from ccproxy.inspector.telemetry import InspectorTracer

logger = logging.getLogger(__name__)

Direction = Literal["inbound"]


class InspectorAddon:
    """Inspector addon for HTTP/HTTPS traffic capture and tracing."""

    def __init__(
        self,
        traffic_source: str | None = None,
        wg_cli_port: int | None = None,
    ) -> None:
        self.traffic_source = traffic_source
        self.tracer: InspectorTracer | None = None
        self._wg_cli_port = wg_cli_port

    def set_tracer(self, tracer: InspectorTracer) -> None:
        self.tracer = tracer

    def _get_direction(self, flow: http.HTTPFlow) -> Direction | None:
        """Detect traffic direction from the proxy mode that accepted this flow.

        All reverse proxy and WireGuard flows are inbound. Returns None for
        unrecognized modes (skipped).
        """
        mode = flow.client_conn.proxy_mode

        if isinstance(mode, (ReverseMode, WireGuardMode)):
            return "inbound"

        return None

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

        return parse_session_id(user_id)

    async def request(self, flow: http.HTTPFlow) -> None:
        direction = self._get_direction(flow)
        if direction is None:
            return

        headers = cast("dict[str, Any]", flow.request.headers)
        record = get_flow_record(headers.get(FLOW_ID_HEADER))

        if record is None:
            flow_id, record = create_flow_record(direction)
            flow.request.headers[FLOW_ID_HEADER] = flow_id
            record.original_headers = dict(flow.request.headers.items())  # type: ignore[no-untyped-call]

        flow.metadata[InspectorMeta.DIRECTION] = direction
        flow.metadata[InspectorMeta.RECORD] = record

        host = flow.request.pretty_host

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
