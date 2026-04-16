"""Inspector integration for HTTP/HTTPS traffic capture."""

from ccproxy.inspector.process import (
    get_inspector_status,
    get_wg_client_conf,
    run_inspector,
)

__all__ = [
    "get_inspector_status",
    "get_wg_client_conf",
    "run_inspector",
]
