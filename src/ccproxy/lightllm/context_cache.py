"""Gemini/Vertex AI context caching via Google's cachedContents API.

Surgically imports LiteLLM's pure transformation functions for message
separation and request body construction. Owns the HTTP layer for
creating and looking up cached content resources.

Caching is best-effort: any API failure falls through gracefully and
the request proceeds without caching.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Literal

import httpx
from litellm.llms.vertex_ai.context_caching.transformation import (
    separate_cached_messages,
    transform_openai_messages_to_gemini_context_caching,
)
from litellm.utils import is_cached_message, is_prompt_caching_valid_prompt

logger = logging.getLogger(__name__)

_client = httpx.Client(timeout=30.0)
_MAX_PAGINATION_PAGES = 100

ProviderType = Literal["gemini", "vertex_ai", "vertex_ai_beta"]


def _has_cached_messages(messages: list[Any]) -> bool:
    return any(is_cached_message(message=m) for m in messages)


def _compute_cache_key(
    cached_messages: list[Any],
    tools: Any | None,
    model: str,
) -> str:
    payload = json.dumps(
        {"messages": cached_messages, "tools": tools, "model": model},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _get_caching_url_and_headers(
    provider: ProviderType,
    api_key: str | None,
    vertex_project: str | None,
    vertex_location: str | None,
) -> tuple[str, dict[str, str]] | None:
    headers: dict[str, str] = {"Content-Type": "application/json"}

    if provider == "gemini":
        is_oauth = api_key is not None and api_key.startswith("ya29.")
        if is_oauth:
            url = "https://generativelanguage.googleapis.com/v1beta/cachedContents"
            headers["Authorization"] = f"Bearer {api_key}"
        else:
            url = f"https://generativelanguage.googleapis.com/v1beta/cachedContents?key={api_key}"
        return url, headers

    # vertex_ai / vertex_ai_beta
    if not vertex_project or not vertex_location:
        logger.warning(
            "Context caching for %s requires dest_vertex_project and "
            "dest_vertex_location in the transform rule — skipping",
            provider,
        )
        return None

    version = "v1beta1" if provider == "vertex_ai_beta" else "v1"
    if vertex_location == "global":
        url = (
            f"https://aiplatform.googleapis.com/{version}/projects/"
            f"{vertex_project}/locations/{vertex_location}/cachedContents"
        )
    else:
        url = (
            f"https://{vertex_location}-aiplatform.googleapis.com/{version}/projects/"
            f"{vertex_project}/locations/{vertex_location}/cachedContents"
        )
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return url, headers


def _find_existing_cache(
    url: str,
    headers: dict[str, str],
    cache_key: str,
) -> str | None:
    page_token: str | None = None

    for _ in range(_MAX_PAGINATION_PAGES):
        paged_url = url
        if page_token:
            sep = "&" if "?" in url else "?"
            paged_url = f"{url}{sep}pageToken={page_token}"

        try:
            resp = _client.get(paged_url, headers=headers)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 403:
                return None
            logger.warning("Context cache list failed: %s", exc)
            return None
        except httpx.HTTPError as exc:
            logger.warning("Context cache list error: %s", exc)
            return None

        body = resp.json()
        items = body.get("cachedContents", [])
        if not items:
            return None

        for item in items:
            if item.get("displayName") == cache_key:
                name: str | None = item.get("name")
                return name

        page_token = body.get("nextPageToken")
        if not page_token:
            break

    return None


def _create_cache(
    url: str,
    headers: dict[str, str],
    request_body: dict[str, Any],
) -> str | None:
    try:
        resp = _client.post(url, headers=headers, json=request_body)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("Context cache creation failed: %s", exc)
        return None

    name: str | None = resp.json().get("name")
    return name


def resolve_cached_content(
    messages: list[Any],
    model: str,
    provider: ProviderType,
    optional_params: dict[str, Any],
    *,
    api_key: str | None = None,
    vertex_project: str | None = None,
    vertex_location: str | None = None,
) -> tuple[list[Any], dict[str, Any], str | None]:
    """Resolve or create a Gemini cached content resource.

    Returns (filtered_messages, optional_params, cached_content_name).
    On any failure, returns the original messages with cached_content=None.
    """
    if not _has_cached_messages(messages):
        return messages, optional_params, None

    cached_messages, non_cached_messages = separate_cached_messages(messages=messages)
    if not cached_messages:
        return messages, optional_params, None

    custom_provider: Literal["gemini", "vertex_ai", "vertex_ai_beta"] = "gemini" if provider == "gemini" else provider

    if not is_prompt_caching_valid_prompt(
        model=model,
        messages=cached_messages,
        custom_llm_provider=custom_provider,
    ):
        logger.debug(
            "Context caching: cached content below minimum token threshold, skipping",
        )
        return messages, optional_params, None

    result = _get_caching_url_and_headers(
        provider,
        api_key,
        vertex_project,
        vertex_location,
    )
    if result is None:
        return messages, optional_params, None
    url, headers = result

    tools = optional_params.pop("tools", None)
    cache_key = _compute_cache_key(cached_messages, tools, model)

    # Check for existing cache
    existing = _find_existing_cache(url, headers, cache_key)
    if existing:
        if tools is not None:
            optional_params["tools"] = tools
        logger.info("Context cache hit: %s", existing)
        return non_cached_messages, optional_params, existing

    # Build and create new cache
    request_body = dict(
        transform_openai_messages_to_gemini_context_caching(
            model=model,
            messages=cached_messages,
            cache_key=cache_key,
            custom_llm_provider=custom_provider,
            vertex_project=vertex_project,
            vertex_location=vertex_location,
        )
    )
    if tools is not None:
        request_body["tools"] = tools

    name = _create_cache(url, headers, request_body)
    if name is None:
        # Restore tools and return original messages
        if tools is not None:
            optional_params["tools"] = tools
        return messages, optional_params, None

    if tools is not None:
        optional_params["tools"] = tools
    logger.info("Context cache created: %s", name)
    return non_cached_messages, optional_params, name
