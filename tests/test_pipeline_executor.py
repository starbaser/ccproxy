"""Tests for PipelineExecutor."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

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


def _make_flow(body: dict | None = None) -> MagicMock:
    flow = MagicMock()
    flow.id = "test-flow-id"
    flow.request.content = json.dumps(
        body
        or {
            "model": "test-model",
            "messages": [{"role": "user", "content": "hello"}],
        }
    ).encode()
    flow.request.headers = {}
    return flow


@pytest.fixture(autouse=True)
def cleanup():
    from ccproxy.config import clear_config_instance

    yield
    clear_config_instance()


class TestPipelineExecutorBasic:
    def test_executes_empty_pipeline(self):
        flow = _make_flow()
        executor = PipelineExecutor(hooks=[])
        executor.execute(flow)
        body = json.loads(flow.request.content)
        assert body["model"] == "test-model"

    def test_executes_single_hook(self):
        calls = []

        def record(ctx, params):
            calls.append("ran")
            return ctx

        flow = _make_flow()
        executor = PipelineExecutor(hooks=[make_spec("h", handler=record)])
        executor.execute(flow)
        assert calls == ["ran"]

    def test_error_isolation_continues(self):
        """A failing hook should not block subsequent hooks."""
        calls = []

        def after(ctx, params):
            calls.append("after")
            return ctx

        flow = _make_flow()
        executor = PipelineExecutor(
            hooks=[
                make_spec("fail", handler=_failing),
                make_spec("after", handler=after),
            ]
        )
        executor.execute(flow)
        assert "after" in calls

    def test_passes_extra_params(self):
        received = {}

        def capture(ctx, params):
            received.update(params)
            return ctx

        flow = _make_flow()
        executor = PipelineExecutor(
            hooks=[make_spec("h", handler=capture)],
            extra_params={"my_key": "my_val"},
        )
        executor.execute(flow)
        assert received["my_key"] == "my_val"

    def test_hook_override_force_skip(self):
        calls = []

        def record(ctx, params):
            calls.append("ran")
            return ctx

        flow = _make_flow()
        flow.request.headers["x-ccproxy-hooks"] = "-h"
        executor = PipelineExecutor(hooks=[make_spec("h", handler=record)])
        executor.execute(flow)
        assert calls == []

    def test_hook_override_force_run_skips_guard(self):
        calls = []

        def never_run(ctx: Context) -> bool:
            return False

        def record(ctx, params):
            calls.append("ran")
            return ctx

        flow = _make_flow()
        flow.request.headers["x-ccproxy-hooks"] = "+h"
        executor = PipelineExecutor(hooks=[make_spec("h", handler=record, guard=never_run)])
        executor.execute(flow)
        assert calls == ["ran"]

    def test_hook_override_logs_debug(self, caplog):
        import logging

        flow = _make_flow()
        flow.request.headers["x-ccproxy-hooks"] = "+h"
        executor = PipelineExecutor(hooks=[make_spec("h")])
        with caplog.at_level(logging.DEBUG, logger="ccproxy.pipeline.executor"):
            executor.execute(flow)

    def test_guard_skip_logs_debug(self, caplog):
        import logging

        def never_run(ctx: Context) -> bool:
            return False

        flow = _make_flow()
        executor = PipelineExecutor(hooks=[make_spec("h", guard=never_run)])
        with caplog.at_level(logging.DEBUG, logger="ccproxy.pipeline.executor"):
            executor.execute(flow)
        assert any("skipped" in r.message for r in caplog.records)

    def test_hook_mutates_body_and_commits(self):
        """Hook body mutations are flushed to flow.request.content."""

        def touch_metadata(ctx, params):
            ctx.metadata["touched"] = True
            return ctx

        flow = _make_flow()
        executor = PipelineExecutor(hooks=[make_spec("touch", handler=touch_metadata)])
        executor.execute(flow)
        body = json.loads(flow.request.content)
        assert body["metadata"]["touched"] is True

    def test_hook_mutates_headers_live(self):
        """Hook header mutations are applied to flow.request.headers immediately."""

        def set_hdr(ctx, params):
            ctx.set_header("x-test", "injected")
            return ctx

        flow = _make_flow()
        executor = PipelineExecutor(hooks=[make_spec("hdr", handler=set_hdr)])
        executor.execute(flow)
        assert flow.request.headers["x-test"] == "injected"


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
    def _make_flow_ctx(self, body: dict | None = None) -> Context:
        flow = _make_flow(body)
        return Context.from_flow(flow)

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
        ctx = self._make_flow_ctx()
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
        ctx = self._make_flow_ctx()
        s.execute(ctx, {"extra": "val"})
        assert received["base"] == "param"
        assert received["extra"] == "val"
