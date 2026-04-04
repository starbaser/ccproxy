"""Inspector integration for HTTP/HTTPS traffic capture."""

from typing import Any

from ccproxy.inspector.process import (
    get_inspector_status,
    start_inspector,
)

__all__ = [
    "get_inspector_status",
    "start_inspector",
]


# Lazy imports for components that may not be available yet
# These will be imported when needed to avoid prisma generation requirements
def __getattr__(name: str) -> Any:
    """Lazy load addon and storage classes to avoid prisma generation requirements."""
    if name == "InspectorAddon":
        from ccproxy.inspector.addon import InspectorAddon

        return InspectorAddon
    if name == "InspectorTracer":
        from ccproxy.inspector.telemetry import InspectorTracer

        return InspectorTracer
    if name == "TraceStorage":
        from ccproxy.inspector.storage import TraceStorage

        return TraceStorage
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
