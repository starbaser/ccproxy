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
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Literal, cast

from mitmproxy import command, flow, http
from mitmproxy.proxy.mode_specs import ReverseMode, WireGuardMode

from ccproxy.flows.store import (
    FLOW_ID_HEADER,
    HttpSnapshot,
    InspectorMeta,
    create_flow_record,
    get_flow_record,
)
from ccproxy.utils import extract_first_user_text, parse_session_id

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

    @staticmethod
    def _extract_session_id_from_body(body: dict[str, Any] | None) -> str | None:
        """Extract session_id from Claude Code's metadata.user_id field."""
        if not body:
            return None

        metadata = body.get("metadata", {})
        if not isinstance(metadata, dict):
            return None

        user_id = str(metadata.get("user_id", ""))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        if not user_id:
            return None

        return parse_session_id(user_id)

    @staticmethod
    def _enrich_record_with_conversation_ids(flow: http.HTTPFlow, record: Any) -> None:
        """Compute ``conversation_id`` and ``system_prompt_sha`` from the JSON body.

        Quietly no-ops on non-JSON bodies, parse errors, or missing fields.
        Stashes the values on both ``flow.metadata`` (for cross-addon access)
        and the record (for typed Python access).
        """
        import hashlib

        content_type = flow.request.headers.get("content-type", "").lower()
        if "application/json" not in content_type:
            return
        body = record.parsed_request_body(flow.request.content)
        if body is None:
            return

        messages = body.get("messages")
        if isinstance(messages, list):
            text = extract_first_user_text(messages=messages)
            # Empty first-text-block messages all collide on the same SHA otherwise;
            # fall back to flow.id so distinct requests stay distinguishable.
            seed = text or f"flow:{flow.id}"
            conv_id = hashlib.sha256(seed.encode()).hexdigest()[:12]
            record.conversation_id = conv_id
            flow.metadata["ccproxy.conversation_id"] = conv_id

        system = body.get("system")
        if system is not None:
            serialized = json.dumps(system, sort_keys=True, default=str)
            sys_sha = hashlib.sha256(serialized.encode()).hexdigest()[:12]
            record.system_prompt_sha = sys_sha
            flow.metadata["ccproxy.system_prompt_sha"] = sys_sha

    async def requestheaders(self, flow: http.HTTPFlow) -> None:
        """Disable request streaming for reverse proxy flows.

        stream_large_bodies is disabled by default, but if re-enabled via
        YAML override, reverse proxy flows still need the full body buffered
        for the transform handler. WireGuard flows already have correct
        destinations and can stream safely.
        """
        if isinstance(flow.client_conn.proxy_mode, ReverseMode) and flow.request.stream:
            flow.request.stream = False

    async def request(self, flow: http.HTTPFlow) -> None:
        direction = self._get_direction(flow)
        if direction is None:
            return

        headers = cast("dict[str, Any]", flow.request.headers)
        record = get_flow_record(headers.get(FLOW_ID_HEADER))

        if record is None:
            flow_id, record = create_flow_record(direction)
            flow.request.headers[FLOW_ID_HEADER] = flow_id
            record.client_request = HttpSnapshot(
                headers=dict(flow.request.headers.items()),  # type: ignore[no-untyped-call]
                body=flow.request.content or b"",
                method=flow.request.method,
                url=flow.request.pretty_url,
            )
            self._enrich_record_with_conversation_ids(flow, record)

        flow.metadata[InspectorMeta.DIRECTION] = direction
        flow.metadata[InspectorMeta.RECORD] = record

        host = flow.request.pretty_host

        try:
            body = record.parsed_request_body(flow.request.content)
            session_id = self._extract_session_id_from_body(body)

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

    async def responseheaders(self, flow: http.HTTPFlow) -> None:
        """Enable SSE streaming for all event-stream responses.

        For cross-provider transformed flows, wraps the stream with an SSE
        chunk transformer. For Gemini redirect-mode streaming flows this
        returns without touching ``flow.response.stream`` so the downstream
        :class:`~ccproxy.inspector.gemini_addon.GeminiAddon` can install its
        envelope-unwrap stream (or skip it during a capacity-fallback retry).
        For same-provider or unmatched flows, passes bytes through unchanged.
        """
        if not flow.response:
            return

        content_type = flow.response.headers.get("content-type", "")
        if "text/event-stream" not in content_type:
            return

        record = flow.metadata.get(InspectorMeta.RECORD)
        transform = getattr(record, "transform", None) if record else None

        if transform is not None and transform.is_streaming and transform.mode == "transform":
            # deferred: heavy LiteLLM provider chain
            from ccproxy.lightllm.dispatch import make_sse_transformer

            optional_params = {k: v for k, v in transform.request_data.items() if k != "messages"}
            try:
                sse_transformer = make_sse_transformer(
                    transform.provider,
                    transform.model,
                    optional_params,
                )
                flow.response.stream = sse_transformer
                flow.metadata["ccproxy.sse_transformer"] = sse_transformer
            except Exception:
                logger.warning(
                    "Failed to create SSE transformer, falling back to passthrough",
                    exc_info=True,
                )
                flow.response.stream = True
        elif transform is not None and transform.is_streaming and transform.provider == "gemini":
            # Capacity-fallback defer branch (Wave 6 absorbs this into GeminiAddon).
            # GeminiAddon.responseheaders installs EnvelopeUnwrapStream when this
            # branch returns without setting the stream — see its docstring.
            from ccproxy.hooks.gemini_capacity_fallback import (
                _CAPACITY_STATUS_CODES,
                has_fallback_configured,
            )

            if flow.response.status_code in _CAPACITY_STATUS_CODES and has_fallback_configured():
                logger.info(
                    "Deferring stream setup for %d to allow capacity fallback retry (flow=%s)",
                    flow.response.status_code,
                    flow.id,
                )
            return
        else:
            flow.response.stream = True

    async def response(self, flow: http.HTTPFlow) -> None:
        try:
            response = flow.response
            if not response:
                return

            record = flow.metadata.get(InspectorMeta.RECORD)
            if record is not None:
                transformer = flow.metadata.pop("ccproxy.sse_transformer", None)
                raw_body = getattr(transformer, "raw_body", None) if transformer else None
                if raw_body is not None:
                    record.provider_response = HttpSnapshot(
                        headers=dict(response.headers.items()),  # type: ignore[no-untyped-call]
                        body=raw_body,
                        status_code=response.status_code,
                    )
                elif response.content is not None:
                    record.provider_response = HttpSnapshot(
                        headers=dict(response.headers.items()),  # type: ignore[no-untyped-call]
                        body=response.content,
                        status_code=response.status_code,
                    )

            if response and flow.metadata.get("ccproxy.oauth_provider") == "gemini":
                from ccproxy.hooks.gemini_capacity_fallback import (
                    _CAPACITY_STATUS_CODES,
                    try_fallback_models,
                )

                if response.status_code in _CAPACITY_STATUS_CODES and await try_fallback_models(flow):
                    response = flow.response

            started = flow.request.timestamp_start
            ended = response.timestamp_end if response else None
            duration_ms = (ended - started) * 1000 if started and ended else None

            if self.tracer and response:
                self.tracer.finish_span(flow, response.status_code, duration_ms)

            logger.debug(
                "Captured response: %s (status: %d, duration: %.2fms, trace_id: %s)",
                flow.request.pretty_url,
                response.status_code if response else 0,
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

            err_msg = str(error)
            response = flow.response
            is_client_disconnect = "Client disconnected" in err_msg

            if self.tracer:
                if is_client_disconnect and response is not None:
                    started = flow.request.timestamp_start
                    ended = response.timestamp_end
                    duration_ms = (ended - started) * 1000 if started and ended else None
                    self.tracer.finish_span_client_disconnect(
                        flow,
                        response.status_code,
                        duration_ms,
                    )
                else:
                    self.tracer.finish_span_error(flow, err_msg)

            if is_client_disconnect:
                logger.info(
                    "Client disconnected mid-request (trace_id: %s, status: %s)",
                    flow.id,
                    response.status_code if response else "n/a",
                )
            else:
                logger.warning("Request error: %s (trace_id: %s)", err_msg, flow.id)

        except Exception as e:
            logger.error("Error handling flow error: %s", e, exc_info=True)

    @command.command("ccproxy.clientrequest")  # type: ignore[untyped-decorator]
    def get_client_request(self, flows: Sequence[flow.Flow]) -> str:
        """Return the pre-pipeline client request for each flow as JSON."""
        results: list[dict[str, object]] = []
        for f in flows:
            record = f.metadata.get(InspectorMeta.RECORD)
            cr = getattr(record, "client_request", None) if record else None
            if cr is None:
                results.append({"flow_id": f.id, "error": "no snapshot"})
                continue
            body_parsed: object
            try:
                body_parsed = json.loads(cr.body) if cr.body else None
            except Exception:
                body_parsed = cr.body.decode("utf-8", errors="replace")
            results.append(
                {
                    "flow_id": f.id,
                    "method": cr.method,
                    "url": cr.url,
                    "headers": cr.headers,
                    "body": body_parsed,
                }
            )
        return json.dumps(results)
