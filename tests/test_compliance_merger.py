"""Tests for compliance profile merge logic."""

import json
from unittest.mock import MagicMock

import pytest

from ccproxy.compliance.merger import ComplianceMerger, resolve_merger_class
from ccproxy.compliance.models import (
    ComplianceProfile,
    ProfileFeatureBodyField,
    ProfileFeatureHeader,
    ProfileFeatureSystem,
)
from ccproxy.inspector.flow_store import FlowRecord, InspectorMeta, TransformMeta
from ccproxy.pipeline.context import Context


def _make_context(
    headers: dict[str, str] | None = None,
    body: dict | None = None,
) -> Context:
    flow = MagicMock()
    flow.request.headers = dict(headers or {})
    flow.request.content = json.dumps(body or {}).encode()
    return Context.from_flow(flow)


def _make_profile(**kwargs) -> ComplianceProfile:
    defaults = {
        "provider": "anthropic",
        "user_agent": "cli/1.0",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "observation_count": 3,
        "is_complete": True,
        "headers": [],
        "body_fields": [],
        "system": None,
    }
    defaults.update(kwargs)
    return ComplianceProfile(**defaults)


class TestMergeHeaders:
    def test_adds_missing_headers(self):
        ctx = _make_context()
        profile = _make_profile(headers=[
            ProfileFeatureHeader(name="x-app", value="cli"),
            ProfileFeatureHeader(name="anthropic-beta", value="flag1,flag2"),
        ])
        ComplianceMerger(ctx, profile).merge()
        assert ctx.get_header("x-app") == "cli"
        assert ctx.get_header("anthropic-beta") == "flag1,flag2"

    def test_does_not_overwrite_existing(self):
        ctx = _make_context(headers={"x-app": "sdk"})
        profile = _make_profile(headers=[
            ProfileFeatureHeader(name="x-app", value="cli"),
        ])
        ComplianceMerger(ctx, profile).merge()
        assert ctx.get_header("x-app") == "sdk"

    def test_no_headers_no_op(self):
        ctx = _make_context(headers={"existing": "val"})
        profile = _make_profile(headers=[])
        ComplianceMerger(ctx, profile).merge()
        assert ctx.get_header("existing") == "val"

    def test_unions_anthropic_beta_tokens(self):
        ctx = _make_context(headers={"anthropic-beta": "oauth-2025-04-20"})
        profile = _make_profile(headers=[
            ProfileFeatureHeader(
                name="anthropic-beta",
                value="oauth-2025-04-20,claude-code-20250219,interleaved-thinking-2025-05-14",
            ),
        ])
        ComplianceMerger(ctx, profile).merge()
        assert ctx.get_header("anthropic-beta") == (
            "oauth-2025-04-20,claude-code-20250219,interleaved-thinking-2025-05-14"
        )

    def test_union_preserves_existing_order(self):
        ctx = _make_context(headers={"anthropic-beta": "custom-flag,oauth-2025-04-20"})
        profile = _make_profile(headers=[
            ProfileFeatureHeader(
                name="anthropic-beta",
                value="oauth-2025-04-20,claude-code-20250219",
            ),
        ])
        ComplianceMerger(ctx, profile).merge()
        tokens = ctx.get_header("anthropic-beta").split(",")
        assert tokens == ["custom-flag", "oauth-2025-04-20", "claude-code-20250219"]

    def test_union_idempotent_when_already_complete(self):
        full = "oauth-2025-04-20,claude-code-20250219,interleaved-thinking-2025-05-14"
        ctx = _make_context(headers={"anthropic-beta": full})
        profile = _make_profile(headers=[
            ProfileFeatureHeader(name="anthropic-beta", value=full),
        ])
        ComplianceMerger(ctx, profile).merge()
        assert ctx.get_header("anthropic-beta") == full

    def test_non_list_header_still_strict(self):
        ctx = _make_context(headers={"anthropic-version": "2024-99-99"})
        profile = _make_profile(headers=[
            ProfileFeatureHeader(name="anthropic-version", value="2023-06-01"),
        ])
        ComplianceMerger(ctx, profile).merge()
        assert ctx.get_header("anthropic-version") == "2024-99-99"

    def test_union_handles_whitespace_in_csv(self):
        ctx = _make_context(headers={"anthropic-beta": "oauth-2025-04-20, custom-flag"})
        profile = _make_profile(headers=[
            ProfileFeatureHeader(name="anthropic-beta", value="claude-code-20250219"),
        ])
        ComplianceMerger(ctx, profile).merge()
        tokens = ctx.get_header("anthropic-beta").split(",")
        assert tokens == ["oauth-2025-04-20", "custom-flag", "claude-code-20250219"]


class TestMergeBodyFields:
    def test_adds_missing_compliance_fields(self):
        ctx = _make_context(body={"model": "test"})
        profile = _make_profile(body_fields=[
            ProfileFeatureBodyField(path="some_envelope", value={"key": "val"}),
        ])
        ComplianceMerger(ctx, profile).merge()
        assert ctx._body["some_envelope"] == {"key": "val"}

    def test_does_not_overwrite_existing(self):
        ctx = _make_context(body={"some_envelope": {"key": "old"}})
        profile = _make_profile(body_fields=[
            ProfileFeatureBodyField(path="some_envelope", value={"key": "new"}),
        ])
        ComplianceMerger(ctx, profile).merge()
        assert ctx._body["some_envelope"] == {"key": "old"}

    def test_generates_user_prompt_id_when_missing(self):
        ctx = _make_context(body={"model": "test"})
        profile = _make_profile(body_fields=[
            ProfileFeatureBodyField(path="user_prompt_id", value="placeholder"),
        ])
        ComplianceMerger(ctx, profile).merge()
        generated = ctx._body.get("user_prompt_id")
        assert generated is not None
        assert len(generated) == 13  # uuid4 hex[:13]
        assert generated != "placeholder"

    def test_preserves_existing_user_prompt_id(self):
        ctx = _make_context(body={"model": "test", "user_prompt_id": "existing-id"})
        profile = _make_profile(body_fields=[
            ProfileFeatureBodyField(path="user_prompt_id", value="placeholder"),
        ])
        ComplianceMerger(ctx, profile).merge()
        assert ctx._body["user_prompt_id"] == "existing-id"

    def test_excludes_feature_config_fields(self):
        ctx = _make_context(body={"model": "test"})
        profile = _make_profile(body_fields=[
            ProfileFeatureBodyField(path="thinking", value={"type": "enabled"}),
            ProfileFeatureBodyField(path="context_management", value={"edits": []}),
            ProfileFeatureBodyField(path="output_config", value={"effort": "max"}),
            ProfileFeatureBodyField(path="metadata", value={"user_id": "test"}),
        ])
        ComplianceMerger(ctx, profile).merge()
        assert "thinking" not in ctx._body
        assert "context_management" not in ctx._body
        assert "output_config" not in ctx._body


class TestMergeSystem:
    def test_sets_system_when_none(self):
        ctx = _make_context(body={"model": "test"})
        profile = _make_profile(system=ProfileFeatureSystem(
            structure=[{"type": "text", "text": "You are Claude"}],
        ))
        ComplianceMerger(ctx, profile).merge()
        assert ctx.system == [{"type": "text", "text": "You are Claude"}]

    def test_wraps_string_system(self):
        ctx = _make_context(body={"system": "Be helpful"})
        profile = _make_profile(system=ProfileFeatureSystem(
            structure=[{"type": "text", "text": "You are Claude"}],
        ))
        ComplianceMerger(ctx, profile).merge()
        assert isinstance(ctx.system, list)
        assert len(ctx.system) == 2
        assert ctx.system[0] == {"type": "text", "text": "You are Claude"}
        assert ctx.system[1] == {"type": "text", "text": "Be helpful"}

    def test_prepends_to_list_without_profile_prefix(self):
        ctx = _make_context(body={"system": [
            {"type": "text", "text": "User block"},
        ]})
        profile = _make_profile(system=ProfileFeatureSystem(
            structure=[{"type": "text", "text": "You are Claude"}],
        ))
        ComplianceMerger(ctx, profile).merge()
        assert ctx.system == [
            {"type": "text", "text": "You are Claude"},
            {"type": "text", "text": "User block"},
        ]

    def test_skips_list_system_with_existing_prefix(self):
        ctx = _make_context(body={"system": [
            {"type": "text", "text": "You are Claude"},
            {"type": "text", "text": "User block"},
        ]})
        profile = _make_profile(system=ProfileFeatureSystem(
            structure=[{"type": "text", "text": "You are Claude"}],
        ))
        ComplianceMerger(ctx, profile).merge()
        assert len(ctx.system) == 2
        assert ctx.system[0]["text"] == "You are Claude"
        assert ctx.system[1]["text"] == "User block"

    def test_prepends_preserves_cache_control(self):
        ctx = _make_context(body={"system": [
            {"type": "text", "text": "Dictation prompt",
             "cache_control": {"type": "ephemeral"}},
        ]})
        profile = _make_profile(system=ProfileFeatureSystem(
            structure=[{"type": "text", "text": "You are Claude Code"}],
        ))
        ComplianceMerger(ctx, profile).merge()
        assert ctx.system[0] == {"type": "text", "text": "You are Claude Code"}
        assert ctx.system[1]["text"] == "Dictation prompt"
        assert ctx.system[1]["cache_control"] == {"type": "ephemeral"}

    def test_list_merge_idempotent(self):
        ctx = _make_context(body={"system": [
            {"type": "text", "text": "User block"},
        ]})
        profile = _make_profile(system=ProfileFeatureSystem(
            structure=[{"type": "text", "text": "You are Claude"}],
        ))
        ComplianceMerger(ctx, profile).merge()
        snapshot = list(ctx.system)
        ComplianceMerger(ctx, profile).merge()
        assert ctx.system == snapshot

    def test_prefix_match_detects_appended_content(self):
        ctx = _make_context(body={"system": [
            {"type": "text", "text":
             "You are Claude Code, Anthropic's official CLI for Claude.\n\nProject: foo"},
        ]})
        profile = _make_profile(system=ProfileFeatureSystem(
            structure=[{"type": "text", "text":
                        "You are Claude Code, Anthropic's official CLI for Claude."}],
        ))
        ComplianceMerger(ctx, profile).merge()
        assert len(ctx.system) == 1

    def test_multi_block_profile_prepends_all(self):
        ctx = _make_context(body={"system": [
            {"type": "text", "text": "User content"},
        ]})
        profile = _make_profile(system=ProfileFeatureSystem(structure=[
            {"type": "text", "text": "You are Claude Code"},
            {"type": "text", "text": "Second system block"},
        ]))
        ComplianceMerger(ctx, profile).merge()
        assert len(ctx.system) == 3
        assert ctx.system[0]["text"] == "You are Claude Code"
        assert ctx.system[1]["text"] == "Second system block"
        assert ctx.system[2]["text"] == "User content"

    def test_skips_profile_blocks_without_text(self):
        ctx = _make_context(body={"system": [
            {"type": "text", "text": "User block"},
        ]})
        profile = _make_profile(system=ProfileFeatureSystem(structure=[
            {"type": "image", "source": "ignored"},
            {"type": "text", "text": ""},
            {"type": "text", "text": "You are Claude"},
        ]))
        ComplianceMerger(ctx, profile).merge()
        assert len(ctx.system) == 4
        assert ctx.system[0]["type"] == "image"
        assert ctx.system[1]["text"] == ""
        assert ctx.system[2]["text"] == "You are Claude"
        assert ctx.system[3]["text"] == "User block"

    def test_no_profile_system_no_op(self):
        ctx = _make_context(body={"system": "Original"})
        profile = _make_profile(system=None)
        ComplianceMerger(ctx, profile).merge()
        assert ctx.system == "Original"

    def test_empty_profile_structure_no_op(self):
        ctx = _make_context(body={"system": "Original"})
        profile = _make_profile(system=ProfileFeatureSystem(structure=[]))
        ComplianceMerger(ctx, profile).merge()
        assert ctx.system == "Original"


class TestMergeSessionMetadata:
    def test_synthesizes_session_from_profile(self):
        ctx = _make_context(body={"model": "test"})
        profile = _make_profile(body_fields=[
            ProfileFeatureBodyField(
                path="metadata",
                value={"user_id": json.dumps({"device_id": "dev123", "account_uuid": "acc456"})},
            ),
        ])
        ComplianceMerger(ctx, profile).merge()
        metadata = ctx._body.get("metadata", {})
        assert "user_id" in metadata
        uid = json.loads(metadata["user_id"])
        assert uid["device_id"] == "dev123"
        assert uid["account_uuid"] == "acc456"
        assert "session_id" in uid

    def test_does_not_overwrite_existing_user_id(self):
        ctx = _make_context(body={"metadata": {"user_id": "existing"}})
        profile = _make_profile(body_fields=[
            ProfileFeatureBodyField(
                path="metadata",
                value={"user_id": json.dumps({"device_id": "dev123"})},
            ),
        ])
        ComplianceMerger(ctx, profile).merge()
        assert ctx._body["metadata"]["user_id"] == "existing"

    def test_no_identity_fields_no_op(self):
        ctx = _make_context(body={"model": "test"})
        profile = _make_profile(body_fields=[
            ProfileFeatureBodyField(path="some_field", value="val"),
        ])
        ComplianceMerger(ctx, profile).merge()
        assert "metadata" not in ctx._body or "user_id" not in ctx._body.get("metadata", {})


class TestIdempotency:
    def test_double_apply_same_result(self):
        ctx = _make_context(body={"model": "test", "system": "Be helpful"})
        profile = _make_profile(
            headers=[ProfileFeatureHeader(name="x-app", value="cli")],
            system=ProfileFeatureSystem(structure=[{"type": "text", "text": "Prefix"}]),
            body_fields=[ProfileFeatureBodyField(path="some_env", value=True)],
        )
        ComplianceMerger(ctx, profile).merge()
        first_system = ctx.system
        first_body = dict(ctx._body)

        ComplianceMerger(ctx, profile).merge()
        assert ctx.system == first_system
        assert ctx._body["some_env"] == first_body["some_env"]
        assert ctx.get_header("x-app") == "cli"

    def test_double_apply_list_system_and_list_valued_header(self):
        ctx = _make_context(
            headers={"anthropic-beta": "oauth-2025-04-20"},
            body={"system": [{"type": "text", "text": "User block"}]},
        )
        profile = _make_profile(
            headers=[ProfileFeatureHeader(
                name="anthropic-beta",
                value="oauth-2025-04-20,claude-code-20250219",
            )],
            system=ProfileFeatureSystem(
                structure=[{"type": "text", "text": "You are Claude"}],
            ),
        )
        ComplianceMerger(ctx, profile).merge()
        first_system = list(ctx.system)
        first_beta = ctx.get_header("anthropic-beta")

        ComplianceMerger(ctx, profile).merge()
        assert ctx.system == first_system
        assert ctx.get_header("anthropic-beta") == first_beta
        assert first_beta == "oauth-2025-04-20,claude-code-20250219"
        assert first_system[0]["text"] == "You are Claude"
        assert first_system[1]["text"] == "User block"


class TestWrapBody:
    def test_wraps_body_into_wrapper_field(self) -> None:
        ctx = _make_context(body={"model": "gemini-pro", "messages": [], "stream": False})
        profile = _make_profile(body_wrapper="request")

        ComplianceMerger(ctx, profile).wrap_body()

        assert "request" in ctx._body
        assert ctx._body["model"] == "gemini-pro"
        assert ctx._body["request"] == {"messages": [], "stream": False}

    def test_noop_when_no_body_wrapper(self) -> None:
        original_body = {"model": "claude-3", "messages": []}
        ctx = _make_context(body=dict(original_body))
        profile = _make_profile(body_wrapper=None)

        ComplianceMerger(ctx, profile).wrap_body()

        assert ctx._body == original_body

    def test_idempotent_when_already_wrapped(self) -> None:
        ctx = _make_context(body={"model": "gemini-pro", "request": {"messages": []}})
        profile = _make_profile(body_wrapper="request")

        ComplianceMerger(ctx, profile).wrap_body()

        assert ctx._body["model"] == "gemini-pro"
        assert ctx._body["request"] == {"messages": []}

    def test_model_extracted_from_transform_meta_when_missing_from_body(self) -> None:
        record = FlowRecord(direction="inbound")
        record.transform = TransformMeta(
            provider="gemini",
            model="gemini-2.5-flash",
            request_data={},
            is_streaming=False,
        )

        flow = MagicMock()
        flow.request.headers = {}
        flow.request.content = json.dumps({"messages": []}).encode()
        flow.metadata = {InspectorMeta.RECORD: record}
        ctx = Context.from_flow(flow)

        profile = _make_profile(body_wrapper="request")

        ComplianceMerger(ctx, profile).wrap_body()

        assert ctx._body["model"] == "gemini-2.5-flash"
        assert "request" in ctx._body

    def test_model_extracted_from_path_when_missing_from_body_and_transform(self) -> None:
        flow = MagicMock()
        flow.request.headers = {}
        flow.request.content = json.dumps({"messages": []}).encode()
        flow.request.path = "/v1beta/models/gemini-pro:generateContent"
        flow.metadata = {}
        ctx = Context.from_flow(flow)

        profile = _make_profile(body_wrapper="request")

        ComplianceMerger(ctx, profile).wrap_body()

        assert ctx._body.get("model") == "gemini-pro"
        assert "request" in ctx._body

    def test_wrap_body_without_model_still_wraps(self) -> None:
        flow = MagicMock()
        flow.request.headers = {}
        flow.request.content = json.dumps({"messages": []}).encode()
        flow.request.path = "/v1/no-model-in-path"
        flow.metadata = {}
        ctx = Context.from_flow(flow)

        profile = _make_profile(body_wrapper="request")

        ComplianceMerger(ctx, profile).wrap_body()

        assert "model" not in ctx._body
        assert ctx._body["request"] == {"messages": []}

    def test_wrap_body_with_model_from_body_and_transform_prefers_body(self) -> None:
        record = FlowRecord(direction="inbound")
        record.transform = TransformMeta(
            provider="gemini",
            model="gemini-2.5-flash",
            request_data={},
            is_streaming=False,
        )

        flow = MagicMock()
        flow.request.headers = {}
        flow.request.content = json.dumps({"model": "explicit-model", "messages": []}).encode()
        flow.metadata = {InspectorMeta.RECORD: record}
        ctx = Context.from_flow(flow)

        profile = _make_profile(body_wrapper="request")

        ComplianceMerger(ctx, profile).wrap_body()

        assert ctx._body["model"] == "explicit-model"
        assert ctx._body["request"] == {"messages": []}


class TestExtractModelFromPath:
    def _extract(self, path: str) -> str | None:
        flow = MagicMock()
        flow.request.path = path
        ctx = MagicMock()
        ctx.flow = flow
        return ComplianceMerger(ctx, _make_profile())._extract_model_from_path()

    def test_extracts_model_from_standard_models_path(self) -> None:
        assert self._extract("/v1beta/models/gemini-pro:generateContent") == "gemini-pro"

    def test_extracts_model_from_path_without_method_suffix(self) -> None:
        assert self._extract("/v1/models/gemini-2.5-flash") == "gemini-2.5-flash"

    def test_returns_none_when_no_models_segment(self) -> None:
        assert self._extract("/v1/messages") is None

    def test_returns_none_for_root_path(self) -> None:
        assert self._extract("/") is None

    def test_extracts_model_with_version_prefix_in_name(self) -> None:
        assert self._extract("/v1/models/gemini-1.5-pro:streamGenerateContent") == "gemini-1.5-pro"

    def test_extracts_first_models_segment_in_complex_path(self) -> None:
        assert self._extract(
            "/projects/my-project/locations/us-central1/models/gemini-pro:predict"
        ) == "gemini-pro"


class TestSubclass:
    def test_override_skips_operation(self):
        class SkipHeaders(ComplianceMerger):
            def merge_headers(self):
                pass

        ctx = _make_context()
        profile = _make_profile(
            headers=[ProfileFeatureHeader(name="x-app", value="cli")],
            system=ProfileFeatureSystem(structure=[{"type": "text", "text": "You are Claude"}]),
        )
        SkipHeaders(ctx, profile).merge()
        assert ctx.get_header("x-app") == ""
        assert ctx.system == [{"type": "text", "text": "You are Claude"}]

    def test_override_extends_with_super(self):
        class ExtendedHeaders(ComplianceMerger):
            def merge_headers(self):
                super().merge_headers()
                self.ctx.set_header("x-custom", "injected")

        ctx = _make_context()
        profile = _make_profile(headers=[ProfileFeatureHeader(name="x-app", value="cli")])
        ExtendedHeaders(ctx, profile).merge()
        assert ctx.get_header("x-app") == "cli"
        assert ctx.get_header("x-custom") == "injected"

    def test_override_merge_reorders_operations(self):
        call_order = []

        class ReorderedMerger(ComplianceMerger):
            def merge(self):
                self.merge_system()
                self.merge_headers()

            def merge_headers(self):
                call_order.append("headers")
                super().merge_headers()

            def merge_system(self):
                call_order.append("system")
                super().merge_system()

        ctx = _make_context(body={"model": "test"})
        profile = _make_profile(
            headers=[ProfileFeatureHeader(name="x-app", value="cli")],
            system=ProfileFeatureSystem(structure=[{"type": "text", "text": "Prefix"}]),
        )
        ReorderedMerger(ctx, profile).merge()
        assert call_order == ["system", "headers"]
        assert ctx.get_header("x-app") == "cli"
        assert ctx.system == [{"type": "text", "text": "Prefix"}]


class TestResolveMergerClass:
    def test_resolves_default_class(self):
        cls = resolve_merger_class("ccproxy.compliance.merger.ComplianceMerger")
        assert cls is ComplianceMerger

    def test_rejects_non_subclass(self):
        with pytest.raises(TypeError, match="not a ComplianceMerger subclass"):
            resolve_merger_class("builtins.dict")

    def test_rejects_nonexistent_module(self):
        with pytest.raises(ModuleNotFoundError):
            resolve_merger_class("nonexistent.module.Foo")

    def test_rejects_nonexistent_attr(self):
        with pytest.raises(AttributeError):
            resolve_merger_class("ccproxy.compliance.merger.NoSuchClass")
