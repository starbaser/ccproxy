"""Tests for ccproxy.specs vendored constants + Pydantic schemas."""

from __future__ import annotations

import pytest

from ccproxy.specs import (
    BASE_BETAS,
    LONG_CONTEXT_BETAS,
    APIRequestParams,
)


def test_base_betas_count_and_membership() -> None:
    """6 base betas; tuple is immutable so it can't be mutated by callers."""
    assert isinstance(BASE_BETAS, tuple)
    assert len(BASE_BETAS) == 6
    assert "claude-code-20250219" in BASE_BETAS
    assert "oauth-2025-04-20" in BASE_BETAS


def test_long_context_betas() -> None:
    """2 long-context betas; ``interleaved-thinking`` overlaps with the base set."""
    assert isinstance(LONG_CONTEXT_BETAS, tuple)
    assert len(LONG_CONTEXT_BETAS) == 2
    assert "context-1m-2025-08-07" in LONG_CONTEXT_BETAS


def test_api_request_params_round_trip_anthropic_shape() -> None:
    """A typical Anthropic request body parses cleanly and round-trips."""
    body = {
        "model": "claude-haiku-4-5-20251001",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1024,
        "stream": True,
        "system": [{"type": "text", "text": "system prompt"}],
    }
    params = APIRequestParams(**body)
    assert params.model == "claude-haiku-4-5-20251001"
    assert params.max_tokens == 1024
    assert params.stream is True
    assert params.messages == [{"role": "user", "content": "hi"}]


def test_api_request_params_allows_extra_fields() -> None:
    """Permissive: unknown fields don't error so we don't break on new server fields."""
    params = APIRequestParams(model="x", future_field={"k": "v"})
    assert params.model == "x"
    # extra="allow" exposes unknown fields via model_extra
    assert params.model_extra == {"future_field": {"k": "v"}}


def test_api_request_params_dump_excludes_unset() -> None:
    """``model_dump(exclude_none=True)`` drops Nones cleanly for downstream use."""
    params = APIRequestParams(model="x", max_tokens=512)
    dumped = params.model_dump(exclude_none=True)
    assert dumped == {"model": "x", "max_tokens": 512}


@pytest.mark.parametrize(
    "field_name",
    [
        "model",
        "messages",
        "system",
        "tools",
        "tool_choice",
        "betas",
        "metadata",
        "max_tokens",
        "thinking",
        "temperature",
        "top_p",
        "top_k",
        "stop_sequences",
        "stream",
        "context_management",
        "output_config",
        "speed",
        "cache_control",
    ],
)
def test_api_request_params_declares_field(field_name: str) -> None:
    """All documented Anthropic fields are explicitly declared (not just allowed via extra)."""
    assert field_name in APIRequestParams.model_fields
