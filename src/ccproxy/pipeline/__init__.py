"""Conditional transformation pipeline for ccproxy hooks.

This module implements a formal hook pipeline with:
- Explicit guards and handlers
- DAG-based automatic ordering via reads/writes declarations
- SDK-controllable overrides via x-ccproxy-hooks header
"""

from ccproxy.pipeline.context import Context
from ccproxy.pipeline.dag import HookDAG
from ccproxy.pipeline.executor import PipelineExecutor
from ccproxy.pipeline.hook import HookSpec, hook
from ccproxy.pipeline.overrides import HookOverride, parse_overrides

__all__ = [
    "Context",
    "HookDAG",
    "HookOverride",
    "HookSpec",
    "PipelineExecutor",
    "hook",
    "parse_overrides",
]
