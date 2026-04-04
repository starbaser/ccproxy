"""Pipeline executor with DAG-ordered execution.

Executes hooks in dependency-safe order with override support.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ccproxy.pipeline.context import Context
from ccproxy.pipeline.dag import HookDAG
from ccproxy.pipeline.overrides import (
    HookOverride,
    OverrideSet,
    extract_overrides_from_context,
)

if TYPE_CHECKING:
    from ccproxy.pipeline.hook import HookSpec

logger = logging.getLogger(__name__)


class PipelineExecutor:
    """Executes hooks in DAG-ordered sequence with override support.

    Attributes:
        dag: Hook dependency graph
        extra_params: Additional parameters passed to all hooks
    """

    def __init__(
        self,
        hooks: list[HookSpec],
        extra_params: dict[str, Any] | None = None,
    ) -> None:
        """Initialize executor with hooks.

        Args:
            hooks: List of hook specifications
            extra_params: Additional parameters passed to all hooks
                         (e.g., classifier, router)

        Raises:
            CycleError: If hook dependencies form a cycle
        """
        self.dag = HookDAG(hooks)
        self.extra_params = extra_params or {}

        # Log execution order at startup
        order = self.dag.execution_order
        logger.info("Pipeline execution order: %s", " → ".join(order))

        # Log parallel groups
        groups = self.dag.parallel_groups
        if any(len(g) > 1 for g in groups):
            logger.info(
                "Parallel execution groups: %s",
                [sorted(g) for g in groups],
            )

        # Log validation warnings
        warnings = self.dag.validate()
        for warning in warnings:
            logger.warning("DAG validation: %s", warning)

    def execute(
        self,
        data: dict[str, Any],
        user_api_key_dict: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute the hook pipeline.

        Args:
            data: LiteLLM request data dict
            user_api_key_dict: LiteLLM user API key info

        Returns:
            Modified data dict
        """
        ctx = Context.from_litellm_data(data)

        overrides = extract_overrides_from_context(ctx.headers)
        if overrides.raw_header:
            logger.debug("Hook overrides: %s", overrides.raw_header)

        hook_params = dict(self.extra_params)
        if user_api_key_dict:
            hook_params["user_api_key_dict"] = user_api_key_dict

        for hook_name in self.dag.execution_order:
            spec = self.dag.get_hook(hook_name)
            ctx = self._execute_hook(ctx, spec, overrides, hook_params)

        return ctx.to_litellm_data()

    def _execute_hook(
        self,
        ctx: Context,
        spec: HookSpec,
        overrides: OverrideSet,
        params: dict[str, Any],
    ) -> Context:
        """Execute a single hook with error isolation.

        Args:
            ctx: Pipeline context
            spec: Hook specification
            overrides: Override configuration
            params: Parameters to pass to hook

        Returns:
            Modified context (original if hook fails)
        """
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

        except Exception as e:
            # Error isolation: log and continue
            logger.error(
                "Hook '%s' failed: %s: %s",
                hook_name,
                type(e).__name__,
                str(e),
            )
            return ctx

    def get_execution_order(self) -> list[str]:
        """Get hook names in execution order."""
        return self.dag.execution_order

    def get_parallel_groups(self) -> list[set[str]]:
        """Get groups of hooks that can execute in parallel."""
        return self.dag.parallel_groups

    def to_mermaid(self) -> str:
        """Generate Mermaid diagram of the pipeline."""
        return self.dag.to_mermaid()

    def to_ascii(self) -> str:
        """Generate ASCII representation of the pipeline."""
        return self.dag.to_ascii()
