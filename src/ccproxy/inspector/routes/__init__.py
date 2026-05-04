"""xepor route handlers for the inspector addon chain."""

from ccproxy.inspector.routes.health import register_health_routes
from ccproxy.inspector.routes.transform import register_transform_routes

__all__ = [
    "register_health_routes",
    "register_transform_routes",
]
