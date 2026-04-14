"""Tests for ccproxy.inspector.pipeline — _load_hooks, build_executor, register_pipeline_routes."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from ccproxy.inspector.flow_store import InspectorMeta
from ccproxy.inspector.pipeline import build_executor, register_pipeline_routes
from ccproxy.pipeline.executor import PipelineExecutor


class TestBuildExecutor:
    def test_empty_returns_executor_instance(self) -> None:
        executor = build_executor([])
        assert isinstance(executor, PipelineExecutor)
        assert executor.get_execution_order() == []

    def test_valid_hook_module_registered(self) -> None:
        # forward_oauth is already imported and registered by other tests
        executor = build_executor(["ccproxy.hooks.forward_oauth"])
        assert isinstance(executor, PipelineExecutor)
        assert "forward_oauth" in executor.get_execution_order()

    def test_invalid_module_handled_gracefully(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.ERROR, logger="ccproxy.inspector.pipeline"):
            executor = build_executor(["ccproxy.hooks.nonexistent_xyz_module"])
        assert isinstance(executor, PipelineExecutor)
        assert "nonexistent_xyz_module" in caplog.text

    def test_dict_entry_attaches_params(self) -> None:
        entry = {"hook": "ccproxy.hooks.forward_oauth", "params": {"timeout": 10, "strict": True}}
        executor = build_executor([entry])
        assert isinstance(executor, PipelineExecutor)
        assert "forward_oauth" in executor.get_execution_order()
        # Verify params reached the spec via the DAG
        spec = executor.dag.get_hook("forward_oauth")
        assert spec is not None
        assert spec.params == {"timeout": 10, "strict": True}

    def test_dict_entry_with_empty_hook_key_skipped(self) -> None:
        entry = {"hook": "", "params": {}}
        executor = build_executor([entry])
        assert isinstance(executor, PipelineExecutor)
        assert executor.get_execution_order() == []

    def test_multiple_hooks_priority_order(self) -> None:
        executor = build_executor(
            [
                "ccproxy.hooks.forward_oauth",
                "ccproxy.hooks.verbose_mode",
            ]
        )
        order = executor.get_execution_order()
        assert "forward_oauth" in order
        assert "verbose_mode" in order
        # forward_oauth has lower index (idx=0) → lower priority number → executes first
        assert order.index("forward_oauth") < order.index("verbose_mode")


class TestRegisterPipelineRoutes:
    def _capture_handler(self, executor: object) -> object:
        """Register routes with a mock router and return the captured route handler."""
        mock_router = MagicMock()
        captured: list = []

        def capture_decorator(*args: object, **kwargs: object):
            def decorator(fn: object) -> object:
                captured.append(fn)
                return fn

            return decorator

        mock_router.route.side_effect = capture_decorator
        register_pipeline_routes(mock_router, executor)  # type: ignore[arg-type]
        assert captured, "No route handler was registered"
        return captured[0]

    def test_inbound_flow_calls_execute(self) -> None:
        mock_executor = MagicMock()
        handler = self._capture_handler(mock_executor)

        flow = MagicMock()
        flow.request.content = b"{}"
        flow.request.headers = {}
        flow.metadata = {InspectorMeta.DIRECTION: "inbound"}

        handler(flow=flow)

        mock_executor.execute.assert_called_once_with(flow)

    def test_non_inbound_flow_skips_execute(self) -> None:
        mock_executor = MagicMock()
        handler = self._capture_handler(mock_executor)

        flow = MagicMock()
        flow.metadata = {InspectorMeta.DIRECTION: "outbound"}

        handler(flow=flow)

        mock_executor.execute.assert_not_called()

    def test_missing_direction_skips_execute(self) -> None:
        mock_executor = MagicMock()
        handler = self._capture_handler(mock_executor)

        flow = MagicMock()
        flow.metadata = {}  # No direction key

        handler(flow=flow)

        mock_executor.execute.assert_not_called()
