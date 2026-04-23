"""Shape hook executor — DAG-ordered sub-pipeline for shape mutations.

Reuses the outer pipeline's ``HookDAG`` for topological ordering and
``load_hooks`` for module import + registry lookup. Caches resolved
specs per hook-list to avoid per-request import overhead.
"""

from __future__ import annotations

import logging
from typing import Any

from ccproxy.pipeline.context import Context
from ccproxy.pipeline.dag import HookDAG
from ccproxy.pipeline.hook import HookSpec
from ccproxy.pipeline.loader import load_hooks

logger = logging.getLogger(__name__)

_shape_hook_cache: dict[tuple[str, ...], list[HookSpec]] = {}


def execute_shape_hooks(
    shape_ctx: Context,
    incoming_ctx: Context,
    hook_entries: list[str],
) -> Context:
    """Load and execute shape hooks in DAG order against shape_ctx."""
    if not hook_entries:
        return shape_ctx

    cache_key = tuple(hook_entries)
    if cache_key not in _shape_hook_cache:
        _shape_hook_cache[cache_key] = load_hooks(hook_entries)

    specs = _shape_hook_cache[cache_key]
    dag = HookDAG(specs)
    extra: dict[str, Any] = {"incoming_ctx": incoming_ctx}

    for name in dag.execution_order:
        spec = dag.get_hook(name)
        if spec.should_run(shape_ctx):
            logger.debug("Executing shape hook '%s'", name)
            shape_ctx = spec.execute(shape_ctx, extra)

    return shape_ctx


def clear_shape_hook_cache() -> None:
    """Reset the cached shape hook specs. Called by test cleanup."""
    _shape_hook_cache.clear()
