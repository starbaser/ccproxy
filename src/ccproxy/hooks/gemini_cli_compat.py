"""Masquerade google-genai SDK traffic as Gemini CLI.

Rewrites ``user-agent`` and ``x-goog-api-client`` headers when the
google-genai Python SDK is detected, so that requests routed through
``cloudcode-pa.googleapis.com`` receive the same capacity allocation
as native Gemini CLI traffic.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from ccproxy.pipeline.hook import hook

if TYPE_CHECKING:
    from ccproxy.pipeline.context import Context

logger = logging.getLogger(__name__)

_SDK_UA_RE = re.compile(r"google-genai-sdk/")
_MODEL_RE = re.compile(r"/models/([^/:]+)")

_CLI_VERSION = "0.36.0"
_NODE_CLIENT_VERSION = "9.15.1"
_NODE_VERSION = "22.22.2"


def gemini_cli_compat_guard(ctx: Context) -> bool:
    """Run for any flow whose user-agent identifies the google-genai SDK."""
    ua = ctx.get_header("user-agent", "")
    return bool(_SDK_UA_RE.search(ua))


@hook(
    reads=["authorization"],
    writes=["user-agent", "x-goog-api-client"],
)
def gemini_cli_compat(ctx: Context, _: dict[str, Any]) -> Context:
    """Rewrite SDK headers to match the Gemini CLI fingerprint."""
    path = ctx.flow.request.path.split("?")[0]
    model_match = _MODEL_RE.search(path)
    model = model_match.group(1) if model_match else "unknown"

    original_ua = ctx.get_header("user-agent", "")

    cli_ua = (
        f"GeminiCLI/{_CLI_VERSION}/{model} "
        f"(linux; x64; terminal) "
        f"google-api-nodejs-client/{_NODE_CLIENT_VERSION}"
    )
    ctx.set_header("user-agent", cli_ua)
    ctx.set_header("x-goog-api-client", f"gl-node/{_NODE_VERSION}")

    logger.info("gemini_cli_compat: %s → %s", original_ua, cli_ua)
    return ctx
