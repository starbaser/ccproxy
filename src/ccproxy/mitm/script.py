"""Mitmproxy addon script for use with mitmdump -s flag.

This script is loaded by mitmdump to capture HTTP/HTTPS traffic and store
traces in PostgreSQL via the CCProxyMitmAddon.

In reverse proxy mode, mitmproxy handles forwarding to LiteLLM automatically.
This addon focuses on logging/storage of traffic.

Usage:
    mitmdump --mode reverse:http://localhost:{litellm_port} -s script.py
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from ccproxy.config import MitmConfig
from ccproxy.mitm.addon import CCProxyMitmAddon, ProxyDirection

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
        self.config: MitmConfig | None = None
        self.storage: TraceStorage | None = None
        self.addon: CCProxyMitmAddon | None = None
        self.proxy_direction: ProxyDirection = ProxyDirection.REVERSE
        self._initialized = False

    def load(self, loader: Any) -> None:  # noqa: ANN401
        """Called when addon is loaded by mitmproxy."""
        logger.info("Loading CCProxy mitmproxy addon...")

        # Get configuration from environment
        mitm_port = int(os.environ.get("CCPROXY_MITM_PORT", "4000"))
        litellm_port = int(os.environ.get("CCPROXY_LITELLM_PORT", "4001"))

        # Determine proxy direction from environment
        mode_str = os.environ.get("CCPROXY_MITM_MODE", "reverse").lower()
        self.proxy_direction = ProxyDirection.FORWARD if mode_str == "forward" else ProxyDirection.REVERSE

        self.config = MitmConfig(
            port=mitm_port,
            upstream_proxy=f"http://localhost:{litellm_port}",
            max_body_size=int(os.environ.get("CCPROXY_MITM_MAX_BODY_SIZE", "0")),
            debug=os.environ.get("CCPROXY_DEBUG", "false").lower() in ("true", "1", "yes"),
        )

        direction_str = "forward" if self.proxy_direction == ProxyDirection.FORWARD else "reverse"
        logger.info(
            "MITM mode: %s, listening on port %d, forwarding to LiteLLM on port %d",
            direction_str,
            mitm_port,
            litellm_port,
        )

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
        """Called when mitmproxy is fully running - async context available."""
        if self._initialized:
            return

        assert self.config is not None

        direction_str = "forward" if self.proxy_direction == ProxyDirection.FORWARD else "reverse"

        if self.storage:
            try:
                await self.storage.connect()
                self.addon = CCProxyMitmAddon(
                    self.storage,
                    self.config,
                    proxy_direction=self.proxy_direction,
                )
                self._initialized = True
                logger.info("CCProxy addon initialized with storage (direction: %s)", direction_str)
            except Exception as e:
                logger.error("Failed to connect storage: %s", e)
                # Still create addon without storage for logging
                self.addon = CCProxyMitmAddon(
                    storage=None,
                    config=self.config,
                    proxy_direction=self.proxy_direction,
                )
                self._initialized = True
                logger.info("CCProxy addon initialized without storage (direction: %s)", direction_str)
        else:
            # No storage configured
            self.addon = CCProxyMitmAddon(
                storage=None,
                config=self.config,
                proxy_direction=self.proxy_direction,
            )
            self._initialized = True
            logger.info("CCProxy addon initialized, no storage (direction: %s)", direction_str)

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
