"""Utility functions for ccproxy."""

import inspect
from pathlib import Path
from typing import Any

from rich import box
from rich.console import Console
from rich.table import Table


def get_templates_dir() -> Path:
    """Get the path to the templates directory.

    This function handles both development (running from source) and
    production (installed package) scenarios.

    Returns:
        Path to the templates directory

    Raises:
        RuntimeError: If templates directory cannot be found
    """
    module_dir = Path(__file__).parent

    # Development mode: templates at project root
    dev_templates = module_dir.parent.parent / "templates"
    if dev_templates.exists() and (dev_templates / "ccproxy.yaml").exists():
        return dev_templates

    # Installed mode: templates inside the package
    package_templates = module_dir / "templates"
    if package_templates.exists() and (package_templates / "ccproxy.yaml").exists():
        return package_templates

    raise RuntimeError("Could not find templates directory. Please ensure ccproxy is properly installed.")


def get_template_file(filename: str) -> Path:
    """Get the path to a specific template file.

    Args:
        filename: Name of the template file

    Returns:
        Path to the template file

    Raises:
        FileNotFoundError: If the template file doesn't exist
    """
    templates_dir = get_templates_dir()
    template_path = templates_dir / filename

    if not template_path.exists():
        raise FileNotFoundError(f"Template file not found: {filename}")

    return template_path


def calculate_duration_ms(start_time: Any, end_time: Any) -> float:
    """Calculate duration in milliseconds between two timestamps.

    Handles both float timestamps and timedelta objects.

    Args:
        start_time: Start timestamp (float or timedelta)
        end_time: End timestamp (float or timedelta)

    Returns:
        Duration in milliseconds, rounded to 2 decimal places
    """
    try:
        if isinstance(end_time, float) and isinstance(start_time, float):
            duration_ms = (end_time - start_time) * 1000
        else:
            # Handle timedelta objects or mixed types
            duration_seconds = (end_time - start_time).total_seconds()  # type: ignore[operator,unused-ignore,unreachable]
            duration_ms = duration_seconds * 1000
    except (TypeError, AttributeError):
        duration_ms = 0.0

    return round(duration_ms, 2)


# Debug printing utilities
console = Console()


def debug_table(
    obj: Any,
    title: str | None = None,
    max_width: int | None = None,
    show_methods: bool = False,
    compact: bool = True,
) -> None:
    """Print any object as a compact debug table.

    Args:
        obj: Object to debug print
        title: Optional title for the table
        max_width: Maximum width for values
        show_methods: Include methods in output
        compact: Use compact table style
    """
    if isinstance(obj, dict):
        _print_dict(obj, title or "Dict", max_width, compact)
    elif isinstance(obj, list | tuple):
        _print_list(obj, title or type(obj).__name__, max_width, compact)
    elif hasattr(obj, "__dict__"):
        _print_object(obj, title or obj.__class__.__name__, max_width, show_methods, compact)
    else:
        from rich.pretty import Pretty

        console.print(Pretty(obj))


def _print_dict(data: dict[Any, Any], title: str, max_width: int | None, compact: bool) -> None:
    """Print dictionary as table."""
    table = Table(
        title=f"[cyan]{title}[/cyan]",
        box=box.SIMPLE if compact else box.ROUNDED,
        show_edge=not compact,
        padding=(0, 1) if compact else (0, 1),
        collapse_padding=compact,
    )

    table.add_column("Key", style="yellow", no_wrap=True)
    table.add_column("Value", style="white", max_width=max_width)
    table.add_column("Type", style="dim cyan")

    for key, value in data.items():
        table.add_row(str(key), _format_value(value, max_width), type(value).__name__)

    console.print(table)


def _print_list(data: list[Any] | tuple[Any, ...], title: str, max_width: int | None, compact: bool) -> None:
    """Print list/tuple as table."""
    table = Table(
        title=f"[cyan]{title}[/cyan] ({len(data)} items)",
        box=box.SIMPLE if compact else box.ROUNDED,
        show_edge=not compact,
        padding=(0, 1) if compact else (0, 1),
    )

    table.add_column("#", style="dim", justify="right", width=4)
    table.add_column("Value", max_width=max_width)
    table.add_column("Type", style="dim cyan")

    for i, value in enumerate(data):
        table.add_row(str(i), _format_value(value, max_width), type(value).__name__)

    console.print(table)


def _print_object(obj: Any, title: str, max_width: int | None, show_methods: bool, compact: bool) -> None:
    """Print object attributes as table."""
    table = Table(
        title=f"[cyan]{title}[/cyan]",
        box=box.SIMPLE if compact else box.ROUNDED,
        show_edge=not compact,
        padding=(0, 1) if compact else (0, 1),
    )

    table.add_column("Attribute", style="yellow", no_wrap=True)
    table.add_column("Value", max_width=max_width)
    table.add_column("Type", style="dim cyan")

    # Get all attributes
    attrs = {}
    for name in dir(obj):
        if name.startswith("_"):
            continue
        try:
            value = getattr(obj, name)
            if not show_methods and callable(value):
                continue
            attrs[name] = value
        except Exception:
            attrs[name] = "<unable to access>"

    # Sort and display
    for name in sorted(attrs.keys()):
        value = attrs[name]
        table.add_row(name, _format_value(value, max_width), type(value).__name__)

    console.print(table)


def _format_value(value: Any, max_width: int | None = None) -> str:
    """Format value for display."""
    if value is None:
        return "[dim]None[/dim]"
    elif isinstance(value, bool):
        return "[green]True[/green]" if value else "[red]False[/red]"
    elif isinstance(value, int | float):
        return f"[cyan]{value}[/cyan]"
    elif isinstance(value, str):
        # Escape markup and truncate if needed
        s = str(value).replace("[", r"\[")
        if max_width and len(s) > max_width:
            s = s[: max_width - 3] + "..."
        return f'"{s}"'
    elif isinstance(value, list | tuple):
        return f"[dim]{type(value).__name__}[{len(value)}][/dim]"
    elif isinstance(value, dict):
        return f"[dim]dict[{len(value)}][/dim]"
    elif callable(value):
        return f"[magenta]{value.__name__}()[/magenta]"
    else:
        s = str(value)
        if max_width and len(s) > max_width:
            s = s[: max_width - 3] + "..."
        return s.replace("[", r"\[")


def dt(obj: Any, **kwargs: Any) -> None:
    """Quick debug table (alias for debug_table)."""
    debug_table(obj, **kwargs)


def dv(*args: Any, **kwargs: Any) -> None:
    """Debug multiple variables with their names."""
    frame = inspect.currentframe()
    if frame is None or frame.f_back is None:
        var_names = [f"arg{i}" for i in range(len(args))]
    else:
        code_context = inspect.getframeinfo(frame.f_back).code_context
        if code_context:
            code = code_context[0].strip()
        else:
            code = ""

        # Extract variable names from the call
        import re

        match = re.search(r"dv\((.*?)\)", code)
        var_names = [n.strip() for n in match.group(1).split(",")] if match else [f"arg{i}" for i in range(len(args))]

    # Create table for all variables
    table = Table(title="[cyan]Debug Variables[/cyan]", box=box.SIMPLE, show_edge=False, padding=(0, 1))

    table.add_column("Name", style="yellow", no_wrap=True)
    table.add_column("Value", max_width=50)
    table.add_column("Type", style="dim cyan")

    for name, value in zip(var_names, args, strict=False):
        table.add_row(name, _format_value(value, 50), type(value).__name__)

    if kwargs:
        for name, value in kwargs.items():
            table.add_row(name, _format_value(value, 50), type(value).__name__)

    console.print(table)


def d(obj: Any, w: int = 60) -> None:
    """Ultra-compact debug print."""
    debug_table(obj, max_width=w, compact=True)


def p(obj: Any) -> None:
    """Print object as minimal compact table for debugging."""
    table = Table(box=box.SIMPLE, show_edge=False)

    if isinstance(obj, dict):
        table.add_column("Key", style="yellow")
        table.add_column("Value")
        for k, v in obj.items():
            table.add_row(str(k), repr(v))
    elif isinstance(obj, list | tuple):
        table.add_column("#", style="dim")
        table.add_column("Value")
        for i, v in enumerate(obj):
            table.add_row(str(i), repr(v))
    elif hasattr(obj, "__dict__"):
        table.add_column("Attr", style="yellow")
        table.add_column("Value")
        for k, v in obj.__dict__.items():
            if not k.startswith("_"):
                table.add_row(k, repr(v))
    else:
        console.print(obj)
        return

    console.print(table)
