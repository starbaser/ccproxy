"""Feature extraction from HttpSnapshot snapshots.

Produces an Envelope containing profiled headers and body envelope
fields, with content fields and sensitive headers excluded.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from ccproxy.compliance.classifier import should_skip_body_field, should_skip_header
from ccproxy.compliance.models import Envelope

if TYPE_CHECKING:
    from ccproxy.inspector.flow_store import HttpSnapshot

logger = logging.getLogger(__name__)


def extract_envelope(
    client_request: HttpSnapshot,
    *,
    additional_header_exclusions: frozenset[str] = frozenset(),
    additional_body_content_fields: frozenset[str] = frozenset(),
) -> Envelope:
    """Extract an Envelope from a raw HttpSnapshot.

    Filters out content fields (messages, tools, etc.), auth tokens,
    and transport headers. Everything else is candidate envelope.
    """
    lc_headers = {k.lower(): v for k, v in client_request.headers.items()}

    headers: dict[str, str] = {}
    for name, value in lc_headers.items():
        if not should_skip_header(name, additional_header_exclusions):
            headers[name] = value

    body_fields: dict[str, Any] = {}
    system: list[dict[str, Any]] | None = None
    body_wrapper: str | None = None

    if client_request.body:
        try:
            body = json.loads(client_request.body)
            if isinstance(body, dict):
                for key, value in body.items():
                    if key == "system":
                        if isinstance(value, list):
                            system = value
                        elif isinstance(value, str):
                            system = [{"type": "text", "text": value}]
                    elif not should_skip_body_field(key, additional_body_content_fields):
                        payload_markers = ("contents", "messages", "prompt")
                        if (
                            body_wrapper is None
                            and isinstance(value, dict)
                            and any(k in value for k in payload_markers)
                        ):
                            body_wrapper = key
                        else:
                            body_fields[key] = value
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.debug("Non-JSON body, skipping body extraction")

    return Envelope(
        headers=headers,
        body_fields=body_fields,
        system=system,
        body_wrapper=body_wrapper,
    )
