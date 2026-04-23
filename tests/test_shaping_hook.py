"""Tests for the shape outbound hook."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from mitmproxy import http
from mitmproxy.test import tflow

from ccproxy.config import ProviderShapingConfig
from ccproxy.flows.store import InspectorMeta
from ccproxy.hooks.shape import shape, shape_guard
from ccproxy.pipeline.context import Context
from ccproxy.shaping.store import ShapeStore, clear_store_instance


@dataclass
class _MockTransformMeta:
    provider: str
    model: str = ""
    request_data: dict[str, Any] = field(default_factory=dict)
    is_streaming: bool = False


@dataclass
class _MockRecord:
    transform: _MockTransformMeta | None = None
    client_request: None = None


@pytest.fixture()
def store(tmp_path: Path) -> Any:
    from ccproxy.config import CCProxyConfig, set_config_instance

    from ccproxy.shaping.store import _store_lock

    set_config_instance(CCProxyConfig(
        shaping={"providers": {
            "anthropic": {
                "content_fields": ["model", "messages", "tools", "system", "stream", "max_tokens"],
                "merge_strategies": {"system": "prepend_shape"},
                "callbacks": [
                    "ccproxy.shaping.callbacks.regenerate_user_prompt_id",
                    "ccproxy.shaping.callbacks.regenerate_session_id",
                ],
                "capture": {"path_pattern": "^/v1/messages"},
            },
        }},
    ))
    shape_store = ShapeStore(tmp_path / "seeds")

    import ccproxy.shaping.store as store_mod

    with _store_lock:
        store_mod._store_instance = shape_store
    yield shape_store
    clear_store_instance()


def _make_flow(
    reverse: bool = False,
    has_transform: bool = True,
    provider: str = "anthropic",
    body: dict[str, Any] | None = None,
    oauth_injected: bool = False,
) -> http.HTTPFlow:
    from mitmproxy.proxy.mode_specs import ReverseMode

    flow = tflow.tflow()
    flow.request = http.Request.make(
        "POST",
        "https://incoming.example/v1",
        json.dumps(body or {}).encode(),
        {"user-agent": "incoming-cli/1.0"},
    )

    if reverse:
        flow.client_conn.proxy_mode = MagicMock(spec=ReverseMode)
    else:
        flow.client_conn.proxy_mode = MagicMock()

    record = _MockRecord(
        transform=_MockTransformMeta(provider=provider) if has_transform else None,
    )
    flow.metadata[InspectorMeta.RECORD] = record
    if oauth_injected:
        flow.metadata["ccproxy.oauth_injected"] = True
    return flow


def _seed_flow(
    host: str = "api.anthropic.com",
    path: str = "/v1/messages",
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> http.HTTPFlow:
    f = tflow.tflow()
    f.request = http.Request.make(
        "POST",
        f"https://{host}{path}",
        json.dumps(body or {"seed_only": True}).encode(),
        headers or {"x-seed-header": "yes"},
    )
    return f


class TestShapeGuard:
    def test_reverse_with_transform_passes(self) -> None:
        ctx = Context.from_flow(_make_flow(reverse=True))
        assert shape_guard(ctx) is True

    def test_wireguard_without_oauth_rejected(self) -> None:
        ctx = Context.from_flow(_make_flow(reverse=False))
        assert shape_guard(ctx) is False

    def test_wireguard_with_oauth_passes(self) -> None:
        ctx = Context.from_flow(_make_flow(reverse=False, oauth_injected=True))
        assert shape_guard(ctx) is True

    def test_no_transform_rejected(self) -> None:
        ctx = Context.from_flow(_make_flow(reverse=True, has_transform=False))
        assert shape_guard(ctx) is False

    def test_no_record_rejected(self) -> None:
        flow = _make_flow(reverse=True)
        flow.metadata = {}
        ctx = Context.from_flow(flow)
        assert shape_guard(ctx) is False


class TestShapeHook:
    def test_no_op_when_no_seed(self, store: ShapeStore) -> None:
        flow = _make_flow(reverse=True, body={"model": "x"})
        original_host = flow.request.host
        ctx = Context.from_flow(flow)
        shape(ctx, {})
        assert flow.request.host == original_host

    def test_no_op_when_no_transform(self, store: ShapeStore) -> None:
        store.add("anthropic", _seed_flow())
        flow = _make_flow(reverse=True, has_transform=False, body={"model": "x"})
        original_host = flow.request.host
        ctx = Context.from_flow(flow)
        shape(ctx, {})
        assert flow.request.host == original_host

    def test_applies_shape_and_injects_content(self, store: ShapeStore) -> None:
        store.add(
            "anthropic",
            _seed_flow(
                host="api.anthropic.com",
                path="/v1/messages",
                body={
                    "messages": [{"role": "user", "content": "seed"}],
                    "envelope_field": "v",
                    "system": [{"type": "text", "text": "shape-system"}],
                },
                headers={"x-seed-header": "yes", "user-agent": "seed-cli/1.0"},
            ),
        )

        flow = _make_flow(
            reverse=True,
            provider="anthropic",
            body={
                "model": "m",
                "messages": [{"role": "user", "content": "incoming"}],
                "system": "user-system",
            },
        )
        ctx = Context.from_flow(flow)
        shape(ctx, {})

        assert flow.request.host == "incoming.example"
        assert flow.request.headers["x-seed-header"] == "yes"

        body = json.loads(flow.request.content or b"{}")
        assert body["model"] == "m"
        assert body["messages"] == [{"role": "user", "content": "incoming"}]
        assert body["envelope_field"] == "v"
        # system: prepend_shape — shape system first, then incoming
        assert len(body["system"]) == 2
        assert body["system"][0]["text"] == "shape-system"
        assert body["system"][1]["text"] == "user-system"

    def test_no_op_when_no_provider_profile(self, store: ShapeStore) -> None:
        store.add("unknown_provider", _seed_flow())
        flow = _make_flow(reverse=True, provider="unknown_provider", body={"model": "x"})
        original_content = flow.request.content
        ctx = Context.from_flow(flow)
        shape(ctx, {})
        assert flow.request.content == original_content

    def test_identity_fields_persist(self, store: ShapeStore) -> None:
        store.add(
            "anthropic",
            _seed_flow(
                body={
                    "thinking": {"budget_tokens": 31999, "type": "enabled"},
                    "context_management": {"edits": []},
                    "messages": [],
                },
            ),
        )
        flow = _make_flow(reverse=True, body={"model": "m", "messages": [{"role": "user", "content": "hi"}]})
        ctx = Context.from_flow(flow)
        shape(ctx, {})

        body = json.loads(flow.request.content or b"{}")
        assert body["thinking"] == {"budget_tokens": 31999, "type": "enabled"}
        assert body["context_management"] == {"edits": []}


class TestUaFamilySkip:
    def test_matching_ua_skips_shaping(self, store: ShapeStore) -> None:
        store.add(
            "anthropic",
            _seed_flow(
                body={"messages": [], "envelope": True},
                headers={"user-agent": "claude-cli/2.1.87 (external, cli)", "x-seed": "yes"},
            ),
        )
        flow = _make_flow(
            reverse=True,
            body={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        )
        flow.request.headers["user-agent"] = "claude-cli/2.2.0 (external, cli)"
        original_content = flow.request.content
        ctx = Context.from_flow(flow)
        shape(ctx, {})
        assert flow.request.content == original_content
        assert "x-seed" not in flow.request.headers

    def test_different_ua_applies_shaping(self, store: ShapeStore) -> None:
        store.add(
            "anthropic",
            _seed_flow(
                body={"messages": [], "envelope": True},
                headers={"user-agent": "claude-cli/2.1.87", "x-seed": "yes"},
            ),
        )
        flow = _make_flow(
            reverse=True,
            body={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        )
        flow.request.headers["user-agent"] = "Anthropic/Python 0.86.0"
        ctx = Context.from_flow(flow)
        shape(ctx, {})
        assert flow.request.headers["x-seed"] == "yes"

    def test_missing_ua_applies_shaping(self, store: ShapeStore) -> None:
        store.add(
            "anthropic",
            _seed_flow(
                body={"messages": [], "envelope": True},
                headers={"user-agent": "claude-cli/2.1.87", "x-seed": "yes"},
            ),
        )
        flow = _make_flow(reverse=True, body={"model": "m", "messages": []})
        ctx = Context.from_flow(flow)
        shape(ctx, {})
        assert flow.request.headers["x-seed"] == "yes"


class TestResolveEntry:
    def test_resolves_real_dotted_path(self) -> None:
        from ccproxy.hooks.shape import _resolve_entry

        fn = _resolve_entry("ccproxy.shaping.prepare.strip_headers")
        from ccproxy.shaping.prepare import strip_headers

        assert fn is strip_headers

    def test_empty_dotted_raises(self) -> None:
        from ccproxy.hooks.shape import _resolve_entry

        with pytest.raises(ValueError, match="invalid dotted path"):
            _resolve_entry("nodotshere")
