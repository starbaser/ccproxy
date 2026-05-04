"""Tests for ccproxy.specs.billing_salt — nested per-provider config accessors."""

from __future__ import annotations

import pytest

from ccproxy.config import (
    AnthropicShapingConfig,
    BillingConfig,
    CCProxyConfig,
    ShapingConfig,
    set_config_instance,
)
from ccproxy.specs.billing_salt import get_billing_cch_seed, get_billing_salt


def _set_config(*, salt: str | None = None, seed: str | None = None) -> None:
    """Install a CCProxyConfig with the given Anthropic billing fields."""
    set_config_instance(
        CCProxyConfig(
            shaping=ShapingConfig(
                providers={
                    "anthropic": AnthropicShapingConfig(
                        billing=BillingConfig(salt=salt, seed=seed),
                    ),
                },
            ),
        ),
    )


class TestGetBillingSalt:
    def test_returns_configured(self) -> None:
        _set_config(salt="0123456789ab")
        assert get_billing_salt() == "0123456789ab"

    def test_none_when_unset(self) -> None:
        _set_config(salt=None)
        assert get_billing_salt() is None

    def test_empty_treated_as_unset(self) -> None:
        _set_config(salt="")
        assert get_billing_salt() is None

    def test_none_when_no_anthropic_profile(self) -> None:
        set_config_instance(CCProxyConfig(shaping=ShapingConfig(providers={})))
        assert get_billing_salt() is None

    def test_env_ref_expansion(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_SALT", "deadbeefcafe")
        _set_config(salt="${MY_SALT}")
        assert get_billing_salt() == "deadbeefcafe"

    def test_env_ref_unset_resolves_to_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MISSING_SALT", raising=False)
        _set_config(salt="${MISSING_SALT}")
        assert get_billing_salt() is None

    def test_env_ref_partial_substitution(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``prefix-${VAR}`` interpolates inline."""
        monkeypatch.setenv("PART", "cafe")
        _set_config(salt="dead${PART}")
        assert get_billing_salt() == "deadcafe"


class TestGetBillingCchSeed:
    def test_parses_hex_with_prefix(self) -> None:
        _set_config(seed="0x0123456789ABCDEF")
        assert get_billing_cch_seed() == 0x0123456789ABCDEF

    def test_parses_bare_hex(self) -> None:
        _set_config(seed="0123456789ABCDEF")
        assert get_billing_cch_seed() == 0x0123456789ABCDEF

    def test_parses_lowercase_hex(self) -> None:
        _set_config(seed="0123456789abcdef")
        assert get_billing_cch_seed() == 0x0123456789ABCDEF

    def test_none_when_unset(self) -> None:
        _set_config(seed=None)
        assert get_billing_cch_seed() is None

    def test_empty_treated_as_unset(self) -> None:
        _set_config(seed="")
        assert get_billing_cch_seed() is None

    def test_unparseable_returns_none(self) -> None:
        _set_config(seed="not-a-hex-literal")
        assert get_billing_cch_seed() is None

    def test_env_ref_expansion(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_SEED", "0xCAFEBABE")
        _set_config(seed="${MY_SEED}")
        assert get_billing_cch_seed() == 0xCAFEBABE

    def test_env_ref_unset_resolves_to_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MISSING_SEED", raising=False)
        _set_config(seed="${MISSING_SEED}")
        assert get_billing_cch_seed() is None
