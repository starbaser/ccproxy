"""OpenAI-compatible ``GET /v1/models`` catalog.

Defined by OpenAI; adopted by Anthropic, Google Gemini, OpenRouter, vLLM,
Ollama, LiteLLM, etc. Response shape::

    {
      "object": "list",
      "data": [
        {"id": "<model-id>", "object": "model", "created": <unix-ts>, "owned_by": "<provider>"},
        ...
      ]
    }

ccproxy serves the union of models routable through configured ``oat_sources``
+ ``inspector.transforms``. The static catalog below is the offline floor;
when ``refresh=True`` is requested, providers' upstream ``/v1/models`` are
queried and unioned in (with provider failures falling back to the floor).
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)


STATIC_MODEL_CATALOG: dict[str, list[str]] = {
    "anthropic": [
        "claude-opus-4-7",
        "claude-sonnet-4-6",
        "claude-sonnet-4-5-20250929",
        "claude-haiku-4-5-20251001",
    ],
    "gemini": [
        "gemini-3-pro-preview",
        "gemini-3-flash-preview",
        "gemini-2.5-pro",
        "gemini-2.5-flash",
    ],
    "deepseek": [
        "deepseek-v4",
    ],
}
"""Provider → model IDs floor list. Updated alongside provider releases."""


_PROVIDER_ENDPOINTS: dict[str, str] = {
    "anthropic": "https://api.anthropic.com/v1/models",
    "openrouter": "https://openrouter.ai/api/v1/models",
}
"""Provider → upstream ``/v1/models`` URL for live merge. gemini is omitted
because it requires GCP project context that ccproxy doesn't have at
catalog-build time."""


def _model_entry(model_id: str, owned_by: str, created: int | None = None) -> dict[str, Any]:
    """Build one OpenAI-shaped model entry."""
    return {
        "id": model_id,
        "object": "model",
        "created": created if created is not None else int(time.time()),
        "owned_by": owned_by,
    }


def _fetch_provider_models(
    provider: str,
    endpoint: str,
    *,
    token: str | None,
    transport: httpx.BaseTransport | None = None,
) -> list[dict[str, Any]] | None:
    """Fetch ``GET /v1/models`` from ``endpoint``. Returns None on any failure."""
    headers: dict[str, str] = {"Accept": "application/json"}
    if token:
        if provider == "anthropic":
            headers["x-api-key"] = token
            headers["anthropic-version"] = "2023-06-01"
        else:
            headers["Authorization"] = f"Bearer {token}"

    try:
        client_kwargs: dict[str, Any] = {"timeout": 5.0}
        if transport is not None:
            client_kwargs["transport"] = transport
        with httpx.Client(**client_kwargs) as client:
            resp = client.get(endpoint, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("Live catalog fetch for %s failed: %s", provider, exc)
        return None

    if resp.status_code != 200:
        logger.warning("Live catalog fetch for %s returned %d", provider, resp.status_code)
        return None

    try:
        payload = resp.json()
    except (ValueError, Exception) as exc:
        logger.warning("Live catalog fetch for %s returned non-JSON: %s", provider, exc)
        return None

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return None

    entries: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        if isinstance(model_id, str):
            entries.append(
                _model_entry(
                    model_id,
                    owned_by=provider,
                    created=item.get("created") if isinstance(item.get("created"), int) else None,
                )
            )
    return entries


def build_catalog(
    *,
    refresh: bool = False,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Return the full OpenAI-shaped ``/v1/models`` payload.

    With ``refresh=False`` (default), returns the static floor only. With
    ``refresh=True``, additionally fetches each provider's upstream
    ``/v1/models`` (using cached OAuth tokens) and unions the results
    deduplicated by ``(owned_by, id)``. Any provider failure silently
    falls back to its static floor for that provider.
    """
    seen: set[tuple[str, str]] = set()
    entries: list[dict[str, Any]] = []

    floor_entries: dict[str, list[dict[str, Any]]] = {}
    for provider, model_ids in STATIC_MODEL_CATALOG.items():
        floor_entries[provider] = [_model_entry(mid, owned_by=provider) for mid in model_ids]

    if refresh:
        from ccproxy.config import get_config

        config = get_config()
        for provider, endpoint in _PROVIDER_ENDPOINTS.items():
            token = config.get_oauth_token(provider)
            live = _fetch_provider_models(provider, endpoint, token=token, transport=transport)
            if live is None:
                continue
            for entry in live:
                key = (entry["owned_by"], entry["id"])
                if key not in seen:
                    seen.add(key)
                    entries.append(entry)

    for floor in floor_entries.values():
        for entry in floor:
            key = (entry["owned_by"], entry["id"])
            if key not in seen:
                seen.add(key)
                entries.append(entry)

    return {"object": "list", "data": entries}
