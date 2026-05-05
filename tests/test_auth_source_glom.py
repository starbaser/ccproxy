# ruff: noqa: S105
"""Narrow tests for ``AuthSource._read_credentials`` and ``_write_credentials``.

These exercise the glom machinery in isolation so failures point at
read/write semantics, not at the surrounding refresh dance.
"""

from __future__ import annotations

from typing import Any, Literal

from ccproxy.oauth.sources import AuthSource


class _TestableAuthSource(AuthSource):
    type: Literal["test"] = "test"

    def _build_refresh_body(self, refresh_token: str) -> dict[str, str]:
        return {"refresh_token": refresh_token}


def _make(
    *, access: str = "access_token", refresh: str = "refresh_token", expiry: str = "expires_at"
) -> _TestableAuthSource:
    return _TestableAuthSource(
        file_path="/dev/null",
        endpoint="https://example.invalid/token",
        client_id="cid",
        access_path=access,
        refresh_path=refresh,
        expiry_path=expiry,
    )


def test_read_present_paths_returns_values() -> None:
    """When all three glom paths resolve, _read_credentials returns the values."""
    source = _make()
    creds = {"access_token": "a", "refresh_token": "r", "expires_at": 12345}
    access, refresh, expiry = source._read_credentials(creds)
    assert access == "a"
    assert refresh == "r"
    assert expiry == 12345


def test_read_absent_paths_returns_none() -> None:
    """When a glom path doesn't resolve, _read_credentials returns None for that slot."""
    source = _make()
    creds: dict[str, Any] = {}
    access, refresh, expiry = source._read_credentials(creds)
    assert access is None
    assert refresh is None
    assert expiry is None


def test_read_partial_paths_returns_partial_none() -> None:
    """Missing fields surface as None; present fields are returned."""
    source = _make()
    creds = {"access_token": "a"}
    access, refresh, expiry = source._read_credentials(creds)
    assert access == "a"
    assert refresh is None
    assert expiry is None


def test_read_nested_paths_resolve_with_glom() -> None:
    """Glom dot-paths read into nested dicts."""
    source = _make(
        access="claudeAiOauth.accessToken",
        refresh="claudeAiOauth.refreshToken",
        expiry="claudeAiOauth.expiresAt",
    )
    creds = {
        "claudeAiOauth": {
            "accessToken": "a",
            "refreshToken": "r",
            "expiresAt": 99999,
        }
    }
    assert source._read_credentials(creds) == ("a", "r", 99999)


def test_write_creates_intermediate_dicts_for_nested_paths() -> None:
    """``glom.assign(..., missing=dict)`` creates intermediate dicts on demand."""
    source = _make(
        access="claudeAiOauth.accessToken",
        refresh="claudeAiOauth.refreshToken",
        expiry="claudeAiOauth.expiresAt",
    )
    creds: dict[str, Any] = {}
    merged = source._write_credentials(creds, "fresh", "new-rt", 222)
    assert merged["claudeAiOauth"]["accessToken"] == "fresh"
    assert merged["claudeAiOauth"]["refreshToken"] == "new-rt"
    assert merged["claudeAiOauth"]["expiresAt"] == 222


def test_write_preserves_existing_siblings() -> None:
    """Sibling fields at each path level survive verbatim (deep-copied input)."""
    source = _make(
        access="claudeAiOauth.accessToken",
        refresh="claudeAiOauth.refreshToken",
        expiry="claudeAiOauth.expiresAt",
    )
    creds = {
        "claudeAiOauth": {
            "accessToken": "old",
            "refreshToken": "rt",
            "expiresAt": 1000,
            "scopes": ["a", "b"],
            "subscriptionType": "max",
        },
        "topLevelExtra": {"keep": True},
    }
    merged = source._write_credentials(creds, "fresh", "new-rt", 222)
    assert merged["claudeAiOauth"]["scopes"] == ["a", "b"]
    assert merged["claudeAiOauth"]["subscriptionType"] == "max"
    assert merged["topLevelExtra"] == {"keep": True}


def test_write_overwrites_existing_value_at_path() -> None:
    """Existing access/refresh/expiry values at the target paths are overwritten."""
    source = _make()
    creds = {
        "access_token": "old-access",
        "refresh_token": "old-refresh",
        "expires_at": 1,
    }
    merged = source._write_credentials(creds, "new-access", "new-refresh", 222)
    assert merged["access_token"] == "new-access"
    assert merged["refresh_token"] == "new-refresh"
    assert merged["expires_at"] == 222


def test_write_does_not_mutate_input() -> None:
    """Input dict must be deep-copied so the caller's view is untouched."""
    source = _make()
    creds = {"access_token": "old", "refresh_token": "rt", "expires_at": 1}
    pre = dict(creds)
    source._write_credentials(creds, "new", "new-rt", 222)
    assert creds == pre
