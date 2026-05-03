"""In-process Anthropic OAuth refresh.

Replaces a per-request shell-out to the `claude` CLI for token refresh.
Mirrors opencode-claude-auth/src/credentials.ts:190-243 (``refreshViaOAuth``):

- POST ``application/x-www-form-urlencoded`` to the OAuth token endpoint.
- Body: ``grant_type=refresh_token&client_id=<...>&refresh_token=<...>``.
- Default ``expires_in=36000`` (10 hours) when the response omits it.
- 15s timeout — token refresh should be sub-second.

The on-disk credential file format mirrors the JSON layout used by
``opencode-claude-auth``: ``{access_token, refresh_token, expires_at}``
where ``expires_at`` is milliseconds-since-epoch.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from ccproxy.oauth.sources import atomic_write_back, needs_refresh

if TYPE_CHECKING:
    from ccproxy.oauth.sources import AnthropicOAuthSource

logger = logging.getLogger(__name__)

_DEFAULT_EXPIRES_IN_SEC = 36_000
_REFRESH_TIMEOUT_SEC = 15.0


def refresh_anthropic_token(
    refresh_token: str,
    *,
    client_id: str,
    endpoint: str,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any] | None:
    """POST to the Anthropic OAuth token endpoint and return the parsed response.

    ``transport`` is only used for testing (httpx.MockTransport).
    Returns ``None`` on network or parse failure.
    """
    body = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": refresh_token,
    }
    try:
        client_kwargs: dict[str, Any] = {"timeout": _REFRESH_TIMEOUT_SEC}
        if transport is not None:
            client_kwargs["transport"] = transport
        with httpx.Client(**client_kwargs) as client:
            resp = client.post(
                endpoint,
                data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
    except httpx.HTTPError as exc:
        logger.error("Anthropic OAuth refresh failed: %s", exc)
        return None

    if resp.status_code != 200:
        logger.error(
            "Anthropic OAuth refresh returned %d: %s",
            resp.status_code,
            resp.text[:500],
        )
        return None

    try:
        payload = resp.json()
    except (json.JSONDecodeError, ValueError) as exc:
        logger.error("Anthropic OAuth refresh returned non-JSON: %s", exc)
        return None

    if not isinstance(payload, dict) or "access_token" not in payload:
        logger.error("Anthropic OAuth refresh response missing access_token: %r", payload)
        return None

    return payload


def resolve_anthropic_token(
    source: AnthropicOAuthSource,
    *,
    label: str = "AnthropicOAuth",
    transport: httpx.BaseTransport | None = None,
) -> str | None:
    """Resolve an access_token from an AnthropicOAuthSource, refreshing if needed.

    1. Read ``refresh_token_file``. If it doesn't parse, return None.
    2. If the cached access_token has > 60s of headroom, return it as-is.
    3. Otherwise POST to ``endpoint`` with the refresh_token, atomically
       write the merged response back, and return the new access_token.
    """
    path = Path(source.refresh_token_file).expanduser()
    if not path.is_file():
        logger.error("%s refresh token file not found: %s", label, path)
        return None

    try:
        creds: dict[str, Any] = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("%s could not read %s: %s", label, path, exc)
        return None

    access_token = creds.get("access_token")
    refresh_token = creds.get("refresh_token")
    expires_at = creds.get("expires_at")

    if not isinstance(refresh_token, str) or not refresh_token:
        logger.error("%s missing refresh_token in %s", label, path)
        return None

    if (
        isinstance(access_token, str)
        and access_token
        and isinstance(expires_at, int | float)
        and not needs_refresh(float(expires_at))
    ):
        return access_token

    logger.info("%s refreshing access_token", label)
    payload = refresh_anthropic_token(
        refresh_token,
        client_id=source.client_id,
        endpoint=source.endpoint,
        transport=transport,
    )
    if payload is None:
        return None

    new_access = payload.get("access_token")
    new_refresh = payload.get("refresh_token") or refresh_token
    expires_in = int(payload.get("expires_in", _DEFAULT_EXPIRES_IN_SEC))
    new_expires_at = int(time.time() * 1000) + expires_in * 1000

    if not isinstance(new_access, str) or not new_access:
        logger.error("%s refresh response missing access_token: %r", label, payload)
        return None

    merged = {
        **creds,
        "access_token": new_access,
        "refresh_token": new_refresh,
        "expires_at": new_expires_at,
    }
    atomic_write_back(path, merged)
    return new_access
