"""Inject buffered MCP terminal events into the conversation.

Drains the notification buffer for the current session and inserts
synthetic tool_use/tool_result message pairs before the final user message,
giving the model awareness of MCP notifications without explicit polling.

Integration flow::

    1. External MCP tool posts a notification:

       POST /mcp/notify
       {"task_id": "task-abc123", "session_id": "sess-xyz",
        "event": {"type": "status", "status": "running", "message": "building..."}}

       The endpoint returns 200 (fire-and-forget). Events accumulate in
       ``NotificationBuffer`` keyed by (task_id, session_id).

    2. On the next outbound ``/v1/messages`` request matching that session,
       this hook drains all buffered events and synthesizes message pairs::

           ModelResponse with ToolCallPart (tasks_get)
           ModelRequest with ToolReturnPart (events JSON)

       Pairs are inserted immediately before the final user message.

    3. Session linkage: ``ccproxy.session_id`` in ``flow.metadata`` (set by
       the ``extract_session_id`` inbound hook) must match the ``session_id``
       from the notification POST.

See also: ``ccproxy.mcp.buffer``, ``ccproxy.mcp.routes``.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart

from ccproxy.mcp.buffer import get_buffer
from ccproxy.pipeline.context import Context
from ccproxy.pipeline.hook import hook

logger = logging.getLogger(__name__)


def inject_mcp_notifications_guard(ctx: Context) -> bool:
    """Guard: skip if no messages or no events for this session."""
    if not ctx.messages:
        return False
    assert ctx.flow is not None
    session_id = ctx.flow.metadata.get("ccproxy.session_id", "")
    if not session_id:
        return False
    return get_buffer().has_events_for_session(session_id)


@hook(
    reads=["messages"],
    writes=["messages"],
)
def inject_mcp_notifications(ctx: Context, params: dict[str, Any]) -> Context:
    """Inject buffered MCP notification events as tool_use/tool_result pairs."""
    assert ctx.flow is not None
    session_id = ctx.flow.metadata.get("ccproxy.session_id", "")
    if not session_id:
        return ctx

    drained = get_buffer().drain_session(session_id)
    if not drained:
        return ctx

    injected: list[ModelMessage] = []
    for task_id, events in drained.items():
        tool_call_id = f"toolu_notify_{uuid.uuid4().hex[:8]}"

        assistant_msg = ModelResponse(parts=[
            ToolCallPart(
                tool_name="tasks_get",
                args={"taskId": task_id},
                tool_call_id=tool_call_id,
            ),
        ])

        user_msg = ModelRequest(parts=[
            ToolReturnPart(
                tool_name="tasks_get",
                content=json.dumps(events),
                tool_call_id=tool_call_id,
            ),
        ])

        injected.append(assistant_msg)
        injected.append(user_msg)

    if injected:
        messages = ctx.messages
        insert_idx = len(messages) - 1 if messages else 0
        ctx.messages = messages[:insert_idx] + injected + messages[insert_idx:]
        logger.debug(
            "Injected %d MCP notification pairs for session %s",
            len(injected) // 2,
            session_id,
        )

    return ctx
