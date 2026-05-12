"""Cached ``httpx.AsyncClient`` instances backed by ``curl-cffi``.

The cache is keyed on ``(host, profile)``. ``profile`` is a ``curl-cffi``
impersonate name (e.g. ``"chrome131"``) and selects the outgoing TLS+HTTP/2
fingerprint via :class:`httpx_curl_cffi.AsyncCurlTransport`. ``host`` is the
destination hostname; using it as a key component keeps each provider's
connection pool isolated so HTTP/2 streams aren't multiplexed across
unrelated targets.

Eviction is bounded both ways: LRU when the cache exceeds
:data:`MAX_SESSIONS`, and idle timeout when an entry hasn't been used for
more than :data:`IDLE_TIMEOUT_SECONDS`. Both run on the access path; there
is no background sweep.

Lifetime:

- Callers MUST NOT close the returned client.
- :func:`aclose_all` closes every cached client; call on inspector shutdown.
- :func:`reset_cache` is a test-only seam that drops the singleton without
  closing entries (tests own their own cleanup).
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import cast, get_args

import httpx
from curl_cffi.requests.impersonate import BrowserTypeLiteral
from httpx_curl_cffi import AsyncCurlTransport

MAX_SESSIONS = 16
"""Cap on cached clients before LRU eviction kicks in."""

IDLE_TIMEOUT_SECONDS = 60.0
"""How long an unused client survives before idle eviction closes it."""

DEFAULT_PROFILE = "chrome131"
"""Fallback impersonate profile when no per-flow profile is set."""

VALID_PROFILES: frozenset[str] = frozenset(get_args(BrowserTypeLiteral))
"""Profile names accepted by ``curl-cffi``'s ``impersonate`` parameter.

Sourced from :data:`curl_cffi.requests.impersonate.BrowserTypeLiteral` so the
set tracks the installed library version without being hand-maintained.
"""


class UnknownFingerprintProfileError(ValueError):
    """Raised when a configured profile name is not in :data:`VALID_PROFILES`."""


@dataclass
class _Entry:
    client: httpx.AsyncClient
    """The cached httpx client wrapped around an :class:`AsyncCurlTransport`."""

    last_used: float
    """Monotonic timestamp of the most recent ``get`` resolution."""


class _Cache:
    """LRU+idle cache of ``httpx.AsyncClient`` per ``(host, profile)``."""

    def __init__(
        self,
        *,
        max_sessions: int = MAX_SESSIONS,
        idle_timeout: float = IDLE_TIMEOUT_SECONDS,
    ) -> None:
        self._max = max_sessions
        self._idle = idle_timeout
        self._entries: OrderedDict[tuple[str, str], _Entry] = OrderedDict()
        self._lock = asyncio.Lock()

    async def get(self, *, host: str, profile: str) -> httpx.AsyncClient:
        """Return a cached client for ``(host, profile)``, creating one if absent.

        Raises:
            UnknownFingerprintProfileError: ``profile`` is not in :data:`VALID_PROFILES`.
        """
        if profile not in VALID_PROFILES:
            raise UnknownFingerprintProfileError(
                f"unknown curl-cffi impersonate profile {profile!r}; "
                f"valid profiles: {sorted(VALID_PROFILES)}"
            )
        impersonate = cast(BrowserTypeLiteral, profile)

        async with self._lock:
            now = time.monotonic()
            await self._evict_idle(now)
            key = (host, profile)
            entry = self._entries.get(key)
            if entry is not None:
                entry.last_used = now
                self._entries.move_to_end(key)
                return entry.client

            client = httpx.AsyncClient(transport=AsyncCurlTransport(impersonate=impersonate))
            self._entries[key] = _Entry(client=client, last_used=now)
            await self._evict_lru()
            return client

    async def _evict_idle(self, now: float) -> None:
        stale = [k for k, e in self._entries.items() if now - e.last_used > self._idle]
        for k in stale:
            entry = self._entries.pop(k)
            await entry.client.aclose()

    async def _evict_lru(self) -> None:
        while len(self._entries) > self._max:
            _, entry = self._entries.popitem(last=False)
            await entry.client.aclose()

    async def aclose_all(self) -> None:
        """Close every cached client and clear the cache. Idempotent."""
        async with self._lock:
            for entry in self._entries.values():
                await entry.client.aclose()
            self._entries.clear()

    def size(self) -> int:
        """Current number of cached clients. Test seam; not lock-guarded."""
        return len(self._entries)


_cache: _Cache | None = None


def _get_cache() -> _Cache:
    global _cache
    if _cache is None:
        _cache = _Cache()
    return _cache


async def get_client(*, host: str, profile: str) -> httpx.AsyncClient:
    """Fetch a cached :class:`httpx.AsyncClient` impersonating ``profile``.

    Args:
        host: Destination hostname. Used as a cache-key component so distinct
            providers don't share a connection pool.
        profile: curl-cffi impersonate profile name (e.g. ``"chrome131"``).

    Returns:
        A cached client. The caller MUST NOT close it; the cache owns the
        lifecycle.
    """
    return await _get_cache().get(host=host, profile=profile)


async def aclose_all() -> None:
    """Close every cached client. Call on inspector shutdown."""
    await _get_cache().aclose_all()


def reset_cache() -> None:
    """Drop the cache singleton without closing entries. Test-only seam."""
    global _cache
    _cache = None
