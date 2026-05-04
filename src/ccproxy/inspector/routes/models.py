"""Synthetic ``GET /v1/models`` handler.

Serves the OpenAI-compatible model catalog directly from ccproxy without
forwarding upstream. Registered as a REQUEST route at higher priority than
``register_transform_routes`` so the transform router doesn't try to forward
``/v1/models`` to a provider that doesn't exist (the placeholder reverse-proxy
backend).

``?refresh=true`` triggers a live merge against configured providers'
upstream ``/v1/models``; otherwise the static catalog is returned instantly.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from ccproxy.specs.model_catalog import build_catalog

if TYPE_CHECKING:
    from mitmproxy.http import HTTPFlow

    from ccproxy.inspector.router import InspectorRouter

logger = logging.getLogger(__name__)

_MODELS_PATH = "/v1/models"


def register_models_routes(router: InspectorRouter) -> None:
    """Register the synthetic ``GET /v1/models`` handler on ``router``."""
    from ccproxy.inspector.router import RouteType

    @router.route(_MODELS_PATH, rtype=RouteType.REQUEST, catch_error=False)
    def handle_models(flow: HTTPFlow, **kwargs: object) -> None:  # pyright: ignore[reportUnusedFunction]
        if flow.request.method != "GET":
            return

        refresh = flow.request.query.get("refresh") == "true"
        try:
            payload = build_catalog(refresh=refresh)
        except Exception:
            logger.exception("Failed to build model catalog")
            from mitmproxy.http import Response

            flow.response = Response.make(
                500,
                json.dumps({
                    "error": {
                        "message": "model catalog build failed",
                        "type": "server_error",
                        "code": 500,
                    },
                }).encode(),
                {"Content-Type": "application/json"},
            )
            return

        from mitmproxy.http import Response

        flow.response = Response.make(
            200,
            json.dumps(payload).encode(),
            {"Content-Type": "application/json"},
        )
        logger.debug("Served /v1/models (%d models, refresh=%s)", len(payload["data"]), refresh)
