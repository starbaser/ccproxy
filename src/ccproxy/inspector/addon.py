"""Mitmproxy addon for HTTP/HTTPS traffic capture.

Captures all HTTP traffic flowing through reverse, forward, and WireGuard
proxy listeners. Mode is detected per-flow via mitmproxy's multi-mode
`flow.client_conn.proxy_mode` attribute.
"""

from __future__ import annotations

import json
import logging
import os
from enum import IntEnum
from typing import TYPE_CHECKING, Any, cast

from mitmproxy import http

from ccproxy.config import InspectorConfig


class ProxyDirection(IntEnum):
    """Internal mode identifier for the mitmproxy listener that handled a flow.

    These integer values are stored in the database and must remain stable
    for backward compatibility with existing traces. They are not user-facing
    concepts — inspect mode activates all three modes as a single unit.
    """

    REVERSE = 0         # External HTTP client → LiteLLM (reverse mode listener)
    FORWARD = 1         # Reserved (was RegularMode / HTTPS_PROXY leg; no longer used)
    WIREGUARD_CLI = 2   # CLI client namespace → mitmweb → LiteLLM (WireGuard port A)
    WIREGUARD_GW = 3    # LiteLLM namespace → mitmweb → provider (WireGuard port B)


if TYPE_CHECKING:
    from ccproxy.inspector.telemetry import InspectorTracer

logger = logging.getLogger(__name__)

# Cached mode type references (avoid repeated imports per-flow)
_ReverseMode: type | None = None


def _get_reverse_mode_type() -> type:
    """Lazily resolve mitmproxy ReverseMode type."""
    global _ReverseMode
    if _ReverseMode is None:
        from mitmproxy.proxy.mode_specs import ReverseMode
        _ReverseMode = ReverseMode
    assert _ReverseMode is not None
    return _ReverseMode


class InspectorAddon:
    """Inspector addon for HTTP/HTTPS traffic capture and tracing."""

    def __init__(
        self,
        config: InspectorConfig,
        traffic_source: str | None = None,
        wg_cli_port: int | None = None,
        wg_gateway_port: int | None = None,
    ) -> None:
        """Initialize the addon.

        Args:
            config: Mitmproxy configuration
            traffic_source: Source label for traces (e.g. "shadow", "litellm")
            wg_cli_port: UDP port of the CLI-namespace WireGuard listener (INBOUND)
            wg_gateway_port: UDP port of the LiteLLM-namespace WireGuard listener (OUTBOUND)
        """
        self.config = config
        self.traffic_source = traffic_source
        self.tracer: InspectorTracer | None = None
        self._WireGuardMode: type | None = None
        self._forward_domains: set[str] = set(config.forward_domains)
        self._wg_cli_port = wg_cli_port
        self._wg_gateway_port = wg_gateway_port

    def set_tracer(self, tracer: InspectorTracer) -> None:
        """Set the OTel tracer for span emission.

        Args:
            tracer: Initialized InspectorTracer instance
        """
        self.tracer = tracer

    def _get_wg_listen_port(self, mode: Any) -> int | None:
        """Extract the UDP listening port from a WireGuardMode instance."""
        try:
            # WireGuardMode.listen_port or WireGuardMode.port
            for attr in ("listen_port", "port"):
                val = getattr(mode, attr, None)
                if isinstance(val, int):
                    return val
            # Fallback: parse from full_spec string (e.g. "wireguard@51820")
            full_spec: str = getattr(mode, "full_spec", "") or ""
            if "@" in full_spec:
                return int(full_spec.split("@")[-1])
        except (AttributeError, ValueError):
            pass
        return None

    def _get_direction(self, flow: http.HTTPFlow) -> ProxyDirection | None:
        """Detect traffic direction from which listener accepted this flow.

        Uses mitmproxy's multi-mode `flow.client_conn.proxy_mode` to determine
        which mitmproxy --mode listener accepted this flow.

        For WireGuard listeners, distinguishes CLI (port A) from gateway (port B)
        using the configured wg_cli_port and wg_gateway_port.

        Args:
            flow: HTTP flow object

        Returns:
            ProxyDirection or None if the flow's mode is unsupported
        """
        if not hasattr(flow, "client_conn") or flow.client_conn is None:
            return None  # Synthetic/replayed flows

        reverse_mode = _get_reverse_mode_type()
        mode = flow.client_conn.proxy_mode

        if isinstance(mode, reverse_mode):
            return ProxyDirection.REVERSE

        if self._WireGuardMode is None:
            from mitmproxy.proxy.mode_specs import WireGuardMode
            self._WireGuardMode = WireGuardMode

        if isinstance(mode, self._WireGuardMode):
            listen_port = self._get_wg_listen_port(mode)
            if listen_port is not None:
                if listen_port == self._wg_gateway_port:
                    return ProxyDirection.WIREGUARD_GW
                # CLI port or any unrecognised WG port treated as INBOUND
                return ProxyDirection.WIREGUARD_CLI
            # Port indeterminate — default to CLI (inbound)
            return ProxyDirection.WIREGUARD_CLI

        return None

    def _truncate_body(self, body: bytes | None) -> bytes | None:
        """Truncate body to configured max size.

        Args:
            body: Request or response body

        Returns:
            Truncated body or None if empty
        """
        if not body:
            return None

        if self.config.max_body_size > 0 and len(body) > self.config.max_body_size:
            return body[: self.config.max_body_size]

        return body

    def _serialize_headers(self, headers: Any) -> dict[str, str]:
        """Convert mitmproxy headers to dict.

        Args:
            headers: Mitmproxy headers object

        Returns:
            Dict of header name -> value
        """
        return {str(k): str(v) for k, v in headers.items()}

    def _extract_session_id(self, request: http.Request) -> str | None:
        """Extract session_id from Claude Code's metadata.user_id field.

        Claude Code embeds session info in the metadata.user_id field in one of two formats:
        - JSON object: {"device_id": "...", "account_uuid": "...", "session_id": "<uuid>"}
        - Legacy compound string: user_{hash}_account_{uuid}_session_{uuid}

        Args:
            request: HTTP request object

        Returns:
            Session ID string or None if not found/parseable
        """
        if not request.content:
            return None

        try:
            body = json.loads(request.content)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

        # Navigate to metadata.user_id
        metadata = body.get("metadata", {})
        if not isinstance(metadata, dict):
            return None

        user_id: str = metadata.get("user_id", "")
        if not user_id:
            return None

        # New format: JSON-encoded object with session_id key
        if user_id.startswith("{"):
            try:
                user_id_obj = json.loads(user_id)
                if isinstance(user_id_obj, dict) and user_id_obj.get("session_id"):
                    return cast(str, user_id_obj["session_id"])
            except (json.JSONDecodeError, TypeError):
                pass

        # Legacy format: user_{hash}_account_{uuid}_session_{uuid}
        if "_session_" in user_id:
            parts = user_id.split("_session_")
            if len(parts) == 2:
                return parts[1]

        return None

    def _maybe_forward(self, flow: http.HTTPFlow, direction: ProxyDirection, host: str) -> None:
        """Forward CLI WireGuard LLM API traffic to LiteLLM.

        Rewrites the request target so mitmproxy connects to LiteLLM instead
        of the original API domain. Only applies to WIREGUARD_CLI flows whose
        host is in the configured forward_domains list.

        WIREGUARD_GW flows (LiteLLM's outbound) are NOT forwarded — they pass
        through to the real provider to avoid an infinite loop.
        """
        if direction != ProxyDirection.WIREGUARD_CLI or host not in self._forward_domains:
            return
        litellm_port = int(os.environ.get("CCPROXY_LITELLM_PORT", "4000"))
        flow.request.headers["X-Forwarded-Host"] = host
        flow.request.host = "localhost"
        flow.request.port = litellm_port
        flow.request.scheme = "http"
        logger.info("Forwarding %s → localhost:%d", host, litellm_port)

    async def request(self, flow: http.HTTPFlow) -> None:
        """Process request: forward WireGuard LLM traffic and emit OTel span.

        Args:
            flow: HTTP flow object
        """
        direction = self._get_direction(flow)
        if direction is None:
            return

        # Tag flow metadata with direction string for route guard use
        if direction == ProxyDirection.WIREGUARD_GW:
            flow.metadata["ccproxy.direction"] = "outbound"
        elif direction in (ProxyDirection.REVERSE, ProxyDirection.WIREGUARD_CLI):
            flow.metadata["ccproxy.direction"] = "inbound"

        host = flow.request.pretty_host
        self._maybe_forward(flow, direction, host)

        try:
            request = flow.request
            session_id = self._extract_session_id(request)

            if self.tracer:
                self.tracer.start_span(flow, direction, host, request.method, session_id)

            logger.debug(
                "Captured request: %s %s (trace_id: %s, direction: %s, session: %s)",
                request.method,
                request.pretty_url,
                flow.id,
                direction.name.lower(),
                session_id or "none",
            )

        except Exception as e:
            logger.error("Error capturing request: %s", e, exc_info=True)

    async def response(self, flow: http.HTTPFlow) -> None:
        """Complete OTel span with response data.

        Args:
            flow: HTTP flow object
        """
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
        """Handle flow errors.

        Args:
            flow: HTTP flow object
        """
        try:
            error = flow.error
            if not error:
                return

            if self.tracer:
                self.tracer.finish_span_error(flow, str(error))

            logger.warning("Request error: %s (trace_id: %s)", error, flow.id)

        except Exception as e:
            logger.error("Error handling flow error: %s", e, exc_info=True)
