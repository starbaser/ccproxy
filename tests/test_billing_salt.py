"""Tests for ccproxy.specs.billing_salt — JSON file lookup."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ccproxy.specs.billing_salt import (
    clear_salts_cache,
    get_billing_salt_for_version,
    load_billing_salts,
)


@pytest.fixture
def salts_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point ``get_config_dir`` at ``tmp_path`` so the salts file lives there."""
    monkeypatch.setenv("CCPROXY_CONFIG_DIR", str(tmp_path))
    clear_salts_cache()
    return tmp_path / "billing_salts.json"


def test_missing_file_returns_empty(salts_file: Path) -> None:
    """No file at ``{config_dir}/billing_salts.json`` → empty map, no error."""
    assert load_billing_salts() == {}
    assert get_billing_salt_for_version("2.1.87") is None


def test_loads_version_salt_pairs(salts_file: Path) -> None:
    salts_file.write_text(json.dumps({"2.1.26": "0123456789ab", "2.1.87": "fedcba987654"}))
    assert load_billing_salts() == {"2.1.26": "0123456789ab", "2.1.87": "fedcba987654"}
    assert get_billing_salt_for_version("2.1.26") == "0123456789ab"
    assert get_billing_salt_for_version("2.1.87") == "fedcba987654"
    assert get_billing_salt_for_version("9.9.9") is None


def test_unparseable_json_returns_empty(salts_file: Path) -> None:
    salts_file.write_text("not json")
    assert load_billing_salts() == {}


def test_non_object_root_returns_empty(salts_file: Path) -> None:
    """A list at the root is not a valid version→salt map."""
    salts_file.write_text(json.dumps(["2.1.26", "abcdef"]))
    assert load_billing_salts() == {}


def test_non_string_values_skipped(salts_file: Path) -> None:
    """Entries whose values aren't strings are filtered out."""
    salts_file.write_text(json.dumps({"2.1.26": "abc", "2.1.87": 12345, "2.1.99": None}))
    salts = load_billing_salts()
    assert salts == {"2.1.26": "abc"}


def test_mtime_cache_invalidates_on_edit(salts_file: Path) -> None:
    """Editing the file is picked up without restart."""
    import os
    import time

    salts_file.write_text(json.dumps({"2.1.26": "first"}))
    os.utime(salts_file, (time.time() - 100, time.time() - 100))
    assert load_billing_salts() == {"2.1.26": "first"}

    salts_file.write_text(json.dumps({"2.1.26": "second"}))
    os.utime(salts_file, (time.time(), time.time()))
    assert load_billing_salts() == {"2.1.26": "second"}


def test_repeat_load_uses_cache(salts_file: Path) -> None:
    """Multiple calls without mtime change return the same cached object."""
    salts_file.write_text(json.dumps({"2.1.26": "abc"}))
    first = load_billing_salts()
    second = load_billing_salts()
    assert first is second
