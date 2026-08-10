"""Tests for the configuration-derived model catalog."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import httpx
import pytest
from mitmproxy.http import Response

from ccproxy.config import CCProxyConfig, Provider, set_config_instance
from ccproxy.constants import AuthConfigError
from ccproxy.inspector.router import InspectorRouter
from ccproxy.inspector.routes.models import register_models_routes
from ccproxy.litellm_config import load_litellm_config
from ccproxy.specs.model_catalog import build_catalog


def _configure(tmp_path: Path, text: str, providers: dict[str, Provider] | None = None) -> CCProxyConfig:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(text)
    config = CCProxyConfig(providers=providers or {})
    frontend = load_litellm_config(config_path, config.providers)
    config.model_bindings = frontend.bindings
    config.deployment_providers = frontend.deployment_providers
    config.litellm_diagnostics = frontend.diagnostics
    set_config_instance(config)
    return config


def test_empty_config_has_no_fabricated_models() -> None:
    set_config_instance(CCProxyConfig())

    assert build_catalog() == {"object": "list", "data": []}


def test_packaged_minimax_entries_include_model_metadata() -> None:
    from importlib.resources import as_file, files

    with as_file(files("ccproxy.templates").joinpath("ccproxy.yaml")) as template_path:
        set_config_instance(CCProxyConfig.from_yaml(Path(template_path)))

    entries = {entry["id"]: entry for entry in build_catalog()["data"]}

    assert entries["MiniMax-M3"]["model_info"]["context_window"] == 1000000
    assert entries["MiniMax-M3"]["model_info"]["input_modalities"] == ["text", "image", "video"]
    assert entries["MiniMax-M3"]["model_info"]["pricing_usd_per_million_tokens"] == {
        "cache_read": 0.12,
        "cache_write": None,
        "input": 0.6,
        "output": 2.4,
    }
    assert entries["MiniMax-M3"]["model_info"]["thinking"] == ["adaptive", "disabled"]
    assert entries["MiniMax-M2.7"]["model_info"]["context_window"] == 204800
    assert entries["MiniMax-M2.7"]["model_info"]["thinking"] == ["always_on"]


def test_concrete_aliases_are_the_offline_catalog(tmp_path: Path) -> None:
    _configure(
        tmp_path,
        """
model_list:
  - model_name: claude
    litellm_params:
      model: anthropic/claude-sonnet-4-6
      api_base: https://api.anthropic.com
    model_info:
      tier: paid
""",
    )

    catalog = build_catalog()

    assert catalog["object"] == "list"
    assert len(catalog["data"]) == 1
    assert catalog["data"][0]["id"] == "claude"
    assert catalog["data"][0]["owned_by"] == "anthropic"
    assert catalog["data"][0]["model_info"] == {"tier": "paid"}


def test_wildcards_are_not_fabricated_without_discovery(tmp_path: Path) -> None:
    _configure(
        tmp_path,
        """
model_list:
  - model_name: requesty/*
    litellm_params:
      model: requesty/*
      api_base: https://router.requesty.ai/v1
""",
    )

    assert build_catalog()["data"] == []


def test_refresh_expands_wildcard_from_configured_endpoint(tmp_path: Path) -> None:
    _configure(
        tmp_path,
        """
model_list:
  - model_name: requesty/*
    litellm_params:
      model: requesty/*
      api_base: https://router.requesty.ai/v1
      api_key: test-key
""",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://router.requesty.ai/v1/models"
        assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "anthropic/claude-sonnet", "created": 1700000000},
                    {"id": "openai/gpt-5"},
                ]
            },
        )

    catalog = build_catalog(refresh=True, transport=httpx.MockTransport(handler))

    assert [entry["id"] for entry in catalog["data"]] == [
        "requesty/anthropic/claude-sonnet",
        "requesty/openai/gpt-5",
    ]
    assert catalog["data"][0]["created"] == 1700000000


def test_refresh_derives_models_url_from_exact_completion_endpoint(tmp_path: Path) -> None:
    _configure(
        tmp_path,
        """
model_list:
  - model_name: local/*
    litellm_params:
      model: openai/*
      api_base: https://router.example/v1/chat/completions
""",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://router.example/v1/models"
        return httpx.Response(200, json={"data": [{"id": "qwen"}]})

    catalog = build_catalog(refresh=True, transport=httpx.MockTransport(handler))

    assert [entry["id"] for entry in catalog["data"]] == ["local/qwen"]


def test_refresh_supports_explicit_minimax_anthropic_provider(tmp_path: Path) -> None:
    _configure(
        tmp_path,
        """
model_list:
  - model_name: minimax/*
    litellm_params:
      model: minimax/*
      api_base: https://api.minimax.io/anthropic
      api_key: test-key
""",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.minimax.io/anthropic/v1/models"
        assert request.headers["x-api-key"] == "test-key"
        assert request.headers["anthropic-version"] == "2023-06-01"
        return httpx.Response(200, json={"data": [{"id": "MiniMax-M3"}]})

    catalog = build_catalog(refresh=True, transport=httpx.MockTransport(handler))

    assert [entry["id"] for entry in catalog["data"]] == ["minimax/MiniMax-M3"]


def test_refresh_inverts_upstream_wildcard_and_filters_nonmatches(tmp_path: Path) -> None:
    _configure(
        tmp_path,
        """
model_list:
  - model_name: local/*
    litellm_params:
      model: openai/qwen-*
      api_base: https://router.example/v1
""",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "qwen-7b"}, {"id": "llama-8b"}]})

    catalog = build_catalog(refresh=True, transport=httpx.MockTransport(handler))

    assert [entry["id"] for entry in catalog["data"]] == ["local/7b"]


def test_refresh_failure_keeps_concrete_aliases(tmp_path: Path) -> None:
    _configure(
        tmp_path,
        """
model_list:
  - model_name: fixed
    litellm_params:
      model: openai/fixed
      api_base: https://router.example/v1
  - model_name: dynamic/*
    litellm_params:
      model: openai/*
      api_base: https://router.example/v1
""",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    catalog = build_catalog(refresh=True, transport=httpx.MockTransport(handler))

    assert [entry["id"] for entry in catalog["data"]] == ["fixed"]


def test_refresh_fails_when_configured_credential_is_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MISSING_API_KEY", raising=False)
    _configure(
        tmp_path,
        """
model_list:
  - model_name: dynamic/*
    litellm_params:
      model: openai/*
      api_base: https://router.example/v1
      api_key: os.environ/MISSING_API_KEY
""",
    )

    with pytest.raises(AuthConfigError, match="credential resolved to an empty value"):
        build_catalog(refresh=True, transport=httpx.MockTransport(lambda request: httpx.Response(200)))


def test_duplicate_discovery_ids_are_deduplicated(tmp_path: Path) -> None:
    _configure(
        tmp_path,
        """
model_list:
  - model_name: local/*
    litellm_params:
      model: openai/*
      api_base: http://127.0.0.1:8000/v1
""",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "qwen"}, {"id": "qwen"}]})

    catalog = build_catalog(refresh=True, transport=httpx.MockTransport(handler))

    assert [entry["id"] for entry in catalog["data"]] == ["local/qwen"]


def test_models_route_handler_returns_configured_catalog(tmp_path: Path) -> None:
    _configure(
        tmp_path,
        """
model_list:
  - model_name: local
    litellm_params:
      model: openai/qwen
      api_base: http://127.0.0.1:8000/v1
""",
    )
    router = InspectorRouter(name="test_models", request_passthrough=True, response_passthrough=True)
    register_models_routes(router)

    flow = MagicMock()
    flow.request.method = "GET"
    flow.request.path = "/v1/models"
    flow.request.query = {}
    flow.response = None

    handler = router.request_routes[0][2]
    handler(flow)

    response = cast(Response, flow.response)
    assert response.status_code == 200
    assert response.content is not None
    payload = json.loads(response.content)
    assert [entry["id"] for entry in payload["data"]] == ["local"]


def test_models_route_handler_skips_non_get() -> None:
    router = InspectorRouter(name="test_models_post", request_passthrough=True, response_passthrough=True)
    register_models_routes(router)

    flow = MagicMock()
    flow.request.method = "POST"
    flow.request.query = {}
    flow.response = None

    handler = router.request_routes[0][2]
    handler(flow)
    assert flow.response is None


def test_models_route_handler_honors_refresh_query() -> None:
    router = InspectorRouter(name="test_models_refresh", request_passthrough=True, response_passthrough=True)
    register_models_routes(router)

    flow = MagicMock()
    flow.request.method = "GET"
    flow.request.query = {"refresh": "true"}
    flow.response = None

    with patch("ccproxy.inspector.routes.models.build_catalog") as build:
        build.return_value = {"object": "list", "data": []}
        handler = router.request_routes[0][2]
        handler(flow)
        build.assert_called_once_with(refresh=True)
