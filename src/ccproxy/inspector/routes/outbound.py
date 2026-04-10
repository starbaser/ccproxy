"""Outbound route handlers — flows from LiteLLM to providers.

Handles beta header injection and auth failure observation on the
outbound leg (LiteLLM → provider API).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

from ccproxy.constants import ANTHROPIC_BETA_HEADERS
from ccproxy.inspector.flow_store import (
    FLOW_ID_HEADER,
    FlowRecord,
    InspectorMeta,
    get_flow_record,
)

if TYPE_CHECKING:
    from mitmproxy.http import HTTPFlow

    from ccproxy.inspector.router import InspectorRouter

logger = logging.getLogger(__name__)


def _is_outbound(flow: HTTPFlow) -> bool:
    return flow.metadata.get(InspectorMeta.DIRECTION) == "outbound"


def register_outbound_routes(router: InspectorRouter) -> None:
    """Register all outbound route handlers on the given router."""
    from ccproxy.inspector.router import RouteType

    @router.route("/{path}", rtype=RouteType.REQUEST)
    def handle_outbound_request(flow: HTTPFlow, **kwargs: object) -> None:  # pyright: ignore[reportUnusedFunction]
        if not _is_outbound(flow):
            return

        flow_id: str | None = cast("str | None", flow.request.headers.get(FLOW_ID_HEADER))  # pyright: ignore[reportUnknownMemberType]
        record: FlowRecord | None = None
        if flow_id:
            record = get_flow_record(flow_id)
            if record:
                flow.metadata[InspectorMeta.RECORD] = record

        if record and record.original_request:
            orig = record.original_request
            flow.request.host = orig.host
            flow.request.port = orig.port
            flow.request.scheme = orig.scheme
            flow.request.path = orig.path
            logger.info(
                "Restored outbound request: %s://%s:%d%s",
                orig.scheme, orig.host, orig.port, orig.path,
            )

        existing: str | None = cast("str | None", flow.request.headers.get("anthropic-beta"))  # pyright: ignore[reportUnknownMemberType]
        if existing is not None:
            existing_list = [h.strip() for h in existing.split(",") if h.strip()]
            merged = list(dict.fromkeys(ANTHROPIC_BETA_HEADERS + existing_list))
            flow.request.headers["anthropic-beta"] = ",".join(merged)

    @router.route("/{path}", rtype=RouteType.RESPONSE)
    def observe_auth_failure(flow: HTTPFlow, **kwargs: object) -> None:  # pyright: ignore[reportUnusedFunction]
        if not _is_outbound(flow):
            return

        if flow.response and flow.response.status_code in (401, 403):
            record: FlowRecord | None = flow.metadata.get(InspectorMeta.RECORD)
            provider = record.auth.provider if record and record.auth else "unknown"
            logger.warning(
                "Auth failure on outbound: %s %d (provider: %s)",
                flow.request.pretty_url,
                flow.response.status_code,
                provider,
                extra={
                    "event": "outbound_auth_failure",
                    "status": flow.response.status_code,
                    "url": flow.request.pretty_url,
                    "provider": provider,
                },
            )
