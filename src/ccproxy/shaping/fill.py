"""Default fill functions — inhabit the shape with incoming content.

Each function takes two ``Context`` objects: the shape context and the
incoming request context. Users compose their own fill lists via the
``shape`` hook's ``fill`` param.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from ccproxy.pipeline.context import Context


def fill_model(shape_ctx: Context, incoming_ctx: Context) -> None:
    """Copy ``incoming_ctx.model`` into the shape if present."""
    if incoming_ctx.model:
        shape_ctx.model = incoming_ctx.model


def fill_messages(shape_ctx: Context, incoming_ctx: Context) -> None:
    """Copy ``incoming_ctx.messages`` into the shape if present."""
    if incoming_ctx.messages:
        shape_ctx.messages = incoming_ctx.messages


def fill_tools(shape_ctx: Context, incoming_ctx: Context) -> None:
    """Copy ``tools`` and ``tool_choice`` from the incoming body."""
    if incoming_ctx.tools:
        shape_ctx.tools = incoming_ctx.tools
    if incoming_ctx.tool_choice is not None:
        shape_ctx.tool_choice = incoming_ctx.tool_choice


def fill_system_append(shape_ctx: Context, incoming_ctx: Context) -> None:
    """Append incoming system blocks after the shape's preserved blocks."""
    if not incoming_ctx.system:
        return
    shape_ctx.system = [*shape_ctx.system, *incoming_ctx.system]


def fill_stream_passthrough(shape_ctx: Context, incoming_ctx: Context) -> None:
    """Copy the incoming body's ``stream`` flag onto the shape."""
    if "stream" in incoming_ctx._body:
        shape_ctx.stream = incoming_ctx.stream


def regenerate_user_prompt_id(shape_ctx: Context, incoming_ctx: Context) -> None:
    """Re-roll ``user_prompt_id`` if the shape carries one."""
    if "user_prompt_id" in shape_ctx._body:
        shape_ctx._body["user_prompt_id"] = uuid.uuid4().hex[:13]


def regenerate_session_id(shape_ctx: Context, incoming_ctx: Context) -> None:
    """Re-roll ``metadata.user_id.session_id`` if the shape carries one."""
    metadata = shape_ctx._body.get("metadata")
    if not isinstance(metadata, dict):
        return
    user_id_raw = metadata.get("user_id")
    if not isinstance(user_id_raw, str):
        return
    try:
        identity: Any = json.loads(user_id_raw)
    except (json.JSONDecodeError, TypeError):
        return
    if not isinstance(identity, dict):
        return
    if "device_id" in identity or "account_uuid" in identity:
        identity["session_id"] = str(uuid.uuid4())
        metadata["user_id"] = json.dumps(identity)
