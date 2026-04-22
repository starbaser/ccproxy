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
            record.client_request = HttpSnapshot(
                headers=dict(flow.request.headers.items()),  # type: ignore[no-untyped-call]
                body=flow.request.content or b"",
                method=flow.request.method,
                url=flow.request.pretty_url,
            )

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

    async def responseheaders(self, flow: http.HTTPFlow) -> None:
        """Enable SSE streaming for all event-stream responses.

        For cross-provider transformed flows, wraps the stream with an SSE
        chunk transformer. For same-provider or unmatched flows, passes bytes
        through unchanged.
        """
        if not flow.response:
            return

        content_type = flow.response.headers.get("content-type", "")
        if "text/event-stream" not in content_type:
            return

        record = flow.metadata.get(InspectorMeta.RECORD)
        transform = getattr(record, "transform", None) if record else None

        if transform is not None and transform.is_streaming and transform.mode == "transform":
            from ccproxy.lightllm.dispatch import make_sse_transformer

            optional_params = {k: v for k, v in transform.request_data.items() if k != "messages"}
            try:
                transformer = make_sse_transformer(
                    transform.provider,
                    transform.model,
                    optional_params,
                )
                flow.response.stream = transformer
                flow.metadata["ccproxy.sse_transformer"] = transformer
            except Exception:
                logger.warning(
                    "Failed to create SSE transformer, falling back to passthrough",
                    exc_info=True,
                )
                flow.response.stream = True
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

            if response.status_code == 401 and flow.metadata.get("ccproxy.oauth_injected"):
                retried = await self._retry_with_refreshed_token(flow)
                if retried:
                    response = flow.response

            # Unwrap cloudcode-pa response envelope for Gemini redirect flows
            if response and response.status_code < 400:
                self._unwrap_gemini_response(flow, response)

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

    @staticmethod
    def _unwrap_gemini_response(flow: http.HTTPFlow, response: http.Response) -> None:
        """Strip cloudcode-pa's {response: {...}} envelope so the genai SDK sees standard format."""
        import json as _json

        record = flow.metadata.get(InspectorMeta.RECORD)
        transform = getattr(record, "transform", None) if record else None
        if not transform or transform.provider != "gemini" or transform.is_streaming:
            return
        try:
            body = _json.loads(response.content or b"{}")
            inner = body.get("response")
            if isinstance(inner, dict):
                response.content = _json.dumps(inner).encode()
        except (ValueError, TypeError):
            pass

    async def _retry_with_refreshed_token(self, flow: http.HTTPFlow) -> bool:
        import httpx

        from ccproxy.config import get_config

        provider = flow.metadata.get("ccproxy.oauth_provider", "")
        if not provider:
            return False

        config = get_config()
        new_token, changed = config.refresh_oauth_token(provider)
        if not changed or not new_token:
            logger.warning("OAuth 401 for provider '%s' — token unchanged, not retrying", provider)
            return False

        logger.info("OAuth 401 for provider '%s' — token refreshed, retrying request", provider)

        headers = dict(flow.request.headers)
        target_header = config.get_auth_header(provider)
        if target_header:
            headers[target_header] = new_token
        else:
            headers["authorization"] = f"Bearer {new_token}"

        headers.pop("x-ccproxy-oauth-injected", None)  # strip if somehow present from old flows

        client_kwargs: dict[str, Any] = {}
        if config.provider_timeout is not None:
            client_kwargs["timeout"] = httpx.Timeout(config.provider_timeout)
        else:
            client_kwargs["timeout"] = None  # Portkey parity: no wrapper, no budget

        async with httpx.AsyncClient(**client_kwargs) as client:
            retry_resp = await client.request(
                method=flow.request.method,
                url=flow.request.pretty_url,
                headers=headers,
                content=flow.request.content,
            )

        assert flow.response is not None
        flow.response.status_code = retry_resp.status_code
        flow.response.headers.clear()
        for key, value in retry_resp.headers.multi_items():
            flow.response.headers.add(key, value)
        flow.response.content = retry_resp.content
        return True

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
