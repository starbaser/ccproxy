"""Orchestrates LiteLLM's BaseConfig transformation pipeline.

Sequences the canonical LiteLLM method chain — validate_environment →
get_complete_url → transform_request → sign_request → transform_response —
without pulling in cost tracking, callbacks, caching, or the Logging class.

Gemini/Vertex AI has a custom code path that bypasses BaseConfig.transform_request()
entirely.  We import ``_transform_request_body`` and ``_get_gemini_url`` directly.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from litellm.types.utils import LlmProviders, ModelResponse
from litellm.utils import ProviderConfigManager

from ccproxy.lightllm.noop_logging import NoopLogging
from ccproxy.lightllm.registry import get_config

_noop = NoopLogging()

# Providers whose get_complete_url() inherits the base class no-op.
# Path suffixes normally added by litellm/main.py.
_PATH_SUFFIXES: dict[str, str] = {
    "anthropic": "/v1/messages",
}

_GEMINI_PROVIDERS = {"gemini", "vertex_ai", "vertex_ai_beta"}


def _resolve_api_base(provider: str, model: str, api_base: str | None) -> str | None:
    """Auto-resolve api_base from the provider's ModelInfo when not given."""
    if api_base is not None:
        return api_base
    try:
        llm_provider = LlmProviders(provider)
        model_info = ProviderConfigManager.get_provider_model_info(model, llm_provider)
        if model_info is not None:
            resolved = model_info.get_api_base()
            if resolved is not None:
                suffix = _PATH_SUFFIXES.get(provider)
                if suffix and not resolved.rstrip("/").endswith(suffix.rstrip("/")):
                    return resolved.rstrip("/") + suffix
                return resolved
    except (ValueError, Exception):
        pass
    return None


def _transform_gemini(
    model: str,
    provider: str,
    messages: list[Any],
    optional_params: dict[str, Any],
    *,
    api_key: str | None = None,
    stream: bool = False,
) -> tuple[str, dict[str, str], bytes]:
    """Gemini-specific transform using _get_gemini_url + _transform_request_body."""
    from litellm.llms.vertex_ai.common_utils import _get_gemini_url
    from litellm.llms.vertex_ai.gemini.transformation import _transform_request_body

    # _get_gemini_url embeds the key in ?key= for API key auth.
    # For OAuth tokens (ya29.*), strip ?key= and use Authorization header only.
    is_oauth = api_key is not None and api_key.startswith("ya29.")

    url, _endpoint = _get_gemini_url(
        mode="chat",
        model=model,
        stream=stream,
        gemini_api_key=api_key if not is_oauth else "placeholder",
    )

    if is_oauth:
        # Strip ?key=placeholder and use Bearer auth instead
        url = url.split("?key=")[0]
        # Preserve &alt=sse for streaming
        if stream:
            url += "?alt=sse"

    config = get_config(provider, model)
    headers = config.validate_environment(
        headers={},
        model=model,
        messages=messages,
        optional_params=optional_params,
        litellm_params={},
        api_key=api_key,
    )

    custom_provider = "gemini" if provider == "gemini" else "vertex_ai"
    request_body = _transform_request_body(
        messages=messages,
        model=model,
        optional_params=optional_params,
        custom_llm_provider=custom_provider,  # type: ignore[arg-type]
        litellm_params={},
        cached_content=None,
    )

    body = json.dumps(request_body).encode()
    return url, headers, body


def transform_to_provider(
    model: str,
    provider: str,
    messages: list[Any],
    optional_params: dict[str, Any] | None = None,
    *,
    api_key: str | None = None,
    api_base: str | None = None,
    stream: bool = False,
) -> tuple[str, dict[str, str], bytes]:
    """Transform an OpenAI chat-completions request into provider-native format.

    Returns:
        ``(url, headers, body_bytes)`` ready for httpx or mitmproxy flow rewrite.
    """
    optional_params = optional_params or {}

    if provider in _GEMINI_PROVIDERS:
        return _transform_gemini(
            model, provider, messages, optional_params,
            api_key=api_key, stream=stream,
        )

    config = get_config(provider, model)
    api_base = _resolve_api_base(provider, model, api_base)
    litellm_params: dict[str, Any] = {"api_key": api_key, "api_base": api_base}

    headers = config.validate_environment(
        headers={},
        model=model,
        messages=messages,
        optional_params=optional_params,
        litellm_params=litellm_params,
        api_key=api_key,
        api_base=api_base,
    )

    url = config.get_complete_url(
        api_base=api_base,
        api_key=api_key,
        model=model,
        optional_params=optional_params,
        litellm_params=litellm_params,
        stream=stream,
    )

    data = config.transform_request(
        model=model,
        messages=messages,
        optional_params=optional_params,
        litellm_params=litellm_params,
        headers=headers,
    )

    # BaseLLMHTTPHandler injects stream after transform_request
    if stream and config.supports_stream_param_in_request_body:
        data["stream"] = True

    headers, signed_body = config.sign_request(
        headers=headers,
        optional_params=optional_params,
        request_data=data,
        api_base=url,
        api_key=api_key,
        stream=stream,
        fake_stream=False,
        model=model,
    )

    body = signed_body if signed_body is not None else json.dumps(data).encode()
    return url, headers, body


def transform_to_openai(
    model: str,
    provider: str,
    raw_response: httpx.Response,
    request_data: dict[str, Any],
    messages: list[Any],
) -> ModelResponse:
    """Transform a provider-native response into an OpenAI ModelResponse."""
    config = get_config(provider, model)
    model_response = ModelResponse()
    return config.transform_response(
        model=model,
        raw_response=raw_response,
        model_response=model_response,
        logging_obj=_noop,  # type: ignore[arg-type]
        request_data=request_data,
        messages=messages,
        optional_params={},
        litellm_params={},
        encoding=None,
        api_key=None,
        json_mode=None,
    )
