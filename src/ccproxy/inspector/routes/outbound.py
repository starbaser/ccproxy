"""Outbound route handlers — last-mile request fixups before provider delivery.

Runs after the transform route has rewritten the flow destination. Handles
beta header injection, Claude Code identity injection, and response
observation (auth failures).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, cast

from ccproxy.constants import ANTHROPIC_BETA_HEADERS, CLAUDE_CODE_SYSTEM_PREFIX
from ccproxy.inspector.flow_store import InspectorMeta

if TYPE_CHECKING:
    from mitmproxy.http import HTTPFlow

    from ccproxy.inspector.router import InspectorRouter

logger = logging.getLogger(__name__)


def _is_anthropic_request(flow: HTTPFlow) -> bool:
    """Check if the flow targets an Anthropic API endpoint."""
    return cast("str | None", flow.request.headers.get("anthropic-version")) is not None  # pyright: ignore[reportUnknownMemberType]


def register_outbound_routes(router: InspectorRouter) -> None:
    """Register all outbound route handlers on the given router."""
    from ccproxy.inspector.router import RouteType

    @router.route("/{path}", rtype=RouteType.REQUEST)
    def handle_outbound_request(flow: HTTPFlow, **kwargs: object) -> None:  # pyright: ignore[reportUnusedFunction]
        if flow.metadata.get(InspectorMeta.DIRECTION) != "inbound":
            return

        # Beta header injection for Anthropic requests
        existing: str | None = cast("str | None", flow.request.headers.get("anthropic-beta"))  # pyright: ignore[reportUnknownMemberType]
        if existing is not None:
            existing_list = [h.strip() for h in existing.split(",") if h.strip()]
            merged = list(dict.fromkeys(ANTHROPIC_BETA_HEADERS + existing_list))
            flow.request.headers["anthropic-beta"] = ",".join(merged)

        # Claude Code identity injection for OAuth Anthropic requests
        oauth_injected = flow.request.headers.get("x-ccproxy-oauth-injected")
        if oauth_injected and _is_anthropic_request(flow):
            _inject_claude_code_identity(flow)

    @router.route("/{path}", rtype=RouteType.RESPONSE)
    def observe_auth_failure(flow: HTTPFlow, **kwargs: object) -> None:  # pyright: ignore[reportUnusedFunction]
        if flow.response and flow.response.status_code in (401, 403):
            logger.warning(
                "Auth failure: %s %d",
                flow.request.pretty_url,
                flow.response.status_code,
            )


def _inject_claude_code_identity(flow: HTTPFlow) -> None:
    """Prepend Claude Code system prefix to the system message if missing."""
    if not flow.request.content:
        return

    try:
        body = json.loads(flow.request.content)
    except (json.JSONDecodeError, TypeError):
        return

    system = body.get("system", "")
    if isinstance(system, str) and not system.startswith(CLAUDE_CODE_SYSTEM_PREFIX):
        body["system"] = CLAUDE_CODE_SYSTEM_PREFIX + ("\n\n" + system if system else "")
        flow.request.content = json.dumps(body).encode()
        logger.debug("Injected Claude Code identity into system message")
