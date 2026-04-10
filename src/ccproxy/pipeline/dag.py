"""DAG-based dependency management for hooks.

Uses Kahn's algorithm with a min-heap to compute execution order
from reads/writes declarations, with priority tie-breaking.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from graphlib import CycleError
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ccproxy.pipeline.hook import HookSpec

logger = logging.getLogger(__name__)


class HookDAG:
    """Directed Acyclic Graph for hook dependencies.

    Builds dependencies from reads/writes declarations:
    - If Hook A writes key X and Hook B reads key X, then B depends on A
    - Uses topological sort to determine execution order
    """

    def __init__(self, hooks: list[HookSpec]) -> None:
        """Initialize DAG with hook specifications.

        Args:
            hooks: List of HookSpec instances

        Raises:
            CycleError: If dependencies form a cycle
        """
        self._hooks: dict[str, HookSpec] = {h.name: h for h in hooks}
        self._key_writers: dict[str, set[str]] = defaultdict(set)
        self._key_readers: dict[str, set[str]] = defaultdict(set)
        self._execution_order: list[str] = []
        self._parallel_groups: list[set[str]] = []

        self._build_key_index()
        self._compute_order()

    def _build_key_index(self) -> None:
        """Build index of which hooks read/write which keys."""
        for name, spec in self._hooks.items():
            for key in spec.writes:
                self._key_writers[key].add(name)
            for key in spec.reads:
                self._key_readers[key].add(name)

    def _build_dependencies(self) -> dict[str, set[str]]:
        """Build dependency graph from reads/writes.

        Returns:
            Dict mapping hook name to set of hooks it depends on
        """
        deps: dict[str, set[str]] = {name: set() for name in self._hooks}

        for hook_name, spec in self._hooks.items():
            for read_key in spec.reads:
                # This hook depends on any hook that writes this key
                writers = self._key_writers.get(read_key, set())
                for writer in writers:
                    if writer != hook_name:
                        deps[hook_name].add(writer)

        return deps

    def _compute_order(self) -> None:
        """Compute execution order via topological sort with priority tie-breaking.

        Uses Kahn's algorithm with a min-heap to break ties among
        independent hooks using their priority field (lower = first).

        Raises:
            CycleError: If dependencies form a cycle
        """
        import heapq

        deps = self._build_dependencies()

        # Validate: warn about reads without writers
        for hook_name, spec in self._hooks.items():
            for read_key in spec.reads:
                if read_key not in self._key_writers:
                    logger.warning(
                        "Hook '%s' reads key '%s' but no hook writes it",
                        hook_name,
                        read_key,
                    )

        # Kahn's algorithm with min-heap for priority tie-breaking
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

        # Compute parallel groups (priority-sorted within each group)
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
        """Get hooks in execution order.

        Returns:
            List of hook names in dependency-safe order
        """
        return list(self._execution_order)

    @property
    def parallel_groups(self) -> list[set[str]]:
        """Get groups of hooks that can execute in parallel.

        Each group contains hooks with no inter-dependencies.

        Returns:
            List of sets, where each set contains hook names
            that can run concurrently
        """
        return [set(g) for g in self._parallel_groups]

    def get_hook(self, name: str) -> HookSpec:
        """Get hook specification by name.

        Args:
            name: Hook name

        Returns:
            HookSpec instance

        Raises:
            KeyError: If hook not found
        """
        return self._hooks[name]

    def get_hooks_in_order(self) -> list[HookSpec]:
        """Get hook specifications in execution order.

        Returns:
            List of HookSpec instances in dependency-safe order
        """
        return [self._hooks[name] for name in self._execution_order]

    def get_dependencies(self, hook_name: str) -> set[str]:
        """Get hooks that a given hook depends on.

        Args:
            hook_name: Name of the hook

        Returns:
            Set of hook names this hook depends on
        """
        deps = self._build_dependencies()
        return deps.get(hook_name, set())

    def get_dependents(self, hook_name: str) -> set[str]:
        """Get hooks that depend on a given hook.

        Args:
            hook_name: Name of the hook

        Returns:
            Set of hook names that depend on this hook
        """
        deps = self._build_dependencies()
        dependents: set[str] = set()
        for name, hook_deps in deps.items():
            if hook_name in hook_deps:
                dependents.add(name)
        return dependents

    def to_mermaid(self) -> str:
        """Generate Mermaid diagram of the DAG.

        Returns:
            Mermaid graph definition string
        """
        lines = ["graph TD"]
        deps = self._build_dependencies()

        edges_added: set[tuple[str, str]] = set()
        for hook_name, hook_deps in deps.items():
            for dep in hook_deps:
                edge = (dep, hook_name)
                if edge not in edges_added:
                    lines.append(f"    {dep} --> {hook_name}")
                    edges_added.add(edge)

        for name in self._hooks:
            if not deps[name] and not self.get_dependents(name):
                lines.append(f"    {name}")

        return "\n".join(lines)

    def to_ascii(self) -> str:
        """Generate unicode box-drawing representation of the DAG."""
        # Pre-compute all content lines per group to determine max width
        group_contents: list[list[str]] = []
        for group in self._parallel_groups:
            group_hooks = sorted(group)
            content: list[str] = []
            if len(group_hooks) == 1:
                spec = self._hooks[group_hooks[0]]
                content.append(group_hooks[0])
                if spec.reads:
                    content.append(f"  reads: {', '.join(sorted(spec.reads))}")
                if spec.writes:
                    content.append(f"  writes: {', '.join(sorted(spec.writes))}")
            else:
                content.append(f"PARALLEL: {', '.join(group_hooks)}")
            group_contents.append(content)

        width = max((max(len(s) for s in c) for c in group_contents), default=20) + 2

        lines: list[str] = []
        deps = self._build_dependencies()

        for i, (group, content) in enumerate(zip(self._parallel_groups, group_contents)):
            if i > 0:
                prev_group = self._parallel_groups[i - 1]
                has_dep = any(deps[h] & prev_group for h in group)
                if has_dep:
                    lines.append("  │")
                    lines.append("  ▼")

            lines.append(f"┌{'─' * width}┐")
            for text in content:
                lines.append(f"│ {text:<{width - 1}}│")
            lines.append(f"└{'─' * width}┘")

        return "\n".join(lines)

    def validate(self) -> list[str]:
        """Validate the DAG configuration.

        Returns:
            List of warning messages (empty if valid)
        """
        warnings: list[str] = []

        for hook_name, spec in self._hooks.items():
            for read_key in spec.reads:
                if read_key not in self._key_writers:
                    warnings.append(f"Hook '{hook_name}' reads '{read_key}' but no hook writes it")

        for write_key, writers in self._key_writers.items():
            readers = self._key_readers.get(write_key, set())
            if not readers:
                for writer in writers:
                    warnings.append(f"Hook '{writer}' writes '{write_key}' but no hook reads it")

        return warnings
