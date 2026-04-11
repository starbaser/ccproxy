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
    from ccproxy.inspector.flow_store import ClientRequest

logger = logging.getLogger(__name__)


def extract_observation(client_request: ClientRequest, provider: str) -> ObservationBundle:
    """Extract an ObservationBundle from a raw ClientRequest snapshot.

    Filters out content fields (messages, tools, etc.), auth tokens,
    and transport headers. Everything else is candidate envelope.
    """
    user_agent = client_request.headers.get("user-agent", "unknown")

    # Extract profiled headers
    headers: dict[str, str] = {}
    for name, value in client_request.headers.items():
        if not should_skip_header(name):
            headers[name.lower()] = value

    # Extract body envelope fields
    body_envelope: dict[str, Any] = {}
    system: Any = None

    if client_request.body:
        try:
            body = json.loads(client_request.body)
            if isinstance(body, dict):
                for key, value in body.items():
                    if key == "system":
                        system = value
                    elif not should_skip_body_field(key):
                        body_envelope[key] = value
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.debug("Non-JSON body, skipping body extraction for %s", provider)

    return ObservationBundle(
        provider=provider,
        user_agent=user_agent,
        headers=headers,
        body_envelope=body_envelope,
        system=system,
    )
