"""Dynamic hook loading from config entries.

Imports hook modules by dotted path (triggering @hook registration),
then filters the global registry by the entries the caller declared.
Validates YAML-supplied params against each hook's declared Pydantic
model (if any) and drops params for hooks that declare no model.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

from pydantic import ValidationError

from ccproxy.pipeline.hook import HookSpec, get_registry

logger = logging.getLogger(__name__)


def load_hooks(entries: list[str | dict[str, Any]]) -> list[HookSpec]:
    """Resolve a config hook-list into a list of HookSpec objects.

    Each entry is either a dotted module path string (the hook fn's
    module) or a dict ``{"hook": "<module_path>", "params": {...}}``.

    Side effects:
    - Imports each module, triggering @hook registration.
    - Mutates the singleton HookSpec objects in the global registry
      by assigning their ``params`` and ``priority`` fields per entry.

    NOTE: this function mutates singleton specs in the global registry.
    Calling it twice (e.g., inbound then outbound) modifies the same
    objects between calls. Safe when the two entry lists are disjoint
    (which they are in show_status and production wiring), but be aware
    if you introduce a case where the same hook appears in both lists.
    """
    hook_priority_map: dict[str, int] = {}
    hook_params_map: dict[str, dict[str, Any]] = {}

    for idx, entry in enumerate(entries):
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
    max_priority = len(entries)

    for name, spec in all_specs.items():
        if name not in hook_priority_map:
            continue
        params = hook_params_map.get(name, {})
        if params and spec.model is not None:
            try:
                validated = spec.model(**params)
            except ValidationError as exc:
                raise ValueError(f"Hook {spec.name!r} params failed validation: {exc}") from exc
            spec.params = validated.model_dump()
        elif params and spec.model is None:
            logger.warning(
                "Hook %r received YAML params but declares no model=; ignoring",
                name,
            )
            spec.params = {}
        elif params:
            spec.params = params
        spec.priority = hook_priority_map.get(name, max_priority)
        hook_specs.append(spec)

    return hook_specs
