"""Default fill functions — inhabit the husk with incoming content.

Each function takes a ``mitmproxy.http.Request`` husk plus the pipeline
``Context`` and mutates the husk's body or headers to carry the incoming
request's content. Users compose their own fill lists via the ``husk``
hook's ``fill`` param; these are shipped as minimal examples.
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any

from mitmproxy import http

from ccproxy.compliance.body import mutate_body

if TYPE_CHECKING:
    from ccproxy.pipeline.context import Context


def fill_model(husk: http.Request, ctx: Context) -> None:
    """Copy ``ctx.model`` into ``body.model`` if present."""
    if ctx.model:
        mutate_body(husk, lambda b: b.update(model=ctx.model))


def fill_messages(husk: http.Request, ctx: Context) -> None:
    """Copy ``ctx.messages`` into ``body.messages`` if present."""
    if ctx.messages:
        mutate_body(husk, lambda b: b.update(messages=ctx.messages))


def fill_tools(husk: http.Request, ctx: Context) -> None:
    """Copy ``tools`` and ``tool_choice`` from the incoming body."""
    source = ctx._body

    def _fill(body: dict[str, Any]) -> None:
        if "tools" in source:
            body["tools"] = source["tools"]
        if "tool_choice" in source:
            body["tool_choice"] = source["tool_choice"]

    mutate_body(husk, _fill)


def fill_system_append(husk: http.Request, ctx: Context) -> None:
    """Append incoming system blocks after the husk's preserved blocks."""
    ctx_system = ctx.system
    if ctx_system is None:
        return
    new_blocks: list[dict[str, Any]] = (
        ctx_system if isinstance(ctx_system, list) else [{"type": "text", "text": ctx_system}]
    )

    def _fill(body: dict[str, Any]) -> None:
        existing = body.get("system")
        if isinstance(existing, list):
            body["system"] = [*existing, *new_blocks]
        else:
            body["system"] = new_blocks

    mutate_body(husk, _fill)


def fill_stream_passthrough(husk: http.Request, ctx: Context) -> None:
    """Copy the incoming body's ``stream`` flag onto the husk."""
    source = ctx._body
    if "stream" in source:
        value = source["stream"]
        mutate_body(husk, lambda b: b.update(stream=value))


def regenerate_user_prompt_id(husk: http.Request, ctx: Context) -> None:
    """Re-roll ``user_prompt_id`` if the husk carries one."""

    def _regen(body: dict[str, Any]) -> None:
        if "user_prompt_id" in body:
            body["user_prompt_id"] = uuid.uuid4().hex[:13]

    mutate_body(husk, _regen)


def regenerate_session_id(husk: http.Request, ctx: Context) -> None:
    """Re-roll ``metadata.user_id.session_id`` if the husk carries one."""

    def _regen(body: dict[str, Any]) -> None:
        metadata = body.get("metadata")
        if not isinstance(metadata, dict):
            return
        user_id_raw = metadata.get("user_id")
        if not isinstance(user_id_raw, str):
            return
        try:
            identity = json.loads(user_id_raw)
        except (json.JSONDecodeError, TypeError):
            return
        if not isinstance(identity, dict):
            return
        if "device_id" in identity or "account_uuid" in identity:
            identity["session_id"] = str(uuid.uuid4())
            metadata["user_id"] = json.dumps(identity)

    mutate_body(husk, _regen)
