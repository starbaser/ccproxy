"""Tests for ccproxy.pipeline.render — Rich DAG renderer."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from rich.console import Console

from ccproxy.pipeline.executor import PipelineExecutor
from ccproxy.pipeline.hook import HookSpec
from ccproxy.pipeline.render import _render_signature, render_pipeline


def _console() -> Console:
    return Console(record=True, force_terminal=True, width=120)


def _render(*hooks_inbound: HookSpec, outbound: list[HookSpec] | None = None) -> str:
    in_exec = PipelineExecutor(hooks=list(hooks_inbound))
    out_exec = PipelineExecutor(hooks=outbound or [])
    con = _console()
    con.print(render_pipeline(in_exec, out_exec))
    return con.export_text()


def _spec(
    name: str,
    reads: list[str],
    writes: list[str],
    priority: int = 0,
    model: type[BaseModel] | None = None,
    params: dict[str, Any] | None = None,
) -> HookSpec:
    return HookSpec(
        name=name,
        handler=lambda ctx, p: ctx,
        reads=frozenset(reads),
        writes=frozenset(writes),
        priority=priority,
        model=model,
        params=params or {},
    )


class RateLimitParams(BaseModel):
    max_rpm: int = 60
    burst: int = 10


class TestRenderPipeline:
    def test_all_parallel_stage(self) -> None:
        hook_a = _spec("hook_alpha", reads=["metadata"], writes=[])
        hook_b = _spec("hook_beta", reads=[], writes=["authorization"])
        text = _render(hook_a, hook_b)

        assert "── inbound ──" in text
        assert "── outbound ──" in text
        assert "hook_alpha" in text
        assert "hook_beta" in text
        assert "◆ lightllm transform ◆" in text
        assert "→ provider API" in text
        assert text.count("(no hooks)") == 1  # only outbound is empty

    def test_multi_layer_stage_ordering(self) -> None:
        # layer_a writes "token", layer_b reads "token" → layer_a before layer_b
        layer_a = _spec("layer_a", reads=[], writes=["token"], priority=0)
        layer_b = _spec("layer_b", reads=["token"], writes=[], priority=1)
        text = _render(layer_a, layer_b)

        assert "layer_a" in text
        assert "layer_b" in text
        assert text.index("layer_a") < text.index("layer_b")

    def test_render_signature_no_params(self) -> None:
        spec = _spec("rate_limit", reads=[], writes=[], model=RateLimitParams)
        sig = _render_signature(spec)
        assert sig is not None
        assert sig.plain == "(max_rpm: int, burst: int)"  # type: ignore[union-attr]

        text = _render(spec)
        assert "(max_rpm: int, burst: int)" in text

    def test_render_signature_partial_params(self) -> None:
        spec = _spec("rate_limit", reads=[], writes=[], model=RateLimitParams, params={"max_rpm": 120})
        sig = _render_signature(spec)
        assert sig is not None
        assert sig.plain == "(max_rpm=120, burst: int)"  # type: ignore[union-attr]

        text = _render(spec)
        assert "(max_rpm=120, burst: int)" in text

    def test_render_signature_no_model_returns_none(self) -> None:
        spec = _spec("no_model_hook", reads=[], writes=[])
        assert _render_signature(spec) is None

        text = _render(spec)
        assert "no_model_hook" in text
        # No signature parentheses should appear (no signature line at all)
        assert "( )" not in text

    def test_empty_reads_and_writes_show_dash(self) -> None:
        spec = _spec("bare_hook", reads=[], writes=[])
        text = _render(spec)
        # em-dash appears for both empty reads and empty writes
        assert "r: \u2014" in text
        assert "w: \u2014" in text

    def test_empty_pipeline_both_stages(self) -> None:
        text = _render()  # no inbound
        assert text.count("(no hooks)") == 2
        assert "◆ lightllm transform ◆" in text
        assert "→ provider API" in text

    def test_full_5_hook_production_shape(self) -> None:
        inbound = [
            _spec("extract_session_id", reads=["metadata"], writes=[]),
            _spec("forward_oauth", reads=["authorization"], writes=["authorization"]),
        ]
        outbound = [
            _spec("inject_mcp_notifications", reads=["messages"], writes=["messages"]),
            _spec("verbose_mode", reads=["anthropic-beta"], writes=["anthropic-beta"]),
            _spec("stamp_compliance", reads=["headers"], writes=["headers"]),
        ]
        text = _render(*inbound, outbound=outbound)

        assert "── inbound ──" in text
        assert "── outbound ──" in text
        assert "◆ lightllm transform ◆" in text
        assert "→ provider API" in text
        hook_names = (
            "extract_session_id",
            "forward_oauth",
            "inject_mcp_notifications",
            "verbose_mode",
            "stamp_compliance",
        )
        for name in hook_names:
            assert name in text
