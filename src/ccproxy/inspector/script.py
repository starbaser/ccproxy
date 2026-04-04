"""Mitmproxy addon script loaded via the -s flag.

Loaded by mitmweb when ccproxy starts with --inspect. Captures HTTP/HTTPS
traffic via the InspectorAddon with OTel span emission. Traffic direction
(reverse, regular, wireguard) is detected per-flow via proxy_mode.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml

from ccproxy.config import InspectorConfig, OtelConfig
from ccproxy.inspector.addon import InspectorAddon

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


class InspectorScript:
    """Mitmproxy addon script that wraps InspectorAddon."""

    def __init__(self) -> None:
        self.config: InspectorConfig | None = None
        self.addon: InspectorAddon | None = None
        self.traffic_source: str | None = None
        self._initialized = False
        self._otel_config: OtelConfig | None = None

    def load(self, _loader: Any) -> None:
        """Called when addon is loaded by mitmproxy."""
        logger.info("Loading ccproxy inspector addon...")

        self.traffic_source = os.environ.get("CCPROXY_TRAFFIC_SOURCE") or None

        reverse_port = int(os.environ.get("CCPROXY_INSPECTOR_REVERSE_PORT", "4002"))
        forward_port = int(os.environ.get("CCPROXY_INSPECTOR_FORWARD_PORT", "4003"))
        litellm_port = int(os.environ.get("CCPROXY_LITELLM_PORT", "4001"))
        logger.info(
            "Inspector: reverse@%d → LiteLLM@%d, regular@%d",
            reverse_port,
            litellm_port,
            forward_port,
        )

        self.config = InspectorConfig(
            max_body_size=int(os.environ.get("CCPROXY_INSPECTOR_MAX_BODY_SIZE", "0")),
            debug=os.environ.get("CCPROXY_DEBUG", "false").lower() in ("true", "1", "yes"),
        )

        # Load OTel config from ccproxy.yaml
        config_dir = os.environ.get("CCPROXY_CONFIG_DIR") or str(Path.home() / ".ccproxy")
        ccproxy_yaml = Path(config_dir) / "ccproxy.yaml"
        if ccproxy_yaml.exists():
            with ccproxy_yaml.open() as f:
                data = yaml.safe_load(f) or {}
            otel_data = data.get("ccproxy", {}).get("otel", {})
            self._otel_config = OtelConfig(**otel_data)
        else:
            self._otel_config = OtelConfig()

    async def running(self) -> None:
        """Called when mitmproxy is fully running — async context available."""
        if self._initialized:
            return

        assert self.config is not None

        self.addon = InspectorAddon(
            config=self.config,
            traffic_source=self.traffic_source,
        )

        # Initialize OTel tracer
        assert self._otel_config is not None
        try:
            from ccproxy.inspector.telemetry import InspectorTracer

            tracer = InspectorTracer(
                enabled=self._otel_config.enabled,
                otlp_endpoint=self._otel_config.endpoint,
                service_name=self._otel_config.service_name,
            )
            self.addon.set_tracer(tracer)
            if self._otel_config.enabled:
                logger.info("OTel tracing enabled, exporting to %s", self._otel_config.endpoint)
        except Exception as e:
            logger.warning("Failed to initialize OTel tracer: %s", e)

        self._initialized = True
        logger.info(
            "Inspector addon initialized (otel: %s)",
            "enabled" if self._otel_config.enabled else "disabled",
        )

    async def done(self) -> None:
        """Called when mitmproxy shuts down."""
        logger.info("Shutting down inspector addon...")

        try:
            from ccproxy.inspector.telemetry import shutdown_tracer

            shutdown_tracer()
        except Exception as e:
            logger.warning("Error shutting down OTel tracer: %s", e)

        logger.info("Inspector addon shutdown complete")

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


addons = [InspectorScript()]
