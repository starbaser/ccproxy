"""Tests for the unified gemini_cli outbound hook."""

from __future__ import annotations

import json
import sys
from unittest.mock import MagicMock, patch

import pytest

from ccproxy.flows.store import FlowRecord, InspectorMeta
from ccproxy.hooks.gemini_cli import (
    _ACTION_RE,
    _KNOWN_GEMINI_ACTIONS,
    EnvelopeUnwrapStream,
    gemini_cli,
    gemini_cli_guard,
    prewarm_project,
    reset_cache,
)
from ccproxy.pipeline.context import Context

gemini_cli_module = sys.modules["ccproxy.hooks.gemini_cli"]


def _make_ctx(
    *,
    body: dict | None = None,
    path: str = "/v1beta/models/gemini-3.1-pro-preview:generateContent",
    headers: dict[str, str] | None = None,
    oauth_provider: str | None = "gemini",
) -> Context:
    flow = MagicMock()
    flow.id = "test-flow-id"
    flow.request.content = json.dumps(body or {"contents": []}).encode()
    default_headers = {"authorization": "Bearer test-token"}
    default_headers.update(headers or {})
    flow.request.headers = default_headers
    flow.request.path = path
    flow.metadata = {}
    if oauth_provider:
        flow.metadata["ccproxy.oauth_provider"] = oauth_provider
    flow.metadata[InspectorMeta.RECORD] = FlowRecord(direction="inbound")
    return Context.from_flow(flow)


@pytest.fixture(autouse=True)
def reset_project_cache():
    reset_cache()
    yield
    reset_cache()


class TestGuard:
    def test_fires_when_provider_is_gemini(self) -> None:
        ctx = _make_ctx()
        assert gemini_cli_guard(ctx) is True

    def test_skipped_when_provider_is_not_gemini(self) -> None:
        ctx = _make_ctx(oauth_provider="anthropic")
        assert gemini_cli_guard(ctx) is False

    def test_skipped_when_no_provider(self) -> None:
        ctx = _make_ctx(oauth_provider=None)
        assert gemini_cli_guard(ctx) is False


class TestEnvelopeWrap:
    def test_native_gemini_body_wraps_in_envelope(self) -> None:
        body = {
            "contents": [{"role": "user", "parts": [{"text": "hello"}]}],
            "generationConfig": {"temperature": 0.5},
        }
        ctx = _make_ctx(body=body)
        gemini_cli_module._cached_project = "test-project"

        gemini_cli(ctx, {})

        wrapped = ctx._body
        assert wrapped["model"] == "gemini-3.1-pro-preview"
        assert wrapped["project"] == "test-project"
        assert wrapped["request"] == body
        assert "user_prompt_id" in wrapped
        assert isinstance(wrapped["user_prompt_id"], str)

    def test_glass_style_body_passes_through_unchanged(self) -> None:
        original = {
            "model": "gemini-2.5-pro",
            "project": "glass-project",
            "request": {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
            "user_prompt_id": "preserved-id",
        }
        ctx = _make_ctx(body=original, path="/v1internal:generateContent")

        gemini_cli(ctx, {})

        assert ctx._body == original

    def test_strips_metadata_field_before_wrapping(self) -> None:
        body = {
            "contents": [{"role": "user", "parts": [{"text": "x"}]}],
            "metadata": {"user_id": "abc"},
        }
        ctx = _make_ctx(body=body)
        gemini_cli_module._cached_project = "proj"

        gemini_cli(ctx, {})

        assert "metadata" not in ctx._body["request"]

    def test_no_project_omits_project_field(self) -> None:
        ctx = _make_ctx(body={"contents": []})

        gemini_cli(ctx, {})

        assert "project" not in ctx._body
        assert "model" in ctx._body
        assert "request" in ctx._body


class TestPathRewriting:
    def test_generate_content_path_rewrites(self) -> None:
        ctx = _make_ctx(path="/v1beta/models/gemini-3.1-pro-preview:generateContent")

        gemini_cli(ctx, {})

        assert ctx.flow.request.path == "/v1internal:generateContent"

    def test_stream_generate_content_appends_alt_sse(self) -> None:
        ctx = _make_ctx(path="/v1beta/models/gemini-3.1-pro-preview:streamGenerateContent")

        gemini_cli(ctx, {})

        assert ctx.flow.request.path == "/v1internal:streamGenerateContent?alt=sse"

    def test_path_without_action_passes_through(self) -> None:
        ctx = _make_ctx(path="/v1beta/models/gemini-3.1-pro-preview")
        original_path = ctx.flow.request.path

        gemini_cli(ctx, {})

        assert ctx.flow.request.path == original_path

    @pytest.mark.parametrize("action", _KNOWN_GEMINI_ACTIONS)
    def test_action_regex_matches_known_actions(self, action: str) -> None:
        path = f"/v1beta/models/gemini-3.1-pro-preview:{action}"
        match = _ACTION_RE.search(path)
        assert match is not None
        assert match.group(1) == action

    def test_unknown_action_passes_through(self) -> None:
        path = "/v1beta/models/gemini-3.1-pro-preview:unknownAction"
        ctx = _make_ctx(path=path)

        gemini_cli(ctx, {})

        assert ctx.flow.request.path == path

    def test_no_colon_action_passes_through(self) -> None:
        path = "/v1beta/models/gemini-3.1-pro-preview"
        ctx = _make_ctx(path=path)

        gemini_cli(ctx, {})

        assert ctx.flow.request.path == path


class TestHostRewriting:
    def test_host_set_to_cloudcode_pa(self) -> None:
        ctx = _make_ctx()

        gemini_cli(ctx, {})

        assert ctx.flow.request.host == "cloudcode-pa.googleapis.com"
        assert ctx.flow.request.port == 443
        assert ctx.flow.request.scheme == "https"
        assert ctx.flow.request.headers["host"] == "cloudcode-pa.googleapis.com"


class TestHeaderMasquerade:
    def test_user_agent_rewritten_for_google_genai_sdk(self) -> None:
        ctx = _make_ctx(headers={"user-agent": "google-genai-sdk/1.0"})

        gemini_cli(ctx, {})

        ua = ctx.flow.request.headers.get("user-agent")
        assert ua.startswith("GeminiCLI/")
        assert "gemini-3.1-pro-preview" in ua

    def test_x_goog_api_client_set_for_google_genai_sdk(self) -> None:
        ctx = _make_ctx(headers={"user-agent": "google-genai-sdk/1.0"})

        gemini_cli(ctx, {})

        assert ctx.flow.request.headers.get("x-goog-api-client") == "gl-node/22.22.2"

    def test_user_agent_preserved_for_non_sdk_clients(self) -> None:
        """Glass and other third-party tools keep their own UA so cloudcode-pa
        doesn't bucket them together with the user's real Gemini CLI session."""
        ctx = _make_ctx(headers={"user-agent": "Python-urllib/3.13"})

        gemini_cli(ctx, {})

        assert ctx.flow.request.headers.get("user-agent") == "Python-urllib/3.13"
        assert "x-goog-api-client" not in ctx.flow.request.headers

    def test_x_goog_api_key_stripped(self) -> None:
        ctx = _make_ctx(headers={"x-goog-api-key": "leftover-key"})

        gemini_cli(ctx, {})

        assert "x-goog-api-key" not in ctx.flow.request.headers


class TestTransformMetadata:
    def test_sets_record_transform_for_response_unwrap(self) -> None:
        ctx = _make_ctx()

        gemini_cli(ctx, {})

        record = ctx.flow.metadata[InspectorMeta.RECORD]
        assert record.transform is not None
        assert record.transform.provider == "gemini"
        assert record.transform.model == "gemini-3.1-pro-preview"
        assert record.transform.is_streaming is False

    def test_streaming_flag_set_for_stream_generate_content(self) -> None:
        ctx = _make_ctx(path="/v1beta/models/gemini-3.1-pro-preview:streamGenerateContent")

        gemini_cli(ctx, {})

        record = ctx.flow.metadata[InspectorMeta.RECORD]
        assert record.transform.is_streaming is True


class TestEnvelopeUnwrapStream:
    def test_buffered_response_unwraps_envelope(self) -> None:
        stream = EnvelopeUnwrapStream()
        chunk = b'data: {"response": {"candidates": [{"content": {"parts": [{"text": "hi"}]}}]}}\n\n'

        out = stream(chunk)

        assert isinstance(out, bytes)
        parsed = json.loads(out.split(b"data: ", 1)[1].rstrip(b"\n\n"))
        assert "candidates" in parsed
        assert parsed["candidates"][0]["content"]["parts"][0]["text"] == "hi"

    def test_crlf_separator_unwraps_envelope(self) -> None:
        """cloudcode-pa uses CRLF (\\r\\n\\r\\n) — must be handled."""
        stream = EnvelopeUnwrapStream()
        chunk = b'data: {"response": {"candidates": [{"x": 1}]}}\r\n\r\n'

        out = stream(chunk)

        assert b'"x": 1' in out
        assert b"response" not in out
        assert out.endswith(b"\r\n\r\n")

    def test_multiple_chunks_unwrapped_independently(self) -> None:
        stream = EnvelopeUnwrapStream()
        chunk1 = b'data: {"response": {"candidates": [{"a": 1}]}}\n\n'
        chunk2 = b'data: {"response": {"candidates": [{"b": 2}]}}\n\n'

        out1 = stream(chunk1)
        out2 = stream(chunk2)

        assert b'"a": 1' in out1 and b"response" not in out1
        assert b'"b": 2' in out2 and b"response" not in out2

    def test_partial_chunk_buffered_until_double_newline(self) -> None:
        stream = EnvelopeUnwrapStream()
        out1 = stream(b'data: {"response": {"x":')
        out2 = stream(b" 1}}\n\n")

        assert out1 == b""
        assert b'"x": 1' in out2

    def test_done_marker_passes_through(self) -> None:
        stream = EnvelopeUnwrapStream()
        out = stream(b"data: [DONE]\n\n")
        assert b"[DONE]" in out

    def test_unparseable_json_passes_through(self) -> None:
        stream = EnvelopeUnwrapStream()
        out = stream(b"data: not-valid-json\n\n")
        assert b"not-valid-json" in out

    def test_chunk_without_response_field_passes_through(self) -> None:
        stream = EnvelopeUnwrapStream()
        out = stream(b'data: {"candidates": [{"x": 1}]}\n\n')
        parsed = json.loads(out.split(b"data: ", 1)[1].rstrip(b"\n\n"))
        assert parsed == {"candidates": [{"x": 1}]}

    def test_raw_body_accumulates_input_chunks(self) -> None:
        stream = EnvelopeUnwrapStream()
        stream(b'data: {"response": {"a": 1}}\n\n')
        stream(b'data: {"response": {"b": 2}}\n\n')

        raw = stream.raw_body
        assert b'{"response": {"a": 1}}' in raw
        assert b'{"response": {"b": 2}}' in raw

    def test_empty_input_returns_empty(self) -> None:
        stream = EnvelopeUnwrapStream()
        assert stream(b"") == b""


class TestPrewarmProject:
    def test_prewarm_caches_project(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"cloudaicompanionProject": "abc-xyz"}

        mock_config = MagicMock()
        mock_config.providers = {"gemini": object()}
        mock_config.get_oauth_token.return_value = "tok"

        with (
            patch("ccproxy.hooks.gemini_cli.get_config", return_value=mock_config),
            patch("httpx.post", return_value=mock_resp) as mock_post,
        ):
            prewarm_project()
            prewarm_project()  # second call should be no-op

        assert gemini_cli_module._cached_project == "abc-xyz"
        assert mock_post.call_count == 1

    def test_prewarm_skips_when_no_gemini_oat_source(self) -> None:
        mock_config = MagicMock()
        mock_config.providers = {}

        with (
            patch("ccproxy.hooks.gemini_cli.get_config", return_value=mock_config),
            patch("httpx.post") as mock_post,
        ):
            prewarm_project()

        assert gemini_cli_module._cached_project is None
        assert mock_post.call_count == 0

    def test_prewarm_skips_when_token_missing(self) -> None:
        mock_config = MagicMock()
        mock_config.providers = {"gemini": object()}
        mock_config.get_oauth_token.return_value = ""

        with (
            patch("ccproxy.hooks.gemini_cli.get_config", return_value=mock_config),
            patch("httpx.post") as mock_post,
        ):
            prewarm_project()

        assert gemini_cli_module._cached_project is None
        assert mock_post.call_count == 0

    def test_prewarm_swallows_failures(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 500

        mock_config = MagicMock()
        mock_config.providers = {"gemini": object()}
        mock_config.get_oauth_token.return_value = "tok"

        with (
            patch("ccproxy.hooks.gemini_cli.get_config", return_value=mock_config),
            patch("httpx.post", return_value=mock_resp),
        ):
            prewarm_project()

        assert gemini_cli_module._cached_project is None
