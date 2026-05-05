# ruff: noqa: S105
"""Tests for the ``AuthSource`` base-class template method.

Covers the read → maybe-refresh → write-back flow against parametrized
credential schemas: the flat ccproxy-native layout and the nested
``claudeAiOauth.*`` layout used by Claude Code CLI's
``~/.claude/.credentials.json``.

All "tokens" in this file are synthetic fixture values, not real secrets.
"""

from __future__ import annotations

import json
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx
import pytest

from ccproxy.oauth.sources import AuthSource


class _TestableAuthSource(AuthSource):
    """Concrete AuthSource that posts a stable refresh body for assertions."""

    type: Literal["test"] = "test"

    def _build_refresh_body(self, refresh_token: str) -> dict[str, str]:
        return {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }


def _now_ms() -> int:
    return int(time.time() * 1000)


def _mock_transport(responses: list[httpx.Response]) -> httpx.MockTransport:
    iter_responses = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        return next(iter_responses)

    return httpx.MockTransport(handler)


def _make_source(
    *,
    file_path: Path,
    access_path: str = "access_token",
    refresh_path: str = "refresh_token",
    expiry_path: str = "expires_at",
    transport: httpx.BaseTransport | None = None,
) -> _TestableAuthSource:
    """Build a TestableAuthSource. Patches ``_refresh_token`` to inject the transport."""
    source = _TestableAuthSource(
        file_path=str(file_path),
        endpoint="https://oauth.test.example/token",
        client_id="cid",
        access_path=access_path,
        refresh_path=refresh_path,
        expiry_path=expiry_path,
    )
    if transport is not None:
        original_refresh = AuthSource._refresh_token

        def _wrapped(rt: str) -> Any:
            return original_refresh(source, rt, transport=transport)

        source._refresh_token = _wrapped  # type: ignore[method-assign]
    return source


@dataclass(frozen=True)
class SchemaCase:
    """A credential-schema test case parametrized over flat vs nested layouts."""

    name: str
    """Descriptive name for the test scenario (used as test ID)."""

    access_path: str
    """glom path for the access_token in the credential JSON."""

    refresh_path: str
    """glom path for the refresh_token."""

    expiry_path: str
    """glom path for the expiry timestamp."""

    creds: dict[str, Any]
    """Initial on-disk credential JSON (writable to a temp file)."""


SCHEMA_CASES: list[SchemaCase] = [
    SchemaCase(
        name="flat_ccproxy",
        access_path="access_token",
        refresh_path="refresh_token",
        expiry_path="expires_at",
        creds={"access_token": "old", "refresh_token": "rt", "expires_at": 1000},
    ),
    SchemaCase(
        name="claude_code_cli",
        access_path="claudeAiOauth.accessToken",
        refresh_path="claudeAiOauth.refreshToken",
        expiry_path="claudeAiOauth.expiresAt",
        creds={
            "claudeAiOauth": {
                "accessToken": "old",
                "refreshToken": "rt",
                "expiresAt": 1000,
                "scopes": ["org:create_api_key", "user:profile"],
                "subscriptionType": "max",
            },
        },
    ),
]


@pytest.mark.parametrize(
    "case",
    [pytest.param(c, id=c.name) for c in SCHEMA_CASES],
)
def test_resolve_reads_via_glom_paths(case: SchemaCase, tmp_path: Path) -> None:
    """resolve() reads access_token at ``access_path``; cached + valid → returned as-is."""
    creds = json.loads(json.dumps(case.creds))  # deep copy
    # Make the cached access_token live with plenty of headroom.
    if case.name == "flat_ccproxy":
        creds["access_token"] = "cached"
        creds["expires_at"] = _now_ms() + 600_000
    else:
        creds["claudeAiOauth"]["accessToken"] = "cached"
        creds["claudeAiOauth"]["expiresAt"] = _now_ms() + 600_000

    creds_path = tmp_path / "creds.json"
    creds_path.write_text(json.dumps(creds))

    source = _make_source(
        file_path=creds_path,
        access_path=case.access_path,
        refresh_path=case.refresh_path,
        expiry_path=case.expiry_path,
    )
    assert source.resolve() == "cached"


@pytest.mark.parametrize(
    "case",
    [pytest.param(c, id=c.name) for c in SCHEMA_CASES],
)
def test_resolve_writes_via_glom_paths(case: SchemaCase, tmp_path: Path) -> None:
    """resolve() refreshes when expired and writes new tokens at the configured paths."""
    creds = json.loads(json.dumps(case.creds))
    # Force expiry → refresh.
    if case.name == "flat_ccproxy":
        creds["expires_at"] = _now_ms() - 1000
    else:
        creds["claudeAiOauth"]["expiresAt"] = _now_ms() - 1000

    creds_path = tmp_path / "creds.json"
    creds_path.write_text(json.dumps(creds))

    transport = _mock_transport(
        [
            httpx.Response(
                200,
                json={"access_token": "fresh", "refresh_token": "new-rt", "expires_in": 3600},
            )
        ]
    )
    source = _make_source(
        file_path=creds_path,
        access_path=case.access_path,
        refresh_path=case.refresh_path,
        expiry_path=case.expiry_path,
        transport=transport,
    )
    assert source.resolve() == "fresh"

    on_disk = json.loads(creds_path.read_text())
    if case.name == "flat_ccproxy":
        assert on_disk["access_token"] == "fresh"
        assert on_disk["refresh_token"] == "new-rt"
    else:
        assert on_disk["claudeAiOauth"]["accessToken"] == "fresh"
        assert on_disk["claudeAiOauth"]["refreshToken"] == "new-rt"


def test_write_preserves_claude_code_siblings(tmp_path: Path) -> None:
    """Writing claudeAiOauth.accessToken must not drop scopes/subscriptionType siblings."""
    creds = {
        "claudeAiOauth": {
            "accessToken": "old",
            "refreshToken": "rt",
            "expiresAt": _now_ms() - 1000,
            "scopes": ["org:create_api_key", "user:profile"],
            "subscriptionType": "max",
        },
    }
    creds_path = tmp_path / "claude.json"
    creds_path.write_text(json.dumps(creds))

    transport = _mock_transport(
        [
            httpx.Response(
                200,
                json={"access_token": "fresh", "refresh_token": "rt-new", "expires_in": 36_000},
            )
        ]
    )
    source = _make_source(
        file_path=creds_path,
        access_path="claudeAiOauth.accessToken",
        refresh_path="claudeAiOauth.refreshToken",
        expiry_path="claudeAiOauth.expiresAt",
        transport=transport,
    )
    assert source.resolve() == "fresh"

    on_disk = json.loads(creds_path.read_text())
    assert on_disk["claudeAiOauth"]["accessToken"] == "fresh"
    assert on_disk["claudeAiOauth"]["refreshToken"] == "rt-new"
    assert on_disk["claudeAiOauth"]["scopes"] == ["org:create_api_key", "user:profile"]
    assert on_disk["claudeAiOauth"]["subscriptionType"] == "max"
    mode = creds_path.stat().st_mode & 0o777
    assert mode == stat.S_IRUSR | stat.S_IWUSR


def test_resolve_missing_file_returns_none(tmp_path: Path) -> None:
    """No credential file → resolve returns None."""
    source = _make_source(file_path=tmp_path / "missing.json")
    assert source.resolve() is None


def test_resolve_corrupt_json_returns_none(tmp_path: Path) -> None:
    """Malformed credential JSON → resolve returns None."""
    creds_path = tmp_path / "bad.json"
    creds_path.write_text("not json{")
    source = _make_source(file_path=creds_path)
    assert source.resolve() is None


def test_resolve_missing_refresh_token_returns_none(tmp_path: Path) -> None:
    """Credential file present but missing refresh_token → resolve returns None."""
    creds_path = tmp_path / "no-rt.json"
    creds_path.write_text(json.dumps({"access_token": "x", "expires_at": _now_ms() - 1000}))
    source = _make_source(file_path=creds_path)
    assert source.resolve() is None


def test_resolve_response_omits_refresh_token_preserves_disk(tmp_path: Path) -> None:
    """gemini-cli #21691 workaround: keep on-disk refresh_token when response omits it."""
    creds_path = tmp_path / "creds.json"
    creds_path.write_text(
        json.dumps(
            {
                "access_token": "stale",
                "refresh_token": "preserve-me",
                "expires_at": _now_ms() - 1000,
            }
        )
    )

    transport = _mock_transport(
        [
            httpx.Response(
                200,
                json={"access_token": "fresh", "expires_in": 3600},
            )
        ]
    )
    source = _make_source(file_path=creds_path, transport=transport)
    assert source.resolve() == "fresh"

    on_disk = json.loads(creds_path.read_text())
    assert on_disk["access_token"] == "fresh"
    assert on_disk["refresh_token"] == "preserve-me"


def test_resolve_refresh_failure_returns_none(tmp_path: Path) -> None:
    """HTTP refresh failure (5xx, network error, etc.) → resolve returns None."""
    creds_path = tmp_path / "creds.json"
    creds_path.write_text(
        json.dumps(
            {
                "access_token": "stale",
                "refresh_token": "rt",
                "expires_at": _now_ms() - 1000,
            }
        )
    )

    transport = _mock_transport([httpx.Response(503, text="upstream error")])
    source = _make_source(file_path=creds_path, transport=transport)
    assert source.resolve() is None


def test_resolve_response_missing_access_token_returns_none(tmp_path: Path) -> None:
    """Refresh response that has no access_token → resolve returns None."""
    creds_path = tmp_path / "creds.json"
    creds_path.write_text(
        json.dumps(
            {
                "access_token": "stale",
                "refresh_token": "rt",
                "expires_at": _now_ms() - 1000,
            }
        )
    )

    transport = _mock_transport([httpx.Response(200, json={"expires_in": 3600})])
    source = _make_source(file_path=creds_path, transport=transport)
    assert source.resolve() is None


def test_resolve_uses_default_expires_in_when_response_omits_it(tmp_path: Path) -> None:
    """Refresh response without ``expires_in`` → use ``default_expires_in_seconds``."""
    creds_path = tmp_path / "creds.json"
    creds_path.write_text(
        json.dumps(
            {
                "access_token": "stale",
                "refresh_token": "rt",
                "expires_at": _now_ms() - 1000,
            }
        )
    )

    transport = _mock_transport([httpx.Response(200, json={"access_token": "fresh", "refresh_token": "rt"})])
    source = _make_source(file_path=creds_path, transport=transport)
    # Override default_expires_in_seconds for a precise assertion.
    source.default_expires_in_seconds = 7200

    before_ms = _now_ms()
    assert source.resolve() == "fresh"
    after_ms = _now_ms()

    on_disk = json.loads(creds_path.read_text())
    new_expiry = on_disk["expires_at"]
    # Expiry should land in [before + 2h, after + 2h] in milliseconds.
    assert before_ms + 7200 * 1000 <= new_expiry <= after_ms + 7200 * 1000


def test_build_refresh_body_unimplemented_on_base() -> None:
    """The base class's _build_refresh_body raises NotImplementedError."""
    # AuthSource is the base; subclasses must override _build_refresh_body.
    # We construct one indirectly through the test subclass to satisfy the
    # mandatory ``type`` discriminator, then call the base method directly.
    source = _TestableAuthSource(
        file_path="/dev/null",
        endpoint="https://example.invalid/token",
        client_id="cid",
    )
    with pytest.raises(NotImplementedError):
        AuthSource._build_refresh_body(source, "rt")
