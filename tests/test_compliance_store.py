"""Tests for compliance ProfileStore persistence and observation pipeline."""

import json
from pathlib import Path

import pytest

from ccproxy.compliance.models import ComplianceProfile, ObservationBundle
from ccproxy.compliance.store import ProfileStore, _build_anthropic_seed_profile


@pytest.fixture()
def store_path(tmp_path: Path) -> Path:
    return tmp_path / "compliance_profiles.json"


@pytest.fixture()
def store(store_path: Path) -> ProfileStore:
    return ProfileStore(store_path, min_observations=3, seed_profiles=None)


def _bundle(provider: str = "anthropic", ua: str = "cli/1.0", **kwargs) -> ObservationBundle:
    return ObservationBundle(
        provider=provider,
        user_agent=ua,
        headers=kwargs.get("headers", {"x-app": "cli"}),
        body_envelope=kwargs.get("body_envelope", {}),
        system=kwargs.get("system"),
    )


class TestSubmitObservation:
    def test_accumulates_observations(self, store: ProfileStore):
        store.submit_observation(_bundle())
        assert store.get_profile("anthropic") is None

    def test_finalizes_after_min_observations(self, store: ProfileStore):
        for _ in range(3):
            store.submit_observation(_bundle())

        profile = store.get_profile("anthropic")
        assert profile is not None
        assert profile.is_complete is True
        assert profile.provider == "anthropic"
        assert profile.observation_count == 3

    def test_stable_headers_in_profile(self, store: ProfileStore):
        for _ in range(3):
            store.submit_observation(_bundle(headers={"x-app": "cli", "beta": "flag1"}))

        profile = store.get_profile("anthropic")
        assert profile is not None
        names = {h.name for h in profile.headers}
        assert "x-app" in names
        assert "beta" in names

    def test_variable_headers_excluded(self, store: ProfileStore):
        for i in range(3):
            store.submit_observation(_bundle(headers={"x-app": "cli", "x-req-id": f"r{i}"}))

        profile = store.get_profile("anthropic")
        assert profile is not None
        names = {h.name for h in profile.headers}
        assert "x-app" in names
        assert "x-req-id" not in names


class TestGetBestProfile:
    def test_returns_none_when_empty(self, store: ProfileStore):
        assert store.get_profile("anthropic") is None

    def test_returns_none_for_wrong_provider(self, store: ProfileStore):
        for _ in range(3):
            store.submit_observation(_bundle(provider="gemini"))
        assert store.get_profile("anthropic") is None

    def test_returns_most_recent(self, store: ProfileStore):
        for _ in range(3):
            store.submit_observation(_bundle(ua="cli/1.0"))
        for _ in range(3):
            store.submit_observation(_bundle(ua="cli/2.0"))

        profile = store.get_profile("anthropic")
        assert profile is not None
        assert profile.user_agent == "cli/2.0"

    def test_multiple_providers(self, store: ProfileStore):
        for _ in range(3):
            store.submit_observation(_bundle(provider="anthropic"))
            store.submit_observation(_bundle(provider="gemini"))

        assert store.get_profile("anthropic") is not None
        assert store.get_profile("gemini") is not None
        assert store.get_profile("openai") is None


class TestPersistence:
    def test_persists_to_disk(self, store_path: Path):
        store = ProfileStore(store_path, min_observations=3, seed_profiles=None)
        for _ in range(3):
            store.submit_observation(_bundle())

        assert store_path.exists()
        data = json.loads(store_path.read_text())
        assert data["format_version"] == 1
        assert len(data["profiles"]) == 1

    def test_loads_from_disk(self, store_path: Path):
        store1 = ProfileStore(store_path, min_observations=3, seed_profiles=None)
        for _ in range(3):
            store1.submit_observation(_bundle())

        store2 = ProfileStore(store_path, min_observations=3, seed_profiles=None)
        profile = store2.get_profile("anthropic")
        assert profile is not None
        assert profile.is_complete is True

    def test_handles_malformed_file(self, store_path: Path):
        store_path.write_text("not json")
        store = ProfileStore(store_path, min_observations=3, seed_profiles=None)
        assert store.get_profile("anthropic") is None

    def test_handles_wrong_version(self, store_path: Path):
        store_path.write_text(json.dumps({"format_version": 99}))
        store = ProfileStore(store_path, min_observations=3, seed_profiles=None)
        assert store.get_profile("anthropic") is None

    def test_degraded_on_version_mismatch_with_data(self, store_path: Path):
        store_path.write_text(json.dumps({
            "format_version": 99,
            "profiles": {"anthropic/v0": {}},
            "accumulators": {},
        }))
        store = ProfileStore(store_path, min_observations=3, seed_profiles=None)
        assert store.is_degraded is True
        assert store.get_profile("anthropic") is None

    def test_not_degraded_on_version_mismatch_without_data(self, store_path: Path):
        store_path.write_text(json.dumps({"format_version": 99}))
        store = ProfileStore(store_path, min_observations=3, seed_profiles=None)
        assert store.is_degraded is False

    def test_not_degraded_on_valid_file(self, store_path: Path):
        store = ProfileStore(store_path, min_observations=3, seed_profiles=None)
        for _ in range(3):
            store.submit_observation(_bundle())
        store2 = ProfileStore(store_path, min_observations=3, seed_profiles=None)
        assert store2.is_degraded is False

    def test_persists_accumulators(self, store_path: Path):
        store1 = ProfileStore(store_path, min_observations=3, seed_profiles=None)
        store1.submit_observation(_bundle())
        # Force flush by submitting 10 observations
        for _ in range(9):
            store1.submit_observation(_bundle())

        store2 = ProfileStore(store_path, min_observations=3, seed_profiles=None)
        profile = store2.get_profile("anthropic")
        assert profile is not None


class TestAnthropicSeed:
    def test_seeds_on_first_run(self, store_path: Path):
        store = ProfileStore(store_path, min_observations=3, seed_profiles=[_build_anthropic_seed_profile()])
        profile = store.get_profile("anthropic")
        assert profile is not None
        assert profile.user_agent == "v0-seed"
        names = {h.name for h in profile.headers}
        assert "anthropic-beta" in names
        assert "anthropic-version" in names
        assert profile.system is not None

    def test_skips_seed_if_profile_exists(self, store_path: Path):
        store1 = ProfileStore(store_path, min_observations=1, seed_profiles=None)
        store1.submit_observation(_bundle(provider="anthropic", ua="real-cli"))

        store2 = ProfileStore(store_path, min_observations=1, seed_profiles=[_build_anthropic_seed_profile()])
        profile = store2.get_profile("anthropic")
        assert profile is not None
        assert profile.user_agent == "real-cli"

    def test_seed_disabled(self, store_path: Path):
        store = ProfileStore(store_path, min_observations=3, seed_profiles=None)
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
            min_observations=3,
            seed_profiles=[_build_anthropic_seed_profile(), seed_openai],
        )
        assert store.get_profile("anthropic") is not None
        assert store.get_profile("openai") is not None


class TestGetAllProfiles:
    def test_returns_all(self, store_path: Path):
        store = ProfileStore(store_path, min_observations=1, seed_profiles=None)
        store.submit_observation(_bundle(provider="a"))
        store.submit_observation(_bundle(provider="b"))
        profiles = store.get_all_profiles()
        assert len(profiles) == 2
