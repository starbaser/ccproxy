"""Tests for compliance profile data models."""

import json

from ccproxy.compliance.models import (
    ComplianceProfile,
    Envelope,
    ObservationAccumulator,
)


class TestEnvelope:
    def test_roundtrip(self):
        env = Envelope(
            headers={"x-app": "cli", "anthropic-beta": "flag1"},
            body_fields={"thinking": {"type": "enabled"}},
            system=[{"type": "text", "text": "You are Claude"}],
            body_wrapper="request",
        )
        restored = Envelope.from_dict(env.to_dict())
        assert restored.headers == env.headers
        assert restored.body_fields == env.body_fields
        assert restored.system == env.system
        assert restored.body_wrapper == env.body_wrapper

    def test_empty_defaults(self):
        env = Envelope()
        assert env.headers == {}
        assert env.body_fields == {}
        assert env.system is None
        assert env.body_wrapper is None

    def test_roundtrip_no_system(self):
        env = Envelope(headers={"x-app": "cli"})
        restored = Envelope.from_dict(env.to_dict())
        assert restored.system is None
        assert restored.body_wrapper is None


class TestComplianceProfile:
    def test_roundtrip(self):
        profile = ComplianceProfile(
            provider="anthropic",
            user_agent="claude-cli/2.1.87",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            observation_count=3,
            is_complete=True,
            envelope=Envelope(
                headers={"x-app": "cli"},
                body_fields={"thinking": {"type": "enabled"}},
                system=[{"type": "text", "text": "Hello"}],
            ),
        )
        d = profile.to_dict()
        restored = ComplianceProfile.from_dict(d)
        assert restored.provider == "anthropic"
        assert restored.is_complete is True
        assert restored.envelope.headers == {"x-app": "cli"}
        assert restored.envelope.body_fields == {"thinking": {"type": "enabled"}}
        assert restored.envelope.system is not None
        assert restored.envelope.system[0]["text"] == "Hello"

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
        assert restored.envelope.system is None

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


class TestObservationAccumulator:
    def test_single_observation(self):
        acc = ObservationAccumulator(provider="anthropic", user_agent="cli/1.0")
        envelope = Envelope(
            headers={"x-app": "cli", "anthropic-beta": "flag1,flag2"},
            body_fields={"thinking": {"type": "enabled"}},
            system=[{"type": "text", "text": "You are Claude"}],
        )
        acc.submit(envelope)
        assert acc.observation_count == 1
        assert acc.last_seen > 0

    def test_stable_features_after_identical_observations(self):
        acc = ObservationAccumulator(provider="anthropic", user_agent="cli/1.0")
        envelope = Envelope(
            headers={"x-app": "cli"},
            body_fields={"thinking": {"type": "enabled"}},
            system=[{"type": "text", "text": "You are Claude"}],
        )
        for _ in range(3):
            acc.submit(envelope)

        profile = acc.finalize()
        assert profile.is_complete is True
        assert profile.observation_count == 3
        assert profile.envelope.headers == {"x-app": "cli"}
        assert "thinking" in profile.envelope.body_fields

    def test_variable_features_excluded(self):
        acc = ObservationAccumulator(provider="anthropic", user_agent="cli/1.0")
        for i in range(3):
            envelope = Envelope(
                headers={"x-app": "cli", "x-request-id": f"req-{i}"},
            )
            acc.submit(envelope)

        profile = acc.finalize()
        assert "x-app" in profile.envelope.headers
        assert "x-request-id" not in profile.envelope.headers

    def test_variable_body_fields_excluded(self):
        acc = ObservationAccumulator(provider="gemini", user_agent="cli/1.0")
        for i in range(3):
            envelope = Envelope(
                body_fields={"generationConfig": {"temp": 0.7}, "requestId": f"r{i}"},
            )
            acc.submit(envelope)

        profile = acc.finalize()
        assert "generationConfig" in profile.envelope.body_fields
        assert "requestId" not in profile.envelope.body_fields

    def test_system_list_preserved(self):
        blocks = [{"type": "text", "text": "Block1"}, {"type": "text", "text": "Block2"}]
        acc = ObservationAccumulator(provider="anthropic", user_agent="cli/1.0")
        for _ in range(3):
            acc.submit(Envelope(system=blocks))

        profile = acc.finalize()
        assert profile.envelope.system is not None
        assert len(profile.envelope.system) == 2

    def test_roundtrip(self):
        acc = ObservationAccumulator(provider="test", user_agent="ua")
        acc.submit(Envelope(
            headers={"h": "v"},
            body_fields={"k": "v"},
            system=[{"type": "text", "text": "sys"}],
        ))
        d = acc.to_dict()
        restored = ObservationAccumulator.from_dict(d)
        assert restored.observation_count == 1
        assert restored.header_candidates == {"h": ["v"]}
