"""Mitmproxy addon for HTTP/HTTPS traffic capture."""

import logging
from datetime import UTC, datetime
from typing import Any

from mitmproxy import http
from prisma import Base64, Json

from ccproxy.config import MitmConfig
from ccproxy.mitm.storage import TraceStorage

logger = logging.getLogger(__name__)


class CCProxyMitmAddon:
    """Mitmproxy addon that captures all HTTP/HTTPS traffic and stores in PostgreSQL."""

    def __init__(self, storage: TraceStorage, config: MitmConfig) -> None:
        """Initialize the addon.

        Args:
            storage: Storage backend for traces
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

        # LLM API patterns
        llm_patterns = [
            "api.anthropic.com",
            "api.openai.com",
            "generativelanguage.googleapis.com",
            "api.cohere.ai",
            "bedrock",
            "azure.com/openai",
        ]

        for pattern in llm_patterns:
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

    def _truncate_body(self, body: bytes | None) -> Base64 | None:
        """Truncate body to configured max size and encode as Base64.

        Args:
            body: Request or response body

        Returns:
            Base64-encoded truncated body or None if empty
        """
        if not body:
            return None

        # Truncate if needed
        if len(body) > self.config.max_body_size:
            body = body[: self.config.max_body_size]

        # Encode as Base64 for Prisma
        return Base64.encode(body)

    def _serialize_headers(self, headers: Any) -> Json:
        """Convert mitmproxy headers to Prisma Json object.

        Args:
            headers: Mitmproxy headers object

        Returns:
            Prisma Json object containing header name -> value mapping
        """
        # Convert headers to dict and ensure all values are strings
        result = {}
        for key, value in headers.items():
            # Ensure key and value are properly typed
            result[str(key)] = str(value)
        return Json(result)

    async def request(self, flow: http.HTTPFlow) -> None:
        """Capture request and create initial trace.

        Args:
            flow: HTTP flow object
        """
        try:
            # Extract request data
            request = flow.request
            host = request.pretty_host
            path = request.path
            traffic_type = self._classify_traffic(host, path)

            # Prepare trace data
            trace_data = {
                "trace_id": flow.id,
                "traffic_type": traffic_type,
                "method": request.method,
                "url": request.pretty_url,
                "host": host,
                "path": path,
                "request_headers": self._serialize_headers(request.headers),
                "request_body": self._truncate_body(request.content),
                "start_time": datetime.now(UTC),
            }

            # Create trace
            await self.storage.create_trace(trace_data)

            logger.debug("Captured request: %s %s (trace_id: %s)", request.method, request.pretty_url, flow.id)

        except Exception as e:
            logger.error("Error capturing request: %s", e, exc_info=True)

    async def response(self, flow: http.HTTPFlow) -> None:
        """Complete trace with response data.

        Args:
            flow: HTTP flow object
        """
        try:
            # Extract response data
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
                "response_body": self._truncate_body(response.content),
                "duration_ms": duration_ms,
                "end_time": datetime.now(UTC),
            }

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
        try:
            # Extract error information
            error = flow.error
            if not error:
                return

            # Prepare error data
            error_data = {
                "status_code": 0,  # Indicate error state
                "response_headers": Json({}),
                "error_message": str(error),
                "end_time": datetime.now(UTC),
            }

            # Complete trace with error
            await self.storage.complete_trace(flow.id, error_data)

            logger.warning("Request error: %s (trace_id: %s)", error, flow.id)

        except Exception as e:
            logger.error("Error handling flow error: %s", e, exc_info=True)
