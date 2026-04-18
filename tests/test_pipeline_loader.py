"""Tests for ccproxy.pipeline.loader.load_hooks."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from pydantic import BaseModel

from ccproxy.pipeline.hook import HookSpec, get_registry
from ccproxy.pipeline.loader import load_hooks


class _RateLimitParams(BaseModel):
    max_rpm: int = 60
    burst: int = 10


_PRODUCTION_HOOK_MODULES = [
    "ccproxy.hooks.forward_oauth",
    "ccproxy.hooks.extract_session_id",
    "ccproxy.hooks.inject_mcp_notifications",
    "ccproxy.hooks.verbose_mode",
    "ccproxy.hooks.stamp_compliance",
]


@pytest.fixture(autouse=True)
def _clear_registry() -> Any:
    """Isolate the global hook registry between tests.

    clear() wipes singleton specs from the global registry but does NOT
    cause Python to re-execute @hook decorators on next import (the module
    is already cached in sys.modules). Reload production hook modules both
    before (for this test's setup) and after (to restore for subsequent tests
    that rely on the production registry state).
    """
    import importlib
    import sys

    def _reload_all() -> None:
        for mod_path in _PRODUCTION_HOOK_MODULES:
            mod = sys.modules.get(mod_path)
            if mod is not None:
                importlib.reload(mod)

    _reload_all()
    yield
    get_registry().clear()
    _reload_all()


class TestLoadHooks:
    def test_empty_entries_returns_empty_list(self) -> None:
        assert load_hooks([]) == []

    def test_unknown_module_logged_and_skipped(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.ERROR, logger="ccproxy.pipeline.loader"):
            result = load_hooks(["ccproxy.hooks.nonexistent_xyz"])
        assert result == []
        assert "nonexistent_xyz" in caplog.text

    def test_string_entry_no_params(self) -> None:
        result = load_hooks(["ccproxy.hooks.forward_oauth"])
        assert len(result) == 1
        assert result[0].name == "forward_oauth"
        assert result[0].params == {}

    def test_valid_params_with_model(self) -> None:
        # Register a fake hook with a Pydantic model directly into the registry
        def _fake_rate_limit(ctx: Any, params: dict[str, Any]) -> Any:
            return ctx

        spec = HookSpec(
            name="_fake_rate_limit",
            handler=_fake_rate_limit,
            reads=frozenset(),
            writes=frozenset(),
            model=_RateLimitParams,
        )
        spec._fake_rate_limit = _fake_rate_limit  # type: ignore[attr-defined]
        _fake_rate_limit._hook_spec = spec  # type: ignore[attr-defined]
        get_registry().register_spec(spec)

        # Simulate a module-path entry by importing a module that has the spec
        # registered — we already did it above, so call load_hooks with the
        # hook name mapped by injecting the priority directly.
        # Since load_hooks imports by module path, we need it findable.
        # Use ccproxy.hooks.forward_oauth as a known importable module that
        # registers forward_oauth, then exercise the model path via the
        # directly-registered fake spec by driving load_hooks' second pass.
        #
        # Simpler: call load_hooks with a string entry for forward_oauth (which
        # has no model) is case (3). For model validation, register and exercise
        # via the registry directly using a dict entry on a real importable hook.
        # forward_oauth doesn't have a model, so use a custom spec + hack:
        # patch load_hooks to avoid the import step and drive the validation path.
        # Instead: use monkeypatching of importlib.import_module is complex.
        #
        # Cleanest approach: register the spec, then call load_hooks with a
        # string entry for a module that will be found (forward_oauth) but
        # also trigger the model validation path via the registry loop.
        # This requires that the spec is already in the registry, which it is.
        #
        # The registry loop in load_hooks iterates get_registry().get_all_specs()
        # and processes only names in hook_priority_map. hook_priority_map is
        # populated from the imported module's _hook_spec attributes.
        # To get _fake_rate_limit into hook_priority_map, we need a module that
        # exposes _fake_rate_limit with ._hook_spec. We can create a fake module.
        import sys
        import types

        fake_mod = types.ModuleType("ccproxy_test_fake_ratelimit_mod")
        fake_mod._fake_rate_limit = _fake_rate_limit  # type: ignore[attr-defined]
        sys.modules["ccproxy_test_fake_ratelimit_mod"] = fake_mod

        try:
            result = load_hooks([{"hook": "ccproxy_test_fake_ratelimit_mod", "params": {"max_rpm": 120}}])
        finally:
            del sys.modules["ccproxy_test_fake_ratelimit_mod"]

        assert len(result) == 1
        assert result[0].name == "_fake_rate_limit"
        assert result[0].params == {"max_rpm": 120, "burst": 10}

    def test_invalid_params_with_model_raises_value_error(self) -> None:
        import sys
        import types

        def _fake_rate_limit2(ctx: Any, params: dict[str, Any]) -> Any:
            return ctx

        spec = HookSpec(
            name="_fake_rate_limit2",
            handler=_fake_rate_limit2,
            reads=frozenset(),
            writes=frozenset(),
            model=_RateLimitParams,
        )
        _fake_rate_limit2._hook_spec = spec  # type: ignore[attr-defined]
        get_registry().register_spec(spec)

        fake_mod = types.ModuleType("ccproxy_test_fake_ratelimit_mod2")
        fake_mod._fake_rate_limit2 = _fake_rate_limit2  # type: ignore[attr-defined]
        sys.modules["ccproxy_test_fake_ratelimit_mod2"] = fake_mod

        try:
            with pytest.raises(ValueError, match="_fake_rate_limit2"):
                load_hooks([{"hook": "ccproxy_test_fake_ratelimit_mod2", "params": {"max_rpm": "not-an-int"}}])
        finally:
            del sys.modules["ccproxy_test_fake_ratelimit_mod2"]

    def test_params_without_model_warns_and_drops(self, caplog: pytest.LogCaptureFixture) -> None:
        # forward_oauth declares no model=; params should be dropped with warning
        entry = {"hook": "ccproxy.hooks.forward_oauth", "params": {"timeout": 10}}
        with caplog.at_level(logging.WARNING, logger="ccproxy.pipeline.loader"):
            result = load_hooks([entry])
        assert len(result) == 1
        assert result[0].name == "forward_oauth"
        assert result[0].params == {}
        assert "no model=" in caplog.text

    def test_empty_hook_key_skipped(self) -> None:
        result = load_hooks([{"hook": "", "params": {}}])
        assert result == []

    def test_priority_assignment_preserved(self) -> None:
        result = load_hooks(
            [
                "ccproxy.hooks.forward_oauth",
                "ccproxy.hooks.verbose_mode",
            ]
        )
        names = [s.name for s in result]
        assert "forward_oauth" in names
        assert "verbose_mode" in names
        fo = next(s for s in result if s.name == "forward_oauth")
        vm = next(s for s in result if s.name == "verbose_mode")
        # forward_oauth is index 0 → priority 0; verbose_mode is index 1 → priority 1
        assert fo.priority == 0
        assert vm.priority == 1
