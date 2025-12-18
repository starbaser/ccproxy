"""ccstatusline integration for ccproxy.

This module provides functionality to:
1. Install ccstatusline and configure Claude Code integration
2. Query proxy status for the statusline widget
3. Format status output for display
"""

import json
import logging
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Configuration paths
CCSTATUSLINE_SETTINGS = Path.home() / ".config" / "ccstatusline" / "settings.json"
CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"
DEFAULT_PROXY_PORT = 4000


def get_proxy_status_url(port: int = DEFAULT_PROXY_PORT) -> str:
    """Get the proxy status endpoint URL."""
    return f"http://localhost:{port}/ccproxy/status"


def query_status(port: int = DEFAULT_PROXY_PORT, timeout: float = 0.1) -> dict[str, Any] | None:
    """Query proxy for current routing status via HTTP.

    Args:
        port: Proxy server port
        timeout: Request timeout in seconds

    Returns:
        Status dict or None if proxy not running/error
    """
    try:
        resp = httpx.get(get_proxy_status_url(port), timeout=timeout)
        if resp.status_code == 200:
            return resp.json()
        return None
    except (httpx.ConnectError, httpx.TimeoutException):
        return None  # Proxy not running
    except Exception as e:
        logger.debug(f"Failed to query proxy status: {e}")
        return None


def format_status_output(status: dict[str, Any] | None, proxy_reachable: bool = True) -> str:
    """Format status for statusline widget output.

    Args:
        status: Status dict from proxy or None
        proxy_reachable: Whether the proxy endpoint was reachable

    Returns:
        Formatted status string
    """
    if not proxy_reachable or status is None:
        return "⸢ccproxy: OFF⸥"
    return "⸢ccproxy: ON⸥"


def check_npm_available() -> bool:
    """Check if npm/npx is available."""
    return shutil.which("npx") is not None


def check_bun_available() -> bool:
    """Check if bun/bunx is available."""
    return shutil.which("bunx") is not None


def install_statusline(
    force: bool = False,
    use_bun: bool = False,
    claude_config_dir: Path | None = None,
) -> bool:
    """Install ccstatusline and configure Claude Code integration.

    Args:
        force: Overwrite existing configuration
        use_bun: Use bunx instead of npx
        claude_config_dir: Override Claude config directory (default: ~/.claude)

    Returns:
        True if installation successful
    """
    from rich import print

    claude_settings_path = claude_config_dir / "settings.json" if claude_config_dir else CLAUDE_SETTINGS

    # Check package manager availability
    if use_bun:
        if not check_bun_available():
            print("[red]Error:[/red] bunx not found. Install bun or use npx instead.")
            return False
        command = "bunx ccstatusline@latest"
    else:
        if not check_npm_available():
            print("[red]Error:[/red] npx not found. Install npm or use --use-bun.")
            return False
        command = "npx -y ccstatusline@latest"

    # Step 1: Configure Claude Code settings.json
    print(f"\n[cyan]Step 1:[/cyan] Configuring Claude Code ({claude_settings_path})")

    try:
        if claude_settings_path.exists():
            settings = json.loads(claude_settings_path.read_text())
        else:
            settings = {}
            claude_settings_path.parent.mkdir(parents=True, exist_ok=True)

        # Check if statusLine already configured
        if "statusLine" in settings and not force:
            print(f"  [yellow]statusLine already configured[/yellow]")
            print(f"  Use --force to overwrite")
        else:
            settings["statusLine"] = {
                "type": "command",
                "command": command,
                "padding": 0,
            }
            claude_settings_path.write_text(json.dumps(settings, indent=2))
            print(f"  [green]Added statusLine configuration[/green]")

    except json.JSONDecodeError as e:
        print(f"  [red]Error parsing {claude_settings_path}: {e}[/red]")
        return False
    except OSError as e:
        print(f"  [red]Error writing {claude_settings_path}: {e}[/red]")
        return False

    # Step 2: Configure ccstatusline widget
    print(f"\n[cyan]Step 2:[/cyan] Configuring ccstatusline ({CCSTATUSLINE_SETTINGS})")

    try:
        if CCSTATUSLINE_SETTINGS.exists():
            cc_settings = json.loads(CCSTATUSLINE_SETTINGS.read_text())
        else:
            cc_settings = {"version": 3, "lines": [[]]}
            CCSTATUSLINE_SETTINGS.parent.mkdir(parents=True, exist_ok=True)

        # Check if ccproxy widget already exists
        ccproxy_widget_exists = False
        lines = cc_settings.get("lines", [[]])
        for line in lines:
            for widget in line:
                if widget.get("commandPath", "").startswith("ccproxy"):
                    ccproxy_widget_exists = True
                    break

        if ccproxy_widget_exists and not force:
            print(f"  [yellow]ccproxy widget already configured[/yellow]")
            print(f"  Use --force to overwrite")
        else:
            # Remove existing ccproxy widgets if force
            if force:
                for line in lines:
                    line[:] = [w for w in line if not w.get("commandPath", "").startswith("ccproxy")]

            # Add ccproxy widget to first line
            ccproxy_widget = {
                "id": str(uuid.uuid4())[:8],
                "type": "custom-command",
                "commandPath": "ccproxy statusline",
                "timeout": 150,
                "color": "yellow",
            }

            if lines and lines[0]:
                # Add separator before widget if line has items
                separator = {"id": str(uuid.uuid4())[:8], "type": "separator"}
                lines[0].append(separator)
            lines[0].append(ccproxy_widget)

            cc_settings["lines"] = lines
            CCSTATUSLINE_SETTINGS.write_text(json.dumps(cc_settings, indent=2))
            print(f"  [green]Added ccproxy widget[/green]")

    except json.JSONDecodeError as e:
        print(f"  [yellow]Warning: Could not parse {CCSTATUSLINE_SETTINGS}: {e}[/yellow]")
        print(f"  [dim]Run ccstatusline TUI to configure manually[/dim]")
    except OSError as e:
        print(f"  [yellow]Warning: Could not write {CCSTATUSLINE_SETTINGS}: {e}[/yellow]")
        print(f"  [dim]Run ccstatusline TUI to configure manually[/dim]")

    # Step 3: Verify ccstatusline is accessible
    print(f"\n[cyan]Step 3:[/cyan] Verifying ccstatusline installation")

    try:
        # Just check if the command exists, don't actually run it
        pkg_cmd = "bunx" if use_bun else "npx"
        result = subprocess.run(
            [pkg_cmd, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            print(f"  [green]{pkg_cmd} available[/green]")
        else:
            print(f"  [yellow]{pkg_cmd} check failed[/yellow]")
    except Exception as e:
        print(f"  [yellow]Warning: Could not verify {pkg_cmd}: {e}[/yellow]")

    print("\n[green]Installation complete![/green]")
    print("\n[dim]Note: ccstatusline will be downloaded on first Claude Code launch.[/dim]")
    print("[dim]The ccproxy widget will show routing info when the proxy is running.[/dim]")

    return True


def uninstall_statusline(claude_config_dir: Path | None = None) -> bool:
    """Remove ccstatusline configuration from Claude Code.

    Args:
        claude_config_dir: Override Claude config directory

    Returns:
        True if uninstallation successful
    """
    from rich import print

    claude_settings_path = claude_config_dir / "settings.json" if claude_config_dir else CLAUDE_SETTINGS

    print(f"\n[cyan]Removing statusLine from Claude Code settings[/cyan]")

    try:
        if not claude_settings_path.exists():
            print(f"  [yellow]No settings file found at {claude_settings_path}[/yellow]")
            return True

        settings = json.loads(claude_settings_path.read_text())

        if "statusLine" not in settings:
            print(f"  [yellow]No statusLine configuration found[/yellow]")
            return True

        del settings["statusLine"]
        claude_settings_path.write_text(json.dumps(settings, indent=2))
        print(f"  [green]Removed statusLine configuration[/green]")

    except json.JSONDecodeError as e:
        print(f"  [red]Error parsing {claude_settings_path}: {e}[/red]")
        return False
    except OSError as e:
        print(f"  [red]Error writing {claude_settings_path}: {e}[/red]")
        return False

    print(f"\n[cyan]Removing ccproxy widget from ccstatusline[/cyan]")

    try:
        if not CCSTATUSLINE_SETTINGS.exists():
            print(f"  [yellow]No ccstatusline settings found[/yellow]")
            return True

        cc_settings = json.loads(CCSTATUSLINE_SETTINGS.read_text())
        lines = cc_settings.get("lines", [])

        # Remove ccproxy widgets
        removed = False
        for line in lines:
            original_len = len(line)
            line[:] = [w for w in line if not w.get("commandPath", "").startswith("ccproxy")]
            if len(line) < original_len:
                removed = True

        if removed:
            cc_settings["lines"] = lines
            CCSTATUSLINE_SETTINGS.write_text(json.dumps(cc_settings, indent=2))
            print(f"  [green]Removed ccproxy widget[/green]")
        else:
            print(f"  [yellow]No ccproxy widget found[/yellow]")

    except (json.JSONDecodeError, OSError) as e:
        print(f"  [yellow]Warning: Could not update ccstatusline settings: {e}[/yellow]")

    print("\n[green]Uninstallation complete![/green]")
    return True


def show_statusline_status(claude_config_dir: Path | None = None) -> None:
    """Show ccstatusline installation status.

    Args:
        claude_config_dir: Override Claude config directory
    """
    from rich import print
    from rich.panel import Panel
    from rich.table import Table

    claude_settings_path = claude_config_dir / "settings.json" if claude_config_dir else CLAUDE_SETTINGS

    table = Table(show_header=False, show_lines=True)
    table.add_column("Component", style="cyan")
    table.add_column("Status", style="white")

    # Check Claude Code settings
    claude_status = "[red]Not configured[/red]"
    if claude_settings_path.exists():
        try:
            settings = json.loads(claude_settings_path.read_text())
            if "statusLine" in settings:
                cmd = settings["statusLine"].get("command", "")
                if "ccstatusline" in cmd:
                    claude_status = f"[green]Configured[/green]\n[dim]{cmd}[/dim]"
                else:
                    claude_status = f"[yellow]Custom command[/yellow]\n[dim]{cmd}[/dim]"
        except (json.JSONDecodeError, OSError):
            claude_status = "[yellow]Error reading settings[/yellow]"
    table.add_row("Claude Code", claude_status)

    # Check ccstatusline settings
    cc_status = "[yellow]Not configured[/yellow]"
    if CCSTATUSLINE_SETTINGS.exists():
        try:
            cc_settings = json.loads(CCSTATUSLINE_SETTINGS.read_text())
            widget_found = False
            for line in cc_settings.get("lines", []):
                for widget in line:
                    if widget.get("commandPath", "").startswith("ccproxy"):
                        widget_found = True
                        break
            if widget_found:
                cc_status = "[green]ccproxy widget configured[/green]"
            else:
                cc_status = "[yellow]No ccproxy widget[/yellow]"
        except (json.JSONDecodeError, OSError):
            cc_status = "[yellow]Error reading settings[/yellow]"
    table.add_row("ccstatusline", cc_status)

    # Check proxy status endpoint
    status = query_status(timeout=0.5)
    if status:
        if "error" in status:
            proxy_status = f"[yellow]{status['error']}[/yellow]"
        else:
            proxy_status = f"[green]Running[/green]\n[dim]{format_status_output(status)}[/dim]"
    else:
        proxy_status = "[red]Not running / unreachable[/red]"
    table.add_row("Proxy status endpoint", proxy_status)

    # Check package managers
    npm_status = "[green]Available[/green]" if check_npm_available() else "[red]Not found[/red]"
    bun_status = "[green]Available[/green]" if check_bun_available() else "[dim]Not found[/dim]"
    table.add_row("npx", npm_status)
    table.add_row("bunx", bun_status)

    print(Panel(table, title="[bold]ccstatusline Integration Status[/bold]", border_style="blue"))
