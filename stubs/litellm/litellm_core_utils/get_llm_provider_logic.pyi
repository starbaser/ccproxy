from typing import Any

def get_llm_provider(
    model: str,
    custom_llm_provider: str | None = None,
    api_base: str | None = None,
    api_key: str | None = None,
    litellm_params: dict[str, Any] | None = None,
) -> tuple[str, str, str, str]: ...
