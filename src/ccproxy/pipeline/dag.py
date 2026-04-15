"""DAG-based dependency management for hooks.

Uses Kahn's algorithm with a min-heap to compute execution order
from reads/writes declarations, with priority tie-breaking.
"""

from __future__ import annotations

from collections import defaultdict
from graphlib import CycleError
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ccproxy.pipeline.hook import HookSpec


class HookDAG:
    """Directed Acyclic Graph for hook dependencies.

    Builds dependencies from reads/writes declarations:
    - If Hook A writes key X and Hook B reads key X, then B depends on A
    - Uses topological sort to determine execution order
    """

    def __init__(self, hooks: list[HookSpec]) -> None:
        self._hooks: dict[str, HookSpec] = {h.name: h for h in hooks}
        self._key_writers: dict[str, set[str]] = defaultdict(set)
        self._execution_order: list[str] = []
        self._parallel_groups: list[set[str]] = []

        self._build_key_index()
        self._compute_order()

    def _build_key_index(self) -> None:
        """Build index of which hooks write which keys."""
        for name, spec in self._hooks.items():
            for key in spec.writes:
                self._key_writers[key].add(name)

    def _build_dependencies(self) -> dict[str, set[str]]:
        """Build dependency graph from reads/writes."""
        deps: dict[str, set[str]] = {name: set() for name in self._hooks}

        for hook_name, spec in self._hooks.items():
            for read_key in spec.reads:
                writers = self._key_writers.get(read_key, set())
                for writer in writers:
                    if writer != hook_name:
                        deps[hook_name].add(writer)

        return deps

    def _compute_order(self) -> None:
        """Compute execution order via topological sort with priority tie-breaking.

        Raises:
            CycleError: If dependencies form a cycle
        """
        import heapq

        deps = self._build_dependencies()

        in_degree = {name: len(dep_set) for name, dep_set in deps.items()}

        heap: list[tuple[int, str]] = [(self._hooks[n].priority, n) for n in self._hooks if in_degree[n] == 0]
        heapq.heapify(heap)

        order: list[str] = []
        while heap:
            _, node = heapq.heappop(heap)
            order.append(node)
            for dependent, dep_set in deps.items():
                if node in dep_set:
                    dep_set.discard(node)
                    in_degree[dependent] -= 1
                    if in_degree[dependent] == 0:
                        heapq.heappush(heap, (self._hooks[dependent].priority, dependent))

        if len(order) != len(self._hooks):
            raise CycleError("Cycle detected in hook dependencies")

        self._execution_order = order

        deps = self._build_dependencies()  # Rebuild since we mutated deps above
        in_degree = {name: len(dep_set) for name, dep_set in deps.items()}
        done: set[str] = set()
        self._parallel_groups = []

        while len(done) < len(self._hooks):
            ready = {n for n in self._hooks if n not in done and in_degree[n] == 0}
            if not ready:
                break
            self._parallel_groups.append(ready)
            done |= ready
            for dependent, dep_set in deps.items():
                if dependent not in done:
                    dep_set -= ready
                    in_degree[dependent] = len(dep_set)

    @property
    def execution_order(self) -> list[str]:
        return list(self._execution_order)

    @property
    def parallel_groups(self) -> list[set[str]]:
        """Groups of hooks with no inter-dependencies that can execute concurrently."""
        return [set(g) for g in self._parallel_groups]

    def get_hook(self, name: str) -> HookSpec:
        return self._hooks[name]

    def get_hooks_in_order(self) -> list[HookSpec]:
        return [self._hooks[name] for name in self._execution_order]

    def get_dependencies(self, hook_name: str) -> set[str]:
        deps = self._build_dependencies()
        return deps.get(hook_name, set())

    def get_dependents(self, hook_name: str) -> set[str]:
        deps = self._build_dependencies()
        dependents: set[str] = set()
        for name, hook_deps in deps.items():
            if hook_name in hook_deps:
                dependents.add(name)
        return dependents
