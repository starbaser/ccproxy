"""Mitmproxy integration for HTTP/HTTPS traffic capture."""

from typing import Any

from ccproxy.mitm.process import get_mitm_status, is_running, start_mitm, stop_mitm

__all__ = [
    "start_mitm",
    "stop_mitm",
    "is_running",
    "get_mitm_status",
]


# Lazy imports for components that may not be available yet
# These will be imported when needed to avoid prisma generation requirements
def __getattr__(name: str) -> Any:
    """Lazy load addon and storage classes to avoid prisma generation requirements."""
    if name == "CCProxyMitmAddon":
        from ccproxy.mitm.addon import CCProxyMitmAddon

        return CCProxyMitmAddon
    if name == "TraceStorage":
        from ccproxy.mitm.storage import TraceStorage

        return TraceStorage
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
