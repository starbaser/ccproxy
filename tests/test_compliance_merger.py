"""Tests for compliance profile merge logic."""

import json
from unittest.mock import MagicMock

from ccproxy.compliance.merger import merge_profile
from ccproxy.compliance.models import (
    ComplianceProfile,
    ProfileFeatureBodyField,
    ProfileFeatureHeader,
    ProfileFeatureSystem,
)
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
    def test_adds_missing_fields(self):
        ctx = _make_context(body={"model": "test"})
        profile = _make_profile(body_fields=[
            ProfileFeatureBodyField(path="thinking", value={"type": "enabled"}),
        ])
        merge_profile(ctx, profile)
        assert ctx._body["thinking"] == {"type": "enabled"}

    def test_does_not_overwrite_existing(self):
        ctx = _make_context(body={"thinking": {"type": "disabled"}})
        profile = _make_profile(body_fields=[
            ProfileFeatureBodyField(path="thinking", value={"type": "enabled"}),
        ])
        merge_profile(ctx, profile)
        assert ctx._body["thinking"] == {"type": "disabled"}


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

    def test_prepends_to_list_system(self):
        ctx = _make_context(body={"system": [{"type": "text", "text": "User block"}]})
        profile = _make_profile(system=ProfileFeatureSystem(
            structure=[{"type": "text", "text": "You are Claude"}],
        ))
        merge_profile(ctx, profile)
        assert isinstance(ctx.system, list)
        assert len(ctx.system) == 2
        assert ctx.system[0]["text"] == "You are Claude"
        assert ctx.system[1]["text"] == "User block"

    def test_idempotent_already_has_prefix(self):
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
            ProfileFeatureBodyField(path="thinking", value={"type": "enabled"}),
        ])
        merge_profile(ctx, profile)
        assert "metadata" not in ctx._body or "user_id" not in ctx._body.get("metadata", {})


class TestIdempotency:
    def test_double_apply_same_result(self):
        ctx = _make_context(body={"model": "test", "system": "Be helpful"})
        profile = _make_profile(
            headers=[ProfileFeatureHeader(name="x-app", value="cli")],
            system=ProfileFeatureSystem(structure=[{"type": "text", "text": "Prefix"}]),
            body_fields=[ProfileFeatureBodyField(path="thinking", value=True)],
        )
        merge_profile(ctx, profile)
        first_system = ctx.system
        first_body = dict(ctx._body)

        merge_profile(ctx, profile)
        assert ctx.system == first_system
        assert ctx._body["thinking"] == first_body["thinking"]
        assert ctx.get_header("x-app") == "cli"
