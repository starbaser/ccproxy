"""Preserve ccproxy beta headers through LiteLLM's beta filter.

LiteLLM's `filter_and_transform_beta_headers` silently drops any
anthropic-beta values not present in its bundled config JSON.  This
strips `claude-code-20250219` (and any future ccproxy-required betas),
causing Anthropic to apply standard API rate limits instead of the
Claude Code / Claude Max tier.

This patch injects ccproxy's required beta headers into the provider
mapping so they pass through the filter unchanged.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

from ccproxy.constants import ANTHROPIC_BETA_HEADERS

if TYPE_CHECKING:
    from ccproxy.handler import CCProxyHandler

logger = logging.getLogger(__name__)

_applied = False


def apply(handler: CCProxyHandler) -> None:
    global _applied
    if _applied:
        return

    _patch_beta_filter()
    _applied = True


def _patch_beta_filter() -> None:
    """Inject ccproxy beta headers into LiteLLM's beta filter config."""
    from litellm.anthropic_beta_headers_manager import _load_beta_headers_config  # pyright: ignore[reportPrivateUsage]

    _original_load = _load_beta_headers_config  # pyright: ignore[reportPrivateUsage]

    def _patched_load() -> dict[str, Any]:
        config: dict[str, Any] = _original_load()
        anthropic_mapping: dict[str, Any] = cast(dict[str, Any], config.get("anthropic", {}))
        for header in ANTHROPIC_BETA_HEADERS:
            if header not in anthropic_mapping:
                anthropic_mapping[header] = header
        config["anthropic"] = anthropic_mapping
        return config

    import litellm.anthropic_beta_headers_manager as mgr

    mgr._load_beta_headers_config = _patched_load  # pyright: ignore[reportPrivateUsage]
    logger.debug(
        "Patched LiteLLM beta header filter to preserve ccproxy headers: %s",
        ANTHROPIC_BETA_HEADERS,
    )
