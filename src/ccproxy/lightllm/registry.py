"""Provider name → BaseConfig resolution.

Local registry checked first for ccproxy-internal providers (e.g. the
Perplexity Pro WebUI subscription path); falls through to LiteLLM's
``ProviderConfigManager`` for upstream-supported providers.
"""

from __future__ import annotations

from collections.abc import Callable

from litellm.llms.base_llm.chat.transformation import BaseConfig
from litellm.types.utils import LlmProviders
from litellm.utils import ProviderConfigManager

from ccproxy.lightllm.perplexity import PERPLEXITY_PROVIDER_NAME, PerplexityProConfig

_LOCAL_CONFIGS: dict[str, Callable[[], BaseConfig]] = {
    PERPLEXITY_PROVIDER_NAME: PerplexityProConfig,
}
"""ccproxy-internal providers not registered with LiteLLM upstream. Each
entry is a zero-arg factory that returns a BaseConfig instance."""


def get_config(provider: str, model: str) -> BaseConfig:
    """Resolve a provider name and model to a concrete BaseConfig instance.

    Local registry wins over LiteLLM's ProviderConfigManager so ccproxy can
    expose providers that don't exist upstream (Perplexity Pro WebUI).
    """
    factory = _LOCAL_CONFIGS.get(provider)
    if factory is not None:
        return factory()

    try:
        llm_provider = LlmProviders(provider)
    except ValueError as exc:
        valid = [p.value for p in LlmProviders] + list(_LOCAL_CONFIGS)
        raise ValueError(f"Unknown provider {provider!r}. Valid providers: {valid}") from exc

    config = ProviderConfigManager.get_provider_chat_config(model, llm_provider)
    if config is None:
        raise ValueError(f"No chat config for provider={provider!r} model={model!r}")
    return config
