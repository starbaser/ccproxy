"""Pipeline router — DAG-driven hook execution at the mitmproxy layer.

Builds PipelineExecutor instances from config and wires them as
mitmproxy addons. Two stages: inbound (pre-transform) and outbound
(post-transform), each with their own DAG.
"""

from __future__ import annotations

import importlib
import logging
from typing import TYPE_CHECKING, Any

from ccproxy.inspector.flow_store import InspectorMeta
from ccproxy.pipeline.executor import PipelineExecutor
from ccproxy.pipeline.hook import HookSpec, get_registry

if TYPE_CHECKING:
    from mitmproxy.http import HTTPFlow

    from ccproxy.inspector.router import InspectorRouter

logger = logging.getLogger(__name__)


def _load_hooks(hook_entries: list[str | dict[str, Any]]) -> list[HookSpec]:
    """Import hook modules and collect registered HookSpecs.

    Each entry is either a module path string or a dict with
    ``hook`` (module path) and optional ``params``.
    """
    hook_priority_map: dict[str, int] = {}
    hook_params_map: dict[str, dict[str, Any]] = {}

    for idx, entry in enumerate(hook_entries):
        params: dict[str, Any] = {}
        if isinstance(entry, str):
            module_path = entry
        else:
            module_path = str(entry.get("hook", ""))
            params = entry.get("params", {})
            if not module_path:
                continue

        try:
            mod = importlib.import_module(module_path)
        except ImportError:
            logger.error("Failed to import hook module: %s", module_path)
            continue

        for attr_name in dir(mod):
            obj = getattr(mod, attr_name, None)
            if callable(obj) and hasattr(obj, "_hook_spec"):
                hook_name: str = obj._hook_spec.name  # type: ignore[union-attr]
                hook_priority_map[hook_name] = idx
                if params:
                    hook_params_map[hook_name] = params

    all_specs = get_registry().get_all_specs()
    hook_specs: list[HookSpec] = []
    max_priority = len(hook_entries)

    for name, spec in all_specs.items():
        if name not in hook_priority_map:
            continue
        if name in hook_params_map:
            spec.params = hook_params_map[name]
        spec.priority = hook_priority_map.get(name, max_priority)
        hook_specs.append(spec)

    return hook_specs


def build_executor(hook_entries: list[str | dict[str, Any]]) -> PipelineExecutor:
    """Build a PipelineExecutor from config hook entries."""
    specs = _load_hooks(hook_entries)
    return PipelineExecutor(hooks=specs)


def register_pipeline_routes(
    router: InspectorRouter,
    executor: PipelineExecutor,
) -> None:
    """Register a pipeline executor as a request handler on the router."""
    from ccproxy.inspector.router import RouteType

    @router.route("/{path}", rtype=RouteType.REQUEST)
    def handle_pipeline(flow: HTTPFlow, **kwargs: object) -> None:  # pyright: ignore[reportUnusedFunction]
        if flow.metadata.get(InspectorMeta.DIRECTION) != "inbound":
            return

        executor.execute(flow)
