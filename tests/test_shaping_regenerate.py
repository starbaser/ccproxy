"""Tests for dynamic shaping hooks."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from mitmproxy import http

from ccproxy.pipeline.context import Context
from ccproxy.shaping.regenerate import (
    _compute_cch,
    _compute_suffix,
    regenerate_billing_header,
    regenerate_session_id,
    regenerate_user_prompt_id,
)

_TEST_VERSION = "2.1.87"


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


_SYNTHETIC_SALT = "deadbeefcafe"


@dataclass(frozen=True)
class BillingComputeCase:
    name: str
    """Descriptive name for the test scenario."""

    text: str
    """First user message text."""

    expected_cch: str
    """Expected ``cch`` (sha256(text)[:5])."""


def _expected_cch(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:5]


def _expected_suffix(text: str, salt: str, version: str) -> str:
    sampled = "".join(text[i] if i < len(text) else "0" for i in (4, 7, 20))
    return hashlib.sha256(f"{salt}{sampled}{version}".encode()).hexdigest()[:3]


_LONG_TEXT = "hello world this is a long message"

BILLING_COMPUTE_CASES: list[BillingComputeCase] = [
    BillingComputeCase(name="empty", text="", expected_cch=_expected_cch("")),
    BillingComputeCase(name="short", text="hi", expected_cch=_expected_cch("hi")),
    BillingComputeCase(name="long", text=_LONG_TEXT, expected_cch=_expected_cch(_LONG_TEXT)),
    BillingComputeCase(name="exact_21_chars", text="a" * 21, expected_cch=_expected_cch("a" * 21)),
]


@pytest.mark.parametrize(
    "case",
    [pytest.param(c, id=c.name) for c in BILLING_COMPUTE_CASES],
)
def test_compute_cch(case: BillingComputeCase) -> None:
    """``_compute_cch`` matches ``sha256(text).hex[:5]`` for varied inputs."""
    assert _compute_cch(case.text) == case.expected_cch


@pytest.mark.parametrize(
    "case",
    [pytest.param(c, id=c.name) for c in BILLING_COMPUTE_CASES],
)
def test_compute_suffix(case: BillingComputeCase) -> None:
    """``_compute_suffix`` mirrors signing.ts (salt + sampled + version)."""
    expected = _expected_suffix(case.text, _SYNTHETIC_SALT, _TEST_VERSION)
    assert _compute_suffix(case.text, _SYNTHETIC_SALT, _TEST_VERSION) == expected


def _user_text_body(text: str = "hello") -> dict[str, Any]:
    return {"messages": [{"role": "user", "content": text}]}


def _shape_billing_block(version: str, entrypoint: str, *, suffix: str = "abc", cch: str = "00000") -> dict[str, str]:
    return {
        "type": "text",
        "text": (
            f"x-anthropic-billing-header: cc_version={version}.{suffix}; "
            f"cc_entrypoint={entrypoint}; cch={cch};"
        ),
    }


def _patch_salts(version_to_salt: dict[str, str]) -> Any:
    """Patch ``get_billing_salt_for_version`` to look up from a fixed dict."""
    from unittest.mock import patch as _patch

    return _patch(
        "ccproxy.shaping.regenerate.get_billing_salt_for_version",
        side_effect=version_to_salt.get,
    )


def test_regenerate_billing_header_uses_shape_version_to_lookup_salt() -> None:
    """Hook parses cc_version from shape, looks up matching salt, signs in place."""
    body = {
        **_user_text_body("what is 7 times 8"),
        "system": [
            _shape_billing_block("2.1.87", "cli", suffix="6d6", cch="fa6f5"),
            {"type": "text", "text": "You are a Claude agent."},
        ],
    }
    shape = _shape_ctx(body)
    with _patch_salts({"2.1.87": _SYNTHETIC_SALT}):
        regenerate_billing_header(shape, {})

    system = shape._body["system"]
    assert len(system) == 2  # No accumulation
    new_text = system[0]["text"]

    expected_cch = _expected_cch("what is 7 times 8")
    expected_suffix = _expected_suffix("what is 7 times 8", _SYNTHETIC_SALT, "2.1.87")
    expected_header = (
        f"x-anthropic-billing-header: cc_version=2.1.87.{expected_suffix}; "
        f"cc_entrypoint=cli; cch={expected_cch};"
    )
    assert new_text == expected_header
    assert system[1] == {"type": "text", "text": "You are a Claude agent."}


def test_regenerate_billing_header_preserves_shape_version() -> None:
    """The shape's version is preserved verbatim (the salt is the matching one)."""
    body = {
        **_user_text_body("x"),
        "system": [_shape_billing_block("3.0.0", "sdk-cli")],
    }
    shape = _shape_ctx(body)
    with _patch_salts({"3.0.0": _SYNTHETIC_SALT}):
        regenerate_billing_header(shape, {})
    text = shape._body["system"][0]["text"]
    expected_suffix = _expected_suffix("x", _SYNTHETIC_SALT, "3.0.0")
    assert f"cc_version=3.0.0.{expected_suffix}" in text
    assert "cc_entrypoint=sdk-cli" in text


def test_regenerate_billing_header_preserves_block_extras() -> None:
    """Non-text fields on the billing block (e.g. cache_control) survive regeneration."""
    body = {
        **_user_text_body("hi"),
        "system": [
            {
                "type": "text",
                "text": "x-anthropic-billing-header: cc_version=2.1.87.6d6; cc_entrypoint=cli; cch=fa6f5;",
                "cache_control": {"type": "ephemeral"},
            },
        ],
    }
    shape = _shape_ctx(body)
    with _patch_salts({"2.1.87": _SYNTHETIC_SALT}):
        regenerate_billing_header(shape, {})
    block = shape._body["system"][0]
    assert block["cache_control"] == {"type": "ephemeral"}
    assert block["type"] == "text"


def test_regenerate_billing_header_skips_when_no_messages_gemini_shape() -> None:
    body_before = {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]}
    shape = _shape_ctx(body_before)
    snapshot = json.loads(json.dumps(shape._body))
    with _patch_salts({"2.1.87": _SYNTHETIC_SALT}):
        regenerate_billing_header(shape, {})
    assert shape._body == snapshot


def test_regenerate_billing_header_skips_when_no_salt_for_version() -> None:
    """Shape's version isn't in the salts file → no-op + warning."""
    body = {
        **_user_text_body("hi"),
        "system": [_shape_billing_block("2.1.87", "cli")],
    }
    shape = _shape_ctx(body)
    snapshot = json.loads(json.dumps(shape._body))
    with _patch_salts({"9.9.9": _SYNTHETIC_SALT}):  # Doesn't include 2.1.87
        regenerate_billing_header(shape, {})
    assert shape._body == snapshot


def test_regenerate_billing_header_skips_when_salts_file_empty() -> None:
    body = {
        **_user_text_body("hi"),
        "system": [_shape_billing_block("2.1.87", "cli")],
    }
    shape = _shape_ctx(body)
    snapshot = json.loads(json.dumps(shape._body))
    with _patch_salts({}):
        regenerate_billing_header(shape, {})
    assert shape._body == snapshot


def test_regenerate_billing_header_skips_when_no_billing_block_in_shape() -> None:
    """Without a captured billing block to patch, the hook logs a warning and no-ops."""
    body = {
        **_user_text_body("hi"),
        "system": [{"type": "text", "text": "Plain system prompt."}],
    }
    shape = _shape_ctx(body)
    snapshot = json.loads(json.dumps(shape._body))
    with _patch_salts({"2.1.87": _SYNTHETIC_SALT}):
        regenerate_billing_header(shape, {})
    assert shape._body == snapshot


def test_regenerate_billing_header_skips_when_system_absent() -> None:
    """If the shape has no ``system`` array, there's nothing to patch — no-op."""
    body = _user_text_body("hi")
    shape = _shape_ctx(body)
    snapshot = json.loads(json.dumps(shape._body))
    with _patch_salts({"2.1.87": _SYNTHETIC_SALT}):
        regenerate_billing_header(shape, {})
    assert shape._body == snapshot
