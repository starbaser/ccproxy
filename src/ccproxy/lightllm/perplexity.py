"""Perplexity Pro WebUI subscription as a LiteLLM ``BaseConfig``.

Routes OpenAI ``/v1/chat/completions`` requests to Perplexity's internal
``POST https://www.perplexity.ai/rest/sse/perplexity_ask`` endpoint using
a ``__Secure-next-auth.session-token`` cookie for auth (Pro subscription).

The Perplexity wire format is not chat-completions-shaped: a single
``query_str`` plus a ``params`` block carrying model preference, search
focus, sources, etc. Streaming responses emit the FULL cumulative answer
on every chunk; ``PerplexityProIterator`` tracks last_content and emits
only the new tail as an OpenAI delta.

Model catalog is vendored from
``perplexity-webui-scraper/_static/models.json`` into
``ccproxy/specs/perplexity_models.json``.

Credits to https://henrique-coder.github.io/perplexity-webui-scraper
"""

from __future__ import annotations

import json
import logging
from importlib.resources import files
from typing import TYPE_CHECKING, Any

from litellm.llms.base_llm.base_model_iterator import BaseModelResponseIterator
from litellm.llms.base_llm.chat.transformation import BaseConfig, BaseLLMException
from litellm.types.utils import ModelResponse, ModelResponseStream

if TYPE_CHECKING:
    import httpx
    from litellm.litellm_core_utils.litellm_logging import Logging as LiteLLMLoggingObj
    from litellm.types.llms.openai import AllMessageValues

logger = logging.getLogger(__name__)


PERPLEXITY_URL = "https://www.perplexity.ai/rest/sse/perplexity_ask"
PERPLEXITY_API_VERSION = "2.18"
PERPLEXITY_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
PERPLEXITY_SESSION_COOKIE = "__Secure-next-auth.session-token"
PERPLEXITY_PROVIDER_NAME = "perplexity_pro"


def _load_models() -> dict[str, dict[str, str]]:
    """Load the vendored Perplexity model catalog keyed by public model id.

    Each entry maps to ``{identifier, mode}`` — the values stamped into the
    outbound payload's ``model_preference`` and ``mode`` fields.
    """
    raw: bytes = files("ccproxy.specs").joinpath("perplexity_models.json").read_bytes()  # type: ignore[arg-type]
    data: list[dict[str, str]] = json.loads(raw)
    return {m["id"]: {"identifier": m["identifier"], "mode": m["mode"]} for m in data}


PERPLEXITY_MODELS: dict[str, dict[str, str]] = _load_models()


_SOURCE_MAP: dict[str, str] = {
    "web": "web",
    "academic": "scholar",
    "social": "social",
    "finance": "edgar",
    "all": "web",
}

_SEARCH_MAP: dict[str, str] = {
    "web": "internet",
    "writing": "writing",
}

_TIME_MAP: dict[str, str] = {
    "all": "",
    "day": "DAY",
    "week": "WEEK",
    "month": "MONTH",
    "year": "YEAR",
}


def _flatten_messages(messages: list[Any]) -> str:
    """Flatten OpenAI-style chat messages into a single Perplexity ``query_str``.

    System messages are prefixed ``[System]: `` and reordered to the front;
    user / assistant messages follow in order, separated by blank lines.
    Multimodal ``image_url`` parts are dropped silently in Phase 1.
    """
    parts: list[str] = []
    for msg in messages:
        role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", None)
        content = (
            msg.get("content")
            if isinstance(msg, dict)
            else getattr(msg, "content", None)
        )

        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text_parts: list[str] = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    t = part.get("text")
                    if isinstance(t, str):
                        text_parts.append(t)
            text = "\n".join(text_parts)

        if not text:
            continue
        if role == "system":
            parts.insert(0, f"[System]: {text}")
        else:
            parts.append(text)

    return "\n\n".join(parts)


def _build_perplexity_payload(
    query: str,
    model_id: str,
    extras: dict[str, Any],
) -> dict[str, Any]:
    """Build the Perplexity SSE ask payload. ``extras`` comes from the
    OpenAI request's ``perplexity`` extra-body block.
    """
    meta = PERPLEXITY_MODELS.get(model_id)
    if meta is None:
        available = ", ".join(sorted(PERPLEXITY_MODELS))
        raise ValueError(
            f"Unknown Perplexity model {model_id!r}. Available: {available}"
        )

    raw_sources = extras.get("source_focus", "web")
    if not isinstance(raw_sources, list):
        raw_sources = [raw_sources]
    sources = [_SOURCE_MAP.get(s, "web") for s in raw_sources]

    coordinates = extras.get("coordinates")
    client_coords: dict[str, Any] | None = None
    if isinstance(coordinates, dict):
        client_coords = {
            "location_lat": coordinates.get("latitude"),
            "location_lng": coordinates.get("longitude"),
            "name": "",
        }

    save_to_library = bool(extras.get("save_to_library", False))

    params: dict[str, Any] = {
        "attachments": extras.get("attachments", []),
        "language": extras.get("language", "en-US"),
        "timezone": extras.get("timezone"),
        "client_coordinates": client_coords,
        "sources": sources,
        "model_preference": meta["identifier"],
        "mode": meta["mode"],
        "search_focus": _SEARCH_MAP.get(extras.get("search_focus", "web"), "internet"),
        "search_recency_filter": _TIME_MAP.get(extras.get("time_range", "all"), "")
        or None,
        "is_incognito": not save_to_library,
        "use_schematized_api": False,
        "local_search_enabled": client_coords is not None,
        "prompt_source": "user",
        "send_back_text_in_streaming_api": True,
        "version": PERPLEXITY_API_VERSION,
    }

    space_uuid = extras.get("space_uuid")
    if space_uuid:
        params["target_collection_uuid"] = space_uuid
        params["target_thread_access_level"] = 1
        params["query_source"] = "collection"
        params["is_incognito"] = False

    last_backend_uuid = extras.get("thread_uuid") or extras.get("last_backend_uuid")
    if last_backend_uuid:
        params["last_backend_uuid"] = last_backend_uuid
        params["query_source"] = "followup"
        if extras.get("read_write_token"):
            params["read_write_token"] = extras["read_write_token"]

    return {"params": params, "query_str": query}


class _PerplexityException(BaseLLMException):
    pass


class PerplexityProConfig(BaseConfig):
    """LiteLLM ``BaseConfig`` for the Perplexity Pro WebUI subscription path."""

    @property
    def supports_stream_param_in_request_body(self) -> bool:
        # Perplexity's /rest/sse/perplexity_ask payload has no ``stream`` field;
        # streaming is implicit (the endpoint always returns SSE).
        return False

    def get_supported_openai_params(self, model: str) -> list[str]:
        return ["stream"]

    def map_openai_params(
        self,
        non_default_params: dict[str, Any],
        optional_params: dict[str, Any],
        model: str,
        drop_params: bool,
    ) -> dict[str, Any]:
        out = dict(optional_params)
        if "perplexity" in non_default_params:
            out["perplexity"] = non_default_params["perplexity"]
        return out

    def validate_environment(
        self,
        headers: dict[str, str],
        model: str,
        messages: list[AllMessageValues],
        optional_params: dict[str, Any],
        litellm_params: dict[str, Any],
        api_key: str | None = None,
        api_base: str | None = None,
    ) -> dict[str, str]:
        if not api_key:
            raise ValueError(
                "Perplexity Pro requires the session-token cookie value as api_key"
            )
        out = dict(headers)
        out["Cookie"] = f"{PERPLEXITY_SESSION_COOKIE}={api_key}"
        out["User-Agent"] = PERPLEXITY_BROWSER_UA
        out["Origin"] = "https://www.perplexity.ai"
        out["Referer"] = "https://www.perplexity.ai/"
        out["Accept"] = "text/event-stream, application/json"
        out["Content-Type"] = "application/json"
        return out

    def get_complete_url(
        self,
        api_base: str | None,
        api_key: str | None,
        model: str,
        optional_params: dict[str, Any],
        litellm_params: dict[str, Any],
        stream: bool | None = None,
    ) -> str:
        return PERPLEXITY_URL

    def transform_request(
        self,
        model: str,
        messages: list[AllMessageValues],
        optional_params: dict[str, Any],
        litellm_params: dict[str, Any],
        headers: dict[str, str],
    ) -> dict[str, Any]:
        raw_extras = optional_params.get("perplexity") or {}
        extras: dict[str, Any] = raw_extras if isinstance(raw_extras, dict) else {}
        return _build_perplexity_payload(
            query=_flatten_messages(messages),
            model_id=model,
            extras=extras,
        )

    def transform_response(
        self,
        model: str,
        raw_response: httpx.Response,
        model_response: ModelResponse,
        logging_obj: LiteLLMLoggingObj,
        request_data: dict[str, Any],
        messages: list[AllMessageValues],
        optional_params: dict[str, Any],
        litellm_params: dict[str, Any],
        encoding: Any,
        api_key: str | None = None,
        json_mode: bool | None = None,
    ) -> ModelResponse:
        full_text = ""
        for raw_line in raw_response.text.splitlines():
            if not raw_line.startswith("data:"):
                continue
            payload = raw_line[5:].strip()
            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                continue
            text = _extract_answer_text(event)
            if text is not None:
                full_text = text

        from litellm.types.utils import Choices, Message

        model_response.id = f"chatcmpl-{model}"
        model_response.model = model
        model_response.choices = [
            Choices(
                index=0,
                message=Message(role="assistant", content=full_text),
                finish_reason="stop",
            )
        ]
        return model_response

    def get_error_class(
        self,
        error_message: str,
        status_code: int,
        headers: Any,
    ) -> BaseLLMException:
        return _PerplexityException(
            status_code=status_code, message=error_message, headers=headers
        )

    def get_model_response_iterator(
        self,
        streaming_response: Any,
        sync_stream: bool,
        json_mode: bool | None = False,
    ) -> Any:
        return PerplexityProIterator(
            streaming_response=iter([]),
            sync_stream=sync_stream,
            json_mode=json_mode,
        )


def _extract_answer_text(event: dict[str, Any]) -> str | None:
    """Extract the cumulative answer text from one Perplexity SSE event.

    Two payload variants:
    - Legacy: ``event["text"]`` is a JSON-encoded string of ``{"answer": "...", ...}``.
    - Schematized: ``event["text"]`` is a JSON-encoded list of step blocks; the
      ``FINAL`` step's ``content.answer`` (sometimes itself a JSON string) is
      the cumulative answer.

    Returns ``None`` for events that don't carry answer text (status pings,
    plan blocks, etc.).
    """
    text_field = event.get("text")
    if not isinstance(text_field, str):
        return None
    try:
        parsed = json.loads(text_field)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict):
        answer = parsed.get("answer")
        return answer if isinstance(answer, str) else None
    if isinstance(parsed, list):
        for block in parsed:
            if not isinstance(block, dict):
                continue
            if block.get("step_type") != "FINAL":
                continue
            content = block.get("content", {})
            if not isinstance(content, dict):
                continue
            answer = content.get("answer")
            if isinstance(answer, str):
                try:
                    inner = json.loads(answer)
                except json.JSONDecodeError:
                    return answer
                if isinstance(inner, dict):
                    inner_answer = inner.get("answer")
                    if isinstance(inner_answer, str):
                        return inner_answer
                return answer
    return None


class PerplexityProIterator(BaseModelResponseIterator):
    """Stateful Perplexity SSE → OpenAI delta chunk parser.

    Perplexity emits the FULL cumulative answer on every chunk. We track
    ``_last`` and emit the new tail as an OpenAI ``ChatCompletionChunk`` delta.
    """

    def __init__(
        self,
        streaming_response: Any,
        sync_stream: bool,
        json_mode: bool | None = False,
    ) -> None:
        super().__init__(
            streaming_response=streaming_response,
            sync_stream=sync_stream,
            json_mode=json_mode,
        )
        self._last: str = ""

    def chunk_parser(self, chunk: dict[str, Any]) -> ModelResponseStream:
        text = _extract_answer_text(chunk)
        is_final = bool(chunk.get("final_sse_message")) or bool(chunk.get("final"))

        delta_content: str | None = None
        if (
            text is not None
            and len(text) >= len(self._last)
            and text.startswith(self._last)
        ):
            delta_content = text[len(self._last) :]
            self._last = text
        elif text is not None and text != self._last:
            delta_content = text
            self._last = text

        from litellm.types.utils import Delta, StreamingChoices

        delta = Delta(content=delta_content) if delta_content else Delta()
        choice = StreamingChoices(
            index=0,
            delta=delta,
            finish_reason="stop" if is_final else None,
        )
        return ModelResponseStream(choices=[choice])
