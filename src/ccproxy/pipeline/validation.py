"""Runtime access validation for debug mode.

Tracks which keys hooks actually access vs. what they declared.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

logger = logging.getLogger(__name__)


class AccessTracker:
    """Tracks runtime access to context keys.

    Use in debug mode to verify hooks only access declared keys.
    """

    def __init__(self) -> None:
        self._reads: dict[str, set[str]] = defaultdict(set)
        self._writes: dict[str, set[str]] = defaultdict(set)
        self._current_hook: str | None = None

    def start_hook(self, hook_name: str) -> None:
        """Mark start of hook execution.

        Args:
            hook_name: Name of the hook starting execution
        """
        self._current_hook = hook_name

    def end_hook(self) -> None:
        """Mark end of hook execution."""
        self._current_hook = None

    def record_read(self, key: str) -> None:
        """Record a key read.

        Args:
            key: Key that was read
        """
        if self._current_hook:
            self._reads[self._current_hook].add(key)

    def record_write(self, key: str) -> None:
        """Record a key write.

        Args:
            key: Key that was written
        """
        if self._current_hook:
            self._writes[self._current_hook].add(key)

    def validate(
        self,
        declared_reads: dict[str, frozenset[str]],
        declared_writes: dict[str, frozenset[str]],
    ) -> list[str]:
        """Validate actual access against declarations.

        Args:
            declared_reads: Mapping of hook name to declared read keys
            declared_writes: Mapping of hook name to declared write keys

        Returns:
            List of violation messages
        """
        violations: list[str] = []

        for hook_name, actual_reads in self._reads.items():
            declared = declared_reads.get(hook_name, frozenset())
            undeclared = actual_reads - declared
            if undeclared:
                violations.append(f"Hook '{hook_name}' read undeclared keys: {undeclared}")

        for hook_name, actual_writes in self._writes.items():
            declared = declared_writes.get(hook_name, frozenset())
            undeclared = actual_writes - declared
            if undeclared:
                violations.append(f"Hook '{hook_name}' wrote undeclared keys: {undeclared}")

        return violations

    def clear(self) -> None:
        """Clear all tracked access."""
        self._reads.clear()
        self._writes.clear()
        self._current_hook = None

    def get_summary(self) -> dict[str, Any]:
        """Get summary of all tracked access.

        Returns:
            Dict with reads and writes per hook
        """
        return {
            "reads": {k: sorted(v) for k, v in self._reads.items()},
            "writes": {k: sorted(v) for k, v in self._writes.items()},
        }


class TrackedContext:
    """Context wrapper that tracks key access.

    Wraps the real Context and records all reads/writes for validation.
    """

    def __init__(self, ctx: Any, tracker: AccessTracker) -> None:
        """Initialize tracked context.

        Args:
            ctx: Real Context instance
            tracker: AccessTracker to record access
        """
        object.__setattr__(self, "_ctx", ctx)
        object.__setattr__(self, "_tracker", tracker)

    def __getattr__(self, name: str) -> Any:
        ctx = object.__getattribute__(self, "_ctx")
        tracker = object.__getattribute__(self, "_tracker")

        # Record read access
        tracker.record_read(name)

        return getattr(ctx, name)

    def __setattr__(self, name: str, value: Any) -> None:
        ctx = object.__getattribute__(self, "_ctx")
        tracker = object.__getattribute__(self, "_tracker")

        # Record write access
        tracker.record_write(name)

        setattr(ctx, name, value)

    def unwrap(self) -> Any:
        """Get the underlying Context.

        Returns:
            The wrapped Context instance
        """
        return object.__getattribute__(self, "_ctx")
