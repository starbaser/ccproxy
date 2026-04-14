"""Tests for ccproxy.lightllm.dispatch — transformation orchestration."""

from __future__ import annotations

import json

import pytest

from ccproxy.lightllm.dispatch import transform_to_provider


class TestTransformToProvider:
    """Verify the canonical BaseConfig method chain produces valid output."""

    def test_anthropic_basic(self) -> None:
        url, headers, body = transform_to_provider(
            model="claude-3-5-sonnet-20241022",
            provider="anthropic",
            messages=[{"role": "user", "content": "hello"}],
            api_key="sk-test-key",
        )

        assert "api.anthropic.com" in url
        assert "/v1/messages" in url
        assert headers.get("x-api-key") == "sk-test-key"
        assert "anthropic-version" in headers

        data = json.loads(body)
        assert data["model"] == "claude-3-5-sonnet-20241022"
        assert isinstance(data["messages"], list)
        assert data["messages"][0]["role"] == "user"

    def test_anthropic_with_stream(self) -> None:
        _url, _headers, body = transform_to_provider(
            model="claude-3-5-sonnet-20241022",
            provider="anthropic",
            messages=[{"role": "user", "content": "hello"}],
            api_key="sk-test-key",
            stream=True,
        )

        data = json.loads(body)
        assert data.get("stream") is True

    def test_anthropic_with_optional_params(self) -> None:
        _url, _headers, body = transform_to_provider(
            model="claude-3-5-sonnet-20241022",
            provider="anthropic",
            messages=[{"role": "user", "content": "hello"}],
            optional_params={"max_tokens": 100, "temperature": 0.5},
            api_key="sk-test-key",
        )

        data = json.loads(body)
        assert data.get("max_tokens") == 100

    def test_openai_basic(self) -> None:
        url, headers, body = transform_to_provider(
            model="gpt-4o",
            provider="openai",
            messages=[{"role": "user", "content": "hello"}],
            api_key="sk-test-key",
        )

        assert "/chat/completions" in url
        assert "Bearer sk-test-key" in headers.get("Authorization", "")

        data = json.loads(body)
        assert data["model"] == "gpt-4o"
        assert data["messages"][0]["role"] == "user"

    def test_gemini_basic(self) -> None:
        url, _headers, body = transform_to_provider(
            model="gemini-2.0-flash",
            provider="gemini",
            messages=[{"role": "user", "content": "hello"}],
            api_key="test-key",
        )

        assert "generativelanguage.googleapis.com" in url
        assert "models/gemini-2.0-flash" in url
        assert "generateContent" in url
        assert "key=test-key" in url

        data = json.loads(body)
        assert "contents" in data

    def test_gemini_streaming(self) -> None:
        url, _, _ = transform_to_provider(
            model="gemini-2.0-flash",
            provider="gemini",
            messages=[{"role": "user", "content": "hello"}],
            api_key="test-key",
            stream=True,
        )

        assert "streamGenerateContent" in url
        assert "alt=sse" in url

    def test_returns_bytes(self) -> None:
        _, _, body = transform_to_provider(
            model="claude-3-5-sonnet-20241022",
            provider="anthropic",
            messages=[{"role": "user", "content": "test"}],
            api_key="key",
        )
        assert isinstance(body, bytes)
        json.loads(body)

    def test_returns_headers_dict(self) -> None:
        _, headers, _ = transform_to_provider(
            model="claude-3-5-sonnet-20241022",
            provider="anthropic",
            messages=[{"role": "user", "content": "test"}],
            api_key="key",
        )
        assert isinstance(headers, dict)

    def test_unknown_provider_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown provider"):
            transform_to_provider(
                model="some-model",
                provider="nonexistent_xyz",
                messages=[{"role": "user", "content": "test"}],
            )

    def test_system_message_handling(self) -> None:
        """Anthropic separates system messages from user messages."""
        _, _, body = transform_to_provider(
            model="claude-3-5-sonnet-20241022",
            provider="anthropic",
            messages=[
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "hello"},
            ],
            api_key="key",
        )
        data = json.loads(body)
        assert "system" in data
        user_msgs = [m for m in data["messages"] if m.get("role") == "user"]
        assert len(user_msgs) >= 1

    def test_multi_turn_conversation(self) -> None:
        _, _, body = transform_to_provider(
            model="claude-3-5-sonnet-20241022",
            provider="anthropic",
            messages=[
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "Hi there!"},
                {"role": "user", "content": "how are you?"},
            ],
            api_key="key",
        )
        data = json.loads(body)
        assert len(data["messages"]) >= 3

    def test_gemini_with_cached_content(self) -> None:
        _, _, body = transform_to_provider(
            model="gemini-2.0-flash",
            provider="gemini",
            messages=[{"role": "user", "content": "hello"}],
            api_key="test-key",
            cached_content="cachedContents/abc123",
        )
        data = json.loads(body)
        assert data.get("cachedContent") == "cachedContents/abc123"

    def test_gemini_without_cached_content(self) -> None:
        _, _, body = transform_to_provider(
            model="gemini-2.0-flash",
            provider="gemini",
            messages=[{"role": "user", "content": "hello"}],
            api_key="test-key",
        )
        data = json.loads(body)
        assert "cachedContent" not in data

    def test_no_api_key_raises_for_anthropic(self) -> None:
        """Anthropic requires an API key — validate_environment raises."""
        from litellm.exceptions import AuthenticationError

        with pytest.raises(AuthenticationError):
            transform_to_provider(
                model="claude-3-5-sonnet-20241022",
                provider="anthropic",
                messages=[{"role": "user", "content": "test"}],
            )
