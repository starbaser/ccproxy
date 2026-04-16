"""Provider name → BaseConfig resolution via LiteLLM's ProviderConfigManager."""

from __future__ import annotations

from litellm.llms.base_llm.chat.transformation import BaseConfig
from litellm.types.utils import LlmProviders
from litellm.utils import ProviderConfigManager


def get_config(provider: str, model: str) -> BaseConfig:
    """Resolve a provider name and model to a concrete BaseConfig instance.

    Args:
        provider: LlmProviders enum value (e.g. ``"anthropic"``, ``"openai"``).
        model: Model name as LiteLLM expects it (e.g. ``"claude-3-5-sonnet-20241022"``).

    Returns:
        A provider-specific BaseConfig subclass instance.

    Raises:
        ValueError: If the provider has no registered chat config, or the
            provider string is not a valid ``LlmProviders`` member.
    """
    try:
        llm_provider = LlmProviders(provider)
    except ValueError as exc:
        raise ValueError(f"Unknown provider {provider!r}. Valid providers: {[p.value for p in LlmProviders]}") from exc

    config = ProviderConfigManager.get_provider_chat_config(model, llm_provider)
    if config is None:
        raise ValueError(f"No chat config for provider={provider!r} model={model!r}")
    return config
