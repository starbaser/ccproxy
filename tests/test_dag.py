"""Tests for HookDAG dependency resolution and priority ordering."""

from __future__ import annotations

from graphlib import CycleError

import pytest

from ccproxy.pipeline.dag import HookDAG
from ccproxy.pipeline.hook import HookSpec


def _noop(ctx, params):
    return ctx


def make_spec(name: str, *, reads=(), writes=(), priority: int = 0) -> HookSpec:
    return HookSpec(
        name=name,
        handler=_noop,
        reads=frozenset(reads),
        writes=frozenset(writes),
        priority=priority,
    )


class TestExecutionOrder:
    def test_single_hook(self):
        dag = HookDAG([make_spec("only")])
        assert dag.execution_order == ["only"]

    def test_no_deps_alphabetic_fallback(self):
        """Independent hooks with equal priority fall back to insertion/heap order."""
        hooks = [make_spec("a"), make_spec("b"), make_spec("c")]
        dag = HookDAG(hooks)
        assert set(dag.execution_order) == {"a", "b", "c"}
        assert len(dag.execution_order) == 3

    def test_dependency_ordering(self):
        """Writer must precede reader."""
        hooks = [
            make_spec("reader", reads=["key"]),
            make_spec("writer", writes=["key"]),
        ]
        dag = HookDAG(hooks)
        order = dag.execution_order
        assert order.index("writer") < order.index("reader")

    def test_chain_ordering(self):
        """A writes key1 -> B reads key1 writes key2 -> C reads key2."""
        hooks = [
            make_spec("c", reads=["key2"]),
            make_spec("a", writes=["key1"]),
            make_spec("b", reads=["key1"], writes=["key2"]),
        ]
        dag = HookDAG(hooks)
        order = dag.execution_order
        assert order.index("a") < order.index("b")
        assert order.index("b") < order.index("c")

    def test_cycle_raises(self):
        hooks = [
            make_spec("x", reads=["b_key"], writes=["a_key"]),
            make_spec("y", reads=["a_key"], writes=["b_key"]),
        ]
        with pytest.raises(CycleError):
            HookDAG(hooks)


class TestPriorityTiebreaking:
    def test_priority_tiebreaking(self):
        """Priority field breaks ties among independent hooks."""
        hooks = [
            make_spec("c_hook", priority=2),
            make_spec("a_hook", priority=0),
            make_spec("b_hook", priority=1),
        ]
        dag = HookDAG(hooks)
        assert dag.execution_order == ["a_hook", "b_hook", "c_hook"], (
            f"Expected priority ordering, got {dag.execution_order}"
        )

    def test_priority_respects_dependencies(self):
        """Dependencies override priority ordering."""
        hooks = [
            make_spec("a_hook", writes=["key"], priority=2),
            make_spec("b_hook", reads=["key"], priority=0),
        ]
        dag = HookDAG(hooks)
        assert dag.execution_order == ["a_hook", "b_hook"], (
            f"Dependencies should override priority, got {dag.execution_order}"
        )

    def test_priority_default_is_zero(self):
        spec = make_spec("h")
        assert spec.priority == 0

    def test_priority_negative_runs_first(self):
        """Negative priority values are valid and sort before zero."""
        hooks = [
            make_spec("normal", priority=0),
            make_spec("urgent", priority=-10),
        ]
        dag = HookDAG(hooks)
        assert dag.execution_order == ["urgent", "normal"]

    def test_priority_mixed_deps_and_priority(self):
        """Three hooks: x (prio 5) is independent, a->b chain (prio 0)."""
        hooks = [
            make_spec("x", priority=5),
            make_spec("a", writes=["k"], priority=0),
            make_spec("b", reads=["k"], priority=0),
        ]
        dag = HookDAG(hooks)
        order = dag.execution_order
        # x has highest priority value so runs last among independent hooks
        # a and b form a chain so a < b always
        assert order.index("a") < order.index("b")
        assert order.index("x") > order.index("a")


class TestParallelGroups:
    def test_independent_hooks_in_one_group(self):
        hooks = [make_spec("a"), make_spec("b"), make_spec("c")]
        dag = HookDAG(hooks)
        groups = dag.parallel_groups
        assert len(groups) == 1
        assert groups[0] == {"a", "b", "c"}

    def test_chain_produces_sequential_groups(self):
        hooks = [
            make_spec("a", writes=["k1"]),
            make_spec("b", reads=["k1"], writes=["k2"]),
            make_spec("c", reads=["k2"]),
        ]
        dag = HookDAG(hooks)
        groups = dag.parallel_groups
        assert len(groups) == 3
        assert groups[0] == {"a"}
        assert groups[1] == {"b"}
        assert groups[2] == {"c"}

    def test_parallel_groups_contain_all_hooks(self):
        hooks = [make_spec("a", writes=["k"]), make_spec("b"), make_spec("c", reads=["k"])]
        dag = HookDAG(hooks)
        all_hooks = set()
        for g in dag.parallel_groups:
            all_hooks |= g
        assert all_hooks == {"a", "b", "c"}


class TestGetHooksInOrder:
    def test_returns_specs_in_order(self):
        hooks = [make_spec("writer", writes=["k"]), make_spec("reader", reads=["k"])]
        dag = HookDAG(hooks)
        specs = dag.get_hooks_in_order()
        assert [s.name for s in specs] == dag.execution_order

    def test_get_hook_by_name(self):
        dag = HookDAG([make_spec("foo")])
        spec = dag.get_hook("foo")
        assert spec.name == "foo"

    def test_get_hook_missing_raises(self):
        dag = HookDAG([make_spec("foo")])
        with pytest.raises(KeyError):
            dag.get_hook("missing")


class TestDependencyQueries:
    def test_get_dependencies(self):
        hooks = [make_spec("writer", writes=["k"]), make_spec("reader", reads=["k"])]
        dag = HookDAG(hooks)
        assert dag.get_dependencies("reader") == {"writer"}
        assert dag.get_dependencies("writer") == set()

    def test_get_dependents(self):
        hooks = [make_spec("writer", writes=["k"]), make_spec("reader", reads=["k"])]
        dag = HookDAG(hooks)
        assert dag.get_dependents("writer") == {"reader"}
        assert dag.get_dependents("reader") == set()


class TestToMermaid:
    def test_basic_dependency_graph(self):
        hooks = [make_spec("writer", writes=["k"]), make_spec("reader", reads=["k"])]
        dag = HookDAG(hooks)
        mermaid = dag.to_mermaid()
        assert "graph TD" in mermaid
        assert "writer --> reader" in mermaid

    def test_independent_hook_appears_standalone(self):
        dag = HookDAG([make_spec("solo")])
        mermaid = dag.to_mermaid()
        assert "solo" in mermaid

    def test_no_duplicate_edges(self):
        hooks = [make_spec("a", writes=["k1", "k2"]), make_spec("b", reads=["k1", "k2"])]
        dag = HookDAG(hooks)
        mermaid = dag.to_mermaid()
        # Should appear exactly once
        assert mermaid.count("a --> b") == 1


class TestToAscii:
    def test_single_hook_ascii(self):
        dag = HookDAG([make_spec("my_hook", reads=["r"], writes=["w"])])
        ascii_art = dag.to_ascii()
        assert "my_hook" in ascii_art

    def test_chain_ascii_has_arrows(self):
        hooks = [
            make_spec("step1", writes=["k1"]),
            make_spec("step2", reads=["k1"], writes=["k2"]),
            make_spec("step3", reads=["k2"]),
        ]
        dag = HookDAG(hooks)
        ascii_art = dag.to_ascii()
        assert "step1" in ascii_art
        assert "step2" in ascii_art
        assert "step3" in ascii_art
        assert "│" in ascii_art or "▼" in ascii_art

    def test_parallel_hooks_ascii(self):
        hooks = [make_spec("a"), make_spec("b"), make_spec("c")]
        dag = HookDAG(hooks)
        ascii_art = dag.to_ascii()
        assert "PARALLEL" in ascii_art

    def test_single_group_no_arrows(self):
        dag = HookDAG([make_spec("only")])
        ascii_art = dag.to_ascii()
        assert "only" in ascii_art
