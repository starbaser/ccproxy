"""Dynamic shaping callbacks — operations that can't be expressed as field injection.

Each callback receives ``(shape_ctx, incoming_ctx)`` and mutates the
shape context in place. Registered via dotted paths in
``shaping.providers.{name}.callbacks``.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from ccproxy.pipeline.context import Context


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
