"""Vendored constant lists from publicly observable claude-code behavior.

Only fact lists are vendored: env-var names, beta strings, telemetry event
names, header names. No prose, diagrams, or TypeScript interface bodies
are reproduced verbatim.

The billing salt and the paired claude-code version (functional
authentication parameters, not facts) are NOT vendored — the user supplies
both via ``shaping.billing_salt`` and ``shaping.cc_version`` in their
``ccproxy.yaml`` and they are read at runtime by ``billing_salt.get_*``.

Sources (kitstore-readable):
- ``community/opencode-claude-auth/src/model-config.ts`` (base betas, long-context betas)
"""

from __future__ import annotations

BASE_BETAS: tuple[str, ...] = (
    "claude-code-20250219",
    "oauth-2025-04-20",
    "interleaved-thinking-2025-05-14",
    "prompt-caching-scope-2026-01-05",
    "context-management-2025-06-27",
    "advisor-tool-2026-03-01",
)
"""Base ``anthropic-beta`` header values that Claude Code includes on every request."""

LONG_CONTEXT_BETAS: tuple[str, ...] = (
    "context-1m-2025-08-07",
    "interleaved-thinking-2025-05-14",
)
"""Beta header values added when long-context (1M) is opted in for Opus/Sonnet >=4.6."""
