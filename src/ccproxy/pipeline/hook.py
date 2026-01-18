"""Hook specification and decorator.

Defines the HookSpec class and @hook decorator for declaring
dependencies via reads/writes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ccproxy.pipeline.context import Context


# Type aliases
GuardFn = Callable[["Context"], bool]
HandlerFn = Callable[["Context", dict[str, Any]], "Context"]


def always_true(ctx: Context) -> bool:
    """Default guard that always returns True."""
    return True


@dataclass
class HookSpec:
    """Specification for a pipeline hook.

    Attributes:
        name: Unique hook identifier
        handler: Function that transforms context
        guard: Predicate that determines if handler should run
        reads: Keys this hook reads from context
        writes: Keys this hook writes to context
        params: Static parameters passed to handler
    """

    name: str
    handler: HandlerFn
    guard: GuardFn = always_true
    reads: frozenset[str] = field(default_factory=frozenset)
    writes: frozenset[str] = field(default_factory=frozenset)
    params: dict[str, Any] = field(default_factory=dict)

    def __hash__(self) -> int:
        return hash(self.name)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, HookSpec):
            return NotImplemented
        return self.name == other.name

    def should_run(self, ctx: Context) -> bool:
        """Check if this hook should run for the given context.

        Args:
            ctx: Pipeline context

        Returns:
            True if guard passes, False otherwise
        """
        return self.guard(ctx)

    def execute(self, ctx: Context, extra_params: dict[str, Any] | None = None) -> Context:
        """Execute the hook handler.

        Args:
            ctx: Pipeline context
            extra_params: Additional parameters to merge with static params

        Returns:
            Modified context
        """
        params = dict(self.params)
        if extra_params:
            params.update(extra_params)
        return self.handler(ctx, params)


class _HookRegistry:
    """Global registry for hooks decorated with @hook."""

    def __init__(self) -> None:
        self._hooks: dict[str, HookSpec] = {}
        self._pending: dict[str, dict[str, Any]] = {}

    def register_spec(self, spec: HookSpec) -> None:
        """Register a hook specification."""
        self._hooks[spec.name] = spec

    def get_spec(self, name: str) -> HookSpec | None:
        """Get a hook specification by name."""
        return self._hooks.get(name)

    def get_all_specs(self) -> dict[str, HookSpec]:
        """Get all registered hook specifications."""
        return dict(self._hooks)

    def store_pending(self, name: str, metadata: dict[str, Any]) -> None:
        """Store pending metadata for a hook being decorated."""
        self._pending[name] = metadata

    def get_pending(self, name: str) -> dict[str, Any] | None:
        """Get and remove pending metadata."""
        return self._pending.pop(name, None)

    def clear(self) -> None:
        """Clear all registered hooks (for testing)."""
        self._hooks.clear()
        self._pending.clear()


# Global registry
_registry = _HookRegistry()


def get_registry() -> _HookRegistry:
    """Get the global hook registry."""
    return _registry


def hook(
    *,
    reads: list[str] | None = None,
    writes: list[str] | None = None,
    guard: GuardFn | None = None,
) -> Callable[[HandlerFn], HandlerFn]:
    """Decorator to register a function as a pipeline hook.

    Args:
        reads: Keys this hook reads from context
        writes: Keys this hook writes to context
        guard: Predicate that determines if handler should run

    Returns:
        Decorator function

    Example:
        @hook(reads=["model"], writes=["metadata.ccproxy_model_name"])
        def rule_evaluator(ctx: Context, params: dict) -> Context:
            ...

        # Define guard separately (naming convention: {hook_name}_guard)
        def rule_evaluator_guard(ctx: Context) -> bool:
            return True
    """

    def decorator(fn: HandlerFn) -> HandlerFn:
        # Try to find guard function by convention
        resolved_guard = guard
        if resolved_guard is None:
            # Look for {fn_name}_guard in the same module
            import sys

            module = sys.modules.get(fn.__module__)
            if module:
                guard_name = f"{fn.__name__}_guard"
                resolved_guard = getattr(module, guard_name, None)

        spec = HookSpec(
            name=fn.__name__,
            handler=fn,
            guard=resolved_guard or always_true,
            reads=frozenset(reads or []),
            writes=frozenset(writes or []),
        )
        _registry.register_spec(spec)

        # Attach spec to function for introspection
        fn._hook_spec = spec  # type: ignore[attr-defined]
        return fn

    return decorator


def create_hook_spec(
    name: str,
    handler: HandlerFn,
    *,
    reads: list[str] | None = None,
    writes: list[str] | None = None,
    guard: GuardFn | None = None,
    params: dict[str, Any] | None = None,
) -> HookSpec:
    """Create a HookSpec programmatically (without decorator).

    Args:
        name: Unique hook identifier
        handler: Function that transforms context
        reads: Keys this hook reads from context
        writes: Keys this hook writes to context
        guard: Predicate that determines if handler should run
        params: Static parameters passed to handler

    Returns:
        HookSpec instance
    """
    return HookSpec(
        name=name,
        handler=handler,
        guard=guard or always_true,
        reads=frozenset(reads or []),
        writes=frozenset(writes or []),
        params=params or {},
    )
