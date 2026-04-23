"""Tests for config-driven content injection in the shape hook."""

from __future__ import annotations

import json
from typing import Any

from mitmproxy import http

from ccproxy.config import ProviderShapingConfig
from ccproxy.hooks.shape import _inject_content
from ccproxy.pipeline.context import Context
from ccproxy.shaping.models import apply_shape


def _shape_ctx(body: dict[str, Any]) -> Context:
    req = http.Request.make(
        "POST",
        "https://shape.example/v1/messages?beta=true",
        json.dumps(body).encode(),
        {"user-agent": "claude-cli/2.0", "anthropic-beta": "oauth-2025"},
    )
    return Context.from_request(req)


def _incoming_ctx(body: dict[str, Any]) -> Context:
    req = http.Request.make(
        "POST",
        "https://incoming.example/v1/messages",
        json.dumps(body).encode(),
        {},
    )
    return Context.from_request(req)


class TestContentInjection:
    def test_replace_copies_incoming_field(self) -> None:
        shape = _shape_ctx({"model": "shape-model", "messages": [{"role": "user", "content": "shape"}]})
        incoming = _incoming_ctx({"model": "incoming-model", "messages": [{"role": "user", "content": "hi"}]})
        profile = ProviderShapingConfig(content_fields=["model", "messages"])

        _inject_content(shape, incoming, profile)
        assert shape._body["model"] == "incoming-model"
        assert shape._body["messages"] == [{"role": "user", "content": "hi"}]

    def test_unlisted_fields_persist_from_shape(self) -> None:
        shape = _shape_ctx({
            "model": "shape-model",
            "thinking": {"budget_tokens": 31999, "type": "enabled"},
            "context_management": {"edits": []},
        })
        incoming = _incoming_ctx({"model": "incoming-model"})
        profile = ProviderShapingConfig(content_fields=["model"])

        _inject_content(shape, incoming, profile)
        assert shape._body["model"] == "incoming-model"
        assert shape._body["thinking"] == {"budget_tokens": 31999, "type": "enabled"}
        assert shape._body["context_management"] == {"edits": []}

    def test_missing_incoming_field_not_injected(self) -> None:
        shape = _shape_ctx({"model": "shape-model", "thinking": {"type": "enabled"}})
        incoming = _incoming_ctx({})
        profile = ProviderShapingConfig(content_fields=["model", "temperature"])

        _inject_content(shape, incoming, profile)
        assert "model" not in shape._body
        assert "temperature" not in shape._body
        assert shape._body["thinking"] == {"type": "enabled"}

    def test_prepend_shape_strategy(self) -> None:
        shape = _shape_ctx({
            "system": [{"type": "text", "text": "shape-system"}],
            "messages": [],
        })
        incoming = _incoming_ctx({
            "system": [{"type": "text", "text": "user-system"}],
        })
        profile = ProviderShapingConfig(
            content_fields=["system"],
            merge_strategies={"system": "prepend_shape"},
        )

        _inject_content(shape, incoming, profile)
        assert len(shape._body["system"]) == 2
        assert shape._body["system"][0]["text"] == "shape-system"
        assert shape._body["system"][1]["text"] == "user-system"

    def test_prepend_shape_normalizes_strings(self) -> None:
        shape = _shape_ctx({"system": "shape-prompt"})
        incoming = _incoming_ctx({"system": "user-prompt"})
        profile = ProviderShapingConfig(
            content_fields=["system"],
            merge_strategies={"system": "prepend_shape"},
        )

        _inject_content(shape, incoming, profile)
        assert len(shape._body["system"]) == 2
        assert shape._body["system"][0] == {"type": "text", "text": "shape-prompt"}
        assert shape._body["system"][1] == {"type": "text", "text": "user-prompt"}

    def test_append_shape_strategy(self) -> None:
        shape = _shape_ctx({
            "system": [{"type": "text", "text": "shape-suffix"}],
        })
        incoming = _incoming_ctx({
            "system": [{"type": "text", "text": "user-system"}],
        })
        profile = ProviderShapingConfig(
            content_fields=["system"],
            merge_strategies={"system": "append_shape"},
        )

        _inject_content(shape, incoming, profile)
        assert shape._body["system"][0]["text"] == "user-system"
        assert shape._body["system"][1]["text"] == "shape-suffix"

    def test_drop_strategy(self) -> None:
        shape = _shape_ctx({"user_prompt_id": "shape-id", "model": "x"})
        incoming = _incoming_ctx({"user_prompt_id": "incoming-id", "model": "y"})
        profile = ProviderShapingConfig(
            content_fields=["user_prompt_id", "model"],
            merge_strategies={"user_prompt_id": "drop"},
        )

        _inject_content(shape, incoming, profile)
        assert "user_prompt_id" not in shape._body
        assert shape._body["model"] == "y"

    def test_generation_params_flow_through(self) -> None:
        shape = _shape_ctx({"max_tokens": 50, "model": "shape"})
        incoming = _incoming_ctx({
            "model": "incoming",
            "max_tokens": 8192,
            "temperature": 0.3,
            "top_p": 0.9,
        })
        profile = ProviderShapingConfig(
            content_fields=["model", "max_tokens", "temperature", "top_p"],
        )

        _inject_content(shape, incoming, profile)
        assert shape._body["model"] == "incoming"
        assert shape._body["max_tokens"] == 8192
        assert shape._body["temperature"] == 0.3
        assert shape._body["top_p"] == 0.9


class TestQueryParamMerge:
    def test_shape_query_params_applied(self) -> None:
        from mitmproxy.test import tflow

        shape_req = http.Request.make(
            "POST",
            "https://api.example.com/v1/messages?beta=true&version=2",
            b"{}",
            {},
        )
        flow = tflow.tflow()
        flow.request = http.Request.make(
            "POST",
            "https://api.example.com/v1/messages",
            b"{}",
            {"authorization": "Bearer token"},
        )
        ctx = Context.from_flow(flow)

        apply_shape(shape_req, ctx, ["authorization", "host"])
        assert flow.request.query.get("beta") == "true"
        assert flow.request.query.get("version") == "2"
