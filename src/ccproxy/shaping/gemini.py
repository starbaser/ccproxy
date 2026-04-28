"""Gemini v1internal shape hooks for nested request envelope merging.

The v1internal body nests content (contents) and envelope (session_id,
generationConfig extras) under a single ``request`` key. Standard
content_fields injection operates on top-level body keys only — it
can't express the nested merge. This hook surgically injects incoming
content into the shape's request while preserving envelope fields.

Symmetric with ``reroute_gemini``: that hook wraps SDK traffic INTO
the v1internal envelope; this hook merges content INTO a v1internal shape.
"""

from __future__ import annotations

from typing import Any

from ccproxy.pipeline.context import Context
from ccproxy.pipeline.hook import hook


@hook(reads=["request"], writes=["request"])
def inject_gemini_content(ctx: Context, params: dict[str, Any]) -> Context:
    """Merge incoming request.contents and generationConfig into shape's request.

    - request.contents: replaced from incoming (user's prompt + files)
    - request.generationConfig: incoming values override, shape fills gaps
      (preserves topP, topK, thinkingConfig from shape if incoming omits them)
    - All other request fields (session_id, etc.): persist from shape
    """
    incoming_ctx = params.get("incoming_ctx")
    if incoming_ctx is None:
        return ctx

    shape_request = ctx._body.get("request")
    if not isinstance(shape_request, dict):
        return ctx

    incoming_request = incoming_ctx._body.get("request")
    if not isinstance(incoming_request, dict):
        return ctx

    if "contents" in incoming_request:
        shape_request["contents"] = incoming_request["contents"]

    shape_gen = shape_request.get("generationConfig", {})
    incoming_gen = incoming_request.get("generationConfig", {})
    if incoming_gen:
        shape_request["generationConfig"] = {**shape_gen, **incoming_gen}

    if "systemInstruction" in incoming_request:
        shape_request["systemInstruction"] = incoming_request["systemInstruction"]

    ctx._body["request"] = shape_request
    return ctx
