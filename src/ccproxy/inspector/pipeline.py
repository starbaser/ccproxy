"""Pipeline router — DAG-driven hook execution at the mitmproxy layer.

Builds PipelineExecutor instances from config and wires them as
mitmproxy addons. Two stages: inbound (pre-transform) and outbound
(post-transform), each with their own DAG.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ccproxy.inspector.flow_store import InspectorMeta
from ccproxy.pipeline.executor import PipelineExecutor
from ccproxy.pipeline.loader import load_hooks

if TYPE_CHECKING:
    from mitmproxy.http import HTTPFlow

    from ccproxy.inspector.router import InspectorRouter

logger = logging.getLogger(__name__)


def build_executor(hook_entries: list[str | dict[str, Any]]) -> PipelineExecutor:
    specs = load_hooks(hook_entries)
    return PipelineExecutor(hooks=specs)


def register_pipeline_routes(
    router: InspectorRouter,
    executor: PipelineExecutor,
) -> None:
    from ccproxy.inspector.router import RouteType

    @router.route("/{path}", rtype=RouteType.REQUEST)
    def handle_pipeline(flow: HTTPFlow, **kwargs: object) -> None:  # pyright: ignore[reportUnusedFunction]
        if flow.metadata.get(InspectorMeta.DIRECTION) != "inbound":
            return

        executor.execute(flow)
