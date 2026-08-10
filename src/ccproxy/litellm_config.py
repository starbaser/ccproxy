"""LiteLLM ``config.yaml`` compatibility frontend.

The module parses LiteLLM's permissive deployment syntax and compiles the
supported subset into ccproxy-native model bindings and providers. It does not
import or execute LiteLLM.
"""

from __future__ import annotations

import os
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from ccproxy.auth.sources import EnvironmentAuthSource, LiteralAuthSource

if TYPE_CHECKING:
    from ccproxy.config import ModelBinding, Provider


class LiteLLMConfigError(ValueError):
    """A LiteLLM declaration requests semantics ccproxy cannot reproduce."""


class CompatibilityDiagnostic(BaseModel):
    model_config = ConfigDict(frozen=True)

    level: Literal["warning"] = "warning"
    path: str
    message: str


class LiteLLMDeployment(BaseModel):
    model_config = ConfigDict(extra="allow")

    model_name: str
    litellm_params: dict[str, Any]
    model_info: dict[str, Any] = Field(default_factory=dict)

    @field_validator("model_name")
    @classmethod
    def _validate_model_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("model_name must be a non-empty string")
        return value

    @field_validator("model_info", mode="before")
    @classmethod
    def _default_model_info(cls, value: Any) -> Any:
        return {} if value is None else value


class LiteLLMDocument(BaseModel):
    model_config = ConfigDict(extra="allow")

    model_list: list[LiteLLMDeployment] = Field(default_factory=list)
    litellm_settings: dict[str, Any] = Field(default_factory=dict)
    router_settings: dict[str, Any] = Field(default_factory=dict)
    general_settings: dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "model_list",
        "litellm_settings",
        "router_settings",
        "general_settings",
        mode="before",
    )
    @classmethod
    def _default_null_sections(cls, value: Any, info: Any) -> Any:
        if value is not None:
            return value
        return [] if info.field_name == "model_list" else {}


@dataclass(frozen=True)
class LiteLLMFrontend:
    document: LiteLLMDocument
    bindings: list[ModelBinding]
    deployment_providers: dict[str, Provider]
    diagnostics: list[CompatibilityDiagnostic]


_STRUCTURAL_KEYS = frozenset(
    {
        "model",
        "api_base",
        "base_url",
        "api_key",
        "custom_llm_provider",
        "extra_headers",
        "organization",
    }
)

_ROUTER_KEYS = frozenset(
    {
        "tpm",
        "rpm",
        "itpm",
        "otpm",
        "weight",
        "order",
        "max_parallel_requests",
        "timeout",
        "stream_timeout",
        "max_retries",
        "num_retries",
        "tags",
        "tag_regex",
        "max_budget",
        "budget_duration",
        "fallbacks",
        "context_window_fallbacks",
        "mock_response",
    }
)

_UNSUPPORTED_CONFIG_KEYS = frozenset(
    {
        "adaptive_router_args",
        "api_version",
        "auto_router_config_path",
        "auto_router_default_model",
        "aws_access_key_id",
        "aws_bedrock_runtime_endpoint",
        "aws_region_name",
        "aws_secret_access_key",
        "aws_session_name",
        "aws_session_token",
        "aws_web_identity_token",
        "complexity_router_config",
        "configurable_clientside_auth_params",
        "credential_name",
        "default_api_key",
        "default_vertex_config",
        "litellm_credential_name",
        "quality_router_config",
        "region_name",
        "use_chat_completions_api",
        "use_in_pass_through",
        "use_litellm_proxy",
        "use_xai_oauth",
        "vertex_credentials",
        "vertex_location",
        "vertex_project",
        "drop_params",
        "merge_reasoning_content_in_choices",
    }
)

_MODEL_INFO_KEYS = frozenset(
    {
        "input_cost_per_token",
        "output_cost_per_token",
        "input_cost_per_character",
        "output_cost_per_character",
        "cache_read_input_token_cost",
        "cache_creation_input_token_cost",
        "mode",
        "supports_system_message",
    }
)

_OPENAI_COMPATIBLE_PROVIDERS = frozenset(
    {
        "fireworks_ai",
        "groq",
        "hosted_vllm",
        "lm_studio",
        "mistral",
        "ollama",
        "ollama_chat",
        "openai",
        "openrouter",
        "perplexity",
        "requesty",
        "together_ai",
        "vllm",
        "xai",
    }
)

_NATIVE_TYPES = frozenset(
    {
        "anthropic",
        "deepseek",
        "gemini",
        "google",
        "minimax",
        "openai",
        "openai_conversations",
        "openai_responses",
        "perplexity_pro",
        "vertex_ai",
        "vertex_ai_beta",
        "zai",
    }
)

_ANTHROPIC_TYPES = frozenset({"anthropic", "deepseek", "minimax", "zai"})
_GOOGLE_TYPES = frozenset({"gemini", "google", "vertex_ai", "vertex_ai_beta"})


def _load_yaml(path: Path, stack: tuple[Path, ...] = ()) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if resolved in stack:
        chain = " -> ".join(str(item) for item in (*stack, resolved))
        raise LiteLLMConfigError(f"cyclic LiteLLM include: {chain}")
    if not resolved.is_file():
        raise LiteLLMConfigError(f"LiteLLM config file not found: {resolved}")
    raw = yaml.safe_load(resolved.read_text())
    if raw is None:
        raise LiteLLMConfigError(f"LiteLLM config cannot be empty: {resolved}")
    if not isinstance(raw, dict):
        raise LiteLLMConfigError(f"LiteLLM config must be a mapping: {resolved}")

    includes = raw.get("include")
    if includes is None:
        return raw
    if not isinstance(includes, list) or not all(isinstance(item, str) for item in includes):
        raise LiteLLMConfigError(f"{resolved}: include must be a list of file paths")

    merged = deepcopy(raw)
    for include in includes:
        included = _load_yaml(resolved.parent / include, (*stack, resolved))
        for key, value in included.items():
            if isinstance(value, list) and key in merged:
                current = merged[key]
                if not isinstance(current, list):
                    raise LiteLLMConfigError(f"{resolved}: cannot extend non-list key {key!r} from include {include!r}")
                current.extend(value)
            else:
                merged[key] = value
    del merged["include"]
    return merged


def _resolve_environment(value: Any, *, path: str = "") -> Any:
    if isinstance(value, dict):
        return {
            key: (
                child
                if key == "model_info"
                or (key == "api_key" and isinstance(child, str) and child.startswith("os.environ/"))
                else _resolve_environment(child, path=f"{path}.{key}" if path else key)
            )
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_resolve_environment(child, path=f"{path}[{index}]") for index, child in enumerate(value)]
    if isinstance(value, str) and value.startswith("os.environ/"):
        variable = value.removeprefix("os.environ/")
        if not variable:
            raise LiteLLMConfigError(f"{path}: empty os.environ reference")
        if variable not in os.environ:
            raise LiteLLMConfigError(f"{path}: environment variable {variable!r} is not set")
        return os.environ[variable]
    return value


def _adapter_type(source_provider: str) -> str:
    if source_provider in _NATIVE_TYPES:
        return source_provider
    if source_provider in _OPENAI_COMPATIBLE_PROVIDERS:
        return "openai"
    raise LiteLLMConfigError(
        f"unsupported LiteLLM provider {source_provider!r}; set custom_llm_provider to a ccproxy-supported dialect"
    )


def _native_provider(
    providers: dict[str, Any],
    *,
    source_provider: str,
    adapter_type: str,
) -> tuple[str, Any] | None:
    def compatible(provider_type: str) -> bool:
        if adapter_type in _ANTHROPIC_TYPES:
            return provider_type in _ANTHROPIC_TYPES
        if adapter_type in _GOOGLE_TYPES:
            return provider_type in _GOOGLE_TYPES
        return provider_type == adapter_type

    for name in (source_provider, adapter_type):
        if name in providers:
            provider = providers[name]
            if not compatible(provider.type):
                raise LiteLLMConfigError(
                    f"ccproxy provider {name!r} uses adapter {provider.type!r}, "
                    f"which is incompatible with LiteLLM provider {source_provider!r}"
                )
            return name, provider
    if source_provider != adapter_type:
        return None
    candidates = [(name, provider) for name, provider in providers.items() if provider.type == adapter_type]
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        names = ", ".join(name for name, _ in candidates)
        raise LiteLLMConfigError(f"provider {source_provider!r} is ambiguous across ccproxy providers: {names}")
    return None


def _endpoint_path(api_base: str, adapter_type: str) -> str:
    path = urlsplit(api_base).path.rstrip("/")
    if adapter_type in _ANTHROPIC_TYPES:
        if path.endswith("/messages"):
            return ""
        return "/messages" if path.endswith("/v1") else "/v1/messages"
    if adapter_type == "openai":
        if path.endswith("/chat/completions"):
            return ""
        return "/chat/completions" if path.endswith("/v1") else "/v1/chat/completions"
    if adapter_type == "openai_responses":
        if path.endswith("/responses"):
            return ""
        return "/responses" if path.endswith("/v1") else "/v1/responses"
    if adapter_type in {"gemini", "google", "vertex_ai", "vertex_ai_beta"}:
        if path.endswith(("/v1alpha", "/v1beta")) or "/publishers/" in path:
            return "/models/{model}:{action}"
        return "/v1beta/models/{model}:{action}"
    raise LiteLLMConfigError(
        f"api_base cannot infer an endpoint path for provider type {adapter_type!r}; use a native ccproxy provider"
    )


def _auth_source(raw: Any, *, source_provider: str, adapter_type: str) -> Any:
    if raw is None:
        return None
    placement: dict[str, str]
    if adapter_type in _ANTHROPIC_TYPES:
        placement = {"header": "x-api-key"}
    elif adapter_type in {"gemini", "google"}:
        placement = {"query_param": "key"}
    else:
        placement = {}

    if isinstance(raw, str) and raw.startswith("os.environ/"):
        variable = raw.removeprefix("os.environ/")
        if not variable:
            raise LiteLLMConfigError("api_key contains an empty os.environ reference")
        return EnvironmentAuthSource(
            variable=variable,
            header=placement.get("header"),
            query_param=placement.get("query_param"),
        )
    if isinstance(raw, str):
        return LiteralAuthSource(
            value=SecretStr(raw),
            header=placement.get("header"),
            query_param=placement.get("query_param"),
        )
    raise LiteLLMConfigError("litellm_params.api_key must be a string or os.environ/NAME reference")


def _is_active(value: Any) -> bool:
    return value not in (None, False, 0, "", [], {})


def _diagnose_document(document: LiteLLMDocument) -> list[CompatibilityDiagnostic]:
    diagnostics: list[CompatibilityDiagnostic] = []
    for key in document.model_extra or {}:
        diagnostics.append(
            CompatibilityDiagnostic(
                path=key,
                message="Unrecognized LiteLLM top-level setting is preserved but has no ccproxy runtime effect",
            )
        )
    for key in document.litellm_settings:
        diagnostics.append(
            CompatibilityDiagnostic(
                path=f"litellm_settings.{key}",
                message="LiteLLM runtime setting is not needed by ccproxy's native runtime",
            )
        )

    for key, value in document.router_settings.items():
        if _is_active(value):
            raise LiteLLMConfigError(
                f"router_settings.{key} requires LiteLLM router behavior that ccproxy does not restore"
            )
        diagnostics.append(
            CompatibilityDiagnostic(
                path=f"router_settings.{key}",
                message="LiteLLM router setting is inert because ccproxy has no LiteLLM runtime router",
            )
        )

    for key in document.general_settings:
        diagnostics.append(
            CompatibilityDiagnostic(
                path=f"general_settings.{key}",
                message="LiteLLM proxy-management setting is not applicable to ccproxy",
            )
        )
    return diagnostics


def load_litellm_config(path: Path, providers: dict[str, Any]) -> LiteLLMFrontend:
    from ccproxy.config import ModelBinding, Provider

    raw = _resolve_environment(_load_yaml(path))
    document = LiteLLMDocument.model_validate(raw)
    diagnostics = _diagnose_document(document)

    by_name: dict[str, tuple[int, LiteLLMDeployment]] = {}
    for index, deployment in enumerate(document.model_list):
        for key, value in (deployment.model_extra or {}).items():
            if _is_active(value):
                raise LiteLLMConfigError(f"model_list[{index}].{key} requests unsupported deployment-level behavior")
            diagnostics.append(
                CompatibilityDiagnostic(
                    path=f"model_list[{index}].{key}",
                    message="zero/empty deployment-level setting is inert in ccproxy",
                )
            )
        if deployment.model_name in by_name:
            first = by_name[deployment.model_name][0]
            raise LiteLLMConfigError(
                f"model_list[{index}].model_name duplicates model_list[{first}]; "
                "multiple deployments require LiteLLM router selection"
            )
        by_name[deployment.model_name] = (index, deployment)

    deployment_providers: dict[str, Provider] = {}
    bindings: list[ModelBinding] = []

    def resolve_params(index: int, deployment: LiteLLMDeployment, stack: tuple[str, ...] = ()) -> dict[str, Any]:
        params = deepcopy(deployment.litellm_params)
        model = params.get("model")
        if not isinstance(model, str) or not model:
            raise LiteLLMConfigError(f"model_list[{index}].litellm_params.model must be a non-empty string")
        if "/" not in model and model in by_name:
            if model in stack:
                chain = " -> ".join((*stack, model))
                raise LiteLLMConfigError(f"cyclic model alias: {chain}")
            target_index, target = by_name[model]
            inherited = resolve_params(target_index, target, (*stack, deployment.model_name))
            inherited.update({key: value for key, value in params.items() if key != "model"})
            return inherited
        return params

    for index, deployment in enumerate(document.model_list):
        params = resolve_params(index, deployment)
        raw_model = params["model"]
        custom_provider = params.get("custom_llm_provider")
        if custom_provider is not None and not isinstance(custom_provider, str):
            raise LiteLLMConfigError(f"model_list[{index}].litellm_params.custom_llm_provider must be a string")
        if "/" in raw_model:
            model_provider, upstream_model = raw_model.split("/", 1)
        else:
            model_provider, upstream_model = "", raw_model
        source_provider = custom_provider or model_provider
        if not source_provider:
            raise LiteLLMConfigError(
                f"model_list[{index}].litellm_params.model={raw_model!r} has no provider prefix or resolvable alias"
            )
        has_explicit_endpoint = params.get("api_base") is not None or params.get("base_url") is not None
        configured_provider = providers.get(source_provider) if not has_explicit_endpoint else None
        # LiteLLM's DeepSeek provider is OpenAI-compatible. ccproxy also has an
        # intentionally Anthropic-compatible native DeepSeek service; retain
        # that only when a declaration inherits the native endpoint.
        if source_provider == "minimax" and configured_provider is not None and configured_provider.type == "openai":
            adapter_type = "openai"
        else:
            adapter_type = (
                "openai" if source_provider == "deepseek" and has_explicit_endpoint else _adapter_type(source_provider)
            )
        inherited = (
            None
            if has_explicit_endpoint
            else _native_provider(
                providers,
                source_provider=source_provider,
                adapter_type=adapter_type,
            )
        )

        for key in _ROUTER_KEYS:
            if key in params and _is_active(params[key]):
                raise LiteLLMConfigError(f"model_list[{index}].litellm_params.{key} requires LiteLLM router behavior")
            if key in params:
                diagnostics.append(
                    CompatibilityDiagnostic(
                        path=f"model_list[{index}].litellm_params.{key}",
                        message="zero/empty LiteLLM router value is inert in ccproxy",
                    )
                )

        for key in _UNSUPPORTED_CONFIG_KEYS:
            if key in params and _is_active(params[key]):
                raise LiteLLMConfigError(
                    f"model_list[{index}].litellm_params.{key} requests provider or proxy "
                    "infrastructure that ccproxy cannot derive from a model deployment"
                )
            if key in params:
                diagnostics.append(
                    CompatibilityDiagnostic(
                        path=f"model_list[{index}].litellm_params.{key}",
                        message="zero/empty LiteLLM infrastructure value is inert in ccproxy",
                    )
                )

        if deployment.model_info.get("blocked") is True:
            raise LiteLLMConfigError(f"model_list[{index}].model_info.blocked requests LiteLLM deployment disabling")

        api_base = params.get("api_base")
        base_url_alias = params.get("base_url")
        if api_base is not None and not isinstance(api_base, str):
            raise LiteLLMConfigError(f"model_list[{index}].litellm_params.api_base must be a string")
        if base_url_alias is not None and not isinstance(base_url_alias, str):
            raise LiteLLMConfigError(f"model_list[{index}].litellm_params.base_url must be a string")
        if api_base is not None and base_url_alias is not None and api_base != base_url_alias:
            raise LiteLLMConfigError(
                f"model_list[{index}].litellm_params.api_base and base_url must match when both are set"
            )
        api_base = api_base or base_url_alias
        if api_base is None:
            if inherited is None:
                raise LiteLLMConfigError(
                    f"model_list[{index}] must set api_base because no native ccproxy provider "
                    f"matches {source_provider!r}"
                )
            inherited_name, inherited_provider = inherited
            base_url = inherited_provider.base_url
            endpoint_path = inherited_provider.path
        else:
            inherited_name, inherited_provider = inherited or ("", None)
            base_url = api_base.rstrip("/")
            endpoint_path = _endpoint_path(base_url, adapter_type)

        explicit_auth = _auth_source(
            params.get("api_key"),
            source_provider=source_provider,
            adapter_type=adapter_type,
        )
        inherit_destination = inherited_provider is not None and not has_explicit_endpoint
        auth = explicit_auth or (inherited_provider.auth if inherit_destination else None)
        if (
            explicit_auth is not None
            and api_base is None
            and inherited_provider is not None
            and inherited_provider.path.startswith("/v1internal")
        ):
            raise LiteLLMConfigError(
                f"model_list[{index}] must set api_base when api_key overrides a native Gemini OAuth deployment"
            )
        headers = dict(inherited_provider.headers) if inherit_destination else {}
        query = dict(inherited_provider.query) if inherit_destination else {}
        extra_headers = params.get("extra_headers")
        if extra_headers is not None:
            if not isinstance(extra_headers, dict) or not all(
                isinstance(key, str) and isinstance(value, str) for key, value in extra_headers.items()
            ):
                raise LiteLLMConfigError(f"model_list[{index}].litellm_params.extra_headers must be a string mapping")
            headers.update(extra_headers)
        organization = params.get("organization")
        if isinstance(organization, str) and organization:
            if adapter_type not in {"openai", "openai_responses", "openai_conversations"}:
                raise LiteLLMConfigError(
                    f"model_list[{index}].litellm_params.organization is only supported for OpenAI dialects"
                )
            headers["openai-organization"] = organization
        elif organization is not None:
            raise LiteLLMConfigError(f"model_list[{index}].litellm_params.organization must be a string")
        if has_explicit_endpoint and adapter_type in _ANTHROPIC_TYPES:
            headers.setdefault("anthropic-version", "2023-06-01")
        effective = Provider(
            base_url=base_url,
            path=endpoint_path,
            type=adapter_type,
            auth=auth,
            headers=headers,
            query=query,
            fingerprint_profile=(inherited_provider.fingerprint_profile if inherit_destination else None),
        )
        if inherited_provider is not None and effective == inherited_provider:
            provider_name = inherited_name
            effective = inherited_provider
        else:
            provider_name = f"litellm:{index}:{deployment.model_name}"
            deployment_providers[provider_name] = effective

        model_info = deepcopy(deployment.model_info)
        for key in _MODEL_INFO_KEYS:
            if key in params:
                model_info.setdefault(key, params[key])

        defaults = {
            key: value
            for key, value in params.items()
            if key not in _STRUCTURAL_KEYS
            and key not in _ROUTER_KEYS
            and key not in _UNSUPPORTED_CONFIG_KEYS
            and key not in _MODEL_INFO_KEYS
        }
        bindings.append(
            ModelBinding.create(
                model_name=deployment.model_name,
                upstream_model=upstream_model,
                owned_by=source_provider,
                provider_name=provider_name,
                provider=effective,
                request_defaults=defaults,
                model_info=model_info,
                source_index=index,
            )
        )

    return LiteLLMFrontend(
        document=document,
        bindings=bindings,
        deployment_providers=deployment_providers,
        diagnostics=diagnostics,
    )
