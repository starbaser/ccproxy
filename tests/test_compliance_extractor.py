"""Tests for compliance feature extraction from HttpSnapshot."""

import json

from ccproxy.compliance.extractor import extract_envelope
from ccproxy.inspector.flow_store import HttpSnapshot


def _make_client_request(
    headers: dict[str, str] | None = None,
    body: dict | None = None,
) -> HttpSnapshot:
    headers = headers or {}
    body_bytes = json.dumps(body).encode() if body else b""
    return HttpSnapshot(
        headers=headers,
        body=body_bytes,
        method="POST",
        url="https://api.anthropic.com:443/v1/messages",
    )


class TestExtractEnvelope:
    def test_extracts_profiled_headers(self):
        cr = _make_client_request(
            headers={
                "user-agent": "claude-cli/2.1.87",
                "anthropic-beta": "oauth-2025-04-20",
                "x-app": "cli",
                "authorization": "Bearer sk-ant-secret",
                "content-length": "1234",
            }
        )
        envelope = extract_envelope(cr)
        assert "anthropic-beta" in envelope.headers
        assert "x-app" in envelope.headers
        assert "authorization" not in envelope.headers
        assert "content-length" not in envelope.headers

    def test_extracts_body_envelope(self):
        cr = _make_client_request(
            headers={"user-agent": "cli/1.0"},
            body={
                "model": "claude-opus-4-5",
                "messages": [{"role": "user", "content": "hi"}],
                "metadata": {"user_id": "test"},
                "thinking": {"type": "enabled"},
                "stream": True,
            },
        )
        envelope = extract_envelope(cr)
        assert "metadata" in envelope.body_fields
        assert "thinking" in envelope.body_fields
        assert "model" not in envelope.body_fields
        assert "messages" not in envelope.body_fields
        assert "stream" not in envelope.body_fields

    def test_extracts_system_as_blocks(self):
        cr = _make_client_request(
            headers={"user-agent": "cli/1.0"},
            body={
                "model": "test",
                "messages": [],
                "system": [{"type": "text", "text": "You are Claude"}],
            },
        )
        envelope = extract_envelope(cr)
        assert envelope.system == [{"type": "text", "text": "You are Claude"}]
        assert "system" not in envelope.body_fields

    def test_normalizes_string_system_to_blocks(self):
        cr = _make_client_request(
            headers={"user-agent": "cli/1.0"},
            body={
                "model": "test",
                "messages": [],
                "system": "You are Claude",
            },
        )
        envelope = extract_envelope(cr)
        assert envelope.system == [{"type": "text", "text": "You are Claude"}]

    def test_handles_non_json_body(self):
        cr = HttpSnapshot(
            headers={"user-agent": "test"},
            body=b"not json",
            method="GET",
            url="https://example.com:443/health",
        )
        envelope = extract_envelope(cr)
        assert envelope.body_fields == {}
        assert envelope.system is None

    def test_handles_empty_body(self):
        cr = _make_client_request(headers={"user-agent": "test"})
        envelope = extract_envelope(cr)
        assert envelope.body_fields == {}

    def test_header_names_lowercased(self):
        cr = _make_client_request(
            headers={
                "User-Agent": "cli/1.0",
                "Anthropic-Beta": "flag1",
                "X-Custom": "val",
            }
        )
        envelope = extract_envelope(cr)
        assert "user-agent" in envelope.headers
        assert "anthropic-beta" in envelope.headers
        assert "x-custom" in envelope.headers

    def test_gemini_body_envelope(self):
        cr = _make_client_request(
            headers={"user-agent": "gemini-cli/1.0"},
            body={
                "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
                "generationConfig": {"temperature": 0.7},
                "safetySettings": [{"category": "BLOCK_NONE"}],
                "model": "gemini-2.0-flash",
            },
        )
        envelope = extract_envelope(cr)
        assert "generationConfig" in envelope.body_fields
        assert "safetySettings" in envelope.body_fields
        assert "contents" not in envelope.body_fields
        assert "model" not in envelope.body_fields

    def test_additional_exclusions_respected(self):
        cr = _make_client_request(
            headers={"user-agent": "cli/1.0", "x-internal": "secret"},
            body={"model": "test", "messages": [], "extra_content": "noise"},
        )
        envelope = extract_envelope(
            cr,
            additional_header_exclusions=frozenset({"x-internal"}),
            additional_body_content_fields=frozenset({"extra_content"}),
        )
        assert "x-internal" not in envelope.headers
        assert "extra_content" not in envelope.body_fields
