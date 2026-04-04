"""Mitmproxy addon script for use with mitmdump/mitmweb -s flag.

This script is loaded by mitmproxy to capture HTTP/HTTPS traffic and store
traces in PostgreSQL via the CCProxyMitmAddon.

In combined mode, mitmproxy runs both reverse and forward proxy listeners
in a single process. Direction is detected per-flow via proxy_mode.

Usage:
    mitmdump --mode reverse:http://localhost:{litellm_port}@{reverse_port} \
             --mode regular@{forward_port} -s script.py
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from ccproxy.config import InspectConfig
from ccproxy.mitm.addon import CCProxyMitmAddon

if TYPE_CHECKING:
    from ccproxy.mitm.storage import TraceStorage

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


class CCProxyScript:
    """Mitmproxy addon script that wraps CCProxyMitmAddon."""

    def __init__(self) -> None:
        self.config: InspectConfig | None = None
        self.storage: TraceStorage | None = None
        self.addon: CCProxyMitmAddon | None = None
        self.traffic_source: str | None = None
        self._initialized = False

        # OTel configuration
        self._otel_enabled = False
        self._otel_endpoint = "http://localhost:4317"
        self._otel_service_name = "ccproxy-mitm"

    def load(self, _loader: Any) -> None:
        """Called when addon is loaded by mitmproxy."""
        logger.info("Loading CCProxy mitmproxy addon...")

        self.traffic_source = os.environ.get("CCPROXY_TRAFFIC_SOURCE") or None

        reverse_port = int(os.environ.get("CCPROXY_MITM_REVERSE_PORT", "4002"))
        forward_port = int(os.environ.get("CCPROXY_MITM_FORWARD_PORT", "4003"))
        litellm_port = int(os.environ.get("CCPROXY_LITELLM_PORT", "4001"))
        logger.info(
            "MITM mode: combined, reverse@%d → LiteLLM@%d, forward@%d",
            reverse_port,
            litellm_port,
            forward_port,
        )

        self.config = InspectConfig(
            upstream_proxy=f"http://localhost:{litellm_port}",
            max_body_size=int(os.environ.get("CCPROXY_MITM_MAX_BODY_SIZE", "0")),
            debug=os.environ.get("CCPROXY_DEBUG", "false").lower() in ("true", "1", "yes"),
        )

        # OTel configuration from env vars
        self._otel_enabled = os.environ.get("CCPROXY_OTEL_ENABLED", "false").lower() in ("true", "1", "yes")
        self._otel_endpoint = os.environ.get("CCPROXY_OTEL_ENDPOINT", "http://localhost:4317")
        self._otel_service_name = os.environ.get("CCPROXY_OTEL_SERVICE_NAME", "ccproxy-mitm")

        database_url = os.environ.get("CCPROXY_DATABASE_URL") or os.environ.get("DATABASE_URL")
        if not database_url:
            logger.warning("CCPROXY_DATABASE_URL not set - traces will not be persisted")
            return

        try:
            from ccproxy.mitm.storage import TraceStorage

            self.storage = TraceStorage(database_url)
            logger.info("Storage configured (will connect on first request)")
        except Exception as e:
            logger.warning("Failed to initialize storage: %s - traces will not be persisted", e)

    async def running(self) -> None:
        """Called when mitmproxy is fully running — async context available."""
        if self._initialized:
            return

        assert self.config is not None

        if self.storage:
            try:
                await self.storage.connect()
            except Exception as e:
                logger.warning("Failed to connect storage: %s", e)
                self.storage = None

        self.addon = CCProxyMitmAddon(
            storage=self.storage,
            config=self.config,
            traffic_source=self.traffic_source,
        )

        # Initialize OTel tracer
        try:
            from ccproxy.mitm.telemetry import MitmTracer

            tracer = MitmTracer(
                enabled=self._otel_enabled,
                otlp_endpoint=self._otel_endpoint,
                service_name=self._otel_service_name,
            )
            self.addon.set_tracer(tracer)
            if self._otel_enabled:
                logger.info("OTel tracing enabled, exporting to %s", self._otel_endpoint)
        except Exception as e:
            logger.warning("Failed to initialize OTel tracer: %s", e)

        self._initialized = True
        logger.info(
            "CCProxy addon initialized (storage: %s, otel: %s)",
            "connected" if self.storage else "disabled",
            "enabled" if self._otel_enabled else "disabled",
        )

    async def done(self) -> None:
        """Called when mitmproxy shuts down."""
        logger.info("Shutting down CCProxy addon...")
        if self.storage:
            await self.storage.disconnect()

        try:
            from ccproxy.mitm.telemetry import shutdown_tracer

            shutdown_tracer()
        except Exception as e:
            logger.warning("Error shutting down OTel tracer: %s", e)

        logger.info("CCProxy addon shutdown complete")

    async def request(self, flow: Any) -> None:
        """Handle HTTP request."""
        if self.addon:
            await self.addon.request(flow)

    async def response(self, flow: Any) -> None:
        """Handle HTTP response."""
        if self.addon:
            await self.addon.response(flow)

    async def error(self, flow: Any) -> None:
        """Handle flow error."""
        if self.addon:
            await self.addon.error(flow)


addons = [CCProxyScript()]
