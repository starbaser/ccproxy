# ruff: noqa: S105, S106
"""Tests for ccproxy.oauth.google in-process Google/Gemini OAuth refresh.

All "tokens" in this file are synthetic fixture values, not real secrets.
"""

from __future__ import annotations

import json
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from ccproxy.oauth.google import refresh_google_token, resolve_google_token
from ccproxy.oauth.sources import GoogleOAuthSource

_TEST_CLIENT_ID = "681255809395-test.apps.googleusercontent.com"
_TEST_CLIENT_SECRET = "GOCSPX-test"
_TEST_ENDPOINT = "https://oauth.test.example/token"


def _mock_transport(responses: list[httpx.Response]) -> httpx.MockTransport:
    iter_responses = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        return next(iter_responses)

    return httpx.MockTransport(handler)


@dataclass
class RefreshCase:
    name: str
    """Descriptive name for the test scenario."""

    response: httpx.Response
    """httpx.Response to return from the mock transport."""

    expected_payload: dict[str, Any] | None
    """Expected return value from refresh_google_token."""


REFRESH_CASES: list[RefreshCase] = [
    RefreshCase(
        name="successful_refresh_with_refresh_token",
        response=httpx.Response(
            200,
            json={"access_token": "ya29.a0", "refresh_token": "1//new", "expires_in": 3599},
        ),
        expected_payload={
            "access_token": "ya29.a0",
            "refresh_token": "1//new",
            "expires_in": 3599,
        },
    ),
    RefreshCase(
        name="successful_refresh_omits_refresh_token_21691_case",
        response=httpx.Response(
            200,
            json={"access_token": "ya29.a0", "expires_in": 3599, "scope": "..."},
        ),
        expected_payload={
            "access_token": "ya29.a0",
            "expires_in": 3599,
            "scope": "...",
        },
    ),
    RefreshCase(
        name="malformed_response_returns_none",
        response=httpx.Response(200, text="not json"),
        expected_payload=None,
    ),
    RefreshCase(
        name="missing_access_token_returns_none",
        response=httpx.Response(200, json={"expires_in": 3599}),
        expected_payload=None,
    ),
    RefreshCase(
        name="error_status_returns_none",
        response=httpx.Response(401, json={"error": "invalid_grant"}),
        expected_payload=None,
    ),
]


@pytest.mark.parametrize(
    "case",
    [pytest.param(c, id=c.name) for c in REFRESH_CASES],
)
def test_refresh_google_token(case: RefreshCase) -> None:
    """refresh_google_token returns the parsed payload or None on error."""
    transport = _mock_transport([case.response])
    payload = refresh_google_token(
        "old-refresh",
        client_id=_TEST_CLIENT_ID,
        client_secret=_TEST_CLIENT_SECRET,
        endpoint=_TEST_ENDPOINT,
        transport=transport,
    )
    assert payload == case.expected_payload


def test_refresh_google_token_posts_form_with_client_secret() -> None:
    """The refresh request includes client_secret (Google's OAuth requires it)."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"access_token": "x", "expires_in": 100})

    refresh_google_token(
        "rt",
        client_id="cid",
        client_secret="csecret",
        endpoint=_TEST_ENDPOINT,
        transport=httpx.MockTransport(handler),
    )
    assert "grant_type=refresh_token" in captured["body"]
    assert "client_id=cid" in captured["body"]
    assert "client_secret=csecret" in captured["body"]
    assert "refresh_token=rt" in captured["body"]


def test_refresh_google_token_network_error_returns_none() -> None:
    """Network failures surface as None."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    transport = httpx.MockTransport(handler)
    result = refresh_google_token(
        "old-refresh",
        client_id=_TEST_CLIENT_ID,
        client_secret=_TEST_CLIENT_SECRET,
        endpoint=_TEST_ENDPOINT,
        transport=transport,
    )
    assert result is None


@dataclass
class ResolveCase:
    name: str
    """Descriptive name for the test scenario."""

    initial_creds: dict[str, Any]
    """Contents written to refresh_token_file before resolve()."""

    response: httpx.Response | None
    """Response from the mock transport (None means resolve should not call HTTP)."""

    expected_token: str | None
    """Expected access_token returned by resolve_google_token."""

    expected_disk_refresh: str | None = None
    """If set, disk file should contain this refresh_token after resolve()."""

    expected_disk_access: str | None = None
    """If set, disk file should contain this access_token after resolve()."""


def _now_ms() -> int:
    return int(time.time() * 1000)


RESOLVE_CASES: list[ResolveCase] = [
    ResolveCase(
        name="cached_token_with_headroom_returned_as_is",
        initial_creds={
            "access_token": "ya29.cached",
            "refresh_token": "1//rt",
            "expiry_date": _now_ms() + 600_000,
        },
        response=None,
        expected_token="ya29.cached",
    ),
    ResolveCase(
        name="near_expiry_triggers_refresh",
        initial_creds={
            "access_token": "ya29.stale",
            "refresh_token": "1//rt",
            "expiry_date": _now_ms() + 30_000,
        },
        response=httpx.Response(
            200,
            json={"access_token": "ya29.fresh", "refresh_token": "1//rotated", "expires_in": 3600},
        ),
        expected_token="ya29.fresh",
        expected_disk_refresh="1//rotated",
        expected_disk_access="ya29.fresh",
    ),
    ResolveCase(
        name="refresh_omits_refresh_token_preserves_disk_value_21691",
        initial_creds={
            "access_token": "ya29.stale",
            "refresh_token": "1//keep-this",
            "expiry_date": _now_ms() - 1000,
        },
        response=httpx.Response(
            200,
            json={"access_token": "ya29.fresh", "expires_in": 3600},
        ),
        expected_token="ya29.fresh",
        expected_disk_refresh="1//keep-this",
        expected_disk_access="ya29.fresh",
    ),
    ResolveCase(
        name="missing_refresh_token_in_disk_returns_none",
        initial_creds={"access_token": "stale", "expiry_date": _now_ms() - 1000},
        response=None,
        expected_token=None,
    ),
    ResolveCase(
        name="refresh_failure_returns_none",
        initial_creds={
            "access_token": "stale",
            "refresh_token": "1//rt",
            "expiry_date": _now_ms() - 1000,
        },
        response=httpx.Response(500, json={"error": "server_error"}),
        expected_token=None,
    ),
]


@pytest.mark.parametrize(
    "case",
    [pytest.param(c, id=c.name) for c in RESOLVE_CASES],
)
def test_resolve_google_token(case: ResolveCase, tmp_path: Path) -> None:
    """End-to-end resolver: read disk, refresh if needed, write back atomically."""
    creds_path = tmp_path / "oauth_creds.json"
    creds_path.write_text(json.dumps(case.initial_creds))

    source = GoogleOAuthSource(
        type="google_oauth",
        refresh_token_file=str(creds_path),
        client_id=_TEST_CLIENT_ID,
        client_secret=_TEST_CLIENT_SECRET,
        endpoint=_TEST_ENDPOINT,
    )

    transport = _mock_transport([case.response]) if case.response is not None else None
    token = resolve_google_token(source, transport=transport)

    assert token == case.expected_token

    if case.expected_disk_refresh is not None or case.expected_disk_access is not None:
        on_disk = json.loads(creds_path.read_text())
        if case.expected_disk_refresh is not None:
            assert on_disk["refresh_token"] == case.expected_disk_refresh
        if case.expected_disk_access is not None:
            assert on_disk["access_token"] == case.expected_disk_access
        mode = creds_path.stat().st_mode & 0o777
        assert mode == stat.S_IRUSR | stat.S_IWUSR


def test_resolve_missing_file_returns_none(tmp_path: Path) -> None:
    """No refresh-token file → resolve returns None."""
    source = GoogleOAuthSource(
        type="google_oauth",
        refresh_token_file=str(tmp_path / "missing.json"),
        client_id=_TEST_CLIENT_ID,
        client_secret=_TEST_CLIENT_SECRET,
    )
    assert resolve_google_token(source) is None


def test_custom_expiry_field_supported(tmp_path: Path) -> None:
    """``expiry_field`` lets non-gemini-cli JSON layouts work without renaming keys on disk."""
    creds_path = tmp_path / "creds.json"
    creds_path.write_text(
        json.dumps(
            {
                "access_token": "tok",
                "refresh_token": "rt",
                "expires_at_ms": _now_ms() + 600_000,
            }
        )
    )

    source = GoogleOAuthSource(
        type="google_oauth",
        refresh_token_file=str(creds_path),
        client_id=_TEST_CLIENT_ID,
        client_secret=_TEST_CLIENT_SECRET,
        expiry_field="expires_at_ms",
    )
    assert resolve_google_token(source) == "tok"
