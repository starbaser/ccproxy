"""Tests for extract_session_id hook."""

from __future__ import annotations

import json

from ccproxy.hooks.extract_session_id import extract_session_id, _inject_langfuse_headers
from ccproxy.pipeline.context import Context


def _make_ctx(body_metadata: dict | None = None, headers: dict | None = None) -> Context:
    metadata = body_metadata or {}
    data: dict = {
        "model": "anthropic/claude-sonnet-4-5-20250929",
        "messages": [],
        "metadata": {},
        "proxy_server_request": {
            "headers": headers or {},
            "body": {"metadata": metadata} if metadata else {},
        },
    }
    return Context.from_litellm_data(data)


class TestExtractSessionIdHook:
    def test_json_user_id_extracts_session(self):
        user_id = json.dumps({"device_id": "dev1", "account_uuid": "acc1", "session_id": "sess-abc"})
        ctx = _make_ctx(body_metadata={"user_id": user_id})
        result = extract_session_id(ctx, {})
        assert result.metadata["session_id"] == "sess-abc"

    def test_json_user_id_sets_trace_user_id(self):
        user_id = json.dumps({"device_id": "dev1", "account_uuid": "acc-uuid", "session_id": "s1"})
        ctx = _make_ctx(body_metadata={"user_id": user_id})
        result = extract_session_id(ctx, {})
        assert result.metadata["trace_user_id"] == "acc-uuid"

    def test_json_user_id_sets_trace_metadata(self):
        user_id = json.dumps({"device_id": "dev-xyz", "account_uuid": "acc-uuid", "session_id": "s1"})
        ctx = _make_ctx(body_metadata={"user_id": user_id})
        result = extract_session_id(ctx, {})
        tm = result.metadata.get("trace_metadata", {})
        assert tm.get("claude_device_id") == "dev-xyz"
        assert tm.get("claude_account_id") == "acc-uuid"

    def test_legacy_user_id_extracts_session(self):
        user_id = "user_hash123_account_acc456_session_sess789"
        ctx = _make_ctx(body_metadata={"user_id": user_id})
        result = extract_session_id(ctx, {})
        assert result.metadata["session_id"] == "sess789"

    def test_legacy_user_id_sets_trace_user_id(self):
        user_id = "user_hashval_account_accval_session_sessval"
        ctx = _make_ctx(body_metadata={"user_id": user_id})
        result = extract_session_id(ctx, {})
        assert result.metadata["trace_user_id"] == "hashval"

    def test_legacy_user_id_sets_trace_metadata(self):
        user_id = "user_hashval_account_accval_session_sessval"
        ctx = _make_ctx(body_metadata={"user_id": user_id})
        result = extract_session_id(ctx, {})
        assert result.metadata.get("trace_metadata", {}).get("claude_account_id") == "accval"

    def test_no_user_id_does_not_set_session(self):
        ctx = _make_ctx(body_metadata={"other_key": "value"})
        result = extract_session_id(ctx, {})
        assert "session_id" not in result.metadata

    def test_body_metadata_forwarded_to_ctx_metadata(self):
        ctx = _make_ctx(body_metadata={"session_id": "client-sid", "trace_name": "my-trace"})
        result = extract_session_id(ctx, {})
        assert result.metadata.get("trace_name") == "my-trace"

    def test_ccproxy_keys_not_overwritten(self):
        ctx = _make_ctx(body_metadata={"ccproxy_foo": "should-be-ignored"})
        result = extract_session_id(ctx, {})
        assert result.metadata.get("ccproxy_foo") is None

    def test_existing_ctx_key_not_overwritten(self):
        data: dict = {
            "model": "test",
            "messages": [],
            "metadata": {"session_id": "existing"},
            "proxy_server_request": {
                "headers": {},
                "body": {"metadata": {"session_id": "new-value"}},
            },
        }
        ctx = Context.from_litellm_data(data)
        result = extract_session_id(ctx, {})
        assert result.metadata["session_id"] == "existing"

    def test_non_dict_body_returns_early(self):
        data: dict = {
            "model": "test",
            "messages": [],
            "metadata": {},
            "proxy_server_request": {
                "headers": {},
                "body": "not-a-dict",
            },
        }
        ctx = Context.from_litellm_data(data)
        result = extract_session_id(ctx, {})
        assert "session_id" not in result.metadata

    def test_no_proxy_server_request_guard(self):
        data: dict = {
            "model": "test",
            "messages": [],
            "metadata": {},
        }
        ctx = Context.from_litellm_data(data)
        from ccproxy.hooks.extract_session_id import extract_session_id_guard
        assert extract_session_id_guard(ctx) is False

    def test_proxy_server_request_present_guard(self):
        ctx = _make_ctx()
        from ccproxy.hooks.extract_session_id import extract_session_id_guard
        assert extract_session_id_guard(ctx) is True


class TestInjectLangfuseHeaders:
    def test_injects_session_id_header(self):
        request: dict = {"headers": {}}
        metadata = {"session_id": "sess-123"}
        _inject_langfuse_headers(request, metadata)
        assert request["headers"]["langfuse_session_id"] == "sess-123"

    def test_skips_non_string_values(self):
        request: dict = {"headers": {}}
        metadata = {"session_id": 12345}
        _inject_langfuse_headers(request, metadata)
        assert "langfuse_session_id" not in request["headers"]

    def test_does_not_overwrite_existing_header(self):
        request: dict = {"headers": {"langfuse_session_id": "existing"}}
        metadata = {"session_id": "new"}
        _inject_langfuse_headers(request, metadata)
        assert request["headers"]["langfuse_session_id"] == "existing"

    def test_non_dict_headers_is_noop(self):
        request: dict = {"headers": None}
        metadata = {"session_id": "sess"}
        _inject_langfuse_headers(request, metadata)
        # Should not raise

    def test_injects_trace_name(self):
        request: dict = {"headers": {}}
        metadata = {"trace_name": "my-trace"}
        _inject_langfuse_headers(request, metadata)
        assert request["headers"]["langfuse_trace_name"] == "my-trace"

    def test_json_user_id_no_account_uuid(self):
        """JSON user_id without account_uuid should not set trace_user_id."""
        user_id = json.dumps({"device_id": "dev1", "session_id": "s1"})
        ctx = _make_ctx(body_metadata={"user_id": user_id})
        result = extract_session_id(ctx, {})
        assert "trace_user_id" not in result.metadata

    def test_json_user_id_no_device_id(self):
        """JSON user_id without device_id should not set claude_device_id."""
        user_id = json.dumps({"account_uuid": "acc1", "session_id": "s1"})
        ctx = _make_ctx(body_metadata={"user_id": user_id})
        result = extract_session_id(ctx, {})
        assert result.metadata.get("trace_metadata", {}).get("claude_device_id") is None
