"""TLS fingerprint-aware outbound HTTP transport.

Exposes cached :class:`httpx.AsyncClient` instances backed by ``curl-cffi``
for browser TLS+HTTP/2 fingerprint impersonation. Callers fetch a client
via :func:`dispatch.get_client` and use it as a normal ``httpx.AsyncClient``;
cache lifecycle owns the connection pool.
"""

from ccproxy.transport.dispatch import (
    DEFAULT_PROFILE,
    IDLE_TIMEOUT_SECONDS,
    MAX_SESSIONS,
    VALID_PROFILES,
    UnknownFingerprintProfileError,
    aclose_all,
    get_client,
    reset_cache,
)

__all__ = [
    "DEFAULT_PROFILE",
    "IDLE_TIMEOUT_SECONDS",
    "MAX_SESSIONS",
    "VALID_PROFILES",
    "UnknownFingerprintProfileError",
    "aclose_all",
    "get_client",
    "reset_cache",
]
