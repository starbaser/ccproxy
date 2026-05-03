# ruff: noqa: S106
"""Tests for ccproxy.oauth.anthropic in-process OAuth refresh.

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

from ccproxy.oauth.anthropic import refresh_anthropic_token, resolve_anthropic_token
from ccproxy.oauth.sources import AnthropicOAuthSource

_TEST_CLIENT_ID = "test-client-id"
_TEST_ENDPOINT = "https://oauth.test.example/v1/oauth/token"


def _mock_transport(responses: list[httpx.Response]) -> httpx.MockTransport:
    """Build a MockTransport that yields successive responses per call."""
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
    """Expected return value from refresh_anthropic_token."""


REFRESH_CASES: list[RefreshCase] = [
    RefreshCase(
        name="successful_refresh",
        response=httpx.Response(
            200,
            json={"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600},
        ),
        expected_payload={
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
        },
    ),
    RefreshCase(
        name="rotated_refresh_token",
        response=httpx.Response(
            200,
            json={"access_token": "new-access", "refresh_token": "rotated", "expires_in": 7200},
        ),
        expected_payload={
            "access_token": "new-access",
            "refresh_token": "rotated",
            "expires_in": 7200,
        },
    ),
    RefreshCase(
        name="malformed_response_returns_none",
        response=httpx.Response(200, text="not json"),
        expected_payload=None,
    ),
    RefreshCase(
        name="missing_access_token_returns_none",
        response=httpx.Response(200, json={"refresh_token": "x"}),
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
def test_refresh_anthropic_token(case: RefreshCase) -> None:
    """refresh_anthropic_token returns the parsed payload or None on error."""
    transport = _mock_transport([case.response])
    payload = refresh_anthropic_token(
        "old-refresh",
        client_id=_TEST_CLIENT_ID,
        endpoint=_TEST_ENDPOINT,
        transport=transport,
    )
    assert payload == case.expected_payload


def test_refresh_anthropic_token_network_error_returns_none() -> None:
    """Network failures surface as None (caller logs and falls back)."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    transport = httpx.MockTransport(handler)
    result = refresh_anthropic_token(
        "old-refresh",
        client_id=_TEST_CLIENT_ID,
        endpoint=_TEST_ENDPOINT,
        transport=transport,
    )
    assert result is None


def test_refresh_anthropic_token_posts_form_encoded(tmp_path: Path) -> None:
    """The refresh request uses application/x-www-form-urlencoded with the right fields."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"access_token": "x", "expires_in": 100})

    refresh_anthropic_token(
        "rt",
        client_id="cid",
        endpoint=_TEST_ENDPOINT,
        transport=httpx.MockTransport(handler),
    )
    assert captured["url"] == _TEST_ENDPOINT
    assert captured["headers"]["content-type"] == "application/x-www-form-urlencoded"
    assert "grant_type=refresh_token" in captured["body"]
    assert "client_id=cid" in captured["body"]
    assert "refresh_token=rt" in captured["body"]


@dataclass
class ResolveCase:
    name: str
    """Descriptive name for the test scenario."""

    initial_creds: dict[str, Any]
    """Contents written to refresh_token_file before resolve()."""

    response: httpx.Response | None
    """Response from the mock transport (None means resolve should not call HTTP)."""

    expected_token: str | None
    """Expected access_token returned by resolve_anthropic_token."""

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
            "access_token": "cached",
            "refresh_token": "rt",
            "expires_at": _now_ms() + 600_000,  # 10 min from now
        },
        response=None,
        expected_token="cached",
    ),
    ResolveCase(
        name="near_expiry_triggers_refresh",
        initial_creds={
            "access_token": "stale",
            "refresh_token": "rt",
            "expires_at": _now_ms() + 30_000,  # 30s — within 60s headroom
        },
        response=httpx.Response(
            200,
            json={"access_token": "fresh", "refresh_token": "rt-new", "expires_in": 3600},
        ),
        expected_token="fresh",
        expected_disk_refresh="rt-new",
        expected_disk_access="fresh",
    ),
    ResolveCase(
        name="refresh_response_omits_refresh_token_preserves_disk",
        initial_creds={
            "access_token": "stale",
            "refresh_token": "rt-keep",
            "expires_at": _now_ms() - 1000,  # already expired
        },
        response=httpx.Response(
            200,
            json={"access_token": "fresh", "expires_in": 3600},  # no refresh_token
        ),
        expected_token="fresh",
        expected_disk_refresh="rt-keep",
        expected_disk_access="fresh",
    ),
    ResolveCase(
        name="missing_refresh_token_in_disk_returns_none",
        initial_creds={"access_token": "stale", "expires_at": _now_ms() - 1000},
        response=None,
        expected_token=None,
    ),
    ResolveCase(
        name="refresh_failure_returns_none",
        initial_creds={
            "access_token": "stale",
            "refresh_token": "rt",
            "expires_at": _now_ms() - 1000,
        },
        response=httpx.Response(500, json={"error": "server_error"}),
        expected_token=None,
    ),
]


@pytest.mark.parametrize(
    "case",
    [pytest.param(c, id=c.name) for c in RESOLVE_CASES],
)
def test_resolve_anthropic_token(case: ResolveCase, tmp_path: Path) -> None:
    """End-to-end resolver: read disk, refresh if needed, write back."""
    creds_path = tmp_path / "anthropic.json"
    creds_path.write_text(json.dumps(case.initial_creds))

    source = AnthropicOAuthSource(
        type="anthropic_oauth",
        refresh_token_file=str(creds_path),
        client_id=_TEST_CLIENT_ID,
        endpoint=_TEST_ENDPOINT,
    )

    transport = _mock_transport([case.response]) if case.response is not None else None
    token = resolve_anthropic_token(source, transport=transport)

    assert token == case.expected_token

    if case.expected_disk_refresh is not None or case.expected_disk_access is not None:
        on_disk = json.loads(creds_path.read_text())
        if case.expected_disk_refresh is not None:
            assert on_disk["refresh_token"] == case.expected_disk_refresh
        if case.expected_disk_access is not None:
            assert on_disk["access_token"] == case.expected_disk_access
        # After atomic_write_back, the file should be mode 0o600.
        mode = creds_path.stat().st_mode & 0o777
        assert mode == stat.S_IRUSR | stat.S_IWUSR


def test_resolve_missing_file_returns_none(tmp_path: Path) -> None:
    """No refresh-token file → resolve returns None."""
    source = AnthropicOAuthSource(
        type="anthropic_oauth",
        refresh_token_file=str(tmp_path / "missing.json"),
        client_id=_TEST_CLIENT_ID,
        endpoint=_TEST_ENDPOINT,
    )
    assert resolve_anthropic_token(source) is None
