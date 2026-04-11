"""Tests for compliance feature classification."""

from ccproxy.compliance.classifier import (
    BODY_CONTENT_FIELDS,
    HEADER_EXCLUSIONS,
    should_skip_body_field,
    should_skip_header,
)


class TestHeaderExclusions:
    def test_auth_headers_excluded(self):
        assert should_skip_header("authorization")
        assert should_skip_header("x-api-key")
        assert should_skip_header("Authorization")

    def test_transport_headers_excluded(self):
        assert should_skip_header("content-length")
        assert should_skip_header("transfer-encoding")
        assert should_skip_header("host")
        assert should_skip_header("connection")

    def test_internal_headers_excluded(self):
        assert should_skip_header("x-ccproxy-flow-id")
        assert should_skip_header("x-ccproxy-oauth-injected")
        assert should_skip_header("x-ccproxy-hooks")

    def test_profile_headers_included(self):
        assert not should_skip_header("anthropic-beta")
        assert not should_skip_header("anthropic-version")
        assert not should_skip_header("x-app")
        assert not should_skip_header("user-agent")
        assert not should_skip_header("x-goog-api-client")

    def test_exclusion_set_complete(self):
        assert "cookie" in HEADER_EXCLUSIONS
        assert "accept-encoding" in HEADER_EXCLUSIONS


class TestBodyFieldClassification:
    def test_content_fields_skipped(self):
        assert should_skip_body_field("messages")
        assert should_skip_body_field("contents")
        assert should_skip_body_field("tools")
        assert should_skip_body_field("model")
        assert should_skip_body_field("stream")
        assert should_skip_body_field("max_tokens")
        assert should_skip_body_field("temperature")

    def test_envelope_fields_kept(self):
        assert not should_skip_body_field("metadata")
        assert not should_skip_body_field("thinking")
        assert not should_skip_body_field("generationConfig")
        assert not should_skip_body_field("safetySettings")
        assert not should_skip_body_field("systemInstruction")

    def test_content_fields_set_completeness(self):
        expected = {
            "messages", "contents", "prompt", "tools", "tool_choice",
            "model", "stream", "max_tokens", "max_completion_tokens",
            "temperature", "top_p", "top_k", "stop", "n",
        }
        assert expected == BODY_CONTENT_FIELDS
