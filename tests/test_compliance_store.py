"""Tests for compliance ProfileStore persistence and profile management."""

import json
from pathlib import Path

import pytest

from ccproxy.compliance.models import (
    ComplianceProfile,
    ObservationAccumulator,
    ObservationBundle,
    ProfileFeatureHeader,
)
from ccproxy.compliance.store import ProfileStore, _build_anthropic_seed_profile


@pytest.fixture()
def store_path(tmp_path: Path) -> Path:
    return tmp_path / "compliance_profiles.json"


@pytest.fixture()
def store(store_path: Path) -> ProfileStore:
    return ProfileStore(store_path, seed_profiles=None)


def _make_profile(
    provider: str = "anthropic",
    ua: str = "cli/1.0",
    headers: list[ProfileFeatureHeader] | None = None,
    updated_at: str = "2025-01-01T00:00:00+00:00",
) -> ComplianceProfile:
    return ComplianceProfile(
        provider=provider,
        user_agent=ua,
        created_at="2025-01-01T00:00:00+00:00",
        updated_at=updated_at,
        observation_count=1,
        is_complete=True,
        headers=headers or [ProfileFeatureHeader(name="x-app", value="cli")],
        body_fields=[],
    )


class TestSetProfile:
    def test_stores_and_retrieves(self, store: ProfileStore):
        profile = _make_profile()
        store.set_profile("anthropic/seed", profile)
        result = store.get_profile("anthropic")
        assert result is not None
        assert result.provider == "anthropic"

    def test_overwrites_existing(self, store: ProfileStore):
        p1 = _make_profile(ua="old")
        p2 = _make_profile(ua="new", updated_at="2026-01-01T00:00:00+00:00")
        store.set_profile("anthropic/seed", p1)
        store.set_profile("anthropic/seed", p2)
        result = store.get_profile("anthropic")
        assert result is not None
        assert result.user_agent == "new"


class TestGetBestProfile:
    def test_returns_none_when_empty(self, store: ProfileStore):
        assert store.get_profile("anthropic") is None

    def test_returns_none_for_wrong_provider(self, store: ProfileStore):
        store.set_profile("gemini/seed", _make_profile(provider="gemini"))
        assert store.get_profile("anthropic") is None

    def test_returns_most_recent(self, store: ProfileStore):
        p1 = _make_profile(ua="cli/1.0", updated_at="2025-01-01T00:00:00+00:00")
        p2 = _make_profile(ua="cli/2.0", updated_at="2025-06-01T00:00:00+00:00")
        store.set_profile("anthropic/v1", p1)
        store.set_profile("anthropic/v2", p2)
        result = store.get_profile("anthropic")
        assert result is not None
        assert result.user_agent == "cli/2.0"

    def test_multiple_providers(self, store: ProfileStore):
        store.set_profile("anthropic/seed", _make_profile(provider="anthropic"))
        store.set_profile("gemini/seed", _make_profile(provider="gemini"))
        assert store.get_profile("anthropic") is not None
        assert store.get_profile("gemini") is not None
        assert store.get_profile("openai") is None


class TestPersistence:
    def test_persists_to_disk(self, store_path: Path):
        store = ProfileStore(store_path, seed_profiles=None)
        store.set_profile("anthropic/seed", _make_profile())
        assert store_path.exists()
        data = json.loads(store_path.read_text())
        assert data["format_version"] == 1
        assert len(data["profiles"]) == 1

    def test_loads_from_disk(self, store_path: Path):
        store1 = ProfileStore(store_path, seed_profiles=None)
        store1.set_profile("anthropic/seed", _make_profile())

        store2 = ProfileStore(store_path, seed_profiles=None)
        profile = store2.get_profile("anthropic")
        assert profile is not None
        assert profile.is_complete is True

    def test_handles_malformed_file(self, store_path: Path):
        store_path.write_text("not json")
        store = ProfileStore(store_path, seed_profiles=None)
        assert store.get_profile("anthropic") is None

    def test_handles_wrong_version(self, store_path: Path):
        store_path.write_text(json.dumps({"format_version": 99}))
        store = ProfileStore(store_path, seed_profiles=None)
        assert store.get_profile("anthropic") is None

    def test_degraded_on_version_mismatch_with_data(self, store_path: Path):
        store_path.write_text(
            json.dumps(
                {
                    "format_version": 99,
                    "profiles": {"anthropic/v0": {}},
                }
            )
        )
        store = ProfileStore(store_path, seed_profiles=None)
        assert store.is_degraded is True
        assert store.get_profile("anthropic") is None

    def test_not_degraded_on_version_mismatch_without_data(self, store_path: Path):
        store_path.write_text(json.dumps({"format_version": 99}))
        store = ProfileStore(store_path, seed_profiles=None)
        assert store.is_degraded is False

    def test_not_degraded_on_valid_file(self, store_path: Path):
        store = ProfileStore(store_path, seed_profiles=None)
        store.set_profile("anthropic/seed", _make_profile())
        store2 = ProfileStore(store_path, seed_profiles=None)
        assert store2.is_degraded is False

    def test_ignores_legacy_accumulators_key(self, store_path: Path):
        store_path.write_text(
            json.dumps(
                {
                    "format_version": 1,
                    "profiles": {},
                    "accumulators": {"anthropic/cli": {"provider": "anthropic"}},
                }
            )
        )
        store = ProfileStore(store_path, seed_profiles=None)
        assert store.get_profile("anthropic") is None


class TestAnthropicSeed:
    def test_seeds_on_first_run(self, store_path: Path):
        store = ProfileStore(store_path, seed_profiles=[_build_anthropic_seed_profile()])
        profile = store.get_profile("anthropic")
        assert profile is not None
        assert profile.user_agent == "v0-seed"
        names = {h.name for h in profile.headers}
        assert "anthropic-beta" in names
        assert "anthropic-version" in names
        assert profile.system is not None

    def test_skips_seed_if_profile_exists(self, store_path: Path):
        store1 = ProfileStore(store_path, seed_profiles=None)
        existing = _make_profile(ua="real-cli", updated_at="2026-01-01T00:00:00+00:00")
        store1.set_profile("anthropic/real-cli", existing)

        store2 = ProfileStore(store_path, seed_profiles=[_build_anthropic_seed_profile()])
        profile = store2.get_profile("anthropic")
        assert profile is not None
        assert profile.user_agent == "real-cli"

    def test_seed_disabled(self, store_path: Path):
        store = ProfileStore(store_path, seed_profiles=None)
        assert store.get_profile("anthropic") is None

    def test_multiple_seed_profiles(self, store_path: Path):
        seed_openai = ComplianceProfile(
            provider="openai",
            user_agent="v0-seed",
            created_at="1970-01-01T00:00:00+00:00",
            updated_at="1970-01-01T00:00:00+00:00",
            observation_count=0,
            is_complete=True,
            headers=[],
            body_fields=[],
        )
        store = ProfileStore(
            store_path,
            seed_profiles=[_build_anthropic_seed_profile(), seed_openai],
        )
        assert store.get_profile("anthropic") is not None
        assert store.get_profile("openai") is not None


class TestGetAllProfiles:
    def test_returns_all(self, store_path: Path):
        store = ProfileStore(store_path, seed_profiles=None)
        store.set_profile("a/seed", _make_profile(provider="a"))
        store.set_profile("b/seed", _make_profile(provider="b"))
        profiles = store.get_all_profiles()
        assert len(profiles) == 2


class TestAccumulatorFinalize:
    """Test that ObservationAccumulator (used ephemerally by ComplianceSeeder) still works."""

    def test_stable_headers(self):
        acc = ObservationAccumulator(provider="anthropic", user_agent="cli/1.0")
        for _ in range(3):
            acc.submit(ObservationBundle(
                provider="anthropic",
                user_agent="cli/1.0",
                headers={"x-app": "cli", "beta": "flag1"},
                body_envelope={},
            ))
        profile = acc.finalize()
        names = {h.name for h in profile.headers}
        assert "x-app" in names
        assert "beta" in names

    def test_variable_headers_excluded(self):
        acc = ObservationAccumulator(provider="anthropic", user_agent="cli/1.0")
        for i in range(3):
            acc.submit(ObservationBundle(
                provider="anthropic",
                user_agent="cli/1.0",
                headers={"x-app": "cli", "x-req-id": f"r{i}"},
                body_envelope={},
            ))
        profile = acc.finalize()
        names = {h.name for h in profile.headers}
        assert "x-app" in names
        assert "x-req-id" not in names
