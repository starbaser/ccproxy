"""Tests for the unified gemini_cli outbound hook."""

from __future__ import annotations

import json
import sys
import uuid
from unittest.mock import MagicMock, patch

import pytest

from ccproxy.flows.store import FlowRecord, InspectorMeta
from ccproxy.hooks.gemini_cli import (
    _ACTION_RE,
    _KNOWN_GEMINI_ACTIONS,
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
    conversation_id: str | None = None,
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
    if conversation_id is not None:
        flow.metadata["ccproxy.conversation_id"] = conversation_id
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
        assert wrapped["request"]["contents"] == body["contents"]
        assert wrapped["request"]["generationConfig"] == body["generationConfig"]
        assert isinstance(wrapped["request"]["session_id"], str)
        uuid.UUID(wrapped["request"]["session_id"])
        assert "user_prompt_id" in wrapped
        assert isinstance(wrapped["user_prompt_id"], str)

    def test_glass_style_body_preserved_except_for_session_id_injection(self) -> None:
        original = {
            "model": "gemini-2.5-pro",
            "project": "glass-project",
            "request": {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
            "user_prompt_id": "preserved-id",
        }
        ctx = _make_ctx(body=dict(original), path="/v1internal:generateContent")

        gemini_cli(ctx, {})

        assert ctx._body["model"] == original["model"]
        assert ctx._body["project"] == original["project"]
        assert ctx._body["request"]["contents"] == original["request"]["contents"]
        assert ctx._body["user_prompt_id"] == "preserved-id"
        # session_id is injected even on already-wrapped bodies
        assert isinstance(ctx._body["request"]["session_id"], str)
        uuid.UUID(ctx._body["request"]["session_id"])  # raises if not a valid UUID

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


class TestSessionIdInjection:
    """Verify request.session_id is stamped for cloudcode-pa implicit prefix cache."""

    @staticmethod
    def _expected_session_id(model: str, project: str, conv_id: str) -> str:
        seed = f"ccproxy:{model}:{project}:{conv_id}"
        return str(uuid.uuid5(uuid.NAMESPACE_OID, seed))

    def test_fresh_wrap_uses_conversation_id_when_present(self) -> None:
        ctx = _make_ctx(
            body={"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
            conversation_id="abc123def456",
        )
        gemini_cli_module._cached_project = "myproject"

        gemini_cli(ctx, {})

        expected = self._expected_session_id("gemini-3.1-pro-preview", "myproject", "abc123def456")
        assert ctx._body["request"]["session_id"] == expected

    def test_fresh_wrap_falls_back_to_flow_id_when_no_conversation_id(self) -> None:
        ctx = _make_ctx(body={"contents": []})

        gemini_cli(ctx, {})

        expected = self._expected_session_id("gemini-3.1-pro-preview", "default", "flow:test-flow-id")
        assert ctx._body["request"]["session_id"] == expected

    def test_default_project_when_cached_project_unset(self) -> None:
        ctx = _make_ctx(body={"contents": []}, conversation_id="conv-xyz")

        gemini_cli(ctx, {})

        expected = self._expected_session_id("gemini-3.1-pro-preview", "default", "conv-xyz")
        assert ctx._body["request"]["session_id"] == expected

    def test_already_wrapped_body_gets_session_id_injected(self) -> None:
        ctx = _make_ctx(
            body={
                "model": "gemini-2.5-pro",
                "project": "glass",
                "request": {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
            },
            path="/v1internal:generateContent",
            conversation_id="conv-abc",
        )

        gemini_cli(ctx, {})

        expected = self._expected_session_id("gemini-2.5-pro", "default", "conv-abc")
        assert ctx._body["request"]["session_id"] == expected

    def test_already_wrapped_with_existing_session_id_is_overwritten(self) -> None:
        ctx = _make_ctx(
            body={
                "model": "gemini-3.1-pro-preview",
                "project": "p",
                "request": {
                    "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
                    "session_id": "client-supplied-old-id",
                },
            },
            path="/v1internal:generateContent",
            conversation_id="conv-abc",
        )

        gemini_cli(ctx, {})

        assert ctx._body["request"]["session_id"] != "client-supplied-old-id"
        uuid.UUID(ctx._body["request"]["session_id"])

    def test_pathological_request_value_does_not_raise(self) -> None:
        ctx = _make_ctx(
            body={"model": "gemini-3.1-pro-preview", "request": "not-a-dict"},
            path="/v1internal:generateContent",
            conversation_id="conv-abc",
        )

        gemini_cli(ctx, {})  # must not raise
        # No session_id injected because inner is not a dict
        assert ctx._body["request"] == "not-a-dict"

    def test_same_conversation_produces_same_session_id_across_calls(self) -> None:
        ctx_a = _make_ctx(body={"contents": []}, conversation_id="conv-shared")
        gemini_cli(ctx_a, {})
        sid_a = ctx_a._body["request"]["session_id"]

        ctx_b = _make_ctx(body={"contents": []}, conversation_id="conv-shared")
        gemini_cli(ctx_b, {})
        sid_b = ctx_b._body["request"]["session_id"]

        assert sid_a == sid_b

    def test_different_conversations_produce_different_session_ids(self) -> None:
        ctx_a = _make_ctx(body={"contents": []}, conversation_id="conv-one")
        gemini_cli(ctx_a, {})
        sid_a = ctx_a._body["request"]["session_id"]

        ctx_b = _make_ctx(body={"contents": []}, conversation_id="conv-two")
        gemini_cli(ctx_b, {})
        sid_b = ctx_b._body["request"]["session_id"]

        assert sid_a != sid_b

    def test_session_id_is_uuid_shaped(self) -> None:
        ctx = _make_ctx(body={"contents": []}, conversation_id="conv-abc")

        gemini_cli(ctx, {})

        sid = ctx._body["request"]["session_id"]
        # str(uuid.uuid5(...)) → "8-4-4-4-12 hex" canonical form
        parsed = uuid.UUID(sid)
        assert str(parsed) == sid


class TestPrewarmProject:
    def test_prewarm_caches_project(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"cloudaicompanionProject": "abc-xyz"}

        mock_config = MagicMock()
        mock_config.providers = {"gemini": object()}
        mock_config.resolve_oauth_token.return_value = "tok"

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
        mock_config.resolve_oauth_token.return_value = ""

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
        mock_config.resolve_oauth_token.return_value = "tok"

        with (
            patch("ccproxy.hooks.gemini_cli.get_config", return_value=mock_config),
            patch("httpx.post", return_value=mock_resp),
        ):
            prewarm_project()

        assert gemini_cli_module._cached_project is None
