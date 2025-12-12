"""Mitmproxy addon for HTTP/HTTPS traffic capture.

In reverse proxy mode, mitmproxy handles forwarding automatically.
This addon focuses on logging/storage of traffic.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from mitmproxy import http

from ccproxy.config import MitmConfig

if TYPE_CHECKING:
    from ccproxy.mitm.storage import TraceStorage

logger = logging.getLogger(__name__)


class CCProxyMitmAddon:
    """Mitmproxy addon that captures all HTTP/HTTPS traffic and stores in PostgreSQL."""

    def __init__(
        self,
        storage: TraceStorage | None,
        config: MitmConfig,
    ) -> None:
        """Initialize the addon.

        Args:
            storage: Storage backend for traces (None if no persistence)
            config: Mitmproxy configuration
        """
        self.storage = storage
        self.config = config

    def _classify_traffic(self, host: str, path: str) -> str:
        """Classify traffic type based on host and path patterns.

        Args:
            host: Request host
            path: Request path

        Returns:
            Traffic type: llm, mcp, web, or other
        """
        host_lower = host.lower()
        path_lower = path.lower()

        # Check LLM patterns from config
        for pattern in self.config.llm_hosts:
            if pattern in host_lower:
                return "llm"

        # MCP patterns (Model Context Protocol)
        if "mcp" in host_lower or "mcp" in path_lower:
            return "mcp"

        # Check if localhost/127.0.0.1 (likely proxy traffic)
        if host_lower in ("localhost", "127.0.0.1", "::1"):
            return "other"

        # Everything else is web traffic
        return "web"

    def _truncate_body(self, body: bytes | None) -> bytes | None:
        """Truncate body to configured max size.

        Args:
            body: Request or response body

        Returns:
            Truncated body or None if empty
        """
        if not body:
            return None

        if len(body) > self.config.max_body_size:
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

    async def request(self, flow: http.HTTPFlow) -> None:
        """Capture request and create initial trace.

        Note: In reverse proxy mode, mitmproxy handles forwarding automatically.
        This method only captures the request for logging/storage.

        Args:
            flow: HTTP flow object
        """
        # Skip trace capture if no storage configured
        if self.storage is None:
            return

        try:
            request = flow.request
            host = request.pretty_host
            path = request.path
            traffic_type = self._classify_traffic(host, path)

            trace_data = {
                "trace_id": flow.id,
                "traffic_type": traffic_type,
                "method": request.method,
                "url": request.pretty_url,
                "host": host,
                "path": path,
                "request_headers": self._serialize_headers(request.headers),
                "start_time": datetime.now(UTC),
            }

            # Add body fields if capture_bodies is enabled
            if self.config.capture_bodies:
                trace_data["request_body"] = self._truncate_body(request.content)
                trace_data["request_body_size"] = len(request.content) if request.content else 0
                trace_data["request_content_type"] = request.headers.get("content-type", "")

            await self.storage.create_trace(trace_data)
            logger.debug("Captured request: %s %s (trace_id: %s)", request.method, request.pretty_url, flow.id)

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
