"""Tests for dynamic shaping hooks."""

from __future__ import annotations

import json
import uuid
from typing import Any

from mitmproxy import http

from ccproxy.pipeline.context import Context
from ccproxy.shaping.callbacks import regenerate_session_id, regenerate_user_prompt_id


def _shape_ctx(body: dict[str, Any] | None = None) -> Context:
    req = http.Request.make(
        "POST",
        "https://seed.example/",
        json.dumps(body or {}).encode(),
        {},
    )
    return Context.from_request(req)


class TestRegenerateUserPromptId:
    def test_regenerates_when_present(self) -> None:
        shape = _shape_ctx({"user_prompt_id": "old-id"})
        shape = regenerate_user_prompt_id(shape, {})
        new_id = shape._body["user_prompt_id"]
        assert new_id != "old-id"
        assert len(new_id) == 13

    def test_absent_key_untouched(self) -> None:
        shape = _shape_ctx({"other": "v"})
        shape = regenerate_user_prompt_id(shape, {})
        assert "user_prompt_id" not in shape._body


class TestRegenerateSessionId:
    def test_regenerates_session_id(self) -> None:
        identity = json.dumps({"device_id": "dev", "session_id": "old"})
        shape = _shape_ctx({"metadata": {"user_id": identity}})
        shape = regenerate_session_id(shape, {})
        new_identity = json.loads(shape._body["metadata"]["user_id"])
        assert new_identity["device_id"] == "dev"
        assert new_identity["session_id"] != "old"
        uuid.UUID(new_identity["session_id"])

    def test_no_identity_untouched(self) -> None:
        shape = _shape_ctx({"metadata": {"other": "v"}})
        shape = regenerate_session_id(shape, {})
        assert shape._body["metadata"] == {"other": "v"}

    def test_no_metadata_untouched(self) -> None:
        shape = _shape_ctx({"model": "x"})
        shape = regenerate_session_id(shape, {})
        assert shape._body == {"model": "x"}

    def test_non_json_user_id_untouched(self) -> None:
        shape = _shape_ctx({"metadata": {"user_id": "not-json"}})
        shape = regenerate_session_id(shape, {})
        assert shape._body["metadata"]["user_id"] == "not-json"

    def test_skips_when_no_identity_fields(self) -> None:
        identity = json.dumps({"other": "value"})
        shape = _shape_ctx({"metadata": {"user_id": identity}})
        shape = regenerate_session_id(shape, {})
        result_identity = json.loads(shape._body["metadata"]["user_id"])
        assert "session_id" not in result_identity

    def test_non_dict_identity_untouched(self) -> None:
        identity = json.dumps([1, 2, 3])
        shape = _shape_ctx({"metadata": {"user_id": identity}})
        shape = regenerate_session_id(shape, {})
        assert shape._body["metadata"]["user_id"] == identity

    def test_non_string_user_id_untouched(self) -> None:
        shape = _shape_ctx({"metadata": {"user_id": 1234}})
        shape = regenerate_session_id(shape, {})
        assert shape._body["metadata"]["user_id"] == 1234
