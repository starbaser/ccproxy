"""Tests for compliance profile data models."""

import json

from ccproxy.compliance.models import (
    ComplianceProfile,
    ObservationAccumulator,
    ObservationBundle,
    ProfileFeatureBodyField,
    ProfileFeatureHeader,
    ProfileFeatureSystem,
)


class TestProfileFeatureHeader:
    def test_roundtrip(self):
        h = ProfileFeatureHeader(name="anthropic-beta", value="oauth-2025-04-20")
        assert ProfileFeatureHeader.from_dict(h.to_dict()) == h


class TestProfileFeatureBodyField:
    def test_roundtrip(self):
        f = ProfileFeatureBodyField(path="metadata", value={"user_id": "test"})
        restored = ProfileFeatureBodyField.from_dict(f.to_dict())
        assert restored.path == f.path
        assert restored.value == f.value


class TestProfileFeatureSystem:
    def test_roundtrip(self):
        s = ProfileFeatureSystem(structure=[{"type": "text", "text": "You are Claude"}])
        assert ProfileFeatureSystem.from_dict(s.to_dict()).structure == s.structure


class TestComplianceProfile:
    def test_roundtrip(self):
        profile = ComplianceProfile(
            provider="anthropic",
            user_agent="claude-cli/2.1.87",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            observation_count=3,
            is_complete=True,
            headers=[ProfileFeatureHeader(name="x-app", value="cli")],
            body_fields=[ProfileFeatureBodyField(path="thinking", value={"type": "enabled"})],
            system=ProfileFeatureSystem(structure=[{"type": "text", "text": "Hello"}]),
        )
        d = profile.to_dict()
        restored = ComplianceProfile.from_dict(d)
        assert restored.provider == "anthropic"
        assert restored.is_complete is True
        assert len(restored.headers) == 1
        assert restored.headers[0].name == "x-app"
        assert len(restored.body_fields) == 1
        assert restored.system is not None
        assert restored.system.structure[0]["text"] == "Hello"

    def test_roundtrip_no_system(self):
        profile = ComplianceProfile(
            provider="gemini",
            user_agent="gemini-cli/1.0",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            observation_count=3,
            is_complete=True,
        )
        d = profile.to_dict()
        restored = ComplianceProfile.from_dict(d)
        assert restored.system is None

    def test_json_serializable(self):
        profile = ComplianceProfile(
            provider="anthropic",
            user_agent="test",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            observation_count=1,
            is_complete=True,
        )
        json.dumps(profile.to_dict())


class TestObservationBundle:
    def test_construction(self):
        bundle = ObservationBundle(
            provider="gemini",
            user_agent="gemini-cli/1.0",
            headers={"x-goog-api-client": "genai-grpc/1.0"},
            body_envelope={"generationConfig": {"temperature": 0.7}},
            system=None,
        )
        assert bundle.provider == "gemini"
        assert bundle.headers["x-goog-api-client"] == "genai-grpc/1.0"


class TestObservationAccumulator:
    def test_single_observation(self):
        acc = ObservationAccumulator(provider="anthropic", user_agent="cli/1.0")
        bundle = ObservationBundle(
            provider="anthropic",
            user_agent="cli/1.0",
            headers={"x-app": "cli", "anthropic-beta": "flag1,flag2"},
            body_envelope={"thinking": {"type": "enabled"}},
            system=[{"type": "text", "text": "You are Claude"}],
        )
        acc.submit(bundle)
        assert acc.observation_count == 1
        assert acc.last_seen > 0

    def test_stable_features_after_identical_observations(self):
        acc = ObservationAccumulator(provider="anthropic", user_agent="cli/1.0")
        bundle = ObservationBundle(
            provider="anthropic",
            user_agent="cli/1.0",
            headers={"x-app": "cli"},
            body_envelope={"thinking": {"type": "enabled"}},
            system="You are Claude",
        )
        for _ in range(3):
            acc.submit(bundle)

        profile = acc.finalize()
        assert profile.is_complete is True
        assert profile.observation_count == 3
        assert len(profile.headers) == 1
        assert profile.headers[0].name == "x-app"
        assert profile.headers[0].value == "cli"
        assert len(profile.body_fields) == 1
        assert profile.body_fields[0].path == "thinking"

    def test_variable_features_excluded(self):
        acc = ObservationAccumulator(provider="anthropic", user_agent="cli/1.0")
        for i in range(3):
            bundle = ObservationBundle(
                provider="anthropic",
                user_agent="cli/1.0",
                headers={"x-app": "cli", "x-request-id": f"req-{i}"},
                body_envelope={},
                system=None,
            )
            acc.submit(bundle)

        profile = acc.finalize()
        header_names = {h.name for h in profile.headers}
        assert "x-app" in header_names
        assert "x-request-id" not in header_names

    def test_variable_body_fields_excluded(self):
        acc = ObservationAccumulator(provider="gemini", user_agent="cli/1.0")
        for i in range(3):
            bundle = ObservationBundle(
                provider="gemini",
                user_agent="cli/1.0",
                headers={},
                body_envelope={"generationConfig": {"temp": 0.7}, "requestId": f"r{i}"},
                system=None,
            )
            acc.submit(bundle)

        profile = acc.finalize()
        paths = {f.path for f in profile.body_fields}
        assert "generationConfig" in paths
        assert "requestId" not in paths

    def test_system_string_converted_to_blocks(self):
        acc = ObservationAccumulator(provider="anthropic", user_agent="cli/1.0")
        for _ in range(3):
            acc.submit(
                ObservationBundle(
                    provider="anthropic",
                    user_agent="cli/1.0",
                    headers={},
                    body_envelope={},
                    system="You are Claude",
                )
            )

        profile = acc.finalize()
        assert profile.system is not None
        assert profile.system.structure == [{"type": "text", "text": "You are Claude"}]

    def test_system_list_preserved(self):
        blocks = [{"type": "text", "text": "Block1"}, {"type": "text", "text": "Block2"}]
        acc = ObservationAccumulator(provider="anthropic", user_agent="cli/1.0")
        for _ in range(3):
            acc.submit(
                ObservationBundle(
                    provider="anthropic",
                    user_agent="cli/1.0",
                    headers={},
                    body_envelope={},
                    system=blocks,
                )
            )

        profile = acc.finalize()
        assert profile.system is not None
        assert len(profile.system.structure) == 2

    def test_roundtrip(self):
        acc = ObservationAccumulator(provider="test", user_agent="ua")
        acc.submit(
            ObservationBundle(
                provider="test",
                user_agent="ua",
                headers={"h": "v"},
                body_envelope={"k": "v"},
                system="sys",
            )
        )
        d = acc.to_dict()
        restored = ObservationAccumulator.from_dict(d)
        assert restored.observation_count == 1
        assert restored.header_candidates == {"h": ["v"]}
