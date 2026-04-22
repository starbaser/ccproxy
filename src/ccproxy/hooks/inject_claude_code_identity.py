"""Inject Claude Code identity — required system message for Anthropic OAuth.

Prepends ``CLAUDE_CODE_SYSTEM_PREFIX`` to the system prompts when the
flow is OAuth-authenticated and targets Anthropic.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic_ai.messages import SystemPromptPart

from ccproxy.constants import CLAUDE_CODE_SYSTEM_PREFIX
from ccproxy.pipeline.context import Context
from ccproxy.pipeline.guards import is_oauth_request
from ccproxy.pipeline.hook import hook

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
    """Prepend Claude Code system prefix to system prompts."""
    parts = ctx.system

    has_prefix = any(p.content.startswith(CLAUDE_CODE_SYSTEM_PREFIX) for p in parts)
    if has_prefix:
        return ctx

    prefix_part = SystemPromptPart(content=CLAUDE_CODE_SYSTEM_PREFIX)
    ctx.system = [prefix_part, *parts]

    return ctx
