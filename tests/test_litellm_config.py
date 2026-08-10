from __future__ import annotations

from pathlib import Path

import pytest

from ccproxy.auth.sources import EnvironmentAuthSource
from ccproxy.config import CCProxyConfig, Provider, clear_config_instance, get_config
from ccproxy.litellm_config import LiteLLMConfigError, load_litellm_config


def _provider(
    *,
    base_url: str = "https://api.anthropic.com",
    path: str = "/v1/messages",
    type: str = "anthropic",
) -> Provider:
    return Provider(base_url=base_url, path=path, type=type)


def _write(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


def test_compiles_native_provider_alias_and_request_defaults(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "config.yaml",
        """
model_list:
  - model_name: claude
    litellm_params:
      model: anthropic/claude-sonnet-4-6
      temperature: 0.25
""",
    )

    frontend = load_litellm_config(path, {"anthropic": _provider()})

    assert len(frontend.bindings) == 1
    binding = frontend.bindings[0]
    assert binding.model_name == "claude"
    assert binding.upstream_model == "claude-sonnet-4-6"
    assert binding.provider_name == "anthropic"
    assert binding.provider is not None
    assert binding.request_defaults == {"temperature": 0.25}
    assert binding.apply_defaults({"model": "claude", "temperature": 0.8})["temperature"] == 0.8


def test_resolves_bare_model_alias_chain(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "config.yaml",
        """
model_list:
  - model_name: default
    litellm_params:
      model: claude
  - model_name: claude
    litellm_params:
      model: anthropic/claude-sonnet-4-6
      api_base: https://api.anthropic.com
""",
    )

    frontend = load_litellm_config(path, {"anthropic": _provider()})

    assert [binding.model_name for binding in frontend.bindings] == ["default", "claude"]
    assert frontend.bindings[0].upstream_model == "claude-sonnet-4-6"


def test_resolves_multi_hop_alias_chain_without_losing_terminal_model(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "config.yaml",
        """
model_list:
  - model_name: default
    litellm_params:
      model: fast
      temperature: 0.2
  - model_name: fast
    litellm_params:
      model: claude
      max_tokens: 1024
  - model_name: claude
    litellm_params:
      model: anthropic/claude-sonnet-4-6
""",
    )

    frontend = load_litellm_config(path, {"anthropic": _provider()})

    default = frontend.bindings[0]
    assert default.upstream_model == "claude-sonnet-4-6"
    assert default.request_defaults == {"max_tokens": 1024, "temperature": 0.2}


def test_rejects_cyclic_model_aliases(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "config.yaml",
        """
model_list:
  - model_name: first
    litellm_params:
      model: second
  - model_name: second
    litellm_params:
      model: first
""",
    )

    with pytest.raises(LiteLLMConfigError, match="cyclic model alias"):
        load_litellm_config(path, {})


def test_compiles_wildcard_model_mapping(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "config.yaml",
        """
model_list:
  - model_name: local/*
    litellm_params:
      model: openai/*
      api_base: http://127.0.0.1:11434/v1
""",
    )

    frontend = load_litellm_config(path, {})
    binding = frontend.bindings[0]

    assert binding.matches("local/qwen3")
    assert not binding.matches("remote/qwen3")
    assert binding.resolve_model("local/qwen3") == "qwen3"
    assert binding.provider.base_url == "http://127.0.0.1:11434/v1"
    assert binding.provider.path == "/chat/completions"


def test_upstream_wildcard_requires_public_wildcard(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "config.yaml",
        """
model_list:
  - model_name: local
    litellm_params:
      model: openai/*
      api_base: http://127.0.0.1:11434/v1
""",
    )

    with pytest.raises(ValueError, match="wildcard requires a wildcard model_name"):
        load_litellm_config(path, {})


def test_base_url_alias_selects_destination_instead_of_becoming_request_default(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "config.yaml",
        """
model_list:
  - model_name: local
    litellm_params:
      model: openai/qwen3
      base_url: http://127.0.0.1:11434/v1
      temperature: 0.25
""",
    )

    frontend = load_litellm_config(path, {})
    binding = frontend.bindings[0]

    assert binding.provider.base_url == "http://127.0.0.1:11434/v1"
    assert binding.provider.path == "/chat/completions"
    assert binding.request_defaults == {"temperature": 0.25}


def test_equal_api_base_and_base_url_are_accepted(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "config.yaml",
        """
model_list:
  - model_name: local
    litellm_params:
      model: openai/qwen3
      api_base: http://127.0.0.1:11434/v1
      base_url: http://127.0.0.1:11434/v1
""",
    )

    frontend = load_litellm_config(path, {})

    assert frontend.bindings[0].provider.base_url == "http://127.0.0.1:11434/v1"
    assert frontend.bindings[0].request_defaults == {}


def test_conflicting_api_base_and_base_url_are_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "config.yaml",
        """
model_list:
  - model_name: local
    litellm_params:
      model: openai/qwen3
      api_base: http://one.test/v1
      base_url: http://two.test/v1
""",
    )

    with pytest.raises(LiteLLMConfigError, match="api_base and base_url must match"):
        load_litellm_config(path, {})


def test_compiles_extra_headers_as_destination_configuration(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "config.yaml",
        """
model_list:
  - model_name: local
    litellm_params:
      model: openai/qwen3
      api_base: http://127.0.0.1:11434/v1
      extra_headers:
        x-tenant: alpha
""",
    )

    frontend = load_litellm_config(path, {})
    binding = frontend.bindings[0]

    assert binding.provider.headers == {"x-tenant": "alpha"}
    assert "extra_headers" not in binding.request_defaults


def test_explicit_endpoint_does_not_inherit_native_secrets_or_destination_metadata(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "config.yaml",
        """
model_list:
  - model_name: hosted
    litellm_params:
      model: anthropic/claude-sonnet
      api_base: https://third-party.example/v1
""",
    )
    native = Provider(
        auth=EnvironmentAuthSource(variable="ANTHROPIC_API_KEY"),
        base_url="https://api.anthropic.com",
        path="/v1/messages",
        type="anthropic",
        headers={"x-private-tenant": "native"},
        query={"private": "native"},
        fingerprint_profile="anthropic",
    )

    binding = load_litellm_config(path, {"anthropic": native}).bindings[0]

    assert binding.provider.base_url == "https://third-party.example/v1"
    assert binding.provider.auth is None
    assert binding.provider.query == {}
    assert "x-private-tenant" not in binding.provider.headers
    assert binding.provider.headers["anthropic-version"] == "2023-06-01"
    assert binding.provider.fingerprint_profile is None


def test_explicit_deepseek_endpoint_uses_litellm_openai_dialect(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "config.yaml",
        """
model_list:
  - model_name: deepseek
    litellm_params:
      model: deepseek/deepseek-chat
      api_base: https://api.deepseek.com/v1
      api_key: test-key
""",
    )

    binding = load_litellm_config(path, {}).bindings[0]

    assert binding.provider.type == "openai"
    assert binding.provider.path == "/chat/completions"
    assert binding.provider.auth is not None
    assert binding.provider.auth.header is None


@pytest.mark.parametrize(
    ("base_url", "provider_path", "provider_type", "expected_binding_type"),
    [
        ("https://api.minimax.io/anthropic", "/v1/messages", "anthropic", "minimax"),
        ("https://api.minimaxi.com/anthropic", "/v1/messages", "anthropic", "minimax"),
        ("https://api.minimax.io/v1", "/chat/completions", "openai", "openai"),
        ("https://api.minimaxi.com/v1", "/chat/completions", "openai", "openai"),
    ],
)
def test_compiles_native_minimax_provider_binding(
    tmp_path: Path,
    base_url: str,
    provider_path: str,
    provider_type: str,
    expected_binding_type: str,
) -> None:
    path = _write(
        tmp_path / "config.yaml",
        """
model_list:
  - model_name: MiniMax-M3
    litellm_params:
      model: minimax/MiniMax-M3
""",
    )
    provider = Provider(
        base_url=base_url,
        path=provider_path,
        type=provider_type,
    )

    binding = load_litellm_config(path, {"minimax": provider}).bindings[0]

    assert binding.owned_by == "minimax"
    assert binding.provider.type == expected_binding_type
    assert binding.provider.base_url == base_url
    assert binding.provider.path == provider_path


def test_explicit_minimax_base_url_uses_anthropic_messages_path(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "config.yaml",
        """
model_list:
  - model_name: MiniMax-M3
    litellm_params:
      model: minimax/MiniMax-M3
      api_base: https://api.minimax.io/anthropic
      api_key: test-key
""",
    )

    binding = load_litellm_config(path, {}).bindings[0]

    assert binding.provider.type == "minimax"
    assert binding.provider.base_url == "https://api.minimax.io/anthropic"
    assert binding.provider.path == "/v1/messages"
    assert binding.provider.auth is not None
    assert binding.provider.auth.header == "x-api-key"


def test_pricing_fields_move_to_model_info_not_request_defaults(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "config.yaml",
        """
model_list:
  - model_name: local
    litellm_params:
      model: openai/qwen3
      api_base: http://127.0.0.1:11434/v1
      temperature: 0.25
      input_cost_per_token: 0.000001
      cache_read_input_token_cost: 0.0000001
""",
    )

    binding = load_litellm_config(path, {}).bindings[0]

    assert binding.request_defaults == {"temperature": 0.25}
    assert binding.model_info["input_cost_per_token"] == 0.000001
    assert binding.model_info["cache_read_input_token_cost"] == 0.0000001


def test_model_info_environment_reference_is_not_expanded_for_catalog_exposure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SENSITIVE_VALUE", "do-not-expose")
    path = _write(
        tmp_path / "config.yaml",
        """
model_list:
  - model_name: local
    litellm_params:
      model: openai/qwen3
      api_base: http://127.0.0.1:11434/v1
    model_info:
      internal_value: os.environ/SENSITIVE_VALUE
""",
    )

    binding = load_litellm_config(path, {}).bindings[0]

    assert binding.model_info["internal_value"] == "os.environ/SENSITIVE_VALUE"


def test_explicit_gemini_version_base_does_not_duplicate_version_path(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "config.yaml",
        """
model_list:
  - model_name: gemini
    litellm_params:
      model: gemini/gemini-2.5-pro
      api_base: https://generativelanguage.googleapis.com/v1beta
""",
    )

    frontend = load_litellm_config(path, {})

    assert frontend.bindings[0].provider.path == "/models/{model}:{action}"


def test_azure_provider_is_rejected_instead_of_misrouted_as_openai(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "config.yaml",
        """
model_list:
  - model_name: azure
    litellm_params:
      model: azure/gpt-5
      api_base: https://example.openai.azure.com
""",
    )

    with pytest.raises(LiteLLMConfigError, match="unsupported LiteLLM provider 'azure'"):
        load_litellm_config(path, {})


def test_provider_dialect_does_not_inherit_unrelated_native_endpoint(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "config.yaml",
        """
model_list:
  - model_name: groq
    litellm_params:
      model: groq/llama-3.3-70b-versatile
""",
    )
    providers = {
        "local": _provider(
            base_url="http://127.0.0.1:8000/v1",
            path="/chat/completions",
            type="openai",
        )
    }

    with pytest.raises(LiteLLMConfigError, match="must set api_base"):
        load_litellm_config(path, providers)


def test_preserves_environment_api_key_as_runtime_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCAL_API_KEY", "secret")
    path = _write(
        tmp_path / "config.yaml",
        """
model_list:
  - model_name: local
    litellm_params:
      model: openai/qwen3
      api_base: http://127.0.0.1:8000/v1
      api_key: os.environ/LOCAL_API_KEY
""",
    )

    frontend = load_litellm_config(path, {})
    auth = frontend.bindings[0].provider.auth

    assert isinstance(auth, EnvironmentAuthSource)
    assert auth.variable == "LOCAL_API_KEY"
    assert auth.resolve() == "secret"


def test_recursively_resolves_environment_values_outside_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCAL_BASE", "http://127.0.0.1:8000/v1")
    path = _write(
        tmp_path / "config.yaml",
        """
model_list:
  - model_name: local
    litellm_params:
      model: openai/qwen3
      api_base: os.environ/LOCAL_BASE
""",
    )

    frontend = load_litellm_config(path, {})

    assert frontend.bindings[0].provider.base_url == "http://127.0.0.1:8000/v1"


def test_include_extends_model_list(tmp_path: Path) -> None:
    _write(
        tmp_path / "models.yaml",
        """
model_list:
  - model_name: included
    litellm_params:
      model: openai/included-model
      api_base: http://127.0.0.1:8000/v1
""",
    )
    path = _write(
        tmp_path / "config.yaml",
        """
include:
  - models.yaml
model_list:
  - model_name: main
    litellm_params:
      model: openai/main-model
      api_base: http://127.0.0.1:8000/v1
""",
    )

    frontend = load_litellm_config(path, {})

    assert [binding.model_name for binding in frontend.bindings] == ["main", "included"]


def test_duplicate_model_name_rejects_router_owned_semantics(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "config.yaml",
        """
model_list:
  - model_name: shared
    litellm_params:
      model: openai/one
      api_base: http://one.test/v1
  - model_name: shared
    litellm_params:
      model: openai/two
      api_base: http://two.test/v1
""",
    )

    with pytest.raises(LiteLLMConfigError, match="multiple deployments require LiteLLM router selection"):
        load_litellm_config(path, {})


def test_active_deployment_router_parameter_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "config.yaml",
        """
model_list:
  - model_name: shared
    litellm_params:
      model: openai/one
      api_base: http://one.test/v1
      rpm: 60
""",
    )

    with pytest.raises(LiteLLMConfigError, match=r"litellm_params\.rpm requires LiteLLM router behavior"):
        load_litellm_config(path, {})


def test_active_provider_infrastructure_parameter_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "config.yaml",
        """
model_list:
  - model_name: vertex
    litellm_params:
      model: vertex_ai/gemini-2.5-pro
      api_base: https://aiplatform.googleapis.com
      vertex_project: project-id
""",
    )

    with pytest.raises(LiteLLMConfigError, match=r"litellm_params\.vertex_project requests"):
        load_litellm_config(path, {})


def test_any_active_router_setting_is_rejected_without_a_router_key_allowlist(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "config.yaml",
        """
model_list: []
router_settings:
  future_router_feature: enabled
""",
    )

    with pytest.raises(LiteLLMConfigError, match=r"router_settings\.future_router_feature requires"):
        load_litellm_config(path, {})


def test_gemini_api_key_requires_explicit_public_api_base(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "config.yaml",
        """
model_list:
  - model_name: gemini
    litellm_params:
      model: gemini/gemini-2.5-pro
      api_key: literal-key
""",
    )
    providers = {
        "gemini": _provider(
            base_url="https://cloudcode-pa.googleapis.com",
            path="/v1internal:{action}",
            type="gemini",
        )
    }

    with pytest.raises(LiteLLMConfigError, match="must set api_base"):
        load_litellm_config(path, providers)


def test_native_provider_adapter_mismatch_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "config.yaml",
        """
model_list:
  - model_name: claude
    litellm_params:
      model: anthropic/claude-sonnet-4-6
""",
    )
    providers = {
        "anthropic": _provider(
            base_url="https://wrong.example/v1",
            path="/chat/completions",
            type="openai",
        )
    }

    with pytest.raises(LiteLLMConfigError, match="incompatible"):
        load_litellm_config(path, providers)


def test_blocked_deployment_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "config.yaml",
        """
model_list:
  - model_name: disabled
    litellm_params:
      model: openai/qwen3
      api_base: http://127.0.0.1:8000/v1
    model_info:
      blocked: true
""",
    )

    with pytest.raises(LiteLLMConfigError, match=r"model_info\.blocked"):
        load_litellm_config(path, {})


def test_zero_router_settings_become_explicit_diagnostics(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "config.yaml",
        """
model_list: []
litellm_settings:
  force_stream: true
router_settings:
  retry_after: 0
general_settings:
  disable_spend_logs: true
""",
    )

    frontend = load_litellm_config(path, {})

    assert [diagnostic.path for diagnostic in frontend.diagnostics] == [
        "litellm_settings.force_stream",
        "router_settings.retry_after",
        "general_settings.disable_spend_logs",
    ]


def test_unknown_top_level_setting_becomes_explicit_diagnostic(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "config.yaml",
        """
model_list: []
custom_management_surface: true
""",
    )

    frontend = load_litellm_config(path, {})

    assert [diagnostic.path for diagnostic in frontend.diagnostics] == ["custom_management_surface"]


def test_null_optional_sections_match_litellm_defaults(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "config.yaml",
        """
model_list:
litellm_settings:
router_settings:
general_settings:
""",
    )

    frontend = load_litellm_config(path, {})

    assert frontend.bindings == []
    assert frontend.diagnostics == []


def test_active_unknown_deployment_field_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "config.yaml",
        """
model_list:
  - model_name: local
    hidden_router_policy: enabled
    litellm_params:
      model: openai/qwen3
      api_base: http://127.0.0.1:8000/v1
""",
    )

    with pytest.raises(LiteLLMConfigError, match="unsupported deployment-level behavior"):
        load_litellm_config(path, {})


def test_active_router_fallback_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "config.yaml",
        """
model_list: []
router_settings:
  fallbacks:
    - primary: [secondary]
""",
    )

    with pytest.raises(LiteLLMConfigError, match="fallbacks requires LiteLLM router behavior"):
        load_litellm_config(path, {})


def test_ccproxy_config_service_loads_sibling_litellm_config(tmp_path: Path) -> None:
    ccproxy_path = _write(
        tmp_path / "ccproxy.yaml",
        """
ccproxy:
  providers:
    anthropic:
      base_url: https://api.anthropic.com
      path: /v1/messages
      type: anthropic
""",
    )
    _write(
        tmp_path / "config.yaml",
        """
model_list:
  - model_name: claude
    litellm_params:
      model: anthropic/claude-sonnet-4-6
""",
    )

    config = CCProxyConfig.from_yaml(ccproxy_path)

    assert config.litellm_config_path == tmp_path / "config.yaml"
    assert [binding.model_name for binding in config.model_bindings] == ["claude"]
    assert config.model_bindings[0].provider is config.providers["anthropic"]


def test_config_yaml_can_be_the_only_configuration_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(
        tmp_path / "config.yaml",
        """
model_list:
  - model_name: local
    litellm_params:
      model: openai/qwen3
      api_base: http://127.0.0.1:8000/v1
""",
    )
    monkeypatch.setenv("CCPROXY_CONFIG_DIR", str(tmp_path))
    clear_config_instance()

    try:
        config = get_config()
        assert [binding.model_name for binding in config.model_bindings] == ["local"]
        assert config.ccproxy_config_path == tmp_path / "ccproxy.yaml"
    finally:
        clear_config_instance()
