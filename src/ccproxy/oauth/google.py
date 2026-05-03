"""In-process Google/Gemini OAuth refresh.

Replaces the legacy ``hooks/gemini_oauth_refresh.py`` workaround that shelled
out to the gemini-cli to force a refresh. This module talks directly to
``oauth2.googleapis.com/token`` using the user-supplied OAuth client_id and
client_secret (gemini-cli's are public installed-app credentials embedded
in its distribution; ccproxy does NOT vendor them).

Workaround for google-gemini/gemini-cli#21691: Google's refresh response
sometimes omits ``refresh_token``. The previous CLI-based path would then
overwrite the on-disk file and lose the persisted refresh_token entirely.
This resolver merges the response with the existing on-disk credentials,
keeping the old ``refresh_token`` if a new one isn't returned.
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
    from ccproxy.oauth.sources import GoogleOAuthSource

logger = logging.getLogger(__name__)

_DEFAULT_EXPIRES_IN_SEC = 3600
_REFRESH_TIMEOUT_SEC = 15.0


def refresh_google_token(
    refresh_token: str,
    *,
    client_id: str,
    client_secret: str,
    endpoint: str = "https://oauth2.googleapis.com/token",
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any] | None:
    """POST to the Google OAuth token endpoint and return the parsed response.

    ``transport`` is only used for testing (httpx.MockTransport).
    Returns ``None`` on network or parse failure.
    """
    body = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
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
        logger.error("Google OAuth refresh failed: %s", exc)
        return None

    if resp.status_code != 200:
        logger.error(
            "Google OAuth refresh returned %d: %s",
            resp.status_code,
            resp.text[:500],
        )
        return None

    try:
        payload = resp.json()
    except (json.JSONDecodeError, ValueError) as exc:
        logger.error("Google OAuth refresh returned non-JSON: %s", exc)
        return None

    if not isinstance(payload, dict) or "access_token" not in payload:
        logger.error("Google OAuth refresh response missing access_token: %r", payload)
        return None

    return payload


def resolve_google_token(
    source: GoogleOAuthSource,
    *,
    label: str = "GoogleOAuth",
    transport: httpx.BaseTransport | None = None,
) -> str | None:
    """Resolve an access_token from a GoogleOAuthSource, refreshing if needed.

    1. Read ``refresh_token_file`` (gemini-cli writes ``~/.gemini/oauth_creds.json``).
    2. If the cached access_token has > 60s of headroom (per ``expiry_field``),
       return it as-is.
    3. Otherwise POST to ``endpoint`` with the refresh_token. The response
       may omit ``refresh_token`` (gemini-cli #21691 upstream bug); the
       merged write preserves the on-disk value in that case.
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
    expiry_value = creds.get(source.expiry_field)

    if not isinstance(refresh_token, str) or not refresh_token:
        logger.error("%s missing refresh_token in %s", label, path)
        return None

    if (
        isinstance(access_token, str)
        and access_token
        and isinstance(expiry_value, int | float)
        and not needs_refresh(float(expiry_value))
    ):
        return access_token

    logger.info("%s refreshing access_token", label)
    payload = refresh_google_token(
        refresh_token,
        client_id=source.client_id,
        client_secret=source.client_secret,
        endpoint=source.endpoint,
        transport=transport,
    )
    if payload is None:
        return None

    new_access = payload.get("access_token")
    # #21691 workaround: keep the on-disk refresh_token if Google omits it.
    new_refresh = payload.get("refresh_token") or refresh_token
    expires_in = int(payload.get("expires_in", _DEFAULT_EXPIRES_IN_SEC))
    new_expiry_ms = int(time.time() * 1000) + expires_in * 1000

    if not isinstance(new_access, str) or not new_access:
        logger.error("%s refresh response missing access_token: %r", label, payload)
        return None

    merged = {
        **creds,
        "access_token": new_access,
        "refresh_token": new_refresh,
        source.expiry_field: new_expiry_ms,
    }
    atomic_write_back(path, merged)
    return new_access
