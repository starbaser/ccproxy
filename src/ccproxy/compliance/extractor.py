"""Feature extraction from ClientRequest snapshots.

Produces an ObservationBundle containing profiled headers and body
envelope fields, with content fields and sensitive headers excluded.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from ccproxy.compliance.classifier import should_skip_body_field, should_skip_header
from ccproxy.compliance.models import ObservationBundle

if TYPE_CHECKING:
    from ccproxy.inspector.flow_store import HttpSnapshot

logger = logging.getLogger(__name__)


def extract_observation(
    client_request: HttpSnapshot,
    provider: str,
    *,
    additional_header_exclusions: frozenset[str] = frozenset(),
    additional_body_content_fields: frozenset[str] = frozenset(),
) -> ObservationBundle:
    """Extract an ObservationBundle from a raw ClientRequest snapshot.

    Filters out content fields (messages, tools, etc.), auth tokens,
    and transport headers. Everything else is candidate envelope.
    """
    lc_headers = {k.lower(): v for k, v in client_request.headers.items()}
    user_agent = lc_headers.get("user-agent", "unknown")

    headers: dict[str, str] = {}
    for name, value in lc_headers.items():
        if not should_skip_header(name, additional_header_exclusions):
            headers[name] = value

    body_envelope: dict[str, Any] = {}
    system: Any = None
    body_wrapper: str | None = None

    if client_request.body:
        try:
            body = json.loads(client_request.body)
            if isinstance(body, dict):
                for key, value in body.items():
                    if key == "system":
                        system = value
                    elif not should_skip_body_field(key, additional_body_content_fields):
                        # Detect wrapper: a dict field containing primary payload fields
                        payload_markers = ("contents", "messages", "prompt")
                        if (
                            body_wrapper is None
                            and isinstance(value, dict)
                            and any(k in value for k in payload_markers)
                        ):
                            body_wrapper = key
                        else:
                            body_envelope[key] = value
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.debug("Non-JSON body, skipping body extraction for %s", provider)

    return ObservationBundle(
        provider=provider,
        user_agent=user_agent,
        headers=headers,
        body_envelope=body_envelope,
        system=system,
        body_wrapper=body_wrapper,
    )
