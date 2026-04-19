"""Tests for the husk outbound hook."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from mitmproxy import http
from mitmproxy.test import tflow

from ccproxy.compliance.store import SeedStore, clear_store_instance
from ccproxy.hooks.husk import HuskParams, husk, husk_guard
from ccproxy.inspector.flow_store import InspectorMeta
from ccproxy.pipeline.context import Context


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
    from ccproxy.compliance.store import _store_lock
    from ccproxy.config import CCProxyConfig, set_config_instance

    set_config_instance(CCProxyConfig())
    seed_store = SeedStore(tmp_path / "seeds")

    import ccproxy.compliance.store as store_mod

    with _store_lock:
        store_mod._store_instance = seed_store
    yield seed_store
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


class TestHuskGuard:
    def test_reverse_with_transform_passes(self) -> None:
        ctx = Context.from_flow(_make_flow(reverse=True))
        assert husk_guard(ctx) is True

    def test_wireguard_without_oauth_rejected(self) -> None:
        ctx = Context.from_flow(_make_flow(reverse=False))
        assert husk_guard(ctx) is False

    def test_wireguard_with_oauth_passes(self) -> None:
        ctx = Context.from_flow(_make_flow(reverse=False, oauth_injected=True))
        assert husk_guard(ctx) is True

    def test_no_transform_rejected(self) -> None:
        ctx = Context.from_flow(_make_flow(reverse=True, has_transform=False))
        assert husk_guard(ctx) is False

    def test_no_record_rejected(self) -> None:
        flow = _make_flow(reverse=True)
        flow.metadata = {}
        ctx = Context.from_flow(flow)
        assert husk_guard(ctx) is False


class TestHuskParams:
    def test_defaults_empty_lists(self) -> None:
        params = HuskParams()
        assert params.prepare == []
        assert params.fill == []

    def test_accepts_dotted_paths(self) -> None:
        params = HuskParams(
            prepare=["ccproxy.compliance.prepare.strip_auth_headers"],
            fill=["ccproxy.compliance.fill.fill_model"],
        )
        assert params.prepare == ["ccproxy.compliance.prepare.strip_auth_headers"]
        assert params.fill == ["ccproxy.compliance.fill.fill_model"]


class TestHuskHook:
    def test_no_op_when_no_seed(self, store: SeedStore) -> None:
        flow = _make_flow(reverse=True, body={"model": "x"})
        original_host = flow.request.host
        ctx = Context.from_flow(flow)
        husk(ctx, {})
        assert flow.request.host == original_host

    def test_no_op_when_no_transform(self, store: SeedStore) -> None:
        store.add("anthropic", _seed_flow())
        flow = _make_flow(reverse=True, has_transform=False, body={"model": "x"})
        original_host = flow.request.host
        ctx = Context.from_flow(flow)
        husk(ctx, {})
        assert flow.request.host == original_host

    def test_applies_seed_shape_and_fills_content(self, store: SeedStore) -> None:
        store.add(
            "anthropic",
            _seed_flow(
                host="api.anthropic.com",
                path="/v1/messages",
                body={"messages": [{"role": "user", "content": "seed"}], "envelope_field": "v"},
                headers={"x-seed-header": "yes", "user-agent": "seed-cli/1.0"},
            ),
        )

        flow = _make_flow(
            reverse=True,
            provider="anthropic",
            body={"model": "m", "messages": [{"role": "user", "content": "incoming"}]},
        )
        ctx = Context.from_flow(flow)

        husk(
            ctx,
            {
                "prepare": ["ccproxy.compliance.prepare.strip_request_content"],
                "fill": [
                    "ccproxy.compliance.fill.fill_model",
                    "ccproxy.compliance.fill.fill_messages",
                ],
            },
        )

        assert flow.request.host == "api.anthropic.com"
        assert flow.request.path == "/v1/messages"
        assert flow.request.headers["x-seed-header"] == "yes"

        body = json.loads(flow.request.content or b"{}")
        assert body["model"] == "m"
        assert body["messages"] == [{"role": "user", "content": "incoming"}]
        assert body["envelope_field"] == "v"

    def test_default_params_means_pure_seed_shape(self, store: SeedStore) -> None:
        store.add(
            "anthropic",
            _seed_flow(body={"seed_only": True}, headers={"x-seed": "v"}),
        )
        flow = _make_flow(reverse=True, body={"unrelated": True})
        ctx = Context.from_flow(flow)
        husk(ctx, {})
        assert flow.request.headers["x-seed"] == "v"
        body = json.loads(flow.request.content or b"{}")
        assert body == {"seed_only": True}

    def test_works_with_different_provider(self, store: SeedStore) -> None:
        store.add(
            "gemini",
            _seed_flow(host="generativelanguage.googleapis.com", path="/v1beta/models/x:generateContent"),
        )
        flow = _make_flow(reverse=True, provider="gemini", body={"model": "gemini-2.5"})
        ctx = Context.from_flow(flow)
        husk(ctx, {})
        assert flow.request.host == "generativelanguage.googleapis.com"


class TestResolveCallable:
    def test_resolves_real_dotted_path(self) -> None:
        from ccproxy.hooks.husk import _resolve_callable

        fn = _resolve_callable("ccproxy.compliance.prepare.strip_auth_headers")
        from ccproxy.compliance.prepare import strip_auth_headers

        assert fn is strip_auth_headers

    def test_empty_dotted_raises(self) -> None:
        from ccproxy.hooks.husk import _resolve_callable

        with pytest.raises(ValueError, match="invalid dotted path"):
            _resolve_callable("nodotshere")
