"""Tests for compliance profile merge logic."""

import json
from unittest.mock import MagicMock

from ccproxy.compliance.merger import _extract_model_from_path, _wrap_body, merge_profile
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
        merge_profile(ctx, profile)
        assert ctx.get_header("x-app") == "cli"
        assert ctx.get_header("anthropic-beta") == "flag1,flag2"

    def test_does_not_overwrite_existing(self):
        ctx = _make_context(headers={"x-app": "sdk"})
        profile = _make_profile(headers=[
            ProfileFeatureHeader(name="x-app", value="cli"),
        ])
        merge_profile(ctx, profile)
        assert ctx.get_header("x-app") == "sdk"

    def test_no_headers_no_op(self):
        ctx = _make_context(headers={"existing": "val"})
        profile = _make_profile(headers=[])
        merge_profile(ctx, profile)
        assert ctx.get_header("existing") == "val"


class TestMergeBodyFields:
    def test_adds_missing_compliance_fields(self):
        ctx = _make_context(body={"model": "test"})
        profile = _make_profile(body_fields=[
            ProfileFeatureBodyField(path="some_envelope", value={"key": "val"}),
        ])
        merge_profile(ctx, profile)
        assert ctx._body["some_envelope"] == {"key": "val"}

    def test_does_not_overwrite_existing(self):
        ctx = _make_context(body={"some_envelope": {"key": "old"}})
        profile = _make_profile(body_fields=[
            ProfileFeatureBodyField(path="some_envelope", value={"key": "new"}),
        ])
        merge_profile(ctx, profile)
        assert ctx._body["some_envelope"] == {"key": "old"}

    def test_generates_user_prompt_id_when_missing(self):
        ctx = _make_context(body={"model": "test"})
        profile = _make_profile(body_fields=[
            ProfileFeatureBodyField(path="user_prompt_id", value="placeholder"),
        ])
        merge_profile(ctx, profile)
        generated = ctx._body.get("user_prompt_id")
        assert generated is not None
        assert len(generated) == 13  # uuid4 hex[:13]
        assert generated != "placeholder"  # should be a fresh random value

    def test_preserves_existing_user_prompt_id(self):
        ctx = _make_context(body={"model": "test", "user_prompt_id": "existing-id"})
        profile = _make_profile(body_fields=[
            ProfileFeatureBodyField(path="user_prompt_id", value="placeholder"),
        ])
        merge_profile(ctx, profile)
        assert ctx._body["user_prompt_id"] == "existing-id"

    def test_excludes_feature_config_fields(self):
        ctx = _make_context(body={"model": "test"})
        profile = _make_profile(body_fields=[
            ProfileFeatureBodyField(path="thinking", value={"type": "enabled"}),
            ProfileFeatureBodyField(path="context_management", value={"edits": []}),
            ProfileFeatureBodyField(path="output_config", value={"effort": "max"}),
            ProfileFeatureBodyField(path="metadata", value={"user_id": "test"}),
        ])
        merge_profile(ctx, profile)
        assert "thinking" not in ctx._body
        assert "context_management" not in ctx._body
        assert "output_config" not in ctx._body


class TestMergeSystem:
    def test_sets_system_when_none(self):
        ctx = _make_context(body={"model": "test"})
        profile = _make_profile(system=ProfileFeatureSystem(
            structure=[{"type": "text", "text": "You are Claude"}],
        ))
        merge_profile(ctx, profile)
        assert ctx.system == [{"type": "text", "text": "You are Claude"}]

    def test_wraps_string_system(self):
        ctx = _make_context(body={"system": "Be helpful"})
        profile = _make_profile(system=ProfileFeatureSystem(
            structure=[{"type": "text", "text": "You are Claude"}],
        ))
        merge_profile(ctx, profile)
        assert isinstance(ctx.system, list)
        assert len(ctx.system) == 2
        assert ctx.system[0] == {"type": "text", "text": "You are Claude"}
        assert ctx.system[1] == {"type": "text", "text": "Be helpful"}

    def test_skips_list_system(self):
        """List system blocks indicate a client that manages its own identity — skip injection."""
        ctx = _make_context(body={"system": [{"type": "text", "text": "User block"}]})
        profile = _make_profile(system=ProfileFeatureSystem(
            structure=[{"type": "text", "text": "You are Claude"}],
        ))
        merge_profile(ctx, profile)
        assert isinstance(ctx.system, list)
        assert len(ctx.system) == 1
        assert ctx.system[0]["text"] == "User block"

    def test_skips_list_system_with_existing_prefix(self):
        ctx = _make_context(body={"system": [
            {"type": "text", "text": "You are Claude"},
            {"type": "text", "text": "User block"},
        ]})
        profile = _make_profile(system=ProfileFeatureSystem(
            structure=[{"type": "text", "text": "You are Claude"}],
        ))
        merge_profile(ctx, profile)
        assert len(ctx.system) == 2

    def test_no_profile_system_no_op(self):
        ctx = _make_context(body={"system": "Original"})
        profile = _make_profile(system=None)
        merge_profile(ctx, profile)
        assert ctx.system == "Original"

    def test_empty_profile_structure_no_op(self):
        ctx = _make_context(body={"system": "Original"})
        profile = _make_profile(system=ProfileFeatureSystem(structure=[]))
        merge_profile(ctx, profile)
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
        merge_profile(ctx, profile)
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
        merge_profile(ctx, profile)
        assert ctx._body["metadata"]["user_id"] == "existing"

    def test_no_identity_fields_no_op(self):
        ctx = _make_context(body={"model": "test"})
        profile = _make_profile(body_fields=[
            ProfileFeatureBodyField(path="some_field", value="val"),
        ])
        merge_profile(ctx, profile)
        assert "metadata" not in ctx._body or "user_id" not in ctx._body.get("metadata", {})


class TestIdempotency:
    def test_double_apply_same_result(self):
        ctx = _make_context(body={"model": "test", "system": "Be helpful"})
        profile = _make_profile(
            headers=[ProfileFeatureHeader(name="x-app", value="cli")],
            system=ProfileFeatureSystem(structure=[{"type": "text", "text": "Prefix"}]),
            body_fields=[ProfileFeatureBodyField(path="some_env", value=True)],
        )
        merge_profile(ctx, profile)
        first_system = ctx.system
        first_body = dict(ctx._body)

        merge_profile(ctx, profile)
        assert ctx.system == first_system
        assert ctx._body["some_env"] == first_body["some_env"]
        assert ctx.get_header("x-app") == "cli"


class TestWrapBody:
    """Tests for the _wrap_body internal function."""

    def test_wraps_body_into_wrapper_field(self) -> None:
        """Body is moved into wrapper_field; model is hoisted to top-level."""
        ctx = _make_context(body={"model": "gemini-pro", "messages": [], "stream": False})
        profile = _make_profile(body_wrapper="request")

        _wrap_body(ctx, profile)

        assert "request" in ctx._body
        assert ctx._body["model"] == "gemini-pro"
        assert ctx._body["request"] == {"messages": [], "stream": False}

    def test_noop_when_no_body_wrapper(self) -> None:
        """Profile without body_wrapper leaves body unchanged."""
        original_body = {"model": "claude-3", "messages": []}
        ctx = _make_context(body=dict(original_body))
        profile = _make_profile(body_wrapper=None)

        _wrap_body(ctx, profile)

        assert ctx._body == original_body

    def test_idempotent_when_already_wrapped(self) -> None:
        """If wrapper_field already present in body, second call is a no-op."""
        ctx = _make_context(body={"model": "gemini-pro", "request": {"messages": []}})
        profile = _make_profile(body_wrapper="request")

        _wrap_body(ctx, profile)

        assert ctx._body["model"] == "gemini-pro"
        assert ctx._body["request"] == {"messages": []}

    def test_model_extracted_from_transform_meta_when_missing_from_body(self) -> None:
        """When body has no 'model', TransformMeta.model is used instead."""
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

        _wrap_body(ctx, profile)

        assert ctx._body["model"] == "gemini-2.5-flash"
        assert "request" in ctx._body

    def test_model_extracted_from_path_when_missing_from_body_and_transform(self) -> None:
        """When body and TransformMeta lack a model, path extraction is tried."""
        flow = MagicMock()
        flow.request.headers = {}
        flow.request.content = json.dumps({"messages": []}).encode()
        flow.request.path = "/v1beta/models/gemini-pro:generateContent"
        flow.metadata = {}
        ctx = Context.from_flow(flow)

        profile = _make_profile(body_wrapper="request")

        _wrap_body(ctx, profile)

        assert ctx._body.get("model") == "gemini-pro"
        assert "request" in ctx._body

    def test_wrap_body_without_model_still_wraps(self) -> None:
        """If no model can be found anywhere, body is still wrapped without model key."""
        flow = MagicMock()
        flow.request.headers = {}
        flow.request.content = json.dumps({"messages": []}).encode()
        flow.request.path = "/v1/no-model-in-path"
        flow.metadata = {}
        ctx = Context.from_flow(flow)

        profile = _make_profile(body_wrapper="request")

        _wrap_body(ctx, profile)

        assert "model" not in ctx._body
        assert ctx._body["request"] == {"messages": []}

    def test_wrap_body_with_model_from_body_and_transform_prefers_body(self) -> None:
        """Body model takes priority over TransformMeta model."""
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

        _wrap_body(ctx, profile)

        assert ctx._body["model"] == "explicit-model"
        assert ctx._body["request"] == {"messages": []}


class TestExtractModelFromPath:
    """Tests for the _extract_model_from_path internal function."""

    def test_extracts_model_from_standard_models_path(self) -> None:
        """/models/gemini-pro:generateContent → 'gemini-pro'."""
        flow = MagicMock()
        flow.request.path = "/v1beta/models/gemini-pro:generateContent"
        ctx = MagicMock()
        ctx.flow = flow

        result = _extract_model_from_path(ctx)
        assert result == "gemini-pro"

    def test_extracts_model_from_path_without_method_suffix(self) -> None:
        """/models/gemini-2.5-flash (no colon suffix) → 'gemini-2.5-flash'."""
        flow = MagicMock()
        flow.request.path = "/v1/models/gemini-2.5-flash"
        ctx = MagicMock()
        ctx.flow = flow

        result = _extract_model_from_path(ctx)
        assert result == "gemini-2.5-flash"

    def test_returns_none_when_no_models_segment(self) -> None:
        """Path with no /models/ segment returns None."""
        flow = MagicMock()
        flow.request.path = "/v1/messages"
        ctx = MagicMock()
        ctx.flow = flow

        result = _extract_model_from_path(ctx)
        assert result is None

    def test_returns_none_for_root_path(self) -> None:
        """Root path returns None."""
        flow = MagicMock()
        flow.request.path = "/"
        ctx = MagicMock()
        ctx.flow = flow

        result = _extract_model_from_path(ctx)
        assert result is None

    def test_extracts_model_with_version_prefix_in_name(self) -> None:
        """/models/gemini-1.5-pro:streamGenerateContent → 'gemini-1.5-pro'."""
        flow = MagicMock()
        flow.request.path = "/v1/models/gemini-1.5-pro:streamGenerateContent"
        ctx = MagicMock()
        ctx.flow = flow

        result = _extract_model_from_path(ctx)
        assert result == "gemini-1.5-pro"

    def test_extracts_first_models_segment_in_complex_path(self) -> None:
        """When /models/ appears deep in path, first match is returned."""
        flow = MagicMock()
        flow.request.path = "/projects/my-project/locations/us-central1/models/gemini-pro:predict"
        ctx = MagicMock()
        ctx.flow = flow

        result = _extract_model_from_path(ctx)
        assert result == "gemini-pro"
