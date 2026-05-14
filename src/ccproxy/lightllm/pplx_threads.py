"""In-memory L1 TTL store for Perplexity thread continuation state.

ccproxy itself holds NO authoritative thread state — Perplexity's
server-side thread library at ``/rest/thread/*`` is the canonical store
(see ``threads-history.md``). This module exists purely as a hot-path
optimization for *organic in-session continuation* where the client
sends Turn N+1 without setting ``metadata.ccproxy_pplx_thread``: the
``PerplexityAddon`` captures identifiers from each completed SSE
response into this store keyed by the conversation_id SHA12 stamped by
``InspectorAddon``, and the next-turn ``pplx_thread_inject`` hook
reads them back when no explicit ``metadata.ccproxy_pplx_thread`` was
supplied.

The store is in-memory only; no disk persistence. Survives no
ccproxy restarts. If a client wants cross-restart resume, they pass
the slug explicitly via ``metadata.ccproxy_pplx_thread`` and the
hook resolves via ``GET /rest/thread/{slug}``.

Pattern modeled on the SessionStore reference at ``core-query.md:1180-1230``.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

__all__ = [
    "PerplexityThreadState",
    "PerplexityThreadStore",
    "clear_pplx_threads",
    "get_pplx_thread_store",
]


_FALLBACK_TTL_SECONDS: float = 1800.0
"""Used when ``get_config()`` is unavailable (early startup, tests without
a config instance). Production reads :attr:`PplxThreadConfig.ttl_seconds`."""


@dataclass(frozen=True)
class PerplexityThreadState:
    """Identifiers captured from a completed Perplexity SSE response.

    All four fields are sourced from the SSE event stream lazily —
    ``backend_uuid`` and ``context_uuid`` typically appear on the
    first event with results, ``read_write_token`` and ``thread_url_slug``
    on the final event per ``threads-history.md:24-44``.
    """

    backend_uuid: str
    read_write_token: str | None
    context_uuid: str
    thread_url_slug: str | None
    last_used: float


def _get_ttl_seconds() -> float:
    """Lazy-read the active TTL from ``CCProxyConfig.pplx.thread.ttl_seconds``.

    Falls back to ``_FALLBACK_TTL_SECONDS`` if the config singleton is not
    yet initialized (e.g. during early startup or in tests that bypass
    config loading). This means YAML changes to ``ttl_seconds`` take effect
    on the very next eviction pass — no singleton state to invalidate.
    """
    try:
        from ccproxy.config import get_config

        return float(get_config().pplx.thread.ttl_seconds)
    except Exception:
        return _FALLBACK_TTL_SECONDS


class PerplexityThreadStore:
    """Thread-safe TTL store keyed by ccproxy conversation_id (SHA12).

    TTL is lazy-bound to :class:`PplxThreadConfig.ttl_seconds` via
    :func:`_get_ttl_seconds` at every eviction pass. A constructor override
    (``ttl_seconds=...``) freezes the TTL for the lifetime of the instance —
    used by tests that need deterministic eviction. Production uses the
    singleton from :func:`get_pplx_thread_store` which omits the override.
    """

    def __init__(self, ttl_seconds: float | None = None) -> None:
        self._ttl_override = ttl_seconds
        self._store: dict[str, PerplexityThreadState] = {}
        self._lock = threading.Lock()

    @property
    def ttl(self) -> float:
        """Current TTL — override if set on the instance, else config-lazy."""
        if self._ttl_override is not None:
            return self._ttl_override
        return _get_ttl_seconds()

    def get(self, conversation_id: str) -> PerplexityThreadState | None:
        """Return the cached state for ``conversation_id`` or ``None``.

        Bumps the entry's ``last_used`` timestamp on hit. Lazy-evicts any
        expired entries during the lookup pass.
        """
        with self._lock:
            self._evict_expired_locked()
            cached = self._store.get(conversation_id)
            if cached is None:
                return None
            refreshed = PerplexityThreadState(
                backend_uuid=cached.backend_uuid,
                read_write_token=cached.read_write_token,
                context_uuid=cached.context_uuid,
                thread_url_slug=cached.thread_url_slug,
                last_used=time.monotonic(),
            )
            self._store[conversation_id] = refreshed
            return refreshed

    def save(
        self,
        conversation_id: str,
        backend_uuid: str,
        read_write_token: str | None,
        context_uuid: str,
        thread_url_slug: str | None,
    ) -> None:
        """Insert or overwrite the state for ``conversation_id``.

        Called by ``PerplexityAddon`` after each completed SSE stream.
        Eviction sweep runs at the end so the store stays bounded.
        """
        with self._lock:
            self._store[conversation_id] = PerplexityThreadState(
                backend_uuid=backend_uuid,
                read_write_token=read_write_token,
                context_uuid=context_uuid,
                thread_url_slug=thread_url_slug,
                last_used=time.monotonic(),
            )
            self._evict_expired_locked()

    def size(self) -> int:
        with self._lock:
            return len(self._store)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def _evict_expired_locked(self) -> None:
        now = time.monotonic()
        ttl = self.ttl
        expired = [k for k, v in self._store.items() if now - v.last_used > ttl]
        for k in expired:
            del self._store[k]


_store_instance: PerplexityThreadStore | None = None
_store_lock = threading.Lock()


def get_pplx_thread_store() -> PerplexityThreadStore:
    """Return the process-wide ``PerplexityThreadStore`` singleton."""
    global _store_instance
    with _store_lock:
        if _store_instance is None:
            _store_instance = PerplexityThreadStore()
        return _store_instance


def clear_pplx_threads() -> None:
    """Reset the singleton. Called from the test cleanup fixture."""
    global _store_instance
    with _store_lock:
        _store_instance = None
