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

    def test_runtime_warning_on_missing_read_key(self, caplog):
        """Hook reads a key not in the request body or headers → runtime warning."""
        import logging

        flow = _make_flow(body={"model": "m"})
        flow.request.path = "/v1/messages"
        executor = PipelineExecutor(hooks=[make_spec("reader", reads=["ghost_key"])])

        with caplog.at_level(logging.WARNING, logger="ccproxy.pipeline.executor"):
            executor.execute(flow)

        assert any("ghost_key" in r.message for r in caplog.records)
        assert any("trace_id=test-flow-id" in r.message for r in caplog.records)
        assert any("path=/v1/messages" in r.message for r in caplog.records)

    def test_no_warning_when_key_present_in_body(self, caplog):
        """`reads=["metadata"]` resolves silently when body has metadata."""
        import logging

        flow = _make_flow(body={"model": "m", "metadata": {"user_id": "foo"}})
        executor = PipelineExecutor(hooks=[make_spec("h", reads=["metadata"])])

        with caplog.at_level(logging.WARNING, logger="ccproxy.pipeline.executor"):
            executor.execute(flow)

        assert not any("unavailable keys" in r.message for r in caplog.records)

    def test_no_warning_when_key_present_in_header(self, caplog):
        """`reads=["authorization"]` resolves silently when header is set."""
        import logging

        flow = _make_flow()
        flow.request.headers = {"authorization": "Bearer x"}
        executor = PipelineExecutor(hooks=[make_spec("h", reads=["authorization"])])

        with caplog.at_level(logging.WARNING, logger="ccproxy.pipeline.executor"):
            executor.execute(flow)

        assert not any("unavailable keys" in r.message for r in caplog.records)

    def test_earlier_hook_writes_satisfy_later_reads(self, caplog):
        """A key produced by an earlier hook's writes must not trigger a warning
        for a later hook that reads it."""
        import logging

        flow = _make_flow()
        executor = PipelineExecutor(
            hooks=[
                make_spec("writer", writes=["computed_key"], priority=0),
                make_spec("reader", reads=["computed_key"], priority=1),
            ]
        )

        with caplog.at_level(logging.WARNING, logger="ccproxy.pipeline.executor"):
            executor.execute(flow)

        assert not any("computed_key" in r.message for r in caplog.records)

    def test_dot_path_read_resolves(self, caplog):
        """`reads=["metadata.user_id"]` resolves against nested body dict."""
        import logging

        flow = _make_flow(body={"model": "m", "metadata": {"user_id": "foo"}})
        executor = PipelineExecutor(hooks=[make_spec("h", reads=["metadata.user_id"])])

        with caplog.at_level(logging.WARNING, logger="ccproxy.pipeline.executor"):
            executor.execute(flow)

        assert not any("unavailable keys" in r.message for r in caplog.records)

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
