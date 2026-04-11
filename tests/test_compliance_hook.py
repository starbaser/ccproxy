"""Tests for the apply_compliance outbound hook."""

import json
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ccproxy.compliance.models import (
    ComplianceProfile,
    ProfileFeatureHeader,
    ProfileFeatureSystem,
)
from ccproxy.compliance.store import ProfileStore, clear_store_instance
from ccproxy.hooks.apply_compliance import apply_compliance, apply_compliance_guard
from ccproxy.inspector.flow_store import InspectorMeta
from ccproxy.pipeline.context import Context


@dataclass
class _MockTransformMeta:
    provider: str
    model: str
    request_data: dict
    is_streaming: bool


@dataclass
class _MockRecord:
    transform: _MockTransformMeta | None = None
    client_request: None = None


def _make_flow(
    reverse: bool = False,
    has_transform: bool = True,
    provider: str = "anthropic",
    body: dict | None = None,
) -> MagicMock:
    from mitmproxy.proxy.mode_specs import ReverseMode

    flow = MagicMock()
    flow.request.headers = dict(body.get("_headers", {}) if body and "_headers" in body else {})
    body_content = body or {"model": "test"}
    body_content.pop("_headers", None)
    flow.request.content = json.dumps(body_content).encode()

    if reverse:
        flow.client_conn.proxy_mode = MagicMock(spec=ReverseMode)
    else:
        flow.client_conn.proxy_mode = MagicMock()

    record = _MockRecord(
        transform=_MockTransformMeta(provider, "model", {}, False) if has_transform else None,
    )
    flow.metadata = {InspectorMeta.RECORD: record}

    return flow


class TestApplyComplianceGuard:
    def test_passes_on_reverse_with_transform(self):
        flow = _make_flow(reverse=True, has_transform=True)
        ctx = Context.from_flow(flow)
        assert apply_compliance_guard(ctx) is True

    def test_rejects_wireguard_mode(self):
        flow = _make_flow(reverse=False, has_transform=True)
        ctx = Context.from_flow(flow)
        assert apply_compliance_guard(ctx) is False

    def test_rejects_no_transform(self):
        flow = _make_flow(reverse=True, has_transform=False)
        ctx = Context.from_flow(flow)
        assert apply_compliance_guard(ctx) is False

    def test_rejects_no_record(self):
        flow = _make_flow(reverse=True)
        flow.metadata = {}
        ctx = Context.from_flow(flow)
        assert apply_compliance_guard(ctx) is False


class TestApplyCompliance:
    @pytest.fixture()
    def store(self, tmp_path: Path) -> ProfileStore:
        from ccproxy.compliance.store import _store_lock
        from ccproxy.config import CCProxyConfig, set_config_instance

        set_config_instance(CCProxyConfig())

        store = ProfileStore(tmp_path / "profiles.json", min_observations=1, seed_anthropic=False)

        import ccproxy.compliance.store as store_mod

        with _store_lock:
            store_mod._store_instance = store
        yield store
        clear_store_instance()

    def test_applies_profile_headers(self, store: ProfileStore):
        from ccproxy.compliance.models import ObservationBundle

        store.submit_observation(ObservationBundle(
            provider="anthropic",
            user_agent="cli/1.0",
            headers={"x-app": "cli"},
            body_envelope={},
            system=None,
        ))

        flow = _make_flow(reverse=True, has_transform=True, provider="anthropic")
        ctx = Context.from_flow(flow)
        result = apply_compliance(ctx, {})
        assert result.get_header("x-app") == "cli"

    def test_applies_system_prompt(self, store: ProfileStore):
        from ccproxy.compliance.models import ObservationBundle

        store.submit_observation(ObservationBundle(
            provider="anthropic",
            user_agent="cli/1.0",
            headers={},
            body_envelope={},
            system="You are Claude",
        ))

        flow = _make_flow(reverse=True, has_transform=True, provider="anthropic",
                          body={"model": "test", "system": "Help me"})
        ctx = Context.from_flow(flow)
        result = apply_compliance(ctx, {})
        assert isinstance(result.system, list)
        assert result.system[0]["text"] == "You are Claude"
        assert result.system[1]["text"] == "Help me"

    def test_no_profile_no_changes(self, store: ProfileStore):
        flow = _make_flow(reverse=True, has_transform=True, provider="gemini")
        ctx = Context.from_flow(flow)
        result = apply_compliance(ctx, {})
        assert result.get_header("x-app") == ""
