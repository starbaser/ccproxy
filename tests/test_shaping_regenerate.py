"""Tests for dynamic shaping hooks."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

import pytest
import xxhash
from mitmproxy import http

from ccproxy.pipeline.context import Context
from ccproxy.shaping.regenerate import (
    _CCH_MASK,
    _compute_suffix,
    regenerate_billing_header,
    regenerate_session_id,
    regenerate_user_prompt_id,
)

_TEST_VERSION = "2.1.87"
_TEST_SEED = 0x0123456789ABCDEF


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


_SYNTHETIC_SALT = "0123456789ab"


@dataclass(frozen=True)
class SuffixCase:
    name: str
    """Descriptive name for the test scenario."""

    text: str
    """First user message text."""


def _expected_suffix(text: str, salt: str, version: str) -> str:
    sampled = "".join(text[i] if i < len(text) else "0" for i in (4, 7, 20))
    return hashlib.sha256(f"{salt}{sampled}{version}".encode()).hexdigest()[:3]


_LONG_TEXT = "hello world this is a long message"

SUFFIX_CASES: list[SuffixCase] = [
    SuffixCase(name="empty", text=""),
    SuffixCase(name="short", text="hi"),
    SuffixCase(name="long", text=_LONG_TEXT),
    SuffixCase(name="exact_21_chars", text="a" * 21),
]


@pytest.mark.parametrize(
    "case",
    [pytest.param(c, id=c.name) for c in SUFFIX_CASES],
)
def test_compute_suffix(case: SuffixCase) -> None:
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


def _patch_billing(salt: str | None, seed: int | None = _TEST_SEED) -> Any:
    """Patch both ``get_billing_salt`` and ``get_billing_cch_seed`` for the duration."""
    from contextlib import ExitStack
    from unittest.mock import patch as _patch

    stack = ExitStack()
    stack.enter_context(_patch("ccproxy.shaping.regenerate.get_billing_salt", return_value=salt))
    stack.enter_context(_patch("ccproxy.shaping.regenerate.get_billing_cch_seed", return_value=seed))
    return stack


def _expected_cch_for_body(body_bytes: bytes) -> str:
    """Replicate the wire-layer xxhash64 against a body that contains ``cch=00000``."""
    digest = xxhash.xxh64(body_bytes, seed=_TEST_SEED).intdigest() & _CCH_MASK
    return f"{digest:05x}"


def test_regenerate_billing_header_signs_cch_via_xxhash64() -> None:
    """End-to-end: cc_version suffix is SHA-256, cch is xxhash64 over the wire bytes."""
    body = {
        **_user_text_body("what is 7 times 8"),
        "system": [
            _shape_billing_block("2.1.87", "cli", suffix="6d6", cch="fa6f5"),
            {"type": "text", "text": "You are a Claude agent."},
        ],
    }
    shape = _shape_ctx(body)
    with _patch_billing(_SYNTHETIC_SALT):
        regenerate_billing_header(shape, {})

    system = shape._body["system"]
    assert len(system) == 2  # No accumulation
    new_text = system[0]["text"]

    expected_suffix = _expected_suffix("what is 7 times 8", _SYNTHETIC_SALT, "2.1.87")
    assert f"cc_version=2.1.87.{expected_suffix};" in new_text
    assert "cc_entrypoint=cli" in new_text
    assert system[1] == {"type": "text", "text": "You are a Claude agent."}

    # Verify the cch matches what xxhash64 would produce on the wire bytes
    # with cch reset to the placeholder.
    wire_bytes = shape._request.content  # type: ignore[union-attr]
    placeholder_bytes = re.sub(rb"\bcch=[0-9a-f]+;", b"cch=00000;", wire_bytes, count=1)
    expected_cch = _expected_cch_for_body(placeholder_bytes)
    assert f"cch={expected_cch};" in new_text


def test_regenerate_billing_header_keeps_shape_version() -> None:
    """The shape's ``cc_version`` major-part is preserved verbatim (only the 3-hex suffix changes)."""
    body = {
        **_user_text_body("x"),
        "system": [_shape_billing_block("3.0.0", "sdk-cli")],
    }
    shape = _shape_ctx(body)
    with _patch_billing(_SYNTHETIC_SALT):
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
    with _patch_billing(_SYNTHETIC_SALT):
        regenerate_billing_header(shape, {})
    block = shape._body["system"][0]
    assert block["cache_control"] == {"type": "ephemeral"}
    assert block["type"] == "text"


def test_regenerate_billing_header_skips_when_no_messages_gemini_shape() -> None:
    body_before = {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]}
    shape = _shape_ctx(body_before)
    snapshot = json.loads(json.dumps(shape._body))
    with _patch_billing(_SYNTHETIC_SALT):
        regenerate_billing_header(shape, {})
    assert shape._body == snapshot


def test_regenerate_billing_header_skips_when_no_salt_configured() -> None:
    """``billing.salt`` not configured → no-op + warning, body untouched."""
    body = {
        **_user_text_body("hi"),
        "system": [_shape_billing_block("2.1.87", "cli")],
    }
    shape = _shape_ctx(body)
    snapshot = json.loads(json.dumps(shape._body))
    with _patch_billing(None):
        regenerate_billing_header(shape, {})
    assert shape._body == snapshot


def test_regenerate_billing_header_skips_when_no_seed_configured() -> None:
    """``billing.seed`` not configured → no-op + warning, body untouched."""
    body = {
        **_user_text_body("hi"),
        "system": [_shape_billing_block("2.1.87", "cli")],
    }
    shape = _shape_ctx(body)
    snapshot = json.loads(json.dumps(shape._body))
    with _patch_billing(_SYNTHETIC_SALT, seed=None):
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
    with _patch_billing(_SYNTHETIC_SALT):
        regenerate_billing_header(shape, {})
    assert shape._body == snapshot


def test_regenerate_billing_header_skips_when_system_absent() -> None:
    """If the shape has no ``system`` array, there's nothing to patch — no-op."""
    body = _user_text_body("hi")
    shape = _shape_ctx(body)
    snapshot = json.loads(json.dumps(shape._body))
    with _patch_billing(_SYNTHETIC_SALT):
        regenerate_billing_header(shape, {})
    assert shape._body == snapshot


def test_signed_body_round_trips_to_wire_bytes() -> None:
    """After signing, ``_body`` re-serializes byte-identically — the outer commit is safe."""
    body = {
        **_user_text_body("round trip me"),
        "system": [_shape_billing_block("2.1.87", "cli")],
    }
    shape = _shape_ctx(body)
    with _patch_billing(_SYNTHETIC_SALT):
        regenerate_billing_header(shape, {})

    wire_bytes = shape._request.content  # type: ignore[union-attr]
    re_serialized = json.dumps(shape._body).encode()
    assert wire_bytes == re_serialized
