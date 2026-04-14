"""Hook specification and decorator.

Defines the HookSpec class and @hook decorator for declaring
dependencies via reads/writes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pydantic import BaseModel

    from ccproxy.pipeline.context import Context


# Type aliases
GuardFn = Callable[["Context"], bool]
HandlerFn = Callable[["Context", dict[str, Any]], "Context"]


def always_true(ctx: Context) -> bool:
    """Default guard that always returns True."""
    return True


@dataclass
class HookSpec:
    """Specification for a pipeline hook."""

    name: str
    handler: HandlerFn
    guard: GuardFn = always_true
    reads: frozenset[str] = field(default_factory=frozenset)  # pyright: ignore[reportUnknownVariableType]
    writes: frozenset[str] = field(default_factory=frozenset)  # pyright: ignore[reportUnknownVariableType]
    params: dict[str, Any] = field(default_factory=dict)  # pyright: ignore[reportUnknownVariableType]
    priority: int = 0
    model: type[BaseModel] | None = None

    def __hash__(self) -> int:
        return hash(self.name)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, HookSpec):
            return NotImplemented
        return self.name == other.name

    def should_run(self, ctx: Context) -> bool:
        """Check if this hook should run for the given context."""
        return self.guard(ctx)

    def execute(self, ctx: Context, extra_params: dict[str, Any] | None = None) -> Context:
        """Execute the hook handler."""
        params = dict(self.params)
        if extra_params:
            params.update(extra_params)
        return self.handler(ctx, params)


class _HookRegistry:
    """Global registry for hooks decorated with @hook."""

    def __init__(self) -> None:
        self._hooks: dict[str, HookSpec] = {}

    def register_spec(self, spec: HookSpec) -> None:
        self._hooks[spec.name] = spec

    def get_spec(self, name: str) -> HookSpec | None:
        return self._hooks.get(name)

    def get_all_specs(self) -> dict[str, HookSpec]:
        return dict(self._hooks)

    def clear(self) -> None:
        self._hooks.clear()


_registry = _HookRegistry()


def get_registry() -> _HookRegistry:
    return _registry


def hook(
    *,
    reads: list[str] | None = None,
    writes: list[str] | None = None,
    guard: GuardFn | None = None,
    model: type[BaseModel] | None = None,
) -> Callable[[HandlerFn], HandlerFn]:
    """Decorator to register a function as a pipeline hook.

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
            model=model,
        )
        _registry.register_spec(spec)

        # Attach spec to function for introspection
        fn._hook_spec = spec  # type: ignore[attr-defined]
        return fn

    return decorator
