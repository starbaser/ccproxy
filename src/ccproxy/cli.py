"""ccproxy CLI for managing the LiteLLM proxy server - Tyro implementation."""

import json
import logging
import logging.config
import os
import shutil
import subprocess
import sys
import time
from builtins import print as builtin_print
from pathlib import Path
from typing import Annotated

import attrs
import tyro
import yaml
from rich import print
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ccproxy.utils import get_templates_dir


# Subcommand definitions using attrs
@attrs.define
class Start:
    """Start the LiteLLM proxy server with ccproxy configuration."""

    args: Annotated[list[str] | None, tyro.conf.Positional] = None
    """Additional arguments to pass to litellm command."""

    detach: Annotated[bool, tyro.conf.arg(aliases=["-d"])] = False
    """Run in background and save PID to litellm.lock."""


@attrs.define
class Install:
    """Install ccproxy configuration files."""

    force: bool = False
    """Overwrite existing configuration."""


@attrs.define
class Run:
    """Run a command with ccproxy environment."""

    command: Annotated[list[str], tyro.conf.Positional]
    """Command and arguments to execute with proxy settings."""


@attrs.define
class Stop:
    """Stop the background LiteLLM proxy server."""


@attrs.define
class Restart:
    """Restart the LiteLLM proxy server (stop then start)."""

    args: Annotated[list[str] | None, tyro.conf.Positional] = None
    """Additional arguments to pass to litellm command."""

    detach: Annotated[bool, tyro.conf.arg(aliases=["-d"])] = False
    """Run in background and save PID to litellm.lock."""


@attrs.define
class Logs:
    """View the LiteLLM log file."""

    follow: Annotated[bool, tyro.conf.arg(aliases=["-f"])] = False
    """Follow log output (like tail -f)."""

    lines: Annotated[int, tyro.conf.arg(aliases=["-n"])] = 100
    """Number of lines to show (default: 100)."""


@attrs.define
class Status:
    """Show the status of LiteLLM proxy and ccproxy configuration."""

    json: bool = False
    """Output status as JSON with boolean values."""


# @attrs.define
# class ShellIntegration:
#     """Generate shell integration for automatic claude aliasing."""
#
#     shell: Annotated[str, tyro.conf.arg(help="Shell type (bash, zsh, or auto)")] = "auto"
#     """Target shell for integration script."""
#
#     install: bool = False
#     """Install the integration to shell config file."""


# Type alias for all subcommands
Command = Start | Install | Run | Stop | Restart | Logs | Status


def setup_logging() -> None:
    """Configure logging with 100-character text width."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)-20s - %(levelname)-8s - %(message).100s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def install_config(config_dir: Path, force: bool = False) -> None:
    """Install ccproxy configuration files.

    Args:
        config_dir: Directory to install configuration files to
        force: Whether to overwrite existing configuration
    """
    # Check if config directory exists
    if config_dir.exists() and not force:
        print(f"Configuration directory {config_dir} already exists.")
        print("Use --force to overwrite existing configuration.")
        sys.exit(1)

    # Create config directory
    config_dir.mkdir(parents=True, exist_ok=True)
    print(f"Creating configuration directory: {config_dir}")

    # Get templates directory
    try:
        templates_dir = get_templates_dir()
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # List of files to copy
    template_files = [
        "ccproxy.yaml",
        "config.yaml",
        "ccproxy.py",
    ]

    # Copy template files
    for filename in template_files:
        src = templates_dir / filename
        dst = config_dir / filename

        if src.exists():
            if dst.exists() and not force:
                print(f"  Skipping {filename} (already exists)")
            else:
                shutil.copy2(src, dst)
                print(f"  Copied {filename}")
        else:
            print(f"  Warning: Template {filename} not found", file=sys.stderr)

    print(f"\nInstallation complete! Configuration files installed to: {config_dir}")
    print("\nNext steps:")
    print(f"  1. Edit {config_dir}/ccproxy.yaml to configure routing rules")
    print(f"  2. Edit {config_dir}/config.yaml to configure LiteLLM models")
    print("  3. Start the proxy with: ccproxy start")


def run_with_proxy(config_dir: Path, command: list[str]) -> None:
    """Run a command with ccproxy environment variables set.

    Args:
        config_dir: Configuration directory
        command: Command and arguments to execute
    """
    # Load litellm config to get proxy settings
    ccproxy_config_path = config_dir / "ccproxy.yaml"
    if not ccproxy_config_path.exists():
        print(f"Error: Configuration not found at {ccproxy_config_path}", file=sys.stderr)
        print("Run 'ccproxy install' first to set up configuration.", file=sys.stderr)
        sys.exit(1)

    # Load config
    with ccproxy_config_path.open() as f:
        config = yaml.safe_load(f)

    litellm_config = config.get("litellm", {}) if config else {}

    # Get proxy settings with defaults
    host = os.environ.get("HOST", litellm_config.get("host", "127.0.0.1"))
    port = int(os.environ.get("PORT", litellm_config.get("port", 4000)))

    # Set up environment for the subprocess
    env = os.environ.copy()

    # Set proxy environment variables
    proxy_url = f"http://{host}:{port}"
    env["OPENAI_API_BASE"] = f"{proxy_url}"
    env["OPENAI_BASE_URL"] = f"{proxy_url}"
    env["ANTHROPIC_BASE_URL"] = f"{proxy_url}"

    # Don't set HTTP_PROXY/HTTPS_PROXY as these cause Claude Code to treat
    # the LiteLLM server as a general HTTP proxy, not an API endpoint

    # Execute the command with the proxy environment
    try:
        # S603: Command comes from user input - this is the intended behavior
        result = subprocess.run(command, env=env)  # noqa: S603
        sys.exit(result.returncode)
    except FileNotFoundError:
        print(f"Error: Command not found: {command[0]}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)  # Standard exit code for Ctrl+C


def start_litellm(config_dir: Path, args: list[str] | None = None, detach: bool = False) -> None:
    """Start the LiteLLM proxy server with ccproxy configuration.

    Args:
        config_dir: Configuration directory containing config files
        args: Additional arguments to pass to litellm command
        detach: Run in background mode with PID tracking
    """
    # Check if config exists
    config_path = config_dir / "config.yaml"
    if not config_path.exists():
        print(f"Error: Configuration not found at {config_path}", file=sys.stderr)
        print("Run 'ccproxy install' first to set up configuration.", file=sys.stderr)
        sys.exit(1)

    # Set environment variable for ccproxy configuration location
    os.environ["CCPROXY_CONFIG_DIR"] = str(config_dir.absolute())

    # Build litellm command
    cmd = ["litellm", "--config", str(config_path)]

    # Add any additional arguments
    if args:
        cmd.extend(args)

    if detach:
        # Run in background mode
        pid_file = config_dir / "litellm.lock"
        log_file = config_dir / "litellm.log"

        # Check if already running
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                # Check if process is still running
                try:
                    os.kill(pid, 0)  # This doesn't kill, just checks if process exists
                    print(f"LiteLLM is already running with PID {pid}", file=sys.stderr)
                    print("To stop it, run: `ccproxy stop`", file=sys.stderr)
                    sys.exit(1)
                except ProcessLookupError:
                    # Process is not running, clean up stale PID file
                    pid_file.unlink()
            except (ValueError, OSError):
                # Invalid PID file, remove it
                pid_file.unlink()

        # Start process in background
        try:
            with log_file.open("w") as log:
                # S603: Command construction is safe - we control the litellm path
                process = subprocess.Popen(  # noqa: S603
                    cmd,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,  # Detach from parent process group
                    env=os.environ.copy(),  # Pass environment variables including CCPROXY_CONFIG_DIR
                )

            # Save PID
            pid_file.write_text(str(process.pid))

            print("LiteLLM started in background")
            print(f"Log file: {log_file}")
            sys.exit(0)

        except FileNotFoundError:
            print("Error: litellm command not found.", file=sys.stderr)
            print("Please ensure LiteLLM is installed: pip install litellm", file=sys.stderr)
            sys.exit(1)
    else:
        # Execute litellm command in foreground
        try:
            # S603: Command construction is safe - we control the litellm path
            result = subprocess.run(cmd, env=os.environ.copy())  # noqa: S603
            sys.exit(result.returncode)
        except FileNotFoundError:
            print("Error: litellm command not found.", file=sys.stderr)
            print("Please ensure LiteLLM is installed: pip install litellm", file=sys.stderr)
            sys.exit(1)
        except KeyboardInterrupt:
            sys.exit(130)


def stop_litellm(config_dir: Path) -> bool:
    """Stop the background LiteLLM proxy server.

    Args:
        config_dir: Configuration directory containing the PID file

    Returns:
        True if server was stopped successfully, False otherwise
    """
    pid_file = config_dir / "litellm.lock"

    # Check if PID file exists
    if not pid_file.exists():
        print("No LiteLLM server is running (PID file not found)", file=sys.stderr)
        return False

    try:
        pid = int(pid_file.read_text().strip())

        # Check if process is still running
        try:
            os.kill(pid, 0)  # Check if process exists

            # Process exists, kill it
            print(f"Stopping LiteLLM server (PID: {pid})...")
            os.kill(pid, 15)  # SIGTERM - graceful shutdown

            # Wait a moment for graceful shutdown
            time.sleep(0.5)

            # Check if still running
            try:
                os.kill(pid, 0)
                # Still running, force kill
                os.kill(pid, 9)  # SIGKILL
                print(f"Force killed LiteLLM server (PID: {pid})")
            except ProcessLookupError:
                print(f"LiteLLM server stopped successfully (PID: {pid})")

            # Remove PID file
            pid_file.unlink()
            return True

        except ProcessLookupError:
            # Process is not running, clean up stale PID file
            print(f"LiteLLM server was not running (stale PID: {pid})")
            pid_file.unlink()
            return False

    except (ValueError, OSError) as e:
        print(f"Error reading PID file: {e}", file=sys.stderr)
        return False


# def generate_shell_integration(config_dir: Path, shell: str = "auto", install: bool = False) -> None:
#     """Generate shell integration for automatic claude aliasing.
#
#     Args:
#         config_dir: Configuration directory
#         shell: Target shell (bash, zsh, or auto)
#         install: Whether to install the integration
#     """
#     # Auto-detect shell if needed
#     if shell == "auto":
#         shell_path = os.environ.get("SHELL", "")
#         if "zsh" in shell_path:
#             shell = "zsh"
#         elif "bash" in shell_path:
#             shell = "bash"
#         else:
#             print("Error: Could not auto-detect shell. Please specify --shell=bash or --shell=zsh", file=sys.stderr)
#             sys.exit(1)
#
#     # Validate shell type
#     if shell not in ["bash", "zsh"]:
#         print(f"Error: Unsupported shell '{shell}'. Use 'bash' or 'zsh'.", file=sys.stderr)
#         sys.exit(1)
#
#     # Generate the integration script
#     integration_script = f"""# ccproxy shell integration
# # This enables the 'claude' alias when LiteLLM proxy is running
#
# # Function to check if LiteLLM proxy is running
# ccproxy_check_running() {{
#     local pid_file="{config_dir}/litellm.lock"
#     if [ -f "$pid_file" ]; then
#         local pid=$(cat "$pid_file" 2>/dev/null)
#         if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
#             return 0  # Running
#         fi
#     fi
#     return 1  # Not running
# }}
#
# # Function to set up claude alias
# ccproxy_setup_alias() {{
#     if ccproxy_check_running; then
#         alias claude='ccproxy run claude'
#     else
#         unalias claude 2>/dev/null || true
#     fi
# }}
#
# # Set up the alias on shell startup
# ccproxy_setup_alias
#
# # For zsh: also check on each prompt
# """
#
#     if shell == "zsh":
#         integration_script += """if [[ -n "$ZSH_VERSION" ]]; then
#     # Add to precmd hooks to check before each prompt
#     if ! (( $precmd_functions[(I)ccproxy_setup_alias] )); then
#         precmd_functions+=(ccproxy_setup_alias)
#     fi
# fi
# """
#     elif shell == "bash":
#         integration_script += """if [[ -n "$BASH_VERSION" ]]; then
#     # For bash, check on PROMPT_COMMAND
#     if [[ ! "$PROMPT_COMMAND" =~ ccproxy_setup_alias ]]; then
#         PROMPT_COMMAND="${PROMPT_COMMAND:+$PROMPT_COMMAND$'\\n'}ccproxy_setup_alias"
#     fi
# fi
# """
#
#     if install:
#         # Determine shell config file
#         home = Path.home()
#         if shell == "zsh":
#             config_files = [home / ".zshrc", home / ".config/zsh/.zshrc"]
#         else:  # bash
#             config_files = [home / ".bashrc", home / ".bash_profile", home / ".profile"]
#
#         # Find the first existing config file
#         shell_config = None
#         for cf in config_files:
#             if cf.exists():
#                 shell_config = cf
#                 break
#
#         if not shell_config:
#             # Create .zshrc or .bashrc if none exist
#             shell_config = home / f".{shell}rc"
#             shell_config.touch()
#
#         # Check if already installed
#         marker = "# ccproxy shell integration"
#         existing_content = shell_config.read_text()
#
#         if marker in existing_content:
#             print(f"ccproxy integration already installed in {shell_config}")
#             print("To update, remove the existing integration first.")
#             sys.exit(0)
#
#         # Append the integration
#         with shell_config.open("a") as f:
#             f.write("\n")
#             f.write(integration_script)
#             f.write("\n")
#
#         print(f"✓ ccproxy shell integration installed to {shell_config}")
#         print("\nTo activate now, run:")
#         print(f"  source {shell_config}")
#         print(f"\nOr start a new {shell} session.")
#         print("\nThe 'claude' alias will be available when LiteLLM proxy is running.")
#     else:
#         # Just print the script
#         print(f"# Add this to your {shell} configuration file:")
#         print(integration_script)
#         print("\n# To install automatically, run:")
#         print(f"  ccproxy shell-integration --shell={shell} --install")


def view_logs(config_dir: Path, follow: bool = False, lines: int = 100) -> None:
    """View the LiteLLM log file using system pager.

    Args:
        config_dir: Configuration directory containing the log file
        follow: Follow log output (like tail -f)
        lines: Number of lines to show
    """
    log_file = config_dir / "litellm.log"

    # Check if log file exists
    if not log_file.exists():
        print("[red]No log file found[/red]", file=sys.stderr)
        print(f"[dim]Expected at: {log_file}[/dim]", file=sys.stderr)
        sys.exit(1)

    if follow:
        # Use tail -f for following logs
        try:
            # S603, S607: tail is a standard system command, file path is validated
            result = subprocess.run(["tail", "-f", str(log_file)])  # noqa: S603, S607
            sys.exit(result.returncode)
        except KeyboardInterrupt:
            sys.exit(0)
        except FileNotFoundError:
            print("[red]Error: 'tail' command not found[/red]", file=sys.stderr)
            sys.exit(1)
    else:
        # Get the pager from environment or use default
        pager = os.environ.get("PAGER", "less")

        # Read the last N lines
        try:
            with log_file.open("r") as f:
                # Read all lines and get the last N
                all_lines = f.readlines()
                tail_lines = all_lines[-lines:] if len(all_lines) > lines else all_lines
                content = "".join(tail_lines)

                if not content.strip():
                    print("[yellow]Log file is empty[/yellow]")
                    sys.exit(0)

                # Use the pager if output is substantial
                if len(tail_lines) > 20 or pager == "cat":
                    # For cat or when there are many lines, use pager
                    # S603: pager comes from PAGER env var, standard practice for CLI tools
                    process = subprocess.Popen([pager], stdin=subprocess.PIPE)  # noqa: S603
                    process.communicate(content.encode())
                    sys.exit(process.returncode)
                else:
                    # For short output, just print directly
                    print(content, end="")
                    sys.exit(0)

        except OSError as e:
            print(f"[red]Error reading log file: {e}[/red]", file=sys.stderr)
            sys.exit(1)


def show_status(config_dir: Path, json_output: bool = False) -> None:
    """Show the status of LiteLLM proxy and ccproxy configuration.

    Args:
        config_dir: Configuration directory to check
        json_output: Output status as JSON with boolean values
    """
    # Check LiteLLM proxy status
    pid_file = config_dir / "litellm.lock"
    log_file = config_dir / "litellm.log"

    proxy_running = False

    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            # Check if process is still running
            try:
                os.kill(pid, 0)
                proxy_running = True
            except ProcessLookupError:
                pass
        except (ValueError, OSError):
            pass

    # Check configuration files
    ccproxy_config = config_dir / "ccproxy.yaml"
    litellm_config = config_dir / "config.yaml"
    user_hooks = config_dir / "ccproxy.py"

    # Build config paths dict
    config_paths = {}
    if ccproxy_config.exists():
        config_paths["ccproxy.yaml"] = str(ccproxy_config)
    if litellm_config.exists():
        config_paths["config.yaml"] = str(litellm_config)
    if user_hooks.exists():
        config_paths["ccproxy.py"] = str(user_hooks)

    # Extract callbacks from config.yaml
    callbacks = []
    if litellm_config.exists():
        try:
            with litellm_config.open() as f:
                config_data = yaml.safe_load(f)
            litellm_settings = config_data.get("litellm_settings", {}) if config_data else {}
            callbacks = litellm_settings.get("callbacks", [])
        except (yaml.YAMLError, OSError):
            pass

    # Build status data
    status_data = {
        "proxy": proxy_running,
        "config": config_paths,
        "callbacks": callbacks,
        "log": str(log_file) if log_file.exists() else None,
    }

    if json_output:
        builtin_print(json.dumps(status_data, indent=2))
    else:
        # Rich table output
        console = Console()

        table = Table(show_header=False, show_lines=True)
        table.add_column("Key", style="white", width=15)
        table.add_column("Value", style="yellow")

        # Proxy status
        proxy_status = "[green]true[/green]" if status_data["proxy"] else "[red]false[/red]"
        table.add_row("proxy", proxy_status)

        # Config files
        if status_data["config"]:
            config_display = "\n".join(
                f"[cyan]{key}[/cyan]: {value}" for key, value in status_data["config"].items()
            )
        else:
            config_display = "[red]No config files found[/red]"
        table.add_row("config", config_display)

        # Callbacks
        if status_data["callbacks"]:
            callbacks_display = "\n".join(f"[green]• {cb}[/green]" for cb in status_data["callbacks"])
        else:
            callbacks_display = "[dim]No callbacks configured[/dim]"
        table.add_row("callbacks", callbacks_display)

        # Log file
        log_display = status_data["log"] if status_data["log"] else "[yellow]No log file[/yellow]"
        table.add_row("log", log_display)

        console.print(Panel(table, title="[bold]ccproxy Status[/bold]", border_style="blue"))


def main(
    cmd: Annotated[Command, tyro.conf.arg(name="")],
    *,
    config_dir: Annotated[Path | None, tyro.conf.arg(help="Configuration directory")] = None,
) -> None:
    """ccproxy - LiteLLM Transformation Hook System.

    A powerful routing system for LiteLLM that dynamically routes requests
    to different models based on configurable rules.
    """
    if config_dir is None:
        config_dir = Path.home() / ".ccproxy"

    # Setup logging with 100-character text width
    setup_logging()

    # Handle each command type
    if isinstance(cmd, Start):
        start_litellm(config_dir, args=cmd.args, detach=cmd.detach)

    elif isinstance(cmd, Install):
        install_config(config_dir, force=cmd.force)

    elif isinstance(cmd, Run):
        if not cmd.command:
            print("Error: No command specified to run", file=sys.stderr)
            print("Usage: ccproxy run <command> [args...]", file=sys.stderr)
            sys.exit(1)
        run_with_proxy(config_dir, cmd.command)

    elif isinstance(cmd, Stop):
        success = stop_litellm(config_dir)
        sys.exit(0 if success else 1)

    elif isinstance(cmd, Restart):
        # Stop the server first
        pid_file = config_dir / "litellm.lock"
        if pid_file.exists():
            print("Stopping LiteLLM server...")
            stop_litellm(config_dir)
        else:
            print("No server running, starting fresh...")

        # Wait for clean shutdown
        time.sleep(1)

        # Start the server
        print("Starting LiteLLM server...")
        start_litellm(config_dir, args=cmd.args, detach=cmd.detach)

    elif isinstance(cmd, Logs):
        view_logs(config_dir, follow=cmd.follow, lines=cmd.lines)

    elif isinstance(cmd, Status):
        show_status(config_dir, json_output=cmd.json)


def entry_point() -> None:
    """Entry point for the ccproxy command."""
    tyro.cli(main)


if __name__ == "__main__":
    entry_point()
