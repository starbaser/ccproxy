# LiteLLM Transformation System — Architecture & Extraction Map

Reference for surgically extracting LiteLLM's provider-to-provider request/response transformation system and importing it as a standalone layer inside ccproxy's inspector routing, leaving behind cost tracking, proxy server, router, callbacks, caching, budgets, and metadata bookkeeping.

All source paths below are relative to:

```
/home/eigenmage/dev/projects/ccproxy/.kitstore/sources/litellm/litellm/
```

---

## 1. What "transformation" means in LiteLLM

LiteLLM's core job is to normalize the OpenAI chat-completions schema across ~100 provider APIs. The transformation layer is the code that:

1. Maps `ChatCompletionRequest` (OpenAI shape) → provider-native request body (Anthropic `messages`, Gemini `contents`, Bedrock Converse, etc.).
2. Maps provider-native response → `ModelResponse` (OpenAI-compatible output).
3. Handles streaming: parses provider-specific SSE chunks into a uniform `ModelResponseStream`.
4. Validates per-model `supported_openai_params` and drops/rewrites unsupported fields.
5. Injects auth headers (`x-api-key`, `Authorization: Bearer …`, AWS SigV4, etc.).
6. Builds the full request URL per provider endpoint.

Everything else — cost math, usage aggregation, callbacks, caching, routing strategies, budgets, guardrails, the proxy server — lives outside this layer and is what we want to leave behind.

---

## 2. The abstract contract — `llms/base_llm/`

```
llms/base_llm/
├── __init__.py
├── base_model_iterator.py       BaseModelResponseIterator, MockResponseIterator,
│                                FakeStreamResponseIterator  (260 LOC)
├── base_utils.py                BaseLLMModelInfo, BaseTokenCounter,
│                                type_to_response_format_param,
│                                map_developer_role_to_system_role  (227 LOC)
└── chat/
    └── transformation.py        BaseConfig, BaseLLMException       (466 LOC)
```

`BaseConfig` in `llms/base_llm/chat/transformation.py` is THE contract every chat provider implements. Total of ~953 LOC across the three base files — trivially extractable.

### 2.1 `BaseConfig(ABC)` abstract surface

```python
class BaseConfig(ABC):
    # ───── abstract ────────────────────────────────────────────────────
    @abstractmethod
    def get_supported_openai_params(self, model: str) -> list: ...

    @abstractmethod
    def map_openai_params(
        self, non_default_params: dict, optional_params: dict,
        model: str, drop_params: bool,
    ) -> dict: ...

    @abstractmethod
    def validate_environment(
        self, headers: dict, model: str,
        messages: list[AllMessageValues],
        optional_params: dict, litellm_params: dict,
        api_key: str | None = None, api_base: str | None = None,
    ) -> dict: ...

    @abstractmethod
    def transform_request(
        self, model: str, messages: list[AllMessageValues],
        optional_params: dict, litellm_params: dict, headers: dict,
    ) -> dict: ...

    @abstractmethod
    def transform_response(
        self, model: str, raw_response: httpx.Response,
        model_response: ModelResponse, logging_obj: Any,
        request_data: dict, messages: list[AllMessageValues],
        optional_params: dict, litellm_params: dict,
        encoding: Any, api_key: str | None = None,
        json_mode: bool | None = None,
    ) -> ModelResponse: ...

    @abstractmethod
    def get_error_class(
        self, error_message: str, status_code: int,
        headers: Union[dict, httpx.Headers],
    ) -> BaseLLMException: ...

    # ───── concrete helpers (non-abstract) ─────────────────────────────
    @classmethod
    def get_config(cls) -> dict: ...                  # class-level defaults
    def get_json_schema_from_pydantic_object(...) -> dict: ...
    def is_thinking_enabled(...) -> bool: ...
    def is_max_tokens_in_request(...) -> bool: ...
    def update_optional_params_with_thinking_tokens(...) -> dict: ...
    def should_fake_stream(...) -> bool: ...          # default False
    def translate_developer_role_to_system_role(...) -> list: ...
    def sign_request(...) -> tuple[dict, bytes | None]: ...   # AWS SigV4 hook
    def get_complete_url(...) -> str: ...             # build API URL
    async def async_transform_request(...) -> dict: ...       # async override
    def get_model_response_iterator(...) -> BaseModelResponseIterator | None: ...
    def get_async_custom_stream_wrapper(...): ...
    def get_sync_custom_stream_wrapper(...): ...
    def post_stream_processing(...): ...
    def calculate_additional_costs(...) -> float: 0   # STUB THIS OUT
    def should_retry_llm_api_inside_llm_translation_on_http_error(...) -> bool: ...
    def transform_request_on_unprocessable_entity_error(...) -> dict: ...

    # ───── properties ──────────────────────────────────────────────────
    @property
    def supports_stream_param_in_request_body(self) -> bool: True
    @property
    def has_custom_stream_wrapper(self) -> bool: False
    @property
    def custom_llm_provider(self) -> str | None: None
```

### 2.2 `BaseLLMException`

```python
class BaseLLMException(Exception):
    def __init__(
        self, status_code: int, message: str,
        headers: dict | httpx.Headers | None = None,
        request: httpx.Request | None = None,
        response: httpx.Response | None = None,
        body: dict | None = None,
    ): ...
```

Every provider subclasses this (`AnthropicError`, `BedrockError`, `GeminiError`, `OpenAIError`, …).

### 2.3 `BaseLLMModelInfo(ABC)` — secondary contract

```python
class BaseLLMModelInfo(ABC):
    @abstractmethod
    def get_models(self, api_key=None, api_base=None) -> list[str]: ...

    @staticmethod
    @abstractmethod
    def get_api_key(api_key=None) -> str | None: ...

    @staticmethod
    @abstractmethod
    def get_api_base(api_base=None) -> str | None: ...

    @abstractmethod
    def validate_environment(self, ...) -> dict: ...

    @staticmethod
    @abstractmethod
    def get_base_model(model: str) -> str | None: ...

    # Concrete:
    def get_provider_info(...) -> ProviderSpecificModelInfo: ...
    def get_token_counter(...) -> BaseTokenCounter | None: ...
```

Providers typically multiply-inherit: `AnthropicConfig(AnthropicModelInfo, BaseConfig)`, `OpenAIGPTConfig(BaseLLMModelInfo, BaseConfig)`.

### 2.4 `BaseModelResponseIterator` — streaming contract

```python
class BaseModelResponseIterator:
    def __init__(self, streaming_response, sync_stream: bool, json_mode: bool = False): ...
    def chunk_parser(self, chunk: dict) -> ModelResponseStream: ...   # subclass impl
    def __iter__(self) -> Iterator[ModelResponseStream]: ...
    async def __aiter__(self) -> AsyncIterator[ModelResponseStream]: ...
```

Sibling classes in the same file:
- `MockResponseIterator` — wraps a complete `ModelResponse` as fake stream (AI21-style).
- `FakeStreamResponseIterator` — emits a non-streaming response as a single streaming chunk.

---

## 3. The dispatch pipeline — `main.py` → `BaseLLMHTTPHandler`

### 3.1 `completion()` / `acompletion()` — `main.py`

```
completion(model, messages, …)
  │
  ├─ validate_and_fix_openai_messages(messages)
  ├─ validate_and_fix_openai_tools(tools)
  │
  ├─ model, provider, api_key, api_base = get_llm_provider(model, …)
  │                                  │
  │                                  └─ litellm_core_utils/get_llm_provider_logic.py
  │
  ├─ provider_config = ProviderConfigManager.get_provider_chat_config(model, provider)
  │                                  │
  │                                  └─ returns a BaseConfig instance (e.g. AnthropicConfig())
  │
  ├─ messages = provider_config.translate_developer_role_to_system_role(messages)
  ├─ optional_params = get_optional_params(…)       # filters/maps to provider-supported
  ├─ litellm_params  = get_litellm_params(…)
  │
  └─ base_llm_http_handler.completion(
         model, messages, api_base, custom_llm_provider, model_response,
         encoding, logging_obj, optional_params, timeout, litellm_params,
         acompletion, stream, fake_stream, api_key, headers, client,
         provider_config=provider_config, shared_session=shared_session,
     )
```

### 3.2 `BaseLLMHTTPHandler.completion()` — `llms/custom_httpx/llm_http_handler.py`

```
┌─────────────────────────────────────────────────────────────────────┐
│  1. headers = provider_config.validate_environment(api_key, …)     │
│     → sets x-api-key / Authorization / anthropic-version / etc.    │
│                                                                     │
│  2. api_base = provider_config.get_complete_url(api_base, …)       │
│     → https://api.anthropic.com/v1/messages                        │
│                                                                     │
│  3. data = provider_config.transform_request(                      │
│         model, messages, optional_params, litellm_params, headers) │
│     → OpenAI → Anthropic body                                      │
│                                                                     │
│  4. data = {**data, **extra_body}                                   │
│                                                                     │
│  5. headers, signed_body = provider_config.sign_request(…)         │
│     → AWS SigV4 / no-op for most providers                         │
│                                                                     │
│  6. logging_obj.pre_call(…)                     ← STUB-ABLE         │
│                                                                     │
│  7. dispatch:                                                       │
│       if acompletion and stream: acompletion_stream_function(…)    │
│       elif acompletion:           async_completion(…)              │
│       elif stream:                make_sync_call(…)                │
│       else:                        sync path → transform_response  │
│                                                                     │
│  8. raw_response = await async_httpx_client.post(api_base, data)   │
│                                                                     │
│  9. initial_response = provider_config.transform_response(         │
│         model, raw_response, model_response, logging_obj,          │
│         request_data=data, …)                                      │
│     → Anthropic JSON → ModelResponse (OpenAI shape)                │
└─────────────────────────────────────────────────────────────────────┘
```

`BaseLLMHTTPHandler` is ~12k LOC and also dispatches embeddings, rerank, audio, image-gen, responses API, OCR, search, anthropic_messages, containers, etc. For the chat-only extraction we only need `completion()`, `async_completion()`, `acompletion_stream_function()`, `make_sync_call()`, and a handful of helpers — most of the file is modality-specific.

### 3.3 `ProviderConfigManager` — `utils.py` (~line 7989)

```python
class ProviderConfigManager:
    _PROVIDER_CONFIG_MAP: dict[LlmProviders, tuple[Callable, bool]] | None = None

    @staticmethod
    def get_provider_chat_config(model: str, provider: LlmProviders) -> BaseConfig | None: ...
    @staticmethod
    def get_provider_embedding_config(model, provider) -> BaseEmbeddingConfig | None: ...
    @staticmethod
    def get_provider_audio_transcription_config(…): ...
    @staticmethod
    def get_provider_text_to_speech_config(…): ...
    @staticmethod
    def get_provider_model_info(model, provider) -> BaseLLMModelInfo | None: ...
```

Internally just a fat lambda dict: `LlmProviders.ANTHROPIC: lambda: litellm.AnthropicConfig()`. A few providers (Bedrock, Vertex, Azure, Cohere) take a `model` arg and sub-dispatch. This whole class is trivially rewritable as a pure-data registry.

### 3.4 `get_llm_provider()` — `litellm_core_utils/get_llm_provider_logic.py`

Returns `(model, custom_llm_provider, dynamic_api_key, api_base)`. Order of precedence:

1. `litellm_params` preset
2. Azure-AI-Studio `azure/…` → `openai`
3. Cohere chat model detection
4. Anthropic text model detection
5. `JSONProviderRegistry` (`llms/openai_like/providers.json`)
6. `litellm.provider_list` prefix matching (e.g. `anthropic/claude-3` → `anthropic`)
7. Known OpenAI-compatible endpoints via `api_base`
8. Giant hardcoded model-name → provider lookup tables in `litellm/__init__.py`

We do not need the full registry for ccproxy — just an explicit mapping.

---

## 4. Representative provider implementations

### 4.1 Anthropic — `llms/anthropic/`

```
anthropic/
├── common_utils.py         AnthropicError(BaseLLMException),
│                           AnthropicModelInfo(BaseLLMModelInfo)
├── chat/
│   ├── transformation.py   AnthropicConfig(AnthropicModelInfo, BaseConfig)   (2004 LOC)
│   └── handler.py          AnthropicChatCompletion, ModelResponseIterator
├── completion/transformation.py   AnthropicTextConfig(BaseConfig)
├── batches/  count_tokens/  experimental_pass_through/  files/  skills/
```

`AnthropicConfig` is the canonical complex provider. Key work:

- `get_supported_openai_params(model)` → ~12 params (`stream`, `temperature`, `tools`, `thinking`, `reasoning_effort`, `cache_control`, …).
- `map_openai_params(…)` → `stop` → `stop_sequences`, tool translation, `tool_choice`, `response_format` → native `output_format` OR tool-based JSON mode, `thinking`/`reasoning_effort` → Anthropic `thinking` block, `web_search_options` → web-search tool, `context_management`, `cache_control`.
- `transform_request(…)` → emits `{"model": …, "messages": […], "system": …, …}`, calling `anthropic_messages_pt()` to convert messages.
- `transform_response(…)` → parses Anthropic JSON, reconstructs thinking blocks, tool calls, JSON mode, usage deltas.
- `validate_environment(…)` → `x-api-key`, `anthropic-version`, `anthropic-beta`.
- `get_complete_url(…)` → `{api_base}/v1/messages`.
- `get_error_class(…)` → `AnthropicError`.

`ModelResponseIterator` in `handler.py` subclasses `BaseModelResponseIterator` and parses Anthropic SSE events: `message_start`, `content_block_start`, `content_block_delta` (thinking + text + tool_use), `content_block_stop`, `message_delta`.

### 4.2 Gemini — `llms/gemini/` + `llms/vertex_ai/gemini/`

```
gemini/chat/transformation.py      GoogleAIStudioGeminiConfig(VertexGeminiConfig)   # thin wrapper
vertex_ai/gemini/
├── transformation.py              _gemini_convert_messages_with_history,
│                                   _transform_request_body, ...
└── vertex_and_google_ai_studio_gemini.py
                                    VertexGeminiConfig(VertexAIBaseConfig, BaseConfig)
```

`VertexGeminiConfig` (~2400 LOC) handles the Gemini/Vertex API shape: `{"contents": [...], "generationConfig": {...}, "tools": [...], "toolConfig": {...}, "thinkingConfig": {...}, "responseModalities": [...]}`. Streaming iterator lives inline in the same file (SSE parser for Gemini's `candidates` streaming format).

### 4.3 Bedrock — `llms/bedrock/`

```
bedrock/
├── base_aws_llm.py                 BaseAWSLLM(BaseLLMModelInfo)   # credentials + SigV4
├── common_utils.py                 BedrockError, get_bedrock_chat_config
├── chat/
│   ├── converse_transformation.py  AmazonConverseConfig(BaseConfig)        (~2100 LOC)
│   ├── converse_handler.py
│   ├── invoke_handler.py
│   ├── invoke_transformations/     AmazonInvokeConfig + per-model-family files
│   ├── invoke_agent/transformation.py   AmazonInvokeAgentConfig
│   └── agentcore/transformation.py      AmazonAgentCoreConfig
```

`AmazonConverseConfig` internally delegates to `AnthropicConfig` for param mapping when the underlying Bedrock model is Claude — i.e. provider configs reuse each other. `sign_request()` performs AWS SigV4 signing via `base_aws_llm.py`.

### 4.4 OpenAI — `llms/openai/`

```
openai/chat/
├── gpt_transformation.py           OpenAIGPTConfig(BaseLLMModelInfo, BaseConfig)   # BASE
├── gpt_5_transformation.py         OpenAIGPT5Config(OpenAIGPTConfig)
├── gpt_audio_transformation.py     OpenAIGPTAudioConfig(OpenAIGPTConfig)
├── o_series_transformation.py      OpenAIOSeriesConfig(OpenAIGPTConfig)
└── o_series_handler.py
```

`OpenAIGPTConfig` is the pivot class: **~20 other "OpenAI-compatible" providers subclass it** (Azure, Cerebras, Baseten, Maritalk, Sambanova, Together, Mistral, OpenRouter, Groq, Perplexity, DeepSeek, Fireworks, Nvidia, Databricks, HostedVLLM, LMStudio, Llama-Vertex, Cohere V2 chat, AmazonBedrockOpenAI, Snowflake, …). They typically only override `validate_environment()` and `get_complete_url()`. This means once you have `OpenAIGPTConfig` extracted, you get dozens of providers for free.

---

## 5. Key shared utilities transformations depend on

### 5.1 `litellm_core_utils/prompt_templates/factory.py` (~5434 LOC)

The message-format translation library. Functions transformations call into:

- `anthropic_messages_pt(messages)` — OpenAI messages → Anthropic format (tool calls, images, documents, thinking blocks, cache_control).
- `_bedrock_converse_messages_pt(messages, …)` — OpenAI → Bedrock Converse content blocks.
- `BedrockConverseMessagesProcessor` (class) — sync/async processor.
- `convert_to_gemini_tool_call_invoke()` / `convert_to_gemini_tool_call_result()` — Gemini tool shape.
- `cohere_messages_pt_v2()` / `cohere_message_pt()` — Cohere.
- `convert_to_anthropic_tool_result()` / `convert_to_anthropic_tool_invoke()` — Anthropic tool shape.
- `_gemini_convert_messages_with_history()` (imported from `vertex_ai/gemini/transformation.py`).
- `BedrockImageProcessor` — image URL → base64 (sync + async).
- `hf_chat_template()` / `ahf_chat_template()` — HuggingFace Jinja templates.
- `map_system_message_pt()` — strips system messages for providers that don't support them.
- `function_call_prompt()` — encodes tool calls into prompt text for providers without native tool support.

This file is big but nearly pure — it only depends on `types/` and `core_helpers`. Extract whole.

### 5.2 `litellm_core_utils/core_helpers.py`

- `map_finish_reason(finish_reason: str) -> OpenAIChatCompletionFinishReason`
- `process_response_headers()`
- `safe_deep_copy()`
- `filter_exceptions_from_params()` / `filter_internal_params()`
- `reconstruct_model_name()`
- `get_litellm_metadata_from_kwargs()` ← drop this one, metadata bleed

### 5.3 `litellm_core_utils/prompt_templates/image_handling.py`

- `convert_url_to_base64(url)` — sync image/pdf fetch + base64.
- `async_convert_url_to_base64(url)` — async variant.

### 5.4 `litellm_core_utils/prompt_templates/common_utils.py`

- `get_file_ids_from_messages()`
- `get_tool_call_names()`
- `_parse_content_for_reasoning()`

### 5.5 `litellm_core_utils/llm_response_utils/convert_dict_to_response.py` (833 LOC)

- `convert_to_model_response_object(...)` — raw provider dict → `ModelResponse`. Used by almost every `transform_response()`.
- `LiteLLMResponseObjectHandler` — handles non-chat modalities.
- `convert_to_streaming_response(…)` / `convert_to_streaming_response_async(…)` — wrap non-streaming as streaming.

### 5.6 `litellm_core_utils/streaming_handler.py` (~2414 LOC)

```python
class CustomStreamWrapper:
    def __init__(self, completion_stream, model, custom_llm_provider, logging_obj, …): ...
    def __iter__(self) -> Iterator[ModelResponseStream]: ...
    def __aiter__(self) -> AsyncIterator[ModelResponseStream]: ...
    def __next__(self) -> ModelResponseStream: ...
    async def __anext__(self) -> ModelResponseStream: ...
    def chunk_creator(self, chunk) -> ModelResponseStream: ...             # huge dispatch method
    def return_processed_chunk_logic(self, chunk) -> ModelResponseStream: ...
    def model_response_creator(self, chunk=None) -> ModelResponseStream: ...
```

`chunk_creator()` dispatches to provider-specific legacy helpers (`handle_openai_chat_completion_chunk`, `handle_azure_chunk`, `handle_predibase_chunk`, `handle_ai21_chunk`, `handle_maritalk_chunk`, `handle_nlp_cloud_chunk`, `handle_baseten_chunk`, `handle_triton_stream`). For the newer providers (Anthropic, Bedrock, OpenAI, Gemini), `chunk_creator` just calls `completion_stream.chunk_parser(chunk)` on the `BaseModelResponseIterator` subclass.

This file has nontrivial entanglement with `logging_obj` (token counting, caching of the streaming response) and with `litellm.cache`. A lean extraction should prune that logic.

### 5.7 `litellm_core_utils/get_llm_provider_logic.py`

`get_llm_provider(model, custom_llm_provider=None, api_base=None, api_key=None, litellm_params=None) -> tuple[str, str, str | None, str | None]`. ~600 LOC of provider detection heuristics.

### 5.8 `litellm_core_utils/exception_mapping_utils.py`

`exception_type()` — maps raw provider exceptions to `litellm.*Error` hierarchy. Needed if you want LiteLLM-compatible exception semantics; otherwise you can just let `BaseLLMException` propagate.

### 5.9 `litellm_core_utils/get_supported_openai_params.py`

Small helper that proxies `provider_config.get_supported_openai_params(model)`. Useful or inlineable.

---

## 6. Types system — `types/`

```
types/
├── utils.py                     (3638 LOC)  ModelResponse, ModelResponseStream,
│                                             Usage, Message, Delta, Choices,
│                                             StreamingChoices, LlmProviders (Enum),
│                                             GenericStreamingChunk, ModelInfo, …
├── llms/
│   ├── openai.py                (2283 LOC)  AllMessageValues,
│   │                                         ChatCompletion{User,Assistant,System,Tool}Message,
│   │                                         ChatCompletionToolParam,
│   │                                         ChatCompletionThinkingBlock, …
│   ├── anthropic.py             AnthropicMessagesRequest, AnthropicMessagesTool,
│   │                             AnthropicThinkingParam, ContentBlockDelta, …
│   ├── vertex_ai.py             ContentType, PartType, ToolConfig, GenerationConfig, …
│   ├── bedrock.py               BedrockContentBlock, InferenceConfig, BedrockToolBlock, …
│   ├── gemini.py                BidiGenerateContentServerMessage, …
│   ├── base.py                  LiteLLMPydanticObjectBase
│   └── {cohere, mistral, azure, watsonx, oci, …}.py
└── completion.py                StandardLoggingPayload, etc.
```

`types/llms/openai.py` imports directly from the `openai` SDK (`from openai.types.chat import …`). The extracted project therefore inherits an `openai>=x` runtime dependency.

`ModelResponse` is the normalized chat output type. `ModelResponseStream` is the streaming chunk. `Usage` uses `PromptTokensDetailsWrapper` / `CompletionTokensDetailsWrapper` for fine-grained token accounting.

---

## 7. HTTP client layer — `llms/custom_httpx/`

```
custom_httpx/
├── http_handler.py              AsyncHTTPHandler, HTTPHandler,
│                                 _get_httpx_client, get_async_httpx_client    (1303 LOC)
├── llm_http_handler.py          BaseLLMHTTPHandler  (universal dispatch)       (12074 LOC)
├── aiohttp_handler.py           aiohttp-based handler
├── aiohttp_transport.py         LiteLLMAiohttpTransport
├── async_client_cleanup.py
├── httpx_handler.py             additional httpx helpers
├── container_handler.py
└── mock_transport.py
```

`AsyncHTTPHandler` wraps `httpx.AsyncClient` with SSL verification, pooling, custom transport, retries, and has a single `async def post(url, headers, data, timeout, stream, logging_obj)` entry. `HTTPHandler` is the sync sibling.

For the mitmproxy-embedded use case we largely do NOT need these — mitmproxy does the outbound HTTP itself once the request is rewritten. The `BaseLLMHTTPHandler` call patterns remain useful as a reference for how to sequence `validate_environment → get_complete_url → transform_request → transform_response`.

---

## 8. Exceptions — `exceptions.py`

```
openai.AuthenticationError     → litellm.AuthenticationError
openai.NotFoundError           → litellm.NotFoundError
openai.BadRequestError         → litellm.BadRequestError
openai.UnprocessableEntityError→ litellm.UnprocessableEntityError
openai.APITimeoutError         → litellm.Timeout
openai.PermissionDeniedError   → litellm.PermissionDeniedError
openai.RateLimitError          → litellm.RateLimitError
openai.InternalServerError     → litellm.InternalServerError
openai.APIConnectionError      → litellm.APIConnectionError
```

Plus litellm-specific children: `ContextWindowExceededError`, `RejectedRequestError`, `UnsupportedParamsError`, `BadGatewayError`, `BudgetExceededError`, `MockException`, `LiteLLMUnknownProvider`, `JSONSchemaValidationError`, `MidStreamFallbackError`, `GuardrailRaisedException`, `BlockedPiiEntityError`.

Provider-specific exceptions all subclass `BaseLLMException` and are mapped via `exception_type()` in `exception_mapping_utils.py`.

---

## 9. Pollution map — what to discard

### 9.1 Tightly coupled (cannot avoid — must be ported as-is)

| `litellm.*` attribute | Used by | Purpose |
|---|---|---|
| `litellm.drop_params` (bool) | all providers | silently drop unsupported params |
| `litellm.modify_params` (bool) | Anthropic | allow adding dummy tools for JSON mode |
| `litellm.disable_add_prefix_to_prompt` (bool) | Anthropic | disable prompt-prefix injection |
| `litellm.Message(...)` | Anthropic, Bedrock | build response message object |
| `litellm.Usage(...)` | all | usage object constructor |
| `litellm.ModelResponse(...)` | all | response object constructor |
| `litellm.UnsupportedParamsError` | Anthropic | raise on unsupported params |
| `litellm.verbose_logger` | many | debug logging |
| `litellm.exceptions.*` | several | error raising |

Replacement strategy: create a thin shim module `ccproxy.lllm.compat` exposing these as plain module-level variables + class re-exports. Wire via `sys.modules['litellm'] = ccproxy_compat_module` OR replace `import litellm` → `from ccproxy.lllm import compat as litellm` via a targeted sed pass during the vendoring step.

### 9.2 Partially coupled — the `logging_obj` entanglement

Every `transform_response(…)` takes a `logging_obj` parameter. At runtime it is typed `Any`. The only method transformations call on it is `logging_obj.post_call(input, api_key, original_response, additional_args)`. `BaseLLMHTTPHandler.completion()` additionally calls `pre_call()` and other methods.

**Stub:**

```python
class NoopLogging:
    model_call_details: dict[str, Any] = {}
    def pre_call(self, *a, **kw) -> None: ...
    def post_call(self, *a, **kw) -> None: ...
    def async_success_handler(self, *a, **kw) -> None: ...
    def success_handler(self, *a, **kw) -> None: ...
    def async_failure_handler(self, *a, **kw) -> None: ...
    def failure_handler(self, *a, **kw) -> None: ...
```

The real `Logging` class is ~3000 LOC of callbacks, cost calculators, and caching integration. Do not port it.

### 9.3 Not needed — discard entirely

```
litellm/proxy/                                    full proxy server
litellm/router.py + router_utils/ + router_strategy/
litellm/caching/                                  cache backends
litellm/integrations/                             langfuse, datadog, arize, …
litellm/cost_calculator.py + llm_cost_calc/       pricing math
litellm/budget_manager.py
litellm/litellm_core_utils/litellm_logging.py     full Logging class
litellm/litellm_core_utils/logging_callback_manager.py
litellm/model_prices_and_context_window_backup.json   pricing data
```

---

## 10. Dependency map

### 10.1 Clean extraction candidates (low coupling)

```
llms/base_llm/chat/transformation.py              BaseConfig, BaseLLMException
llms/base_llm/base_utils.py                       BaseLLMModelInfo, BaseTokenCounter
llms/base_llm/base_model_iterator.py              BaseModelResponseIterator
constants.py                                      DEFAULT_MAX_TOKENS, RESPONSE_FORMAT_TOOL_NAME, …
types/llms/openai.py                              (pulls in openai SDK types)
types/llms/anthropic.py                           pure TypedDicts
types/llms/vertex_ai.py                           pure TypedDicts
types/llms/bedrock.py                             pure TypedDicts
types/utils.py                                    core Pydantic types
litellm_core_utils/core_helpers.py                finish_reason, response_headers
litellm_core_utils/prompt_templates/image_handling.py
litellm_core_utils/prompt_templates/common_utils.py
litellm_core_utils/get_supported_openai_params.py
```

### 10.2 Files that do `import litellm` (need the compat shim)

```
llms/anthropic/chat/transformation.py             uses litellm.drop_params, litellm.Message, …
llms/bedrock/chat/converse_transformation.py      uses litellm.exceptions.BadRequestError
llms/vertex_ai/gemini/vertex_and_google_ai_studio_gemini.py   uses litellm.verbose_logger, …
llms/openai/chat/gpt_transformation.py            uses litellm flags
```

All transformations rely on the circular-import trick: `from litellm.llms.anthropic.chat.transformation import AnthropicConfig` works because by the time `AnthropicConfig` methods execute, `litellm` module is fully loaded. In our extraction we sever this: `import litellm` becomes `from ccproxy.lllm import compat as litellm` (or equivalent `sys.modules` override).

---

## 11. Full data flow — `completion(model="anthropic/claude-3-5-sonnet", …)`

```
completion(model="anthropic/claude-3-5-sonnet", messages=[…])
          │
          ▼
  litellm/main.py::completion()
          │
┌─────────┴──────────────────────────────────────────┐
│ 1. validate_and_fix_openai_messages()              │
│ 2. get_llm_provider() → ("claude-3-5-sonnet",      │
│                          "anthropic", None, None)  │
│ 3. provider_config = AnthropicConfig()             │
│ 4. messages = config.translate_developer_role(…)   │
│ 5. get_optional_params()                           │
│       → config.map_openai_params(…)                │
│       → optional_params = {"max_tokens": 8192, …}  │
│ 6. litellm_params = get_litellm_params(…)          │
│ 7. base_llm_http_handler.completion(               │
│       …, provider_config=config)                   │
└─────────┬──────────────────────────────────────────┘
          │
          ▼
  BaseLLMHTTPHandler.completion()
          │
┌─────────┴──────────────────────────────────────────┐
│ A. headers = config.validate_environment()         │
│       → x-api-key, anthropic-version, …            │
│ B. api_base = config.get_complete_url()            │
│       → https://api.anthropic.com/v1/messages      │
│ C. data = config.transform_request()               │
│       → calls anthropic_messages_pt()              │
│ D. headers, signed = config.sign_request()         │
│       → no-op for Anthropic                        │
│ E. logging_obj.pre_call()                          │
│ F. dispatch:                                       │
│    acompletion_stream_function() | async_completion│
└─────────┬──────────────────────────────────────────┘
          │
          ▼ (non-stream)
  async_httpx_client.post(api_base, data)
          │
          ▼
  config.transform_response(raw_response, …)
          │
          ▼
     ModelResponse (OpenAI shape)
```

### Streaming path

```
BaseLLMHTTPHandler.acompletion_stream_function()
          │
          ▼
  async_httpx_client.stream(…) → SSE bytes
          │
          ▼
  iterator = config.get_model_response_iterator(streaming_response, …)
          │   (AnthropicConfig returns ModelResponseIterator from anthropic/chat/handler.py)
          ▼
  CustomStreamWrapper(completion_stream=iterator,
                      custom_llm_provider="anthropic", model=model, …)
          │
          ▼  async for chunk in wrapper:
  iterator.chunk_parser(raw_sse_json) → ModelResponseStream
          │
          ▼
  client receives ModelResponseStream
```

---

## 12. Provider inventory (chat-capable)

Top-level provider directories under `llms/`:

```
a2a, ai21, aiml, aiohttp_openai, amazon_nova, anthropic, aws_polly, azure, azure_ai,
base_llm, baseten, bedrock, bedrock_mantle, black_forest_labs, brave, bytez, cerebras,
chatgpt, clarifai, cloudflare, codestral, cohere, cometapi, compactifai, custom_httpx,
dashscope, databricks, dataforseo, datarobot, deepgram, deepinfra, deepseek,
docker_model_runner, duckduckgo, elevenlabs, empower, exa_ai, fal_ai, featherless_ai,
firecrawl, fireworks_ai, friendliai, galadriel, gemini, gigachat, github,
github_copilot, google_pse, gradient_ai, groq, heroku, hosted_vllm, huggingface,
hyperbolic, infinity, jina_ai, lambda_ai, langgraph, lemonade, linkup, litellm_proxy,
llamafile, lm_studio, manus, maritalk.py, meta_llama, minimax, mistral, moonshot,
morph, nebius, nlp_cloud, novita, nscale, nvidia_nim, oci, ollama, oobabooga, openai,
openai_like, openrouter, ovhcloud, parallel_ai, pass_through, perplexity, petals,
predibase, ragflow, recraft, replicate, runwayml, sagemaker, sambanova, sap, snowflake,
stability, tavily, together_ai, topaz, triton, v0, vercel_ai_gateway, vertex_ai, vllm,
volcengine, voyage, wandb, watsonx, xai, xinference, zai
```

~80+ provider directories plus single-file providers like `maritalk.py`. Because ~20 providers just subclass `OpenAIGPTConfig`, the effective number of distinct transformation shapes is closer to 10–15.

---

## 13. Extraction recommendation — minimum viable set

```
EXTRACT (mandatory):
  llms/base_llm/                    (full)
  llms/custom_httpx/http_handler.py (AsyncHTTPHandler + HTTPHandler)
  llms/custom_httpx/llm_http_handler.py (BaseLLMHTTPHandler — trim to chat-only)
  llms/<provider>/chat/transformation.py  (per provider as needed)
  llms/<provider>/chat/handler.py         (per provider, for streaming iterator)
  llms/<provider>/common_utils.py         (per provider)
  llms/base.py                            (legacy BaseLLM used by some handlers)
  constants.py                            (trim)
  exceptions.py                           (trim to BaseLLMException hierarchy)
  _logging.py                             (verbose_logger singleton — lightweight)
  _uuid.py                                (uuid helper)
  litellm_core_utils/core_helpers.py
  litellm_core_utils/prompt_templates/factory.py
  litellm_core_utils/prompt_templates/common_utils.py
  litellm_core_utils/prompt_templates/image_handling.py
  litellm_core_utils/llm_response_utils/convert_dict_to_response.py
  litellm_core_utils/streaming_handler.py (CustomStreamWrapper — trim logging/cache)
  litellm_core_utils/get_llm_provider_logic.py
  litellm_core_utils/get_supported_openai_params.py
  litellm_core_utils/exception_mapping_utils.py
  types/utils.py
  types/llms/openai.py
  types/llms/anthropic.py
  types/llms/vertex_ai.py
  types/llms/bedrock.py
  types/llms/base.py

STUB / REPLACE:
  logging_obj           → NoopLogging
  litellm.drop_params   → config singleton bool
  litellm.modify_params → config singleton bool
  litellm.disable_add_prefix_to_prompt → config singleton bool
  ProviderConfigManager → pure data registry dict

LEAVE BEHIND:
  proxy/, router.py, router_utils/, router_strategy/, caching/, integrations/,
  cost_calculator.py, llm_cost_calc/, budget_manager.py,
  litellm_core_utils/litellm_logging.py,
  litellm_core_utils/logging_callback_manager.py,
  model_prices_and_context_window_backup.json
```

Raw LOC budget: the base abstractions are ~950 LOC; adding core_helpers + factory + convert_dict_to_response + streaming_handler + types + a handful of providers lands in the 25–40k LOC range. A truly minimal extraction (base + Anthropic + OpenAI + Gemini only) is achievable in ~15k LOC.

---

## 14. The `litellm_logging.py` entanglement — key caveat

`transform_response(…)` signature takes `logging_obj: Any` and calls `logging_obj.post_call(input, api_key, original_response, additional_args)` internally. `BaseLLMHTTPHandler.completion()` calls `pre_call()`, `async_success_handler()`, and a few others.

The real `Logging` class in `litellm_core_utils/litellm_logging.py` is ~3000 LOC of cost math, callbacks, caching, langfuse/datadog/arize integrations. We do not want any of it. The duck-typed stub from §9.2 is sufficient — every method is a no-op that returns `None` and exposes an empty `model_call_details` dict.

The only delicate spot: `streaming_handler.CustomStreamWrapper` reads `logging_obj.model_call_details` and occasionally writes to it. The stub provides this as an empty dict; the `CustomStreamWrapper` needs a pruning pass to remove cache-streaming, cost-tracking, and callback invocation paths.
