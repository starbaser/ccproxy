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

# Required system message prefix for Claude Code OAuth tokens
CLAUDE_CODE_SYSTEM_PREFIX = "You are Claude Code, Anthropic's official CLI for Claude."

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
    ) -> None:
        """Initialize the addon.

        Args:
            storage: Storage backend for traces (None if no persistence)
            config: Mitmproxy configuration
            proxy_direction: Traffic direction (REVERSE for client->LiteLLM, FORWARD for LiteLLM->provider)
        """
        self.storage = storage
        self.config = config
        self.proxy_direction = proxy_direction

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

        Claude Code embeds session info in the metadata.user_id field with format:
        user_{hash}_account_{uuid}_session_{uuid}

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
        if not user_id or "_session_" not in user_id:
            return None

        # Parse: user_{hash}_account_{uuid}_session_{uuid}
        parts = user_id.split("_session_")
        if len(parts) == 2:
            return parts[1]
        return None

    def _inject_claude_code_identity(self, request: http.Request) -> None:
        """Inject Claude Code identity into system message for OAuth authentication.

        Anthropic's OAuth tokens are restricted to Claude Code. The API request
        must include a system message that starts with "You are Claude Code".
        This method prepends that required prefix to the system message.

        Args:
            request: HTTP request object
        """
        if not request.content:
            return

        try:
            body = json.loads(request.content)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        # Only process if this looks like an Anthropic messages request
        if "messages" not in body:
            return

        system = body.get("system")
        modified = False

        if system is None:
            # No system message - add the prefix as the system
            body["system"] = CLAUDE_CODE_SYSTEM_PREFIX
            modified = True
        elif isinstance(system, str):
            # String system message - prepend prefix if not already present
            if not system.startswith(CLAUDE_CODE_SYSTEM_PREFIX):
                body["system"] = f"{CLAUDE_CODE_SYSTEM_PREFIX}\n\n{system}"
                modified = True
        elif isinstance(system, list):
            # List of content blocks - insert prefix as first text block
            has_prefix = False
            for block in system:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "")
                    if text.startswith(CLAUDE_CODE_SYSTEM_PREFIX):
                        has_prefix = True
                        break
            if not has_prefix:
                system.insert(0, {"type": "text", "text": CLAUDE_CODE_SYSTEM_PREFIX})
                modified = True

        if modified:
            request.content = json.dumps(body).encode("utf-8")
            # Update content-length header
            request.headers["content-length"] = str(len(request.content))
            logger.info("Injected Claude Code identity into system message")

    def _fix_oauth_headers(self, flow: http.HTTPFlow) -> None:
        """Fix OAuth headers for Anthropic-type API requests from Claude Code clients.

        When using OAuth Bearer tokens, the x-api-key header must be removed so
        the provider uses the Authorization header instead. LiteLLM always sends
        x-api-key, so we remove it here at the HTTP layer.

        Detection: Claude CLI user-agent + /v1/messages endpoint = Anthropic-type
        This works for api.anthropic.com, api.z.ai, and other Claude Code providers.

        Args:
            flow: HTTP flow object
        """
        request = flow.request
        path = request.path.lower()

        # Detect Anthropic-type API by endpoint pattern
        is_messages_endpoint = "/v1/messages" in path

        if not is_messages_endpoint:
            return

        auth_header = request.headers.get("authorization", "")
        api_key = request.headers.get("x-api-key", "")
        host = request.pretty_host

        # Detect OAuth token: either Bearer header present, or OAuth token in x-api-key.
        # LiteLLM's Anthropic handler hardcodes x-api-key from api_key param,
        # so OAuth tokens (sk-ant-oat*) end up in x-api-key instead of Authorization.
        has_bearer = auth_header.lower().startswith("bearer ")
        has_oauth_in_apikey = api_key and api_key.startswith("sk-ant-oat")

        if not has_bearer and not has_oauth_in_apikey:
            return

        # If OAuth token is in x-api-key (LiteLLM converted it), move back to Authorization
        if has_oauth_in_apikey and not has_bearer:
            request.headers["authorization"] = f"Bearer {api_key}"
            del request.headers["x-api-key"]
            logger.info(
                "Restored OAuth token to Authorization header for %s%s",
                host,
                path,
            )
        elif has_bearer and "x-api-key" in request.headers:
            # Bearer present but also x-api-key - remove the duplicate
            del request.headers["x-api-key"]
            logger.info(
                "Removed x-api-key for OAuth request to %s%s",
                host,
                path,
            )

        # Ensure required beta headers are present for OAuth
        required_betas = ["oauth-2025-04-20", "claude-code-20250219", "interleaved-thinking-2025-05-14"]
        existing_beta = request.headers.get("anthropic-beta", "")
        existing_list = [b.strip() for b in existing_beta.split(",") if b.strip()]

        # Add missing required betas
        merged = list(dict.fromkeys(required_betas + existing_list))
        request.headers["anthropic-beta"] = ",".join(merged)
        logger.info("Set anthropic-beta: %s", request.headers["anthropic-beta"])

        # Inject Claude Code system message prefix for OAuth authentication
        # Anthropic requires system message to start with "You are Claude Code" for OAuth tokens
        self._inject_claude_code_identity(request)

        # Log request body for debugging (only in debug mode to avoid token exposure)
        if request.content and self.config.debug:
            body_preview = request.content[:3000].decode("utf-8", errors="replace")
            logger.info("Request body: %s", body_preview)

    async def request(self, flow: http.HTTPFlow) -> None:
        """Process request: fix OAuth headers and capture trace.

        Args:
            flow: HTTP flow object
        """
        # Fix OAuth headers at the HTTP layer AFTER LiteLLM constructs them.
        # LiteLLM's Anthropic handler hardcodes x-api-key from api_key in
        # get_anthropic_headers(), overriding extra_headers["x-api-key"]="".
        # The pipeline hook sets the token correctly, but only the MITM layer
        # can strip x-api-key after LiteLLM's final header construction.
        self._fix_oauth_headers(flow)

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
