"""Tests for ComplianceSeeder — raw flow saving to SeedStore."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from mitmproxy import http
from mitmproxy.test import tflow

from ccproxy.compliance.store import SeedStore, clear_store_instance
from ccproxy.inspector.compliance_seeder import ComplianceSeeder


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


def _flow(flow_id: str = "abc123") -> http.HTTPFlow:
    f = tflow.tflow()
    f.id = flow_id
    f.request = http.Request.make(
        "POST",
        "https://api.anthropic.com/v1/messages",
        b'{"model": "claude", "messages": [{"role": "user", "content": "hi"}]}',
        {"x-app": "cli", "user-agent": "test-cli/1.0"},
    )
    return f


def _run_seed(
    seeder: ComplianceSeeder,
    flows_by_id: dict[str, http.HTTPFlow],
    ids: str,
    provider: str,
) -> dict[str, Any]:
    with patch.object(
        seeder,
        "_find_http_flow",
        side_effect=lambda fid: flows_by_id.get(fid),
    ):
        result = seeder.ccproxy_seed(ids, provider)
    return json.loads(result)


class TestComplianceSeeder:
    def test_single_flow(self, store: SeedStore) -> None:
        seeder = ComplianceSeeder()
        result = _run_seed(seeder, {"abc123": _flow("abc123")}, "abc123", "anthropic")
        assert result["status"] == "ok"
        assert result["provider"] == "anthropic"
        assert result["flows_saved"] == 1
        assert result["missing"] == []
        assert store.pick("anthropic") is not None

    def test_multiple_flows(self, store: SeedStore) -> None:
        flows = {fid: _flow(fid) for fid in ("f1", "f2", "f3")}
        seeder = ComplianceSeeder()
        result = _run_seed(seeder, flows, "f1,f2,f3", "anthropic")
        assert result["flows_saved"] == 3

    def test_skips_missing_flows(self, store: SeedStore) -> None:
        seeder = ComplianceSeeder()
        result = _run_seed(
            seeder,
            {"exists": _flow("exists")},
            "exists,missing",
            "anthropic",
        )
        assert result["flows_saved"] == 1
        assert result["missing"] == ["missing"]

    def test_empty_ids_raises(self) -> None:
        seeder = ComplianceSeeder()
        with pytest.raises(ValueError, match="no flow ids"):
            seeder.ccproxy_seed("", "anthropic")

    def test_all_missing_reports_empty(self, store: SeedStore) -> None:
        seeder = ComplianceSeeder()
        result = _run_seed(seeder, {}, "missing", "anthropic")
        assert result["status"] == "empty"
        assert result["flows_saved"] == 0
        assert result["missing"] == ["missing"]

    def test_strips_whitespace_and_empty_tokens(self, store: SeedStore) -> None:
        seeder = ComplianceSeeder()
        result = _run_seed(
            seeder,
            {"f1": _flow("f1")},
            " f1 , ,",
            "anthropic",
        )
        assert result["flows_saved"] == 1

    def test_preserves_full_flow_on_disk(self, store: SeedStore) -> None:
        seeder = ComplianceSeeder()
        _run_seed(seeder, {"abc123": _flow("abc123")}, "abc123", "anthropic")
        picked = store.pick("anthropic")
        assert picked is not None
        assert picked.request is not None
        assert picked.request.method == "POST"
        assert picked.request.pretty_host == "api.anthropic.com"
        assert picked.request.headers.get("user-agent") == "test-cli/1.0"


class TestFindHttpFlow:
    def test_returns_none_when_view_missing(self) -> None:
        master = MagicMock()
        master.addons.get.return_value = None
        with patch("ccproxy.inspector.compliance_seeder.ctx") as mock_ctx:
            mock_ctx.master = master
            assert ComplianceSeeder._find_http_flow("x") is None

    def test_returns_flow_when_found(self) -> None:
        flow = _flow("abc")
        view = MagicMock()
        view.get_by_id.return_value = flow
        master = MagicMock()
        master.addons.get.return_value = view
        with patch("ccproxy.inspector.compliance_seeder.ctx") as mock_ctx:
            mock_ctx.master = master
            assert ComplianceSeeder._find_http_flow("abc") is flow

    def test_returns_none_for_non_http_flow(self) -> None:
        view = MagicMock()
        view.get_by_id.return_value = object()
        master = MagicMock()
        master.addons.get.return_value = view
        with patch("ccproxy.inspector.compliance_seeder.ctx") as mock_ctx:
            mock_ctx.master = master
            assert ComplianceSeeder._find_http_flow("x") is None
