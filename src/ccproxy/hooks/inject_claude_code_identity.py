"""Inject Claude Code identity — required system message for Anthropic OAuth.

Prepends ``CLAUDE_CODE_SYSTEM_PREFIX`` to the ``system`` field in the
request body when the flow is OAuth-authenticated and targets Anthropic.
Handles both string and list (content-block) system message formats.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ccproxy.constants import CLAUDE_CODE_SYSTEM_PREFIX
from ccproxy.pipeline.guards import is_oauth_request
from ccproxy.pipeline.hook import hook

if TYPE_CHECKING:
    from ccproxy.pipeline.context import Context

logger = logging.getLogger(__name__)


def inject_claude_code_identity_guard(ctx: Context) -> bool:
    """Guard: run if OAuth is active and targeting Anthropic."""
    if not is_oauth_request(ctx) and not ctx.ccproxy_oauth_provider:
        return False
    return ctx.get_header("anthropic-version") != ""


@hook(
    reads=["authorization", "ccproxy_oauth_provider", "system"],
    writes=["system"],
)
def inject_claude_code_identity(ctx: Context, params: dict[str, Any]) -> Context:
    """Prepend Claude Code system prefix to system message."""
    system = ctx.system

    if system is None:
        ctx.system = CLAUDE_CODE_SYSTEM_PREFIX
    elif isinstance(system, str):
        if not system.startswith(CLAUDE_CODE_SYSTEM_PREFIX):
            ctx.system = CLAUDE_CODE_SYSTEM_PREFIX + "\n\n" + system
    elif isinstance(system, list):
        has_prefix = any(
            isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
            and block["text"].startswith(CLAUDE_CODE_SYSTEM_PREFIX)
            for block in system
        )
        if not has_prefix:
            ctx.system = [{"type": "text", "text": CLAUDE_CODE_SYSTEM_PREFIX}, *system]

    return ctx
