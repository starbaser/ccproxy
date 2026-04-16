"""Orchestrates LiteLLM's BaseConfig transformation pipeline without
importing any LiteLLM proxy depedencies.

The canonical LiteLLM method chain:
validate_environment → get_complete_url →
   transform_request → sign_request → transform_response
→ to outbound ccproxy pipeline


Gemini/Vertex AI has a custom code path that bypasses BaseConfig.transform_request()
entirely.  We import ``_transform_request_body`` and ``_get_gemini_url`` directly.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from typing import Any

import httpx
from litellm.types.utils import LlmProviders, ModelResponse
from litellm.utils import ProviderConfigManager

from ccproxy.lightllm.noop_logging import NoopLogging
from ccproxy.lightllm.registry import get_config

logger = logging.getLogger(__name__)

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
    except Exception as e:
        logger.debug("api_base auto-resolve failed for %s/%s: %s", provider, model, e)
    return None


def _transform_gemini(
    model: str,
    provider: str,
    messages: list[Any],
    optional_params: dict[str, Any],
    *,
    api_key: str | None = None,
    stream: bool = False,
    cached_content: str | None = None,
) -> tuple[str, dict[str, str], bytes]:
    """Gemini-specific transform (bypasses BaseConfig.transform_request)."""
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

    # For API key auth, ?key= in the URL is the sole auth mechanism.
    # validate_environment() injects Authorization: Bearer {api_key} which
    # Google rejects (it's not an OAuth token). Strip it.
    if not is_oauth:
        headers.pop("Authorization", None)

    custom_provider = "gemini" if provider == "gemini" else "vertex_ai"
    request_body = _transform_request_body(
        messages=messages,
        model=model,
        optional_params=optional_params,
        custom_llm_provider=custom_provider,  # type: ignore[arg-type]
        litellm_params={},
        cached_content=cached_content,
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
    cached_content: str | None = None,
) -> tuple[str, dict[str, str], bytes]:
    """Transform an OpenAI chat-completions request into provider-native format."""
    optional_params = optional_params or {}

    if provider in _GEMINI_PROVIDERS:
        return _transform_gemini(
            model,
            provider,
            messages,
            optional_params,
            api_key=api_key,
            stream=stream,
            cached_content=cached_content,
        )

    config = get_config(provider, model)
    api_base = _resolve_api_base(provider, model, api_base)
    litellm_params: dict[str, Any] = {"api_key": api_key, "api_base": api_base}

    # Convert OpenAI-format params (tool_choice, tools, etc.) to provider-native format.
    optional_params = config.map_openai_params(
        non_default_params=optional_params,
        optional_params={},
        model=model,
        drop_params=True,
    )

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
        stream=stream,
        fake_stream=False,
        model=model,
    )

    body = signed_body if signed_body is not None else json.dumps(data).encode()
    return url, headers, body


class MitmResponseShim:
    """Duck-types httpx.Response for BaseConfig.transform_response()."""

    def __init__(self, mitm_response: Any) -> None:
        self.status_code: int = mitm_response.status_code
        self.headers: dict[str, str] = dict(mitm_response.headers.items())  # type: ignore[no-untyped-call]
        self._content: bytes = mitm_response.content

    @property
    def text(self) -> str:
        return self._content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self._content)


def transform_to_openai(
    model: str,
    provider: str,
    raw_response: httpx.Response | MitmResponseShim,
    request_data: dict[str, Any],
    messages: list[Any],
) -> ModelResponse:
    """Transform a provider-native response into an OpenAI ModelResponse."""
    config = get_config(provider, model)
    model_response = ModelResponse()
    return config.transform_response(
        model=model,
        raw_response=raw_response,  # type: ignore[arg-type]
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


def _make_response_iterator(provider: str, model: str, optional_params: dict[str, Any]) -> Any:
    """Create a provider-specific ModelResponseIterator for SSE chunk parsing.

    The iterator is instantiated with a dummy empty iterable — we call
    chunk_parser() directly rather than driving __next__().
    """
    if provider in _GEMINI_PROVIDERS:
        from litellm.llms.vertex_ai.gemini.vertex_and_google_ai_studio_gemini import (
            ModelResponseIterator as GeminiIterator,
        )

        return GeminiIterator(
            streaming_response=iter([]),
            sync_stream=True,
            logging_obj=NoopLogging(optional_params),  # type: ignore[arg-type]
        )

    if provider == "anthropic":
        from litellm.llms.anthropic.chat.handler import (
            ModelResponseIterator as AnthropicIterator,
        )

        return AnthropicIterator(
            streaming_response=iter([]),
            sync_stream=True,
        )

    # Generic path: use BaseConfig.get_model_response_iterator()
    config = get_config(provider, model)
    iterator = config.get_model_response_iterator(
        streaming_response=iter([]),
        sync_stream=True,
    )
    if iterator is not None:
        return iterator

    # Fallback: provider returns OpenAI-format SSE natively — no iterator needed
    return None


class SseTransformer:
    """Stateful SSE chunk transformer for flow.response.stream.

    If no iterator is available (provider already emits OpenAI-format SSE),
    bytes pass through unchanged.
    """

    def __init__(self, provider: str, model: str, optional_params: dict[str, Any]) -> None:
        self._iterator = _make_response_iterator(provider, model, optional_params)
        self._buf = b""
        self._raw_chunks: list[bytes] = []

    def __call__(self, data: bytes) -> bytes | Iterable[bytes]:
        self._raw_chunks.append(data)

        if self._iterator is None:
            return data

        if data == b"":
            return b"data: [DONE]\n\n"

        self._buf += data
        out = bytearray()

        while b"\n\n" in self._buf:
            event, self._buf = self._buf.split(b"\n\n", 1)
            out += self._process_event(event)

        return bytes(out)

    def _process_event(self, event: bytes) -> bytes:
        payloads: list[bytes] = []
        for line in event.split(b"\n"):
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if payload == b"[DONE]":
                return b""
            payloads.append(payload)

        if not payloads:
            return b""

        raw = b"\n".join(payloads)
        try:
            chunk_dict = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug("SSE transform: skipping unparseable chunk")
            return b""
        try:
            model_chunk = self._iterator.chunk_parser(chunk_dict)
        except Exception:
            logger.debug("SSE transform: chunk_parser failed", exc_info=True)
            err = json.dumps({"error": {"message": "stream chunk parse error", "type": "server_error"}})
            return b"data: " + err.encode() + b"\n\n"
        if model_chunk is None:
            return b""
        return b"data: " + json.dumps(model_chunk.model_dump(mode="json", exclude_none=True)).encode() + b"\n\n"

    @property
    def raw_body(self) -> bytes:
        """Reassembled raw provider response body (pre-transform)."""
        return b"".join(self._raw_chunks)


def make_sse_transformer(
    provider: str,
    model: str,
    optional_params: dict[str, Any] | None = None,
) -> SseTransformer:
    return SseTransformer(provider, model, optional_params or {})
