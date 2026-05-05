# ruff: noqa: S105, S106
"""Tests for GoogleAuthSource end-to-end resolve behavior.

Covers the Google-specific ``_build_refresh_body`` (requires client_secret),
the ``expiry_path = "expiry_date"`` default override matching gemini-cli,
and the inherited ``AuthSource.resolve()`` template against
``httpx.MockTransport``.

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

from ccproxy.oauth.sources import GoogleAuthSource

_TEST_CLIENT_ID = "681255809395-test.apps.googleusercontent.com"
_TEST_CLIENT_SECRET = "GOCSPX-test"
_TEST_ENDPOINT = "https://oauth.test.example/token"


def _mock_transport(responses: list[httpx.Response]) -> httpx.MockTransport:
    iter_responses = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        return next(iter_responses)

    return httpx.MockTransport(handler)


def test_default_expiry_path_matches_gemini_cli() -> None:
    """gemini-cli writes ``expiry_date`` (ms since epoch); our default matches."""
    assert GoogleAuthSource.model_fields["expiry_path"].default == "expiry_date"


def test_default_file_path_matches_gemini_cli() -> None:
    """gemini-cli writes ``~/.gemini/oauth_creds.json``; our default matches."""
    assert GoogleAuthSource.model_fields["file_path"].default == "~/.gemini/oauth_creds.json"


def test_build_refresh_body_includes_client_secret() -> None:
    """Google's OAuth requires client_secret in the refresh request."""
    source = GoogleAuthSource(
        client_id="cid",
        client_secret="csecret",
        endpoint=_TEST_ENDPOINT,
    )
    body = source._build_refresh_body("rt")
    assert body == {
        "grant_type": "refresh_token",
        "client_id": "cid",
        "client_secret": "csecret",
        "refresh_token": "rt",
    }


def test_build_refresh_body_without_client_secret_raises() -> None:
    """Constructing a GoogleAuthSource without client_secret is allowed
    (matches AuthSource.client_secret optional default), but actually
    issuing a refresh body must raise — the upstream POST would 400."""
    source = GoogleAuthSource(
        client_id="cid",
        endpoint=_TEST_ENDPOINT,
    )
    with pytest.raises(ValueError, match="GoogleAuthSource requires client_secret"):
        source._build_refresh_body("rt")


def test_refresh_token_form_includes_client_secret() -> None:
    """The HTTP refresh wire body includes client_secret."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"access_token": "x", "expires_in": 100})

    source = GoogleAuthSource(
        client_id="cid",
        client_secret="csecret",
        endpoint=_TEST_ENDPOINT,
    )
    source._refresh_token("rt", transport=httpx.MockTransport(handler))

    assert "grant_type=refresh_token" in captured["body"]
    assert "client_id=cid" in captured["body"]
    assert "client_secret=csecret" in captured["body"]
    assert "refresh_token=rt" in captured["body"]


@dataclass
class RefreshCase:
    name: str
    """Descriptive name for the test scenario."""

    response: httpx.Response
    """httpx.Response to return from the mock transport."""

    expected_payload: dict[str, Any] | None
    """Expected return value from _refresh_token."""


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
def test_refresh_token_returns_payload_or_none(case: RefreshCase) -> None:
    """_refresh_token returns the parsed payload or None on error."""
    source = GoogleAuthSource(
        client_id=_TEST_CLIENT_ID,
        client_secret=_TEST_CLIENT_SECRET,
        endpoint=_TEST_ENDPOINT,
    )
    transport = _mock_transport([case.response])
    payload = source._refresh_token("old-refresh", transport=transport)
    assert payload == case.expected_payload


def test_refresh_token_network_error_returns_none() -> None:
    """Network failures surface as None."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    source = GoogleAuthSource(
        client_id=_TEST_CLIENT_ID,
        client_secret=_TEST_CLIENT_SECRET,
        endpoint=_TEST_ENDPOINT,
    )
    result = source._refresh_token("old-refresh", transport=httpx.MockTransport(handler))
    assert result is None


@dataclass
class ResolveCase:
    name: str
    """Descriptive name for the test scenario."""

    initial_creds: dict[str, Any]
    """Contents written to file_path before resolve()."""

    response: httpx.Response | None
    """Response from the mock transport (None means resolve should not call HTTP)."""

    expected_token: str | None
    """Expected access_token returned by resolve()."""

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
def test_resolve_end_to_end(case: ResolveCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end resolve: read disk, refresh if needed, write back atomically."""
    creds_path = tmp_path / "oauth_creds.json"
    creds_path.write_text(json.dumps(case.initial_creds))

    source = GoogleAuthSource(
        file_path=str(creds_path),
        client_id=_TEST_CLIENT_ID,
        client_secret=_TEST_CLIENT_SECRET,
        endpoint=_TEST_ENDPOINT,
    )

    if case.response is not None:
        transport = _mock_transport([case.response])
        monkeypatch.setattr(
            source,
            "_refresh_token",
            lambda rt: GoogleAuthSource._refresh_token(source, rt, transport=transport),
        )

    token = source.resolve()
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
    """No credential file → resolve returns None."""
    source = GoogleAuthSource(
        file_path=str(tmp_path / "missing.json"),
        client_id=_TEST_CLIENT_ID,
        client_secret=_TEST_CLIENT_SECRET,
    )
    assert source.resolve() is None


def test_custom_expiry_path_supported(tmp_path: Path) -> None:
    """``expiry_path`` lets non-gemini-cli JSON layouts work without renaming keys."""
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

    source = GoogleAuthSource(
        file_path=str(creds_path),
        client_id=_TEST_CLIENT_ID,
        client_secret=_TEST_CLIENT_SECRET,
        expiry_path="expires_at_ms",
    )
    assert source.resolve() == "tok"
