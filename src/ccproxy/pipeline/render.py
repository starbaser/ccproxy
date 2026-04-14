"""Rich-based ASCII rendering of the hook pipeline DAG.

Builds a rich.console.Group representing the full pipeline:
inbound stage → lightllm transform bridge → outbound stage → provider sink.
Each hook becomes a rich.panel.Panel containing param signature (if any),
reads, and writes. Parallel-group rows use rich.columns.Columns for
horizontal layout; stages and arrows are composed via rich.console.Group
and rich.align.Align.

Layout algorithm is intentionally trivial — rich handles all width,
alignment, box drawing, and padding. There is no hand-rolled ASCII
geometry.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING

from rich.align import Align
from rich.columns import Columns
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.text import Text

if TYPE_CHECKING:
    from ccproxy.pipeline.executor import PipelineExecutor
    from ccproxy.pipeline.hook import HookSpec


def render_pipeline(
    inbound: PipelineExecutor,
    outbound: PipelineExecutor,
) -> RenderableType:
    """Return a Rich renderable for the full hook pipeline.

    Layout: inbound stage → lightllm transform → outbound stage → provider sink.
    The caller wraps the result in Panel(title="Pipeline", ...).
    """
    transform = Panel(
        Text(" ◆ lightllm transform ◆ ", style="bold magenta"),
        border_style="magenta",
        padding=(0, 1),
        expand=False,
    )
    provider = Panel(
        Text(" → provider API ", style="bold green"),
        border_style="green",
        padding=(0, 1),
        expand=False,
    )
    return Group(
        Align.center(Text("── inbound ──", style="bold")),
        Text(""),
        _render_stage(inbound),
        _arrow(),
        Align.center(transform),
        _arrow(),
        Align.center(Text("── outbound ──", style="bold")),
        Text(""),
        _render_stage(outbound),
        _arrow(),
        Align.center(provider),
    )


def _render_stage(executor: PipelineExecutor) -> RenderableType:
    groups = executor.get_parallel_groups()
    if not groups:
        return Align.center(Text("(no hooks)", style="dim"))
    rows: list[RenderableType] = []
    for i, parallel_set in enumerate(groups):
        specs = sorted(
            (executor.dag.get_hook(name) for name in parallel_set),
            key=lambda s: (s.priority, s.name),
        )
        panels = [_hook_panel(spec) for spec in specs]
        rows.append(Align.center(Columns(panels, padding=(0, 3), expand=False)))
        if i < len(groups) - 1:
            rows.append(_arrow())
    return Group(*rows)


def _hook_panel(spec: HookSpec) -> Panel:
    reads = ", ".join(sorted(spec.reads)) or "—"
    writes = ", ".join(sorted(spec.writes)) or "—"
    lines: list[tuple[str, str]] = []
    sig = _render_signature(spec)
    if sig is not None:
        lines.append((sig, "yellow"))
    lines.append((f"r: {reads}", "green"))
    lines.append((f"w: {writes}", "red"))
    content = Text("\n").join(Text(text, style=style) for text, style in lines)
    return Panel(
        content,
        title=f"[bold cyan]{spec.name}[/bold cyan]",
        border_style="blue",
        padding=(0, 1),
        expand=False,
    )


def _render_signature(spec: HookSpec) -> str | None:
    """Render a hook's param signature, or None if the hook has no model."""
    if spec.model is None:
        return None
    sig = spec.model.__signature__
    parts: list[str] = []
    for param in sig.parameters.values():
        ann = inspect.formatannotation(param.annotation)
        if param.name in spec.params:
            parts.append(f"{param.name}={spec.params[param.name]!r}")
        else:
            parts.append(f"{param.name}: {ann}")
    return f"({', '.join(parts)})"


def _arrow() -> RenderableType:
    return Align.center(Text("│\n▼", style="dim"))
