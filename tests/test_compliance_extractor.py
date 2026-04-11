"""Tests for compliance feature extraction from ClientRequest."""

import json

from ccproxy.compliance.extractor import extract_observation
from ccproxy.inspector.flow_store import ClientRequest


def _make_client_request(
    headers: dict[str, str] | None = None,
    body: dict | None = None,
) -> ClientRequest:
    headers = headers or {}
    body_bytes = json.dumps(body).encode() if body else b""
    return ClientRequest(
        method="POST",
        scheme="https",
        host="api.anthropic.com",
        port=443,
        path="/v1/messages",
        headers=headers,
        body=body_bytes,
        content_type="application/json",
    )


class TestExtractObservation:
    def test_extracts_profiled_headers(self):
        cr = _make_client_request(headers={
            "user-agent": "claude-cli/2.1.87",
            "anthropic-beta": "oauth-2025-04-20",
            "x-app": "cli",
            "authorization": "Bearer sk-ant-secret",
            "content-length": "1234",
        })
        bundle = extract_observation(cr, "anthropic")
        assert bundle.user_agent == "claude-cli/2.1.87"
        assert "anthropic-beta" in bundle.headers
        assert "x-app" in bundle.headers
        assert "authorization" not in bundle.headers
        assert "content-length" not in bundle.headers

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
        bundle = extract_observation(cr, "anthropic")
        assert "metadata" in bundle.body_envelope
        assert "thinking" in bundle.body_envelope
        assert "model" not in bundle.body_envelope
        assert "messages" not in bundle.body_envelope
        assert "stream" not in bundle.body_envelope

    def test_extracts_system_separately(self):
        cr = _make_client_request(
            headers={"user-agent": "cli/1.0"},
            body={
                "model": "test",
                "messages": [],
                "system": [{"type": "text", "text": "You are Claude"}],
            },
        )
        bundle = extract_observation(cr, "anthropic")
        assert bundle.system == [{"type": "text", "text": "You are Claude"}]
        assert "system" not in bundle.body_envelope

    def test_handles_non_json_body(self):
        cr = ClientRequest(
            method="GET", scheme="https", host="example.com", port=443,
            path="/health", headers={"user-agent": "test"}, body=b"not json",
            content_type="text/plain",
        )
        bundle = extract_observation(cr, "unknown")
        assert bundle.body_envelope == {}
        assert bundle.system is None

    def test_handles_empty_body(self):
        cr = _make_client_request(headers={"user-agent": "test"})
        bundle = extract_observation(cr, "test")
        assert bundle.body_envelope == {}

    def test_header_names_lowercased(self):
        cr = _make_client_request(headers={
            "User-Agent": "cli/1.0",
            "Anthropic-Beta": "flag1",
            "X-Custom": "val",
        })
        bundle = extract_observation(cr, "anthropic")
        assert "user-agent" in bundle.headers
        assert "anthropic-beta" in bundle.headers
        assert "x-custom" in bundle.headers

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
        bundle = extract_observation(cr, "gemini")
        assert "generationConfig" in bundle.body_envelope
        assert "safetySettings" in bundle.body_envelope
        assert "contents" not in bundle.body_envelope
        assert "model" not in bundle.body_envelope

    def test_unknown_ua_defaults(self):
        cr = _make_client_request(headers={})
        bundle = extract_observation(cr, "test")
        assert bundle.user_agent == "unknown"
