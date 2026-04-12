"""Tests for ccproxy.lightllm.context_cache — Gemini context caching orchestration."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from ccproxy.lightllm.context_cache import (
    _compute_cache_key,
    _get_caching_url_and_headers,
    resolve_cached_content,
)


def _make_cached_messages(text: str = "x" * 5000) -> list[dict]:
    return [
        {
            "role": "system",
            "content": [
                {"type": "text", "text": "You are helpful."},
                {
                    "type": "text",
                    "text": text,
                    "cache_control": {"type": "ephemeral"},
                },
            ],
        },
        {"role": "user", "content": "What is this?"},
    ]


def _make_plain_messages() -> list[dict]:
    return [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "hello"},
    ]


class TestGetCachingUrlAndHeaders:
    def test_gemini_api_key(self) -> None:
        result = _get_caching_url_and_headers("gemini", "AIza-key", None, None)
        assert result is not None
        url, headers = result
        assert "generativelanguage.googleapis.com" in url
        assert "key=AIza-key" in url
        assert "Authorization" not in headers

    def test_gemini_oauth_token(self) -> None:
        result = _get_caching_url_and_headers("gemini", "ya29.something", None, None)
        assert result is not None
        url, headers = result
        assert "key=" not in url
        assert headers["Authorization"] == "Bearer ya29.something"

    def test_vertex_ai(self) -> None:
        result = _get_caching_url_and_headers(
            "vertex_ai", "ya29.tok", "my-project", "us-central1",
        )
        assert result is not None
        url, headers = result
        assert "us-central1-aiplatform.googleapis.com/v1/" in url
        assert "my-project" in url
        assert "us-central1" in url
        assert headers["Authorization"] == "Bearer ya29.tok"

    def test_vertex_ai_beta(self) -> None:
        result = _get_caching_url_and_headers(
            "vertex_ai_beta", "ya29.tok", "proj", "europe-west1",
        )
        assert result is not None
        url, _ = result
        assert "/v1beta1/" in url

    def test_vertex_ai_global_location(self) -> None:
        result = _get_caching_url_and_headers(
            "vertex_ai", "ya29.tok", "proj", "global",
        )
        assert result is not None
        url, _ = result
        assert url.startswith("https://aiplatform.googleapis.com/")

    def test_vertex_ai_missing_project(self) -> None:
        result = _get_caching_url_and_headers("vertex_ai", "ya29.tok", None, None)
        assert result is None

    def test_vertex_ai_missing_location(self) -> None:
        result = _get_caching_url_and_headers("vertex_ai", "ya29.tok", "proj", None)
        assert result is None


class TestComputeCacheKey:
    def test_deterministic(self) -> None:
        msgs = [{"role": "user", "content": "hello"}]
        k1 = _compute_cache_key(msgs, None, "gemini-2.0-flash")
        k2 = _compute_cache_key(msgs, None, "gemini-2.0-flash")
        assert k1 == k2

    def test_different_messages_different_keys(self) -> None:
        k1 = _compute_cache_key([{"role": "user", "content": "a"}], None, "m")
        k2 = _compute_cache_key([{"role": "user", "content": "b"}], None, "m")
        assert k1 != k2

    def test_different_model_different_keys(self) -> None:
        msgs = [{"role": "user", "content": "hello"}]
        k1 = _compute_cache_key(msgs, None, "gemini-2.0-flash")
        k2 = _compute_cache_key(msgs, None, "gemini-1.5-pro")
        assert k1 != k2

    def test_tools_affect_key(self) -> None:
        msgs = [{"role": "user", "content": "hello"}]
        k1 = _compute_cache_key(msgs, None, "m")
        k2 = _compute_cache_key(msgs, [{"type": "function", "name": "f"}], "m")
        assert k1 != k2


class TestResolveCachedContent:
    def test_no_cache_control_annotations(self) -> None:
        messages = _make_plain_messages()
        result_msgs, params, name = resolve_cached_content(
            messages=messages,
            model="gemini-2.0-flash",
            provider="gemini",
            optional_params={},
            api_key="test-key",
        )
        assert name is None
        assert result_msgs is messages

    @patch("ccproxy.lightllm.context_cache.is_prompt_caching_valid_prompt", return_value=False)
    def test_below_token_threshold(self, mock_valid: MagicMock) -> None:
        messages = _make_cached_messages(text="short")
        result_msgs, _, name = resolve_cached_content(
            messages=messages,
            model="gemini-2.0-flash",
            provider="gemini",
            optional_params={},
            api_key="test-key",
        )
        assert name is None
        assert result_msgs is messages
        mock_valid.assert_called_once()

    @patch("ccproxy.lightllm.context_cache._client")
    @patch("ccproxy.lightllm.context_cache.is_prompt_caching_valid_prompt", return_value=True)
    def test_cache_hit_gemini(self, _mock_valid: MagicMock, mock_client: MagicMock) -> None:
        cache_name = "cachedContents/hit123"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "cachedContents": [
                {"displayName": "wrong-key", "name": "cachedContents/other"},
            ],
            "nextPageToken": "page2",
        }
        mock_resp2 = MagicMock()
        mock_resp2.status_code = 200
        mock_resp2.raise_for_status = MagicMock()
        # Second page has the match — use a dynamic displayName check
        mock_client.get.side_effect = [mock_resp, mock_resp2]

        # We need the cache key to match. Patch _compute_cache_key to return a known value.
        with patch("ccproxy.lightllm.context_cache._compute_cache_key", return_value="the-key"):
            mock_resp2.json.return_value = {
                "cachedContents": [
                    {"displayName": "the-key", "name": cache_name},
                ],
            }

            messages = _make_cached_messages()
            result_msgs, _, name = resolve_cached_content(
                messages=messages,
                model="gemini-2.0-flash",
                provider="gemini",
                optional_params={},
                api_key="test-key",
            )

        assert name == cache_name
        # Cached system message should be filtered out
        assert len(result_msgs) < len(messages)
        # No POST call (only GETs)
        mock_client.post.assert_not_called()

    @patch("ccproxy.lightllm.context_cache._client")
    @patch("ccproxy.lightllm.context_cache.is_prompt_caching_valid_prompt", return_value=True)
    def test_cache_miss_then_create_gemini(self, _mock_valid: MagicMock, mock_client: MagicMock) -> None:
        # GET returns empty list (no existing cache)
        list_resp = MagicMock()
        list_resp.raise_for_status = MagicMock()
        list_resp.json.return_value = {"cachedContents": []}
        mock_client.get.return_value = list_resp

        # POST creates new cache
        create_resp = MagicMock()
        create_resp.raise_for_status = MagicMock()
        create_resp.json.return_value = {"name": "cachedContents/new456", "model": "models/gemini-2.0-flash"}
        mock_client.post.return_value = create_resp

        messages = _make_cached_messages()
        result_msgs, _, name = resolve_cached_content(
            messages=messages,
            model="gemini-2.0-flash",
            provider="gemini",
            optional_params={},
            api_key="test-key",
        )

        assert name == "cachedContents/new456"
        assert len(result_msgs) < len(messages)
        mock_client.post.assert_called_once()

    @patch("ccproxy.lightllm.context_cache._client")
    @patch("ccproxy.lightllm.context_cache.is_prompt_caching_valid_prompt", return_value=True)
    def test_cache_hit_vertex_ai(self, _mock_valid: MagicMock, mock_client: MagicMock) -> None:
        list_resp = MagicMock()
        list_resp.raise_for_status = MagicMock()

        with patch("ccproxy.lightllm.context_cache._compute_cache_key", return_value="vkey"):
            list_resp.json.return_value = {
                "cachedContents": [
                    {"displayName": "vkey", "name": "projects/p/locations/l/cachedContents/v1"},
                ],
            }
            mock_client.get.return_value = list_resp

            messages = _make_cached_messages()
            result_msgs, _, name = resolve_cached_content(
                messages=messages,
                model="gemini-2.0-flash",
                provider="vertex_ai",
                optional_params={},
                api_key="ya29.token",
                vertex_project="my-project",
                vertex_location="us-central1",
            )

        assert name == "projects/p/locations/l/cachedContents/v1"
        # Verify URL was constructed for vertex_ai
        call_url = mock_client.get.call_args[0][0]
        assert "us-central1-aiplatform.googleapis.com" in call_url

    @patch("ccproxy.lightllm.context_cache.is_prompt_caching_valid_prompt", return_value=True)
    def test_vertex_ai_missing_project_skips(self, _mock_valid: MagicMock) -> None:
        messages = _make_cached_messages()
        result_msgs, _, name = resolve_cached_content(
            messages=messages,
            model="gemini-2.0-flash",
            provider="vertex_ai",
            optional_params={},
            api_key="ya29.token",
        )
        assert name is None
        assert result_msgs is messages

    @patch("ccproxy.lightllm.context_cache._client")
    @patch("ccproxy.lightllm.context_cache.is_prompt_caching_valid_prompt", return_value=True)
    def test_list_http_error_graceful(self, _mock_valid: MagicMock, mock_client: MagicMock) -> None:
        list_resp = MagicMock()
        list_resp.status_code = 500
        list_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Server Error", request=MagicMock(), response=list_resp,
        )
        mock_client.get.return_value = list_resp

        # Creation also fails (server is down)
        mock_client.post.side_effect = httpx.ConnectError("connection refused")

        messages = _make_cached_messages()
        result_msgs, _, name = resolve_cached_content(
            messages=messages,
            model="gemini-2.0-flash",
            provider="gemini",
            optional_params={},
            api_key="test-key",
        )
        assert name is None
        assert result_msgs is messages

    @patch("ccproxy.lightllm.context_cache._client")
    @patch("ccproxy.lightllm.context_cache.is_prompt_caching_valid_prompt", return_value=True)
    def test_create_http_error_graceful(self, _mock_valid: MagicMock, mock_client: MagicMock) -> None:
        # List returns empty (no existing cache)
        list_resp = MagicMock()
        list_resp.raise_for_status = MagicMock()
        list_resp.json.return_value = {"cachedContents": []}
        mock_client.get.return_value = list_resp

        # POST fails
        mock_client.post.side_effect = httpx.ConnectError("connection refused")

        messages = _make_cached_messages()
        result_msgs, _, name = resolve_cached_content(
            messages=messages,
            model="gemini-2.0-flash",
            provider="gemini",
            optional_params={},
            api_key="test-key",
        )
        assert name is None
        assert result_msgs is messages

    @patch("ccproxy.lightllm.context_cache._client")
    @patch("ccproxy.lightllm.context_cache.is_prompt_caching_valid_prompt", return_value=True)
    def test_tools_included_in_cache_body(self, _mock_valid: MagicMock, mock_client: MagicMock) -> None:
        list_resp = MagicMock()
        list_resp.raise_for_status = MagicMock()
        list_resp.json.return_value = {"cachedContents": []}
        mock_client.get.return_value = list_resp

        create_resp = MagicMock()
        create_resp.raise_for_status = MagicMock()
        create_resp.json.return_value = {"name": "cachedContents/tools1"}
        mock_client.post.return_value = create_resp

        tools = [{"type": "function", "function": {"name": "get_weather"}}]
        messages = _make_cached_messages()
        _, result_params, name = resolve_cached_content(
            messages=messages,
            model="gemini-2.0-flash",
            provider="gemini",
            optional_params={"tools": tools, "temperature": 0.5},
            api_key="test-key",
        )

        assert name == "cachedContents/tools1"
        # tools should be restored in optional_params
        assert "tools" in result_params
        assert result_params["tools"] is tools
        # temperature should be preserved
        assert result_params["temperature"] == 0.5

        # Verify tools were included in the POST body
        post_body = mock_client.post.call_args.kwargs.get("json", {})
        assert post_body.get("tools") is tools
