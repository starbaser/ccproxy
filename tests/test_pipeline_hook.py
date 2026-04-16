"""Tests for HookSpec, HookRegistry, and @hook decorator."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from ccproxy.pipeline.context import Context
from ccproxy.pipeline.hook import (
    HookSpec,
    _HookRegistry,
    always_true,
    get_registry,
    hook,
)


def _make_ctx() -> Context:
    flow = MagicMock()
    flow.id = "test-id"
    flow.request.content = json.dumps({"model": "test-model", "messages": [], "metadata": {}}).encode()
    flow.request.headers = {}
    return Context.from_flow(flow)


class TestHookRegistry:
    def setup_method(self):
        self.reg = _HookRegistry()

    def test_register_and_get(self):
        spec = HookSpec(name="my_hook", handler=lambda ctx, p: ctx)
        self.reg.register_spec(spec)
        assert self.reg.get_spec("my_hook") is spec

    def test_get_missing_returns_none(self):
        assert self.reg.get_spec("nonexistent") is None

    def test_get_all_specs(self):
        spec1 = HookSpec(name="a", handler=lambda ctx, p: ctx)
        spec2 = HookSpec(name="b", handler=lambda ctx, p: ctx)
        self.reg.register_spec(spec1)
        self.reg.register_spec(spec2)
        all_specs = self.reg.get_all_specs()
        assert "a" in all_specs
        assert "b" in all_specs

    def test_clear(self):
        spec = HookSpec(name="h", handler=lambda ctx, p: ctx)
        self.reg.register_spec(spec)
        self.reg.clear()
        assert self.reg.get_all_specs() == {}

    def test_get_registry_returns_global(self):
        reg = get_registry()
        assert isinstance(reg, _HookRegistry)


class TestHookDecorator:
    def test_registers_hook(self):
        reg = get_registry()

        @hook(reads=["key"], writes=["out"])
        def my_unique_test_hook(ctx: Context, params: dict) -> Context:
            return ctx

        spec = reg.get_spec("my_unique_test_hook")
        assert spec is not None
        assert "key" in spec.reads
        assert "out" in spec.writes

    def test_attaches_spec_to_function(self):
        @hook(reads=[], writes=[])
        def another_test_hook(ctx: Context, params: dict) -> Context:
            return ctx

        assert hasattr(another_test_hook, "_hook_spec")
        assert another_test_hook._hook_spec.name == "another_test_hook"

    def test_finds_guard_by_convention(self):
        import sys
        import types

        # Create a fake module with a guard function
        mod = types.ModuleType("fake_hook_module")
        mod.__name__ = "fake_hook_module"

        def my_conv_hook_guard(ctx: Context) -> bool:
            return False

        mod.my_conv_hook_guard = my_conv_hook_guard

        def my_conv_hook(ctx: Context, params: dict) -> Context:
            return ctx

        my_conv_hook.__module__ = "fake_hook_module"
        sys.modules["fake_hook_module"] = mod

        try:
            hook(reads=[], writes=[])(my_conv_hook)
            spec = get_registry().get_spec("my_conv_hook")
            assert spec is not None
            assert spec.guard is my_conv_hook_guard
        finally:
            del sys.modules["fake_hook_module"]

    def test_default_guard_is_always_true(self):
        @hook(reads=[], writes=[])
        def no_guard_hook(ctx: Context, params: dict) -> Context:
            return ctx

        spec = get_registry().get_spec("no_guard_hook")
        assert spec is not None
        ctx = _make_ctx()
        assert spec.guard(ctx) is True

    def test_explicit_guard_overrides_convention(self):
        def my_guard(ctx: Context) -> bool:
            return False

        @hook(reads=[], writes=[], guard=my_guard)
        def explicit_guard_hook(ctx: Context, params: dict) -> Context:
            return ctx

        spec = get_registry().get_spec("explicit_guard_hook")
        assert spec is not None
        assert spec.guard is my_guard


class TestAlwaysTrue:
    def test_always_true_returns_true(self):
        ctx = _make_ctx()
        assert always_true(ctx) is True
