"""Inspector integration for HTTP/HTTPS traffic capture."""

from ccproxy.inspector.process import (
    get_inspector_status,
    start_inspector,
)

__all__ = [
    "get_inspector_status",
    "start_inspector",
]
