"""Tests for PipelineExecutor."""

from __future__ import annotations

from ccproxy.pipeline.context import Context
from ccproxy.pipeline.executor import PipelineExecutor
from ccproxy.pipeline.hook import HookSpec, always_true


def _noop(ctx: Context, params: dict) -> Context:
    return ctx


def _failing(ctx: Context, params: dict) -> Context:
    raise ValueError("intentional failure")


def make_spec(
    name: str,
    *,
    handler=None,
    reads=(),
    writes=(),
    priority: int = 0,
    guard=None,
) -> HookSpec:
    return HookSpec(
        name=name,
        handler=handler or _noop,
        guard=guard or always_true,
        reads=frozenset(reads),
        writes=frozenset(writes),
        priority=priority,
    )


def _make_data(**extra) -> dict:
    base = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "hi"}],
        "metadata": {},
    }
    base.update(extra)
    return base


class TestPipelineExecutorBasic:
    def test_executes_empty_pipeline(self):
        executor = PipelineExecutor(hooks=[])
        result = executor.execute(_make_data())
        assert result["model"] == "test-model"

    def test_executes_single_hook(self):
        calls = []

        def record(ctx, params):
            calls.append("ran")
            return ctx

        executor = PipelineExecutor(hooks=[make_spec("h", handler=record)])
        executor.execute(_make_data())
        assert calls == ["ran"]

    def test_error_isolation_continues(self):
        """A failing hook should not block subsequent hooks."""
        calls = []

        def after(ctx, params):
            calls.append("after")
            return ctx

        executor = PipelineExecutor(
            hooks=[
                make_spec("fail", handler=_failing),
                make_spec("after", handler=after),
            ]
        )
        executor.execute(_make_data())
        assert "after" in calls

    def test_passes_extra_params(self):
        received = {}

        def capture(ctx, params):
            received.update(params)
            return ctx

        executor = PipelineExecutor(
            hooks=[make_spec("h", handler=capture)],
            extra_params={"my_key": "my_val"},
        )
        executor.execute(_make_data())
        assert received["my_key"] == "my_val"

    def test_passes_user_api_key_dict(self):
        received = {}

        def capture(ctx, params):
            received.update(params)
            return ctx

        executor = PipelineExecutor(hooks=[make_spec("h", handler=capture)])
        executor.execute(_make_data(), user_api_key_dict={"token": "abc"})
        assert received["user_api_key_dict"] == {"token": "abc"}

    def test_hook_override_force_skip(self):
        calls = []

        def record(ctx, params):
            calls.append("ran")
            return ctx

        executor = PipelineExecutor(hooks=[make_spec("h", handler=record)])
        data = _make_data(
            proxy_server_request={"headers": {"x-ccproxy-hooks": "-h"}}
        )
        executor.execute(data)
        assert calls == []

    def test_hook_override_force_run_skips_guard(self):
        calls = []

        def never_run(ctx: Context) -> bool:
            return False

        def record(ctx, params):
            calls.append("ran")
            return ctx

        executor = PipelineExecutor(hooks=[make_spec("h", handler=record, guard=never_run)])
        data = _make_data(
            proxy_server_request={"headers": {"x-ccproxy-hooks": "+h"}}
        )
        executor.execute(data)
        assert calls == ["ran"]

    def test_hook_override_logs_debug(self, caplog):
        import logging

        executor = PipelineExecutor(hooks=[make_spec("h")])
        data = _make_data(
            proxy_server_request={"headers": {"x-ccproxy-hooks": "+h"}}
        )
        with caplog.at_level(logging.DEBUG, logger="ccproxy.pipeline.executor"):
            executor.execute(data)

    def test_guard_skip_logs_debug(self, caplog):
        import logging

        def never_run(ctx: Context) -> bool:
            return False

        executor = PipelineExecutor(hooks=[make_spec("h", guard=never_run)])
        with caplog.at_level(logging.DEBUG, logger="ccproxy.pipeline.executor"):
            executor.execute(_make_data())
        assert any("skipped" in r.message for r in caplog.records)


class TestPipelineExecutorIntrospection:
    def test_get_execution_order(self):
        executor = PipelineExecutor(hooks=[make_spec("a", writes=["k"]), make_spec("b", reads=["k"])])
        order = executor.get_execution_order()
        assert order.index("a") < order.index("b")

    def test_get_parallel_groups(self):
        executor = PipelineExecutor(hooks=[make_spec("x"), make_spec("y")])
        groups = executor.get_parallel_groups()
        assert len(groups) == 1
        assert groups[0] == {"x", "y"}

    def test_to_mermaid(self):
        executor = PipelineExecutor(hooks=[make_spec("a", writes=["k"]), make_spec("b", reads=["k"])])
        mermaid = executor.to_mermaid()
        assert "graph TD" in mermaid

    def test_to_ascii(self):
        executor = PipelineExecutor(hooks=[make_spec("single")])
        ascii_art = executor.to_ascii()
        assert "single" in ascii_art


class TestHookSpec:
    def test_hash_by_name(self):
        s1 = make_spec("h")
        s2 = make_spec("h")
        assert hash(s1) == hash(s2)
        assert s1 == s2

    def test_eq_different_names(self):
        s1 = make_spec("a")
        s2 = make_spec("b")
        assert s1 != s2

    def test_eq_non_hookspec(self):
        s = make_spec("h")
        assert s.__eq__("not-a-hookspec") == NotImplemented

    def test_should_run_default_guard(self):
        s = make_spec("h")
        ctx = Context.from_litellm_data(_make_data())
        assert s.should_run(ctx) is True

    def test_execute_passes_params(self):
        received = {}

        def capture(ctx, params):
            received.update(params)
            return ctx

        s = HookSpec(
            name="h",
            handler=capture,
            params={"base": "param"},
        )
        ctx = Context.from_litellm_data(_make_data())
        s.execute(ctx, {"extra": "val"})
        assert received["base"] == "param"
        assert received["extra"] == "val"
