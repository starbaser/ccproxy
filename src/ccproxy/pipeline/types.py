"""Extension types for Pydantic AI objects that lack cache_control fields.

UserPromptPart content uses CachePoint inline (already in Pydantic AI).
SystemPromptPart and ToolDefinition need cache_control for Anthropic wire
format round-tripping — these subclasses add that field.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic_ai.messages import SystemPromptPart
from pydantic_ai.tools import ToolDefinition


@dataclass
class CachedSystemPromptPart(SystemPromptPart):
    """SystemPromptPart with Anthropic cache_control annotation."""

    cache_control: dict[str, str] | None = field(default=None)


@dataclass
class CachedToolDefinition(ToolDefinition):
    """ToolDefinition with Anthropic cache_control annotation."""

    cache_control: dict[str, Any] | None = field(default=None)
