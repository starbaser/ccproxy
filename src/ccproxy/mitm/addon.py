"""Mitmproxy addon for HTTP/HTTPS traffic capture.

In reverse proxy mode, mitmproxy handles forwarding automatically.
This addon focuses on logging/storage of traffic.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from enum import IntEnum
from typing import TYPE_CHECKING, Any

from mitmproxy import http

from ccproxy.config import MitmConfig


class ProxyDirection(IntEnum):
    """Proxy direction for traffic classification."""

    REVERSE = 0  # Client -> LiteLLM (inbound)
    FORWARD = 1  # LiteLLM -> Provider (outbound)


if TYPE_CHECKING:
    from ccproxy.mitm.storage import TraceStorage

logger = logging.getLogger(__name__)


class CCProxyMitmAddon:
    """Mitmproxy addon that captures all HTTP/HTTPS traffic and stores in PostgreSQL."""

    def __init__(
        self,
        storage: TraceStorage | None,
        config: MitmConfig,
        proxy_direction: ProxyDirection = ProxyDirection.REVERSE,
        traffic_source: str | None = None,
    ) -> None:
        """Initialize the addon.

        Args:
            storage: Storage backend for traces (None if no persistence)
            config: Mitmproxy configuration
            proxy_direction: Traffic direction (REVERSE for client->LiteLLM, FORWARD for LiteLLM->provider)
            traffic_source: Source label for traces (e.g. "shadow", "litellm")
        """
        self.storage = storage
        self.config = config
        self.proxy_direction = proxy_direction
        self.traffic_source = traffic_source

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

        user_id = metadata.get("user_id", "")
        if not user_id:
            return None

        # New format: JSON-encoded object with session_id key
        if user_id.startswith("{"):
            try:
                user_id_obj = json.loads(user_id)
                if isinstance(user_id_obj, dict) and user_id_obj.get("session_id"):
                    return user_id_obj["session_id"]
            except (json.JSONDecodeError, TypeError):
                pass

        # Legacy format: user_{hash}_account_{uuid}_session_{uuid}
        if "_session_" in user_id:
            parts = user_id.split("_session_")
            if len(parts) == 2:
                return parts[1]

        return None

    async def request(self, flow: http.HTTPFlow) -> None:
        """Process request: capture trace data.

        Args:
            flow: HTTP flow object
        """
        # Skip trace capture if no storage configured
        if self.storage is None:
            return

        try:
            request = flow.request
            host = request.pretty_host

            # Filter based on proxy direction
            if self.proxy_direction == ProxyDirection.REVERSE:
                # Reverse: only trace client→LiteLLM traffic (localhost)
                if host.lower() not in ("localhost", "127.0.0.1", "::1"):
                    return
            else:
                # Forward: only trace LiteLLM→provider traffic (external APIs)
                if host.lower() in ("localhost", "127.0.0.1", "::1"):
                    return

            path = request.path

            # Extract session_id from request body metadata
            session_id = self._extract_session_id(request)

            trace_data = {
                "trace_id": flow.id,
                "proxy_direction": self.proxy_direction.value,
                "session_id": session_id,
                "traffic_source": self.traffic_source,
                "method": request.method,
                "url": request.pretty_url,
                "host": host,
                "path": path,
                "request_headers": self._serialize_headers(request.headers),
                "start_time": datetime.now(UTC),
            }

            # Add body fields if capture_bodies is enabled
            if self.config.capture_bodies:
                logger.info(
                    "max_body_size=%d, content_len=%d",
                    self.config.max_body_size,
                    len(request.content) if request.content else 0,
                )
                trace_data["request_body"] = self._truncate_body(request.content)
                trace_data["request_body_size"] = len(request.content) if request.content else 0
                trace_data["request_content_type"] = request.headers.get("content-type", "")

            await self.storage.create_trace(trace_data)
            direction_str = "reverse" if self.proxy_direction == ProxyDirection.REVERSE else "forward"
            logger.debug(
                "Captured request: %s %s (trace_id: %s, direction: %s, session: %s)",
                request.method,
                request.pretty_url,
                flow.id,
                direction_str,
                session_id or "none",
            )

        except Exception as e:
            logger.error("Error capturing request: %s", e, exc_info=True)

    async def response(self, flow: http.HTTPFlow) -> None:
        """Complete trace with response data.

        Args:
            flow: HTTP flow object
        """
        if self.storage is None:
            return

        try:
            response = flow.response
            if not response:
                return

            # Calculate duration
            started = flow.request.timestamp_start
            ended = response.timestamp_end
            duration_ms = (ended - started) * 1000 if started and ended else None

            # Prepare response data
            response_data = {
                "status_code": response.status_code,
                "response_headers": self._serialize_headers(response.headers),
                "duration_ms": duration_ms,
                "end_time": datetime.now(UTC),
            }

            # Add body fields if capture_bodies is enabled
            if self.config.capture_bodies:
                response_data["response_body"] = self._truncate_body(response.content)
                response_data["response_body_size"] = len(response.content) if response.content else 0
                response_data["response_content_type"] = response.headers.get("content-type", "")

            # Complete trace
            await self.storage.complete_trace(flow.id, response_data)

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
        if self.storage is None:
            return

        try:
            error = flow.error
            if not error:
                return

            # Prepare error data
            error_data = {
                "status_code": 0,  # Indicate error state
                "response_headers": {},
                "error_message": str(error),
                "end_time": datetime.now(UTC),
            }

            # Complete trace with error
            await self.storage.complete_trace(flow.id, error_data)

            logger.warning("Request error: %s (trace_id: %s)", error, flow.id)

        except Exception as e:
            logger.error("Error handling flow error: %s", e, exc_info=True)
