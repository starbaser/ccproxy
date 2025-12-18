"""Custom routes for ccproxy status endpoints.

This module provides FastAPI routes that can be integrated with LiteLLM proxy
to expose ccproxy internal state, primarily for the ccstatusline widget.

Route Registration
------------------
LiteLLM proxy doesn't support custom routes via configuration. To add these routes,
you must modify the LiteLLM proxy server startup process to include this router.

Method 1: Modify LiteLLM Source (Advanced)
    Import and include this router in litellm.proxy.proxy_server's FastAPI app:

    ```python
    from ccproxy.routes import router as ccproxy_router
    app.include_router(ccproxy_router)
    ```

Method 2: Monkey Patch via Handler (Recommended)
    The CCProxyHandler can access the FastAPI app during initialization and
    register routes. Add this to handler.py __init__:

    ```python
    # Access LiteLLM's FastAPI app and register custom routes
    try:
        from litellm.proxy.proxy_server import app
        from ccproxy.routes import router as ccproxy_router
        app.include_router(ccproxy_router)
    except Exception as e:
        logger.debug(f"Could not register custom routes: {e}")
    ```

Method 3: Standalone Server
    Run ccproxy routes as a separate FastAPI service on a different port,
    and have the statusline query this separate endpoint.

Current Implementation
----------------------
The status endpoint queries CCProxyHandler.get_status() which returns the last
routing decision stored as class-level state. This includes:
- model_name: Classification rule that matched
- original_model: Original model requested by client
- routed_model: Model after routing logic applied
- is_passthrough: Whether request passed through without routing
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/ccproxy", tags=["ccproxy"])


@router.get("/status")
async def get_status() -> JSONResponse:
    """Get the last routing decision for statusline widget.

    Returns:
        JSONResponse with routing info:
        {
            "rule": "thinking_model",
            "model": "openai/o3-mini",
            "original_model": "claude-sonnet-4-5-20250929",
            "is_passthrough": false,
            "timestamp": "2025-12-12T10:30:45.123456"
        }

        Or error response if no requests have been processed yet:
        {
            "error": "no requests yet"
        }
    """
    from ccproxy.handler import CCProxyHandler

    status = CCProxyHandler.get_status()
    if status:
        return JSONResponse(content=status)
    return JSONResponse(content={"error": "no requests yet"}, status_code=404)
