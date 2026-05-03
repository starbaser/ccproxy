"""Pydantic model mirroring the Anthropic ``/v1/messages`` request schema.

Permissive (``extra="allow"``) so ccproxy doesn't break on new fields the
upstream API accepts before we update this file. Used by request inspection
and shape-replay tooling that wants typed access to common fields without
re-deriving the schema everywhere.

Field set is the public ``/v1/messages`` surface as observed in shape captures
and the Anthropic SDK; not intended to be exhaustive of every internal field.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class APIRequestParams(BaseModel):
    """Anthropic ``/v1/messages`` request body shape (permissive)."""

    model_config = ConfigDict(extra="allow")

    model: str | None = None
    messages: list[dict[str, Any]] | None = None
    system: str | list[dict[str, Any]] | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: dict[str, Any] | None = None
    betas: list[str] | None = None
    metadata: dict[str, Any] | None = None
    max_tokens: int | None = None
    thinking: dict[str, Any] | None = None
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    stop_sequences: list[str] | None = None
    stream: bool | None = None
    context_management: dict[str, Any] | None = None
    output_config: dict[str, Any] | None = None
    speed: str | None = None
    cache_control: dict[str, Any] | None = None
