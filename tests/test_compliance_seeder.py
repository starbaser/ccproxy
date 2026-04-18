"""Tests for the ComplianceSeeder addon."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ccproxy.compliance.models import ComplianceProfile
from ccproxy.compliance.store import ProfileStore, clear_store_instance
from ccproxy.inspector.compliance_seeder import ComplianceSeeder, _load_classifier_config
from ccproxy.inspector.flow_store import FlowRecord, HttpSnapshot, InspectorMeta


@pytest.fixture()
def store(tmp_path: Path) -> ProfileStore:
    from ccproxy.compliance.store import _store_lock
    from ccproxy.config import CCProxyConfig, set_config_instance

    set_config_instance(CCProxyConfig())

    store = ProfileStore(tmp_path / "profiles.json", seed_profiles=None)

    import ccproxy.compliance.store as store_mod

    with _store_lock:
        store_mod._store_instance = store
    yield store
    clear_store_instance()


def _make_flow_with_snapshot(
    flow_id: str = "abc123",
    headers: dict[str, str] | None = None,
    body: dict | None = None,
    user_agent: str = "test-cli/1.0",
) -> MagicMock:
    """Create a mock flow with a FlowRecord containing an HttpSnapshot."""
    snapshot_headers = {"user-agent": user_agent, **(headers or {"x-app": "cli"})}
    snapshot_body = json.dumps(body or {"model": "test", "messages": [{"role": "user", "content": "hi"}]}).encode()

    snapshot = HttpSnapshot(
        headers=snapshot_headers,
        body=snapshot_body,
        method="POST",
        url="https://api.anthropic.com/v1/messages",
    )
    record = FlowRecord(direction="inbound", client_request=snapshot)

    flow = MagicMock()
    flow.id = flow_id
    flow.metadata = {InspectorMeta.RECORD: record}
    return flow


class TestComplianceSeeder:
    def test_seeds_profile_from_single_flow(self, store: ProfileStore):
        flow = _make_flow_with_snapshot()
        seeder = ComplianceSeeder()

        with patch.object(seeder, "_find_http_flow", return_value=flow):
            result_json = seeder.ccproxy_seed("abc123", "anthropic")

        result = json.loads(result_json)
        assert result["status"] == "ok"
        assert result["key"] == "anthropic/seed"
        assert result["flows_used"] == 1
        assert result["user_agent"] == "test-cli/1.0"

        profile = store.get_profile("anthropic")
        assert profile is not None
        assert profile.is_complete is True

    def test_seeds_profile_from_multiple_flows(self, store: ProfileStore):
        flow1 = _make_flow_with_snapshot(flow_id="f1", headers={"x-app": "cli", "beta": "v1"})
        flow2 = _make_flow_with_snapshot(flow_id="f2", headers={"x-app": "cli", "beta": "v1"})
        flow3 = _make_flow_with_snapshot(flow_id="f3", headers={"x-app": "cli", "beta": "v1"})

        seeder = ComplianceSeeder()

        def find_flow(fid: str) -> MagicMock | None:
            return {"f1": flow1, "f2": flow2, "f3": flow3}.get(fid)

        with patch.object(seeder, "_find_http_flow", side_effect=find_flow):
            result_json = seeder.ccproxy_seed("f1,f2,f3", "anthropic")

        result = json.loads(result_json)
        assert result["flows_used"] == 3

        profile = store.get_profile("anthropic")
        assert profile is not None
        names = {h.name for h in profile.headers}
        assert "x-app" in names
        assert "beta" in names

    def test_variable_headers_excluded_across_flows(self, store: ProfileStore):
        flow1 = _make_flow_with_snapshot(flow_id="f1", headers={"x-app": "cli", "x-req-id": "r1"})
        flow2 = _make_flow_with_snapshot(flow_id="f2", headers={"x-app": "cli", "x-req-id": "r2"})

        seeder = ComplianceSeeder()

        def find_flow(fid: str) -> MagicMock | None:
            return {"f1": flow1, "f2": flow2}.get(fid)

        with patch.object(seeder, "_find_http_flow", side_effect=find_flow):
            seeder.ccproxy_seed("f1,f2", "anthropic")

        profile = store.get_profile("anthropic")
        assert profile is not None
        names = {h.name for h in profile.headers}
        assert "x-app" in names
        assert "x-req-id" not in names

    def test_skips_flow_without_snapshot(self, store: ProfileStore):
        flow_good = _make_flow_with_snapshot(flow_id="good")
        flow_bad = MagicMock()
        flow_bad.id = "bad"
        flow_bad.metadata = {InspectorMeta.RECORD: FlowRecord(direction="inbound")}

        seeder = ComplianceSeeder()

        def find_flow(fid: str) -> MagicMock | None:
            return {"good": flow_good, "bad": flow_bad}.get(fid)

        with patch.object(seeder, "_find_http_flow", side_effect=find_flow):
            result_json = seeder.ccproxy_seed("good,bad", "anthropic")

        result = json.loads(result_json)
        assert result["flows_used"] == 1

    def test_skips_missing_flow(self, store: ProfileStore):
        flow = _make_flow_with_snapshot(flow_id="exists")
        seeder = ComplianceSeeder()

        def find_flow(fid: str) -> MagicMock | None:
            return flow if fid == "exists" else None

        with patch.object(seeder, "_find_http_flow", side_effect=find_flow):
            result_json = seeder.ccproxy_seed("exists,missing", "anthropic")

        result = json.loads(result_json)
        assert result["flows_used"] == 1

    def test_raises_on_no_valid_flows(self, store: ProfileStore):
        seeder = ComplianceSeeder()

        with (
            patch.object(seeder, "_find_http_flow", return_value=None),
            pytest.raises(ValueError, match="no valid flows"),
        ):
            seeder.ccproxy_seed("missing", "anthropic")

    def test_raises_on_empty_ids(self, store: ProfileStore):
        seeder = ComplianceSeeder()
        with pytest.raises(ValueError, match="no flow ids"):
            seeder.ccproxy_seed("", "anthropic")

    def test_overwrites_existing_profile(self, store: ProfileStore):
        old = ComplianceProfile(
            provider="anthropic",
            user_agent="old",
            created_at="2020-01-01T00:00:00+00:00",
            updated_at="2020-01-01T00:00:00+00:00",
            observation_count=1,
            is_complete=True,
            headers=[],
            body_fields=[],
        )
        store.set_profile("anthropic/seed", old)

        flow = _make_flow_with_snapshot(headers={"x-new": "header"})
        seeder = ComplianceSeeder()

        with patch.object(seeder, "_find_http_flow", return_value=flow):
            seeder.ccproxy_seed("abc123", "anthropic")

        profile = store.get_profile("anthropic")
        assert profile is not None
        assert profile.user_agent == "test-cli/1.0"


class TestLoadClassifierConfig:
    def test_returns_empty_on_no_config(self):
        with patch("ccproxy.config.get_config", side_effect=RuntimeError):
            headers, fields = _load_classifier_config()
        assert headers == frozenset()
        assert fields == frozenset()
