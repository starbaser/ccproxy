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
    parts: list[RenderableType] = []
    sig = _render_signature(spec)
    if sig is not None:
        parts.append(sig)
    parts.append(Text(f"r: {reads}", style="green"))
    parts.append(Text(f"w: {writes}", style="red"))
    return Panel(
        Group(*parts),
        title=f"[bold cyan]{spec.name}[/bold cyan]",
        border_style="blue",
        padding=(0, 1),
        expand=False,
    )


def _render_signature(spec: HookSpec) -> RenderableType | None:
    """Render a hook's param signature, or None if the hook has no model.

    List-of-dotted-path params render as side-by-side numbered columns;
    scalar params render inline.
    """
    if spec.model is None:
        return None
    sig = spec.model.__signature__
    list_params: dict[str, list[str]] = {}
    scalar_parts: list[str] = []
    for param in sig.parameters.values():
        if param.name in spec.params:
            val = spec.params[param.name]
            if isinstance(val, list) and all(isinstance(v, str) and "." in v for v in val):
                list_params[param.name] = val
            else:
                scalar_parts.append(f"{param.name}={val!r}")
        else:
            ann = inspect.formatannotation(param.annotation)
            scalar_parts.append(f"{param.name}: {ann}")
    if not list_params and not scalar_parts:
        return None
    result: list[RenderableType] = []
    if scalar_parts:
        result.append(Text(f"({', '.join(scalar_parts)})", style="yellow"))
    if list_params:
        cols: list[RenderableType] = []
        for name, paths in list_params.items():
            bare = [p.split("(")[0] for p in paths]
            prefix = _common_prefix(bare)
            lines: list[Text] = [Text(name, style="bold yellow")]
            for i, p in enumerate(paths, 1):
                short = p[len(prefix) :] if p.startswith(prefix) else p
                lines.append(Text(f" {i}. {short}", style="yellow"))
            cols.append(Text("\n").join(lines))
        result.append(Columns(cols, padding=(0, 3), expand=False))
    return Group(*result) if len(result) > 1 else result[0]


def _common_prefix(paths: list[str]) -> str:
    """Return the longest shared dotted prefix including the trailing dot."""
    if not paths:
        return ""
    parts = [p.split(".") for p in paths]
    prefix: list[str] = []
    for segments in zip(*parts):
        if len(set(segments)) == 1:
            prefix.append(segments[0])
        else:
            break
    return ".".join(prefix) + "." if prefix else ""


def _arrow() -> RenderableType:
    return Align.center(Text("│\n▼", style="dim"))
