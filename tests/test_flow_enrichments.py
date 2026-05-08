"""Tests for FlowRecord conversation_id + system_prompt_sha enrichment."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

from ccproxy.flows.store import FlowRecord, HttpSnapshot
from ccproxy.inspector.addon import InspectorAddon


def _flow_with_body(
    body: dict[str, Any],
    content_type: str = "application/json",
    flow_id: str = "fixed-flow-id",
) -> Any:
    """Build a fake HTTPFlow whose request.content is serialized JSON."""
    flow = MagicMock()
    flow.id = flow_id
    flow.request.content = json.dumps(body).encode()
    flow.request.headers = {"content-type": content_type}
    flow.metadata = {}
    return flow


def _expected_conversation_id(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def _expected_system_prompt_sha(system: Any) -> str:
    serialized = json.dumps(system, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode()).hexdigest()[:12]


@dataclass
class EnrichmentCase:
    name: str
    """Descriptive name for the test scenario."""

    body: dict[str, Any]
    """Request body to serialize as JSON."""

    expected_conv_id_text: str | None
    """Text the conversation_id should derive from, or None if no enrichment."""

    expected_system: Any | None
    """System value the system_prompt_sha should derive from, or None."""

    content_type: str = "application/json"
    """Optional Content-Type override."""


ENRICHMENT_CASES: list[EnrichmentCase] = [
    EnrichmentCase(
        name="anthropic_string_user_message",
        body={
            "messages": [{"role": "user", "content": "what's 2+2"}],
            "system": [{"type": "text", "text": "You are Claude."}],
        },
        expected_conv_id_text="what's 2+2",
        expected_system=[{"type": "text", "text": "You are Claude."}],
    ),
    EnrichmentCase(
        name="anthropic_text_block",
        body={
            "messages": [{"role": "user", "content": [{"type": "text", "text": "long question"}]}],
            "system": "string system",
        },
        expected_conv_id_text="long question",
        expected_system="string system",
    ),
    EnrichmentCase(
        name="gemini_native_contents_derives_conv_id",
        body={"contents": [{"role": "user", "parts": [{"text": "gemini-shape"}]}]},
        expected_conv_id_text="gemini-shape",
        expected_system=None,
    ),
    EnrichmentCase(
        name="gemini_v1internal_wrapped_contents_derives_conv_id",
        body={
            "model": "gemini-3.1-pro-preview",
            "request": {"contents": [{"role": "user", "parts": [{"text": "wrapped-text"}]}]},
        },
        expected_conv_id_text="wrapped-text",
        expected_system=None,
    ),
    EnrichmentCase(
        name="empty_body_no_messages_no_contents",
        body={"random_key": "random_value"},
        expected_conv_id_text=None,
        expected_system=None,
    ),
    EnrichmentCase(
        name="empty_user_message",
        body={"messages": [{"role": "user", "content": ""}]},
        expected_conv_id_text="flow:fixed-flow-id",
        expected_system=None,
    ),
    EnrichmentCase(
        name="non_json_content_type_skips_enrichment",
        body={"messages": [{"role": "user", "content": "x"}]},
        expected_conv_id_text=None,
        expected_system=None,
        content_type="text/plain",
    ),
]


@pytest.mark.parametrize(
    "case",
    [pytest.param(c, id=c.name) for c in ENRICHMENT_CASES],
)
def test_enrich_record_with_conversation_ids(case: EnrichmentCase) -> None:
    """Verify enrichment derives the right SHA12 values and skips on bad inputs."""
    flow = _flow_with_body(case.body, content_type=case.content_type)
    record = FlowRecord(direction="inbound")

    InspectorAddon._enrich_record_with_conversation_ids(flow, record)

    if case.expected_conv_id_text is None:
        assert record.conversation_id is None
        assert "ccproxy.conversation_id" not in flow.metadata
    else:
        expected = _expected_conversation_id(case.expected_conv_id_text)
        assert record.conversation_id == expected
        assert flow.metadata["ccproxy.conversation_id"] == expected

    if case.expected_system is None:
        assert record.system_prompt_sha is None
        assert "ccproxy.system_prompt_sha" not in flow.metadata
    else:
        expected = _expected_system_prompt_sha(case.expected_system)
        assert record.system_prompt_sha == expected
        assert flow.metadata["ccproxy.system_prompt_sha"] == expected


def test_default_flow_record_has_none_enrichments() -> None:
    """Defaults are None — only set when ``_enrich_record_with_conversation_ids`` runs."""
    record = FlowRecord(direction="inbound")
    assert record.conversation_id is None
    assert record.system_prompt_sha is None


def test_enrichment_handles_missing_body() -> None:
    """Empty request body → no-op."""
    flow = MagicMock()
    flow.request.content = b""
    flow.request.headers = {"content-type": "application/json"}
    flow.metadata = {}
    record = FlowRecord(direction="inbound")
    InspectorAddon._enrich_record_with_conversation_ids(flow, record)
    assert record.conversation_id is None


def test_enrichment_handles_invalid_json() -> None:
    """Body that doesn't parse as JSON → no-op (no exception)."""
    flow = MagicMock()
    flow.request.content = b"<<not json>>"
    flow.request.headers = {"content-type": "application/json"}
    flow.metadata = {}
    record = FlowRecord(direction="inbound")
    InspectorAddon._enrich_record_with_conversation_ids(flow, record)
    assert record.conversation_id is None
    assert record.system_prompt_sha is None


def test_empty_first_text_uses_flow_id_seed_to_avoid_collision() -> None:
    """Two flows whose first user message has empty text must NOT collide on conversation_id.

    Regression for the bug where ``extract_first_user_text`` returns ``""`` for
    empty first-text-block messages (intentional, for billing-validator parity),
    and the enrichment blindly hashed it — causing every empty-message request
    to share the same SHA12 (``e3b0c44298fc``).
    """
    body_a = {"messages": [{"role": "user", "content": [{"type": "text", "text": ""}]}]}
    body_b = {"messages": [{"role": "user", "content": ""}]}

    flow_a = _flow_with_body(body_a, flow_id="flow-a-uuid")
    flow_b = _flow_with_body(body_b, flow_id="flow-b-uuid")
    record_a = FlowRecord(direction="inbound")
    record_b = FlowRecord(direction="inbound")

    InspectorAddon._enrich_record_with_conversation_ids(flow_a, record_a)
    InspectorAddon._enrich_record_with_conversation_ids(flow_b, record_b)

    assert record_a.conversation_id is not None
    assert record_b.conversation_id is not None
    assert record_a.conversation_id != record_b.conversation_id
    empty_sha = _expected_conversation_id("")
    assert record_a.conversation_id != empty_sha
    assert record_b.conversation_id != empty_sha


def test_record_preserves_client_request_alongside_enrichment() -> None:
    """The enrichment doesn't disturb the existing client_request snapshot."""
    snapshot = HttpSnapshot(
        headers={"content-type": "application/json"},
        body=json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode(),
        method="POST",
        url="https://api.test/v1/messages",
    )
    record = FlowRecord(direction="inbound", client_request=snapshot)
    flow = _flow_with_body({"messages": [{"role": "user", "content": "hi"}]})

    InspectorAddon._enrich_record_with_conversation_ids(flow, record)

    assert record.client_request is snapshot
    assert record.conversation_id == _expected_conversation_id("hi")


class TestParsedRequestBodyCache:
    """Tests for FlowRecord.parsed_request_body parse-once cache."""

    def test_caches_one_parse_per_flow(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``json.loads`` runs exactly once even when the cache is queried twice."""
        record = FlowRecord(direction="inbound")
        content = json.dumps({"messages": [{"role": "user", "content": "x"}], "metadata": {"user_id": "u"}}).encode()

        import ccproxy.flows.store as store_mod

        call_count = 0
        real_loads = json.loads

        def counting_loads(*args: Any, **kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            return real_loads(*args, **kwargs)

        monkeypatch.setattr(store_mod.json, "loads", counting_loads)

        first = record.parsed_request_body(content)
        second = record.parsed_request_body(content)
        assert first is second  # same cached dict, not a fresh parse
        assert call_count == 1

    def test_returns_none_on_invalid_json(self) -> None:
        """Invalid bytes cache as ``None`` and never re-parse."""
        record = FlowRecord(direction="inbound")
        assert record.parsed_request_body(b"not json") is None
        assert record._parse_attempted is True
        # Subsequent call still returns None without re-parsing
        assert record.parsed_request_body(b"not json") is None

    def test_invalid_json_does_not_re_parse(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Failed parse caches the failure; second call must not invoke ``json.loads``."""
        record = FlowRecord(direction="inbound")
        import ccproxy.flows.store as store_mod

        call_count = 0
        real_loads = json.loads

        def counting_loads(*args: Any, **kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            return real_loads(*args, **kwargs)

        monkeypatch.setattr(store_mod.json, "loads", counting_loads)

        record.parsed_request_body(b"<<malformed>>")
        record.parsed_request_body(b"<<malformed>>")
        assert call_count == 1

    def test_returns_none_on_empty_content(self) -> None:
        """Empty bodies never invoke the parser but still mark ``_parse_attempted``."""
        record = FlowRecord(direction="inbound")
        assert record.parsed_request_body(b"") is None
        assert record._parse_attempted is True

    def test_returns_none_on_none_content(self) -> None:
        """``None`` content (request without body) yields ``None`` and marks attempted."""
        record = FlowRecord(direction="inbound")
        assert record.parsed_request_body(None) is None
        assert record._parse_attempted is True

    def test_returns_none_when_root_not_dict(self) -> None:
        """JSON arrays at the root yield ``None`` (we only model dict bodies)."""
        record = FlowRecord(direction="inbound")
        assert record.parsed_request_body(b"[1, 2, 3]") is None

    def test_returns_none_when_root_is_string(self) -> None:
        record = FlowRecord(direction="inbound")
        assert record.parsed_request_body(b'"just a string"') is None

    def test_returns_dict_on_valid_json(self) -> None:
        record = FlowRecord(direction="inbound")
        body = record.parsed_request_body(b'{"k": "v"}')
        assert body == {"k": "v"}

    def test_handles_invalid_utf8(self) -> None:
        """Bytes that aren't valid UTF-8 surface as ``None`` rather than crashing."""
        record = FlowRecord(direction="inbound")
        assert record.parsed_request_body(b"\xff\xfe\x00bad") is None


class TestSingleParseAcrossEnrichmentAndExtract:
    """Integration: enrichment + session-id extraction share one parse per flow."""

    def test_single_body_parse_for_full_request_pipeline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Both addon-side body consumers share one parse per flow.

        The legacy ``user_..._session_<id>`` user_id format is used so
        ``parse_session_id`` doesn't introduce its own ``json.loads`` for the
        inner user_id payload — letting us assert exactly one body parse.
        """
        body_dict = {
            "messages": [{"role": "user", "content": "what's 2+2"}],
            "system": [{"type": "text", "text": "You are Claude."}],
            "metadata": {"user_id": "user_h_account_acct_session_sess-xyz"},
        }
        content = json.dumps(body_dict).encode()
        flow = _flow_with_body(body_dict)
        record = FlowRecord(direction="inbound")

        import ccproxy.flows.store as store_mod

        call_count = 0
        real_loads = json.loads

        def counting_loads(*args: Any, **kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            return real_loads(*args, **kwargs)

        monkeypatch.setattr(store_mod.json, "loads", counting_loads)

        # First consumer: enrichment hashes messages + system
        InspectorAddon._enrich_record_with_conversation_ids(flow, record)
        # Second consumer: session_id extraction reads the cached body
        body = record.parsed_request_body(content)
        session_id = InspectorAddon._extract_session_id_from_body(body)

        assert call_count == 1
        assert session_id == "sess-xyz"
        assert record.conversation_id == _expected_conversation_id("what's 2+2")
        assert record.system_prompt_sha == _expected_system_prompt_sha([{"type": "text", "text": "You are Claude."}])
