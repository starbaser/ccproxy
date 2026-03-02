"""FastAPI routes for MCP notification ingestion."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ccproxy.mcp.buffer import get_buffer

router = APIRouter(prefix="/mcp", tags=["mcp"])


class NotifyRequest(BaseModel):
    """Incoming notification from mcptty."""

    task_id: str
    session_id: str
    claude_session_id: str = ""
    event: dict[str, Any]


@router.post("/notify")
async def mcp_notify(request: NotifyRequest) -> JSONResponse:
    """Buffer an MCP notification event. Always returns 200 (fire-and-forget)."""
    get_buffer().append(request.task_id, request.session_id, request.event)
    return JSONResponse({"status": "ok"}, status_code=200)
