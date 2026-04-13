"""Startup outbound-connectivity probe.

ccproxy forwards LLM traffic with no enforced request timeout (see
``provider_timeout``). Rather than relying on a short per-request
timeout to catch network problems — which misfires on slow inference —
we catch them once at startup: probe a single well-known external host
and refuse to start if we can't reach the open internet.

Verifying one canary is enough. The failure modes we care about
(missing routes, blocked egress, broken DNS, broken system CA bundle,
namespace not actually joining the jail) are global to the network
stack, not per-provider. The provider-specific failure modes (auth
wrong, request format wrong, API down) require real traffic to surface
and cannot be diagnosed at startup anyway.

This is a hard failure by design. If ccproxy cannot reach the internet
at startup, it cannot serve requests, and silently accepting traffic
that will hang is worse than refusing to start.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from ccproxy.config import CCProxyConfig

logger = logging.getLogger(__name__)


class ReadinessError(RuntimeError):
    """Raised when ccproxy cannot reach the external network at startup."""


async def verify_outbound_reachability(config: CCProxyConfig) -> None:
    """Probe the configured readiness canary once.

    Success is strictly defined: the canary host returned an HTTP response.
    The status code is irrelevant — 200, 301, 404 all prove the full stack
    (DNS → routing → TCP → TLS → HTTP) is working. Any exception raised
    by httpx is a hard failure.

    Raises ``ReadinessError`` on any failure.
    """
    url = config.readiness_probe_url
    timeout = httpx.Timeout(config.readiness_probe_timeout_seconds)

    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.head(url, follow_redirects=False)
        except httpx.ConnectError as e:
            raise ReadinessError(
                f"Outbound reachability probe failed: connect error to {url}: {e}",
            ) from e
        except httpx.ConnectTimeout as e:
            raise ReadinessError(
                f"Outbound reachability probe failed: connect timeout to {url} "
                f"(after {config.readiness_probe_timeout_seconds}s)",
            ) from e
        except httpx.ReadTimeout as e:
            raise ReadinessError(
                f"Outbound reachability probe failed: read timeout from {url} "
                f"(after {config.readiness_probe_timeout_seconds}s) — "
                f"TCP/TLS connected but no HTTP response received",
            ) from e
        except httpx.HTTPError as e:
            raise ReadinessError(
                f"Outbound reachability probe failed: {type(e).__name__} for {url}: {e}",
            ) from e

    logger.info("Outbound readiness OK: %s → HTTP %d", url, resp.status_code)


async def verify_or_shutdown(
    config: CCProxyConfig,
    on_failure: Callable[[], Awaitable[None]],
) -> None:
    """Run the readiness probe; on failure, run ``on_failure`` then re-raise.

    Thin wrapper around ``verify_outbound_reachability`` that coordinates
    the cleanup callback so the caller does not have to repeat the
    try/except/raise pattern. The callback is awaited even if it itself
    raises (its exception is swallowed so the original ReadinessError is
    what propagates).
    """
    try:
        await verify_outbound_reachability(config)
    except ReadinessError as e:
        logger.error("Startup readiness probe failed: %s", e)
        try:
            await on_failure()
        except Exception:
            logger.exception("Cleanup after readiness failure itself raised")
        raise
