"""xepor route handlers for the inspector addon chain."""

from ccproxy.inspector.routes.inbound import register_inbound_routes
from ccproxy.inspector.routes.outbound import register_outbound_routes
from ccproxy.inspector.routes.transform import register_transform_routes

__all__ = [
    "register_inbound_routes",
    "register_outbound_routes",
    "register_transform_routes",
]
