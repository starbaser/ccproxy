"""Tests for ccproxy.lightllm.registry — provider → BaseConfig resolution."""

from __future__ import annotations

import pytest

from ccproxy.lightllm.registry import get_config


class TestGetConfig:
    def test_anthropic(self) -> None:
        config = get_config("anthropic", "claude-3-5-sonnet-20241022")
        assert type(config).__name__ == "AnthropicConfig"

    def test_openai(self) -> None:
        config = get_config("openai", "gpt-4o")
        assert type(config).__name__ == "OpenAIGPTConfig"

    def test_gemini(self) -> None:
        config = get_config("gemini", "gemini-pro")
        assert type(config).__name__ == "GoogleAIStudioGeminiConfig"

    def test_unknown_provider_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown provider"):
            get_config("nonexistent_provider_xyz", "some-model")

    def test_returns_base_config_subclass(self) -> None:
        from litellm.llms.base_llm.chat.transformation import BaseConfig

        config = get_config("anthropic", "claude-3-5-sonnet-20241022")
        assert isinstance(config, BaseConfig)

    def test_openai_compatible_providers(self) -> None:
        """OpenAI-compatible providers should resolve via ProviderConfigManager."""
        config = get_config("groq", "llama-3.1-70b")
        assert "Config" in type(config).__name__

    def test_bedrock(self) -> None:
        config = get_config("bedrock", "anthropic.claude-3-5-sonnet-20241022-v2:0")
        assert "Config" in type(config).__name__
