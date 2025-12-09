"""Mitmproxy addon script for use with mitmdump -s flag.

This script is loaded by mitmdump to capture HTTP/HTTPS traffic and store
traces in PostgreSQL via the CCProxyMitmAddon.

Usage:
    mitmdump --mode upstream:http://localhost:4000 -s script.py
"""

import logging
import os
from typing import Any

from ccproxy.config import MitmConfig
from ccproxy.mitm.addon import CCProxyMitmAddon
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
        self.config: MitmConfig | None = None
        self.storage: TraceStorage | None = None
        self.addon: CCProxyMitmAddon | None = None
        self._initialized = False

    def load(self, loader: Any) -> None:  # noqa: ANN401
        """Called when addon is loaded by mitmproxy."""
        logger.info("Loading CCProxy mitmproxy addon...")

        # Get configuration from environment or use defaults
        self.config = MitmConfig(
            port=int(os.environ.get("CCPROXY_MITM_PORT", "8081")),
            upstream_proxy=os.environ.get("CCPROXY_MITM_UPSTREAM", "http://localhost:4000"),
            max_body_size=int(os.environ.get("CCPROXY_MITM_MAX_BODY_SIZE", "65536")),
        )

        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            logger.warning("DATABASE_URL not set - traces will not be persisted")
            self._initialized = True
            return

        self.storage = TraceStorage(database_url)
        logger.info("CCProxy addon configured (storage will connect on first request)")

    async def running(self) -> None:
        """Called when mitmproxy is fully running - async context available."""
        if self.storage and not self._initialized:
            try:
                await self.storage.connect()
                self.addon = CCProxyMitmAddon(self.storage, self.config)  # type: ignore[arg-type]
                self._initialized = True
                logger.info("CCProxy addon initialized successfully")
            except Exception as e:
                logger.error("Failed to connect storage: %s", e)

    async def done(self) -> None:
        """Called when mitmproxy shuts down."""
        if self.storage:
            logger.info("Shutting down CCProxy addon...")
            await self.storage.disconnect()
            logger.info("CCProxy addon shutdown complete")

    async def request(self, flow: Any) -> None:  # noqa: ANN401
        """Handle HTTP request."""
        if self.addon:
            await self.addon.request(flow)

    async def response(self, flow: Any) -> None:  # noqa: ANN401
        """Handle HTTP response."""
        if self.addon:
            await self.addon.response(flow)

    async def error(self, flow: Any) -> None:  # noqa: ANN401
        """Handle flow error."""
        if self.addon:
            await self.addon.error(flow)


addons = [CCProxyScript()]
