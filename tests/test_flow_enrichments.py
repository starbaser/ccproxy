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


def _flow_with_body(body: dict[str, Any], content_type: str = "application/json") -> Any:
    """Build a fake HTTPFlow whose request.content is serialized JSON."""
    flow = MagicMock()
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
        name="no_messages_no_system",
        body={"contents": [{"role": "user", "parts": [{"text": "gemini-shape"}]}]},
        expected_conv_id_text=None,
        expected_system=None,
    ),
    EnrichmentCase(
        name="empty_user_message",
        body={"messages": [{"role": "user", "content": ""}]},
        expected_conv_id_text="",
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
