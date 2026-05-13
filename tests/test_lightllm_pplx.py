"""Tests for the Perplexity Pro lightllm adapter and supporting helpers."""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from ccproxy.config import PplxConfig, PplxThreadConfig
from ccproxy.lightllm.pplx import (
    PERPLEXITY_BLOCK_USE_CASES,
    PERPLEXITY_MODELS,
    _build_pplx_payload,
    _extract_deltas,
    _flatten_messages,
    _parse_sse_line,
    PerplexityClarifyingQuestionsError,
    StreamState,
    _thread_to_openai_messages,
)
from ccproxy.lightllm.pplx_threads import (
    PerplexityThreadStore,
    clear_pplx_threads,
    get_pplx_thread_store,
)
from ccproxy.lightllm.registry import get_config


def test_registry_resolves_perplexity_pro() -> None:
    config = get_config("perplexity_pro", "perplexity/best")
    assert type(config).__name__ == "PerplexityProConfig"


def test_models_catalog_has_known_ids() -> None:
    assert "perplexity/best" in PERPLEXITY_MODELS
    assert "perplexity/deep-research" in PERPLEXITY_MODELS
    assert "openai/gpt-5.4" in PERPLEXITY_MODELS
    assert PERPLEXITY_MODELS["perplexity/best"]["identifier"] == "default"


def test_build_payload_first_turn_full_production_shape() -> None:
    payload = _build_pplx_payload(
        query="what is quantum?", model_id="perplexity/best", extras={}
    )
    params = payload["params"]
    assert payload["query_str"] == "what is quantum?"
    assert params["query_source"] == "home"
    assert params["time_from_first_type"] == 18361
    assert params["use_schematized_api"] is True
    assert params["send_back_text_in_streaming_api"] is False
    assert params["prompt_source"] == "user"
    assert params["dsl_query"] == "what is quantum?"
    assert params["version"] == "2.18"
    assert params["model_preference"] == "default"
    assert isinstance(params["frontend_uuid"], str) and params["frontend_uuid"]
    assert isinstance(params["frontend_context_uuid"], str) and params["frontend_context_uuid"]
    assert params["supported_block_use_cases"] == PERPLEXITY_BLOCK_USE_CASES
    assert params["supported_features"] == ["browser_agent_permission_banner_v1.1"]


def test_build_payload_followup_injects_identifiers() -> None:
    payload = _build_pplx_payload(
        query="and superposition?",
        model_id="perplexity/best",
        extras={
            "last_backend_uuid": "backend-1",
            "read_write_token": "rw-1",
            "frontend_context_uuid": "ctx-stable",
        },
    )
    params = payload["params"]
    assert params["query_source"] == "followup"
    assert params["followup_source"] == "link"
    assert params["last_backend_uuid"] == "backend-1"
    assert params["read_write_token"] == "rw-1"
    assert params["frontend_context_uuid"] == "ctx-stable"
    assert params["time_from_first_type"] == 8758


def test_build_payload_unknown_model_raises() -> None:
    with pytest.raises(ValueError, match="Unknown Perplexity model"):
        _build_pplx_payload(query="hi", model_id="not-a-real-model", extras={})


def test_build_payload_space_uuid_forces_collection_query_source() -> None:
    payload = _build_pplx_payload(
        query="ask",
        model_id="perplexity/best",
        extras={"space_uuid": "space-1", "save_to_library": False},
    )
    params = payload["params"]
    assert params["query_source"] == "collection"
    assert params["target_collection_uuid"] == "space-1"
    assert params["target_thread_access_level"] == 1
    assert params["is_incognito"] is False


def test_flatten_messages_drops_image_url_parts() -> None:
    messages = [
        {"role": "system", "content": "you are helpful"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "what is in this image?"},
                {"type": "image_url", "image_url": {"url": "http://x/img.png"}},
            ],
        },
    ]
    out = _flatten_messages(messages)
    assert out.startswith("[System]: you are helpful")
    assert "what is in this image?" in out
    assert "image_url" not in out


def test_parse_sse_line_basic() -> None:
    assert _parse_sse_line('data: {"a": 1}') == {"a": 1}
    assert _parse_sse_line(b'data: {"b": 2}') == {"b": 2}
    assert _parse_sse_line("event: ping") is None
    assert _parse_sse_line("data: [DONE]") is None
    assert _parse_sse_line("not data") is None


def test_extract_deltas_prefix_diffs_answer_and_reasoning() -> None:
    state = StreamState()
    e1 = {
        "blocks": [
            {
                "intended_usage": "ask_text_0_markdown",
                "diff_block": {
                    "field": "markdown_block",
                    "patches": [
                        {"path": "/markdown_block", "value": {"answer": "Hello"}},
                    ],
                },
            }
        ],
        "backend_uuid": "B-1",
        "context_uuid": "C-1",
    }
    ans, reason = _extract_deltas(e1, state)
    assert ans == "Hello"
    assert reason is None
    assert state.ids["backend_uuid"] == "B-1"

    e2 = {
        "blocks": [
            {
                "intended_usage": "ask_text_0_markdown",
                "diff_block": {
                    "field": "markdown_block",
                    "patches": [
                        {"path": "/markdown_block", "value": {"answer": "Hello, world"}},
                    ],
                },
            },
            {
                "intended_usage": "pro_search_steps",
                "plan_block": {"goals": [{"description": "Searching"}]},
            },
        ]
    }
    ans, reason = _extract_deltas(e2, state)
    assert ans == ", world"
    assert reason == "Searching"

    e3 = {"final_sse_message": True, "thread_url_slug": "slug-1", "read_write_token": "rw-1"}
    ans, reason = _extract_deltas(e3, state)
    assert ans is None
    assert reason is None
    assert state.final is True
    assert state.ids["thread_url_slug"] == "slug-1"
    assert state.ids["read_write_token"] == "rw-1"


def test_extract_deltas_raises_on_clarifying_questions() -> None:
    state = StreamState()
    event = {
        "text": json.dumps(
            [{"step_type": "RESEARCH_CLARIFYING_QUESTIONS", "content": {"questions": ["a?", "b?"]}}]
        )
    }
    with pytest.raises(PerplexityClarifyingQuestionsError) as exc_info:
        _extract_deltas(event, state)
    assert exc_info.value.questions == ["a?", "b?"]


def test_thread_to_openai_messages_round_trip() -> None:
    thread = {
        "entries": [
            {
                "query_str": "what is quantum computing?",
                "structured_answer": [
                    {
                        "step_type": "FINAL",
                        "content": {
                            "answer": json.dumps(
                                {
                                    "answer": "Quantum [1] computing [2].",
                                    "web_results": [
                                        {"url": "http://a"},
                                        {"url": "http://b"},
                                    ],
                                }
                            ),
                            "web_results": [
                                {"url": "http://a"},
                                {"url": "http://b"},
                            ],
                        },
                    }
                ],
            },
            {
                "query_str": "follow up",
                "structured_answer": [
                    {
                        "step_type": "FINAL",
                        "content": {"answer": "Plain answer."},
                    }
                ],
            },
        ]
    }
    msgs = _thread_to_openai_messages(thread, citation_mode="markdown")
    assert len(msgs) == 4
    assert msgs[0] == {"role": "user", "content": "what is quantum computing?"}
    assert msgs[1]["role"] == "assistant"
    assert "[1](http://a)" in msgs[1]["content"]
    assert "[2](http://b)" in msgs[1]["content"]
    assert msgs[2] == {"role": "user", "content": "follow up"}
    assert msgs[3] == {"role": "assistant", "content": "Plain answer."}


def test_thread_store_save_get_lifecycle() -> None:
    clear_pplx_threads()
    store = get_pplx_thread_store()
    store.save(
        conversation_id="conv-1",
        backend_uuid="B-1",
        read_write_token="RW-1",
        context_uuid="C-1",
        thread_url_slug="slug-1",
    )
    state = store.get("conv-1")
    assert state is not None
    assert state.backend_uuid == "B-1"
    assert state.thread_url_slug == "slug-1"
    assert store.get("nonexistent") is None


def test_thread_store_ttl_eviction() -> None:
    store = PerplexityThreadStore(ttl_seconds=0.05)
    store.save(
        conversation_id="conv-1",
        backend_uuid="B-1",
        read_write_token="RW-1",
        context_uuid="C-1",
        thread_url_slug="slug-1",
    )
    assert store.size() == 1
    time.sleep(0.1)
    store.save(
        conversation_id="conv-2",
        backend_uuid="B-2",
        read_write_token="RW-2",
        context_uuid="C-2",
        thread_url_slug="slug-2",
    )
    assert store.get("conv-1") is None
    assert store.get("conv-2") is not None


def test_pplx_thread_config_defaults() -> None:
    cfg = PplxConfig()
    assert cfg.thread.consistency_mode == "warn"
    assert cfg.thread.citation_mode == "markdown"
    assert cfg.thread.ttl_seconds == 1800.0


def test_pplx_thread_config_rejects_invalid_literal() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        PplxThreadConfig(consistency_mode="bogus")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        PplxThreadConfig(citation_mode="bogus")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        PplxThreadConfig(ttl_seconds=-1)


def test_extract_pplx_files_data_uri_path() -> None:
    from ccproxy.hooks.extract_pplx_files import _decode_data_uri

    info = _decode_data_uri(
        "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
    assert info is not None
    assert info.mimetype == "image/png"
    assert info.is_image is True


def test_count_client_user_turns_with_system_messages() -> None:
    from ccproxy.hooks.pplx_thread_inject import _count_client_user_turns

    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "u3-new"},
    ]
    assert _count_client_user_turns(messages) == 2


def test_pplx_addon_scan_for_ids() -> None:
    from ccproxy.inspector.pplx_addon import PerplexityAddon

    raw = (
        b'data: {"backend_uuid":"B-1","context_uuid":"C-1","thread_url_slug":"slug-X","blocks":[]}\n'
        b'data: {"final":true,"read_write_token":"RW-1","blocks":[]}'
    )
    ids = PerplexityAddon._scan_for_ids(raw)
    assert ids == {
        "backend_uuid": "B-1",
        "context_uuid": "C-1",
        "thread_url_slug": "slug-X",
        "read_write_token": "RW-1",
    }


def _make_payload_bytes(payload: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode()


def test_iterator_emits_content_and_reasoning_deltas() -> None:
    from ccproxy.lightllm.pplx import PerplexityProIterator

    iterator = PerplexityProIterator(
        streaming_response=iter([]), sync_stream=True, json_mode=False
    )
    e1 = {
        "blocks": [
            {
                "intended_usage": "ask_text_0_markdown",
                "diff_block": {
                    "field": "markdown_block",
                    "patches": [
                        {"path": "/markdown_block", "value": {"answer": "Hi"}},
                    ],
                },
            }
        ]
    }
    e2 = {
        "blocks": [
            {
                "intended_usage": "pro_search_steps",
                "plan_block": {"goals": [{"description": "searching"}]},
            }
        ]
    }
    e3 = {"final_sse_message": True, "thread_url_slug": "slug-final"}

    c1 = iterator.chunk_parser(e1)
    assert c1.choices[0].delta.content == "Hi"
    assert c1.choices[0].finish_reason is None

    c2 = iterator.chunk_parser(e2)
    assert getattr(c2.choices[0].delta, "reasoning_content", None) == "searching"

    c3 = iterator.chunk_parser(e3)
    assert c3.choices[0].finish_reason == "stop"
    assert getattr(c3, "pplx_thread_url_slug", None) == "slug-final"
