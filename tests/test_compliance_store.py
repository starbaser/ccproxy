"""Tests for ccproxy.compliance.store.SeedStore."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from mitmproxy import http
from mitmproxy.test import tflow

from ccproxy.compliance.store import SeedStore


@pytest.fixture()
def seeds_dir(tmp_path: Path) -> Path:
    return tmp_path / "seeds"


def _flow(host: str = "api.anthropic.com", path: str = "/v1/messages") -> http.HTTPFlow:
    f = tflow.tflow()
    f.request = http.Request.make(
        "POST",
        f"https://{host}{path}",
        b'{"hello": "world"}',
        {"x-custom": "v"},
    )
    return f


class TestSeedStore:
    def test_init_creates_directory(self, seeds_dir: Path) -> None:
        assert not seeds_dir.exists()
        SeedStore(seeds_dir)
        assert seeds_dir.is_dir()

    def test_add_and_pick_roundtrip(self, seeds_dir: Path) -> None:
        store = SeedStore(seeds_dir)
        store.add("anthropic", _flow())
        picked = store.pick("anthropic")
        assert picked is not None
        assert picked.request is not None
        assert picked.request.pretty_host == "api.anthropic.com"

    def test_pick_returns_none_when_missing(self, seeds_dir: Path) -> None:
        store = SeedStore(seeds_dir)
        assert store.pick("anthropic") is None

    def test_pick_returns_most_recent(self, seeds_dir: Path) -> None:
        store = SeedStore(seeds_dir)
        store.add("anthropic", _flow(host="old.example"))
        store.add("anthropic", _flow(host="new.example"))
        picked = store.pick("anthropic")
        assert picked is not None
        assert picked.request is not None
        assert picked.request.pretty_host == "new.example"

    def test_clear_removes_seed_file(self, seeds_dir: Path) -> None:
        store = SeedStore(seeds_dir)
        store.add("anthropic", _flow())
        assert (seeds_dir / "anthropic.mflow").exists()
        store.clear("anthropic")
        assert not (seeds_dir / "anthropic.mflow").exists()

    def test_clear_is_idempotent(self, seeds_dir: Path) -> None:
        SeedStore(seeds_dir).clear("never-seeded")

    def test_list_providers(self, seeds_dir: Path) -> None:
        store = SeedStore(seeds_dir)
        store.add("anthropic", _flow())
        store.add("gemini", _flow())
        assert store.list_providers() == ["anthropic", "gemini"]

    def test_isolates_per_provider(self, seeds_dir: Path) -> None:
        store = SeedStore(seeds_dir)
        store.add("anthropic", _flow(host="a.example"))
        store.add("gemini", _flow(host="g.example"))
        a = store.pick("anthropic")
        g = store.pick("gemini")
        assert a is not None and a.request is not None
        assert g is not None and g.request is not None
        assert a.request.pretty_host == "a.example"
        assert g.request.pretty_host == "g.example"

    def test_persists_across_instances(self, seeds_dir: Path) -> None:
        SeedStore(seeds_dir).add("anthropic", _flow())
        picked = SeedStore(seeds_dir).pick("anthropic")
        assert picked is not None


class TestGetStoreSingleton:
    def test_get_store_uses_configured_seeds_dir(self, tmp_path: Path) -> None:
        from ccproxy.compliance.store import clear_store_instance, get_store
        from ccproxy.config import CCProxyConfig, set_config_instance

        explicit_dir = tmp_path / "custom-seeds"
        config = CCProxyConfig()
        config.compliance.seeds_dir = str(explicit_dir)
        set_config_instance(config)
        clear_store_instance()

        store = get_store()
        store.add("anthropic", _flow())
        assert (explicit_dir / "anthropic.mflow").exists()
        clear_store_instance()

    def test_get_store_falls_back_to_config_dir(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        from ccproxy.compliance.store import clear_store_instance, get_store
        from ccproxy.config import CCProxyConfig, set_config_instance

        monkeypatch.setenv("CCPROXY_CONFIG_DIR", str(tmp_path))
        set_config_instance(CCProxyConfig())
        clear_store_instance()

        store = get_store()
        store.add("anthropic", _flow())
        assert (tmp_path / "compliance" / "seeds" / "anthropic.mflow").exists()
        clear_store_instance()

    def test_get_store_is_a_singleton(self, tmp_path: Path, monkeypatch: Any) -> None:
        from ccproxy.compliance.store import clear_store_instance, get_store
        from ccproxy.config import CCProxyConfig, set_config_instance

        monkeypatch.setenv("CCPROXY_CONFIG_DIR", str(tmp_path))
        set_config_instance(CCProxyConfig())
        clear_store_instance()

        assert get_store() is get_store()
        clear_store_instance()
