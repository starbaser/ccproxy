"""Dynamic shaping hooks — DAG-ordered operations that can't be expressed as field injection.

Each hook is decorated with ``@hook(reads=..., writes=...)`` for DAG ordering
and receives ``(ctx, params) -> Context`` where ``ctx`` is the shape context.
The incoming pipeline context is available via ``params["incoming_ctx"]``.

Registered via dotted paths in ``shaping.providers.{name}.shape_hooks``.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from glom import assign, glom

from ccproxy.pipeline.context import Context
from ccproxy.pipeline.hook import hook


@hook(reads=["user_prompt_id"], writes=["user_prompt_id"])
def regenerate_user_prompt_id(ctx: Context, params: dict[str, Any]) -> Context:
    """Re-roll ``user_prompt_id`` if the shape carries one."""
    if glom(ctx._body, "user_prompt_id", default=None) is not None:
        assign(ctx._body, "user_prompt_id", uuid.uuid4().hex[:13])
    return ctx


@hook(reads=["metadata.user_id"], writes=["metadata.user_id"])
def regenerate_session_id(ctx: Context, params: dict[str, Any]) -> Context:
    """Re-roll ``metadata.user_id.session_id`` if the shape carries one."""
    metadata = glom(ctx._body, "metadata", default=None)
    if not isinstance(metadata, dict):
        return ctx
    user_id_raw = glom(metadata, "user_id", default=None)
    if not isinstance(user_id_raw, str):
        return ctx
    try:
        identity: Any = json.loads(user_id_raw)
    except (json.JSONDecodeError, TypeError):
        return ctx
    if not isinstance(identity, dict):
        return ctx
    if "device_id" in identity or "account_uuid" in identity:
        identity["session_id"] = str(uuid.uuid4())
        metadata["user_id"] = json.dumps(identity)
    return ctx
