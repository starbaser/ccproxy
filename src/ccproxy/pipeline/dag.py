"""DAG-based dependency management for hooks.

Uses graphlib.TopologicalSorter to compute execution order
from reads/writes declarations.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from graphlib import CycleError, TopologicalSorter
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
        """Compute execution order via topological sort.

        Raises:
            CycleError: If dependencies form a cycle
        """
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

        # Compute order with TopologicalSorter
        sorter = TopologicalSorter(deps)

        try:
            self._execution_order = list(sorter.static_order())
        except CycleError as e:
            logger.error("Cycle detected in hook dependencies: %s", e.args[1])
            raise

        # Compute parallel groups
        sorter = TopologicalSorter(deps)
        sorter.prepare()
        while sorter.is_active():
            ready = set(sorter.get_ready())
            self._parallel_groups.append(ready)
            sorter.done(*ready)

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

        # Add edges
        edges_added: set[tuple[str, str]] = set()
        for hook_name, hook_deps in deps.items():
            for dep in hook_deps:
                edge = (dep, hook_name)
                if edge not in edges_added:
                    lines.append(f"    {dep} --> {hook_name}")
                    edges_added.add(edge)

        # Add isolated nodes (no dependencies)
        for name in self._hooks:
            if not deps[name] and not self.get_dependents(name):
                lines.append(f"    {name}")

        return "\n".join(lines)

    def to_ascii(self) -> str:
        """Generate ASCII representation of the DAG.

        Returns:
            ASCII art string showing hook dependencies
        """
        lines: list[str] = []
        deps = self._build_dependencies()

        for i, group in enumerate(self._parallel_groups):
            if i > 0:
                # Draw arrows from previous group
                prev_group = self._parallel_groups[i - 1]
                for hook_name in group:
                    hook_deps = deps[hook_name]
                    from_prev = hook_deps & prev_group
                    if from_prev:
                        lines.append("       │")
                        lines.append("       ▼")

            # Draw group
            group_hooks = sorted(group)
            if len(group_hooks) == 1:
                spec = self._hooks[group_hooks[0]]
                lines.append(f"┌{'─' * 40}┐")
                lines.append(f"│ {group_hooks[0]:<38} │")
                if spec.reads:
                    reads_str = ", ".join(sorted(spec.reads))
                    lines.append(f"│   reads: {reads_str:<28} │")
                if spec.writes:
                    writes_str = ", ".join(sorted(spec.writes))
                    lines.append(f"│   writes: {writes_str:<27} │")
                lines.append(f"└{'─' * 40}┘")
            else:
                # Multiple hooks in parallel
                lines.append(f"┌{'─' * 40}┐")
                lines.append(f"│ PARALLEL: {', '.join(group_hooks):<27} │")
                lines.append(f"└{'─' * 40}┘")

        return "\n".join(lines)

    def validate(self) -> list[str]:
        """Validate the DAG configuration.

        Returns:
            List of warning messages (empty if valid)
        """
        warnings: list[str] = []

        # Check for reads without writers
        for hook_name, spec in self._hooks.items():
            for read_key in spec.reads:
                if read_key not in self._key_writers:
                    warnings.append(f"Hook '{hook_name}' reads '{read_key}' but no hook writes it")

        # Check for unused writes
        for write_key, writers in self._key_writers.items():
            readers = self._key_readers.get(write_key, set())
            if not readers:
                for writer in writers:
                    warnings.append(f"Hook '{writer}' writes '{write_key}' but no hook reads it")

        return warnings
