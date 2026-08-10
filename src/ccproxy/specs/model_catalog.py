"""OpenAI-compatible catalog derived from LiteLLM model bindings."""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from ccproxy.config import ModelBinding, Provider, get_config
from ccproxy.constants import AuthConfigError

logger = logging.getLogger(__name__)


def _model_entry(binding: ModelBinding, model_id: str | None = None) -> dict[str, Any]:
    created = binding.model_info.get("created")
    entry: dict[str, Any] = {
        "id": model_id or binding.model_name,
        "object": "model",
        "created": created if isinstance(created, int) else int(time.time()),
        "owned_by": binding.owned_by,
    }
    if binding.model_info:
        entry["model_info"] = binding.model_info
    return entry


def _models_endpoint(provider: Provider) -> str | None:
    if provider.type not in {
        "anthropic",
        "deepseek",
        "minimax",
        "openai",
        "openai_responses",
        "zai",
    }:
        return None
    parsed = urlsplit(provider.base_url)
    path = parsed.path.rstrip("/")
    for endpoint_suffix in ("/chat/completions", "/responses", "/messages"):
        if path.endswith(endpoint_suffix):
            path = path.removesuffix(endpoint_suffix)
            break
    models_path = f"{path}/models" if path.endswith("/v1") else f"{path}/v1/models"
    return urlunsplit(parsed._replace(path=models_path))


def _auth_request_parts(
    provider_name: str,
    provider: Provider,
) -> tuple[dict[str, str], dict[str, str]]:
    headers = {"accept": "application/json", **provider.headers}
    query = dict(provider.query)
    if provider.auth is None:
        return headers, query

    config = get_config()
    token = config.resolve_provider_auth(
        provider_name,
        provider,
        label=f"Catalog/{provider_name}",
    )
    if not token:
        raise AuthConfigError(f"Catalog/{provider_name} credential resolved to an empty value")

    if provider.auth.query_param is not None:
        query[provider.auth.query_param] = token
    elif provider.auth.header is None or provider.auth.header.lower() == "authorization":
        headers["authorization"] = f"Bearer {token}"
    else:
        headers[provider.auth.header] = token
    headers.update(provider.auth.extra_headers(f"Catalog/{provider_name}"))
    if provider.type in {"anthropic", "deepseek", "minimax", "zai"}:
        headers.setdefault("anthropic-version", "2023-06-01")
    return headers, query


def _fetch_provider_models(
    provider_name: str,
    provider: Provider,
    endpoint: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> list[dict[str, Any]] | None:
    headers, query = _auth_request_parts(provider_name, provider)
    try:
        client_kwargs: dict[str, Any] = {"timeout": 5.0}
        if transport is not None:
            client_kwargs["transport"] = transport
        with httpx.Client(**client_kwargs) as client:
            response = client.get(endpoint, headers=headers, params=query)
    except httpx.HTTPError as exc:
        logger.warning("Model discovery for %s failed: %s", provider_name, exc)
        return None

    if response.status_code != 200:
        logger.warning(
            "Model discovery for %s returned %d",
            provider_name,
            response.status_code,
        )
        return None
    try:
        payload = response.json()
    except ValueError as exc:
        logger.warning("Model discovery for %s returned non-JSON: %s", provider_name, exc)
        return None

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return None
    return [item for item in data if isinstance(item, dict) and isinstance(item.get("id"), str)]


def _configured_entries(bindings: list[ModelBinding]) -> list[dict[str, Any]]:
    return [_model_entry(binding) for binding in bindings if "*" not in binding.model_name]


def _discovered_entries(
    bindings: list[ModelBinding],
    *,
    transport: httpx.BaseTransport | None,
) -> list[dict[str, Any]]:
    discovered: list[dict[str, Any]] = []
    by_provider: dict[str, list[ModelBinding]] = {}
    for binding in bindings:
        if "*" in binding.model_name:
            by_provider.setdefault(binding.provider_name, []).append(binding)

    for provider_name, wildcard_bindings in by_provider.items():
        provider = wildcard_bindings[0].provider
        endpoint = _models_endpoint(provider)
        if endpoint is None:
            logger.warning(
                "Model discovery is unavailable for provider type %s",
                provider.type,
            )
            continue
        upstream = _fetch_provider_models(
            provider_name,
            provider,
            endpoint,
            transport=transport,
        )
        if upstream is None:
            continue
        for binding in wildcard_bindings:
            for item in upstream:
                upstream_id = item["id"]
                public_id = binding.public_model_for_upstream(upstream_id)
                if public_id is None:
                    continue
                entry = _model_entry(binding, public_id)
                created = item.get("created")
                if isinstance(created, int):
                    entry["created"] = created
                discovered.append(entry)
    return discovered


def build_catalog(
    *,
    refresh: bool = False,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Return models that are actually selectable through compiled bindings."""
    bindings = get_config().model_bindings
    candidates = _configured_entries(bindings)
    if refresh:
        candidates.extend(_discovered_entries(bindings, transport=transport))

    seen: set[str] = set()
    entries: list[dict[str, Any]] = []
    for entry in candidates:
        model_id = entry["id"]
        if model_id in seen:
            continue
        seen.add(model_id)
        entries.append(entry)
    return {"object": "list", "data": entries}
