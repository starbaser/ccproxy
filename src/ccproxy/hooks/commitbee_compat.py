"""Commitbee compatibility hook — strips markdown fencing instruction.

Detects commitbee requests by their system prompt signature and appends
an instruction to emit raw JSON without markdown code block wrapping.
Runs after the shape hook so the system prompt is already assembled.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ccproxy.pipeline.hook import hook

if TYPE_CHECKING:
    from ccproxy.pipeline.context import Context

logger = logging.getLogger(__name__)

_COMMITBEE_SIGNATURE = "You generate Conventional Commit messages from git diffs"
_RAW_JSON_INSTRUCTION = (
    "\n\nCRITICAL FORMATTING RULE: You MUST output ONLY the raw JSON object. "
    "Do NOT use ```json code fences. Do NOT use any markdown formatting. "
    "Your entire response must be parseable by JSON.parse() with zero preprocessing."
)


def commitbee_compat_guard(ctx: Context) -> bool:
    """Only run for requests whose system prompt contains the commitbee signature."""
    system = ctx._body.get("system")
    if isinstance(system, str):
        return _COMMITBEE_SIGNATURE in system
    if isinstance(system, list):
        return any(
            isinstance(b, dict) and _COMMITBEE_SIGNATURE in b.get("text", "")
            for b in system
        )
    return False


@hook(reads=["system"], writes=["system"])
def commitbee_compat(ctx: Context, _: dict[str, Any]) -> Context:
    """Append raw-JSON instruction to commitbee's system prompt."""
    system = ctx._body.get("system")
    if isinstance(system, str):
        ctx._body["system"] = system + _RAW_JSON_INSTRUCTION
    elif isinstance(system, list):
        for block in reversed(system):
            if isinstance(block, dict) and _COMMITBEE_SIGNATURE in block.get("text", ""):
                block["text"] += _RAW_JSON_INSTRUCTION
                break
    logger.info("commitbee_compat: appended raw-JSON instruction")
    return ctx
