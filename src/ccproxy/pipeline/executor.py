"""Pipeline executor with DAG-ordered execution.

Executes hooks in dependency-safe order with override support.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ccproxy.constants import OAuthConfigError
from ccproxy.pipeline.context import Context
from ccproxy.pipeline.dag import HookDAG
from ccproxy.pipeline.keyspace import extract_available_keys
from ccproxy.pipeline.overrides import (
    HookOverride,
    OverrideSet,
    extract_overrides_from_context,
)

if TYPE_CHECKING:
    from mitmproxy.http import HTTPFlow

    from ccproxy.pipeline.hook import HookSpec

logger = logging.getLogger(__name__)


class PipelineExecutor:
    """Executes hooks in DAG-ordered sequence with override support."""

    def __init__(
        self,
        hooks: list[HookSpec],
        extra_params: dict[str, Any] | None = None,
    ) -> None:
        self.dag = HookDAG(hooks)
        self.extra_params = extra_params or {}

        order = self.dag.execution_order
        logger.info("Pipeline execution order: %s", " → ".join(order))

        groups = self.dag.parallel_groups
        if any(len(g) > 1 for g in groups):
            logger.info(
                "Parallel execution groups: %s",
                [sorted(g) for g in groups],
            )

    def execute(self, flow: HTTPFlow) -> None:
        """Execute the hook pipeline against a mitmproxy flow.

        Builds a Context from the flow, runs all hooks in DAG order,
        then commits body mutations back to the flow. Header mutations
        are applied live during hook execution.

        Per-hook runtime validation: before each hook runs, checks that
        its declared ``reads`` are satisfied by either the initial flow
        vocabulary (request body keys, header names) or by earlier hooks'
        ``writes``. Missing reads emit a WARNING with the request path
        and trace_id, but do not block execution.
        """
        ctx = Context.from_flow(flow)
        available = extract_available_keys(ctx)

        overrides = extract_overrides_from_context(ctx.headers)
        if overrides.raw_header:
            logger.debug("Hook overrides: %s", overrides.raw_header)

        for hook_name in self.dag.execution_order:
            spec = self.dag.get_hook(hook_name)

            missing = spec.reads - available
            if missing:
                logger.warning(
                    "Hook '%s' reads unavailable keys: %s (path=%s, trace_id=%s)",
                    hook_name,
                    sorted(missing),
                    flow.request.path,
                    flow.id,
                )

            ctx = self._execute_hook(ctx, spec, overrides, self.extra_params)
            available |= set(spec.writes)

        ctx.commit()

    def _execute_hook(
        self,
        ctx: Context,
        spec: HookSpec,
        overrides: OverrideSet,
        params: dict[str, Any],
    ) -> Context:
        """Execute a single hook with error isolation."""
        hook_name = spec.name

        try:
            override = overrides.get_override(hook_name)

            if override == HookOverride.FORCE_SKIP:
                logger.debug("Hook '%s' skipped (override)", hook_name)
                return ctx

            if override != HookOverride.FORCE_RUN and not spec.should_run(ctx):
                logger.debug("Hook '%s' skipped (guard)", hook_name)
                return ctx

            logger.debug("Executing hook '%s'", hook_name)
            return spec.execute(ctx, params)

        except OAuthConfigError:
            raise
        except Exception as e:
            logger.error(
                "Hook '%s' failed: %s: %s",
                hook_name,
                type(e).__name__,
                str(e),
            )
            return ctx

    def get_execution_order(self) -> list[str]:
        return self.dag.execution_order

    def get_parallel_groups(self) -> list[set[str]]:
        return self.dag.parallel_groups
