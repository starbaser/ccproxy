"""ccproxy CLI for managing the LiteLLM proxy server - Tyro implementation."""

import contextlib
import json
import logging
import logging.config
import os
import shutil
import signal
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

from ccproxy.process import is_process_running, write_pid
from ccproxy.utils import get_templates_dir


def _expand_env_vars(value: str) -> str:
    """Expand environment variables in a string.

    Supports ${VAR} and ${VAR:-default} patterns.
    """
    import re

    def replace_var(match: re.Match[str]) -> str:
        var_expr = match.group(1)
        if ":-" in var_expr:
            var_name, default = var_expr.split(":-", 1)
            return os.environ.get(var_name, default)
        return os.environ.get(var_expr, match.group(0))

    return re.sub(r"\$\{([^}]+)\}", replace_var, value)


# Subcommand definitions using attrs
@attrs.define
class Start:
    """Start the LiteLLM proxy server with ccproxy configuration."""

    args: Annotated[list[str] | None, tyro.conf.Positional] = None
    """Additional arguments to pass to litellm command."""

    detach: Annotated[bool, tyro.conf.arg(aliases=["-d"])] = False
    """Run in background and save PID to litellm.lock."""

    mitm: Annotated[bool, tyro.conf.arg(aliases=["-m"])] = False
    """Also start mitmproxy for traffic capture."""


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


@attrs.define
class StatuslineOutput:
    """Output routing status for ccstatusline widget."""


@attrs.define
class StatuslineInstall:
    """Install ccstatusline and configure Claude Code integration."""

    force: bool = False
    """Overwrite existing configuration."""

    use_bun: bool = False
    """Use bunx instead of npx."""


@attrs.define
class StatuslineUninstall:
    """Remove ccstatusline configuration."""


@attrs.define
class StatuslineStatus:
    """Show ccstatusline installation status."""


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
Command = (
    Annotated[Start, tyro.conf.subcommand(name="start")]
    | Annotated[Install, tyro.conf.subcommand(name="install")]
    | Annotated[Run, tyro.conf.subcommand(name="run")]
    | Annotated[Stop, tyro.conf.subcommand(name="stop")]
    | Annotated[Restart, tyro.conf.subcommand(name="restart")]
    | Annotated[Logs, tyro.conf.subcommand(name="logs")]
    | Annotated[Status, tyro.conf.subcommand(name="status")]
    | Annotated[StatuslineOutput, tyro.conf.subcommand(name="statusline")]
    | Annotated[StatuslineInstall, tyro.conf.subcommand(name="statusline-install")]
    | Annotated[StatuslineUninstall, tyro.conf.subcommand(name="statusline-uninstall")]
    | Annotated[StatuslineStatus, tyro.conf.subcommand(name="statusline-status")]
)


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

    The main port (default 4000) is always the entry point:
    - Without MITM: LiteLLM runs on port 4000
    - With MITM: MITM runs on port 4000, forwards to LiteLLM on a random port

    Args:
        config_dir: Configuration directory
        command: Command and arguments to execute
    """
    # Load config to get proxy settings
    ccproxy_config_path = config_dir / "ccproxy.yaml"
    if not ccproxy_config_path.exists():
        print(f"Error: Configuration not found at {ccproxy_config_path}", file=sys.stderr)
        print("Run 'ccproxy install' first to set up configuration.", file=sys.stderr)
        sys.exit(1)

    with ccproxy_config_path.open() as f:
        config = yaml.safe_load(f)

    litellm_config = config.get("litellm", {}) if config else {}

    # Get proxy settings - port 4000 is always the entry point
    host = os.environ.get("HOST", litellm_config.get("host", "127.0.0.1"))
    port = int(os.environ.get("PORT", litellm_config.get("port", 4000)))

    # Set up environment for the subprocess
    env = os.environ.copy()

    # Always point to the main port (4000) - either LiteLLM or MITM in front
    proxy_url = f"http://{host}:{port}"
    env["OPENAI_API_BASE"] = proxy_url
    env["OPENAI_BASE_URL"] = proxy_url
    env["ANTHROPIC_BASE_URL"] = proxy_url

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


def generate_handler_file(config_dir: Path) -> None:
    """Generate the ccproxy.py handler file that LiteLLM will import.

    Args:
        config_dir: Configuration directory where ccproxy.py will be generated
    """
    import yaml

    # Load ccproxy.yaml to get handler configuration
    ccproxy_config_path = config_dir / "ccproxy.yaml"
    handler_import = "ccproxy.handler:CCProxyHandler"  # default

    if ccproxy_config_path.exists():
        try:
            with ccproxy_config_path.open() as f:
                config = yaml.safe_load(f)
                if config and "ccproxy" in config and "handler" in config["ccproxy"]:
                    handler_import = config["ccproxy"]["handler"]
        except Exception:
            pass  # Use default if config can't be loaded

    # Parse handler import path (format: "module.path:ClassName")
    if ":" in handler_import:
        module_path, class_name = handler_import.split(":", 1)
    else:
        # Fallback: assume it's just the module path
        module_path = handler_import
        class_name = "CCProxyHandler"

    # Check if handler file exists and is a user's custom file
    handler_file = config_dir / "ccproxy.py"
    if handler_file.exists():
        try:
            existing_content = handler_file.read_text()
            # Check if this is an auto-generated file
            if "Auto-generated handler file" not in existing_content:
                # This is a user's custom file - preserve it
                err_console = Console(stderr=True)
                err_console.print(
                    Panel(
                        "[yellow]Warning:[/yellow] Custom ccproxy.py file detected!\n\n"
                        f"Found existing file at: [cyan]{handler_file}[/cyan]\n\n"
                        "This file appears to be custom (not auto-generated).\n"
                        "It will NOT be overwritten.\n\n"
                        "To use auto-generation:\n"
                        f"  1. Remove the file: [dim]rm {handler_file}[/dim]\n"
                        "  2. Restart the proxy: [dim]ccproxy restart[/dim]\n\n"
                        "To use your custom handler:\n"
                        f"  • Set [bold]handler:[/bold] in [cyan]{ccproxy_config_path}[/cyan]\n"
                        "  • Example: [dim]handler: your_module.path:YourHandler[/dim]",
                        title="[bold red]Custom Handler Preserved[/bold red]",
                        border_style="yellow",
                    )
                )
                return
        except OSError:
            pass  # If we can't read the file, proceed with generation

    # Generate the handler file
    content = f'''"""
Auto-generated handler file for LiteLLM callbacks.
This file is generated by ccproxy on startup.
DO NOT EDIT - changes will be overwritten.
"""
import sys

# Import the handler class from the configured module
from {module_path} import {class_name}

# Create the handler instance that LiteLLM will use
handler = {class_name}()
'''

    handler_file.write_text(content)


def start_litellm(config_dir: Path, args: list[str] | None = None, detach: bool = False, mitm: bool = False) -> None:
    """Start the LiteLLM proxy server with ccproxy configuration.

    Args:
        config_dir: Configuration directory containing config files
        args: Additional arguments to pass to litellm command
        detach: Run in background mode with PID tracking
        mitm: Also start MITM proxy for traffic capture
    """
    from ccproxy.utils import find_available_port

    # Check if config exists
    config_path = config_dir / "config.yaml"
    if not config_path.exists():
        print(f"Error: Configuration not found at {config_path}", file=sys.stderr)
        print("Run 'ccproxy install' first to set up configuration.", file=sys.stderr)
        sys.exit(1)

    # Generate the handler file before starting LiteLLM
    try:
        generate_handler_file(config_dir)
    except Exception as e:
        print(f"Error generating handler file: {e}", file=sys.stderr)
        sys.exit(1)

    # Load litellm settings from ccproxy.yaml
    ccproxy_config_path = config_dir / "ccproxy.yaml"
    litellm_host = "127.0.0.1"
    main_port = 4000  # The port users connect to (reverse proxy)
    forward_port = 8081  # Forward proxy port for provider API calls

    if ccproxy_config_path.exists():
        with ccproxy_config_path.open() as f:
            ccproxy_config = yaml.safe_load(f)
            if ccproxy_config:
                litellm_section = ccproxy_config.get("litellm", {})
                litellm_host = os.environ.get("HOST", litellm_section.get("host", "127.0.0.1"))
                main_port = int(os.environ.get("PORT", litellm_section.get("port", 4000)))
                # Get forward proxy port from mitm config
                mitm_section = ccproxy_config.get("ccproxy", {}).get("mitm", {})
                forward_port = mitm_section.get("port", 8081)

    # Determine LiteLLM's actual port
    # When MITM enabled: MITM takes main_port, LiteLLM gets random port
    # When MITM disabled: LiteLLM runs on main_port directly
    if mitm:
        litellm_port = find_available_port()
        # Write LiteLLM port to state file for status/other tools
        litellm_port_file = config_dir / ".litellm_port"
        litellm_port_file.write_text(str(litellm_port))
    else:
        litellm_port = main_port
        # Remove port file if it exists (not using MITM)
        litellm_port_file = config_dir / ".litellm_port"
        if litellm_port_file.exists():
            litellm_port_file.unlink()

    # Set environment variable for ccproxy configuration location
    env = os.environ.copy()
    env["CCPROXY_CONFIG_DIR"] = str(config_dir.absolute())

    # Apply environment variables from litellm.environment config
    # Set in both os.environ (for MITM inheritance) and env dict (for LiteLLM subprocess)
    if ccproxy_config_path.exists() and ccproxy_config:
        litellm_env = litellm_section.get("environment", {})
        for key, value in litellm_env.items():
            # Expand ${VAR} and ${VAR:-default} patterns
            expanded = _expand_env_vars(str(value))
            env[key] = expanded
            os.environ[key] = expanded

    # When MITM is enabled, route LiteLLM's outbound traffic through forward proxy
    if mitm:
        forward_proxy_url = f"http://localhost:{forward_port}"
        env["HTTPS_PROXY"] = forward_proxy_url
        env["HTTP_PROXY"] = forward_proxy_url

    # Build litellm command using the bundled version from the same venv
    venv_bin = Path(sys.executable).parent
    litellm_path = venv_bin / "litellm"

    if not litellm_path.exists():
        print(f"Error: litellm not found in virtual environment at {litellm_path}", file=sys.stderr)
        print(
            "Make sure ccproxy is installed with: uv tool install claude-ccproxy --with 'litellm[proxy]'",
            file=sys.stderr,
        )
        sys.exit(1)

    cmd = [
        str(litellm_path),
        "--config",
        str(config_path),
        "--host",
        litellm_host,
        "--port",
        str(litellm_port),
    ]

    # Add any additional arguments
    if args:
        cmd.extend(args)

    # Start both MITM proxies if enabled (treated as a single unit)
    if mitm:
        import time

        from ccproxy.mitm import ProxyMode, start_mitm, stop_mitm
        from ccproxy.mitm.process import is_running as mitm_is_running

        print("Starting MITM reverse proxy...")
        # MITM₁ (reverse) listens on main_port (4000) and forwards to LiteLLM's random port
        start_mitm(config_dir, port=main_port, litellm_port=litellm_port, mode=ProxyMode.REVERSE, detach=True)

        # Verify reverse proxy started
        time.sleep(0.5)
        reverse_running, _ = mitm_is_running(config_dir, ProxyMode.REVERSE)
        if not reverse_running:
            print("Error: MITM reverse proxy failed to start", file=sys.stderr)
            sys.exit(1)

        print("Starting MITM forward proxy...")
        # MITM₂ (forward) listens on forward_port (8081) for LiteLLM's outbound calls
        start_mitm(config_dir, port=forward_port, mode=ProxyMode.FORWARD, detach=True)

        # Verify forward proxy started
        time.sleep(0.5)
        forward_running, _ = mitm_is_running(config_dir, ProxyMode.FORWARD)
        if not forward_running:
            print("Error: MITM forward proxy failed to start", file=sys.stderr)
            print("Stopping reverse proxy...")
            stop_mitm(config_dir, ProxyMode.REVERSE)
            sys.exit(1)

    if detach:
        # Run in background mode
        pid_file = config_dir / "litellm.lock"
        log_file = config_dir / "litellm.log"

        # Check if already running
        running, pid = is_process_running(pid_file)
        if running:
            print(f"LiteLLM is already running with PID {pid}", file=sys.stderr)
            print("To stop it, run: `ccproxy stop`", file=sys.stderr)
            sys.exit(1)

        # Start process in background
        try:
            with log_file.open("w") as log:
                # S603: Command construction is safe - we control the litellm path
                process = subprocess.Popen(  # noqa: S603
                    cmd,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,  # Detach from parent process group
                    env=env,
                )

            # Save PID
            write_pid(pid_file, process.pid)

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
            result = subprocess.run(cmd, env=env)  # noqa: S603
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
    # Also stop MITM if either proxy is running
    from ccproxy.mitm import stop_mitm
    from ccproxy.mitm.process import ProxyMode, is_running as mitm_is_running
    from ccproxy.process import read_pid

    reverse_running, _ = mitm_is_running(config_dir, ProxyMode.REVERSE)
    forward_running, _ = mitm_is_running(config_dir, ProxyMode.FORWARD)
    if reverse_running or forward_running:
        print("Stopping MITM proxies...")
        stop_mitm(config_dir)  # Stops all modes

    pid_file = config_dir / "litellm.lock"

    # Check if PID file exists
    if not pid_file.exists():
        print("No LiteLLM server is running (PID file not found)", file=sys.stderr)
        return False

    # Read PID to display in messages
    pid = read_pid(pid_file)
    if pid is None:
        print("Error reading PID file", file=sys.stderr)
        return False

    # Check if process is running
    running, _ = is_process_running(pid_file)
    if not running:
        print(f"LiteLLM server was not running (stale PID: {pid})")
        return False

    # Attempt to stop the process
    print(f"Stopping LiteLLM server (PID: {pid})...")

    # Stop the process and capture whether force kill was needed
    # We need to replicate stop_process logic to know which method was used
    try:
        os.kill(pid, signal.SIGTERM)
        time.sleep(0.5)

        # Check if still running
        try:
            os.kill(pid, 0)
            # Still running, force kill
            os.kill(pid, signal.SIGKILL)
            print(f"Force killed LiteLLM server (PID: {pid})")
        except ProcessLookupError:
            print(f"LiteLLM server stopped successfully (PID: {pid})")

        # Remove PID file
        pid_file.unlink()
        return True

    except OSError as e:
        print(f"Error stopping process: {e}", file=sys.stderr)
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


def handle_statusline_output(config_dir: Path) -> None:
    """Output routing status for ccstatusline widget.

    Args:
        config_dir: Configuration directory to get proxy settings
    """
    from ccproxy.statusline import format_status_output, query_status

    # Load config to get port
    ccproxy_config_path = config_dir / "ccproxy.yaml"
    port = 4000  # default

    if ccproxy_config_path.exists():
        try:
            with ccproxy_config_path.open() as f:
                config = yaml.safe_load(f)
                if config and "litellm" in config:
                    port = int(os.environ.get("PORT", config["litellm"].get("port", 4000)))
        except Exception:
            pass  # Use default port

    # Query proxy and format output
    status = query_status(port=port, timeout=0.1)
    proxy_reachable = status is not None
    output = format_status_output(status, proxy_reachable=proxy_reachable)

    # Always print output (ON or OFF)
    builtin_print(output)


def show_status(config_dir: Path, json_output: bool = False) -> None:
    """Show the status of LiteLLM proxy and ccproxy configuration.

    Args:
        config_dir: Configuration directory to check
        json_output: Output status as JSON with boolean values
    """
    from ccproxy.mitm import ProxyMode
    from ccproxy.mitm.process import is_running as mitm_is_running

    # Check LiteLLM proxy status
    pid_file = config_dir / "litellm.lock"
    log_file = config_dir / "litellm.log"

    proxy_running, _ = is_process_running(pid_file)

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

    # Extract callbacks and model_list from config.yaml
    callbacks = []
    model_list = []
    if litellm_config.exists():
        try:
            with litellm_config.open() as f:
                config_data = yaml.safe_load(f)
            if config_data:
                litellm_settings = config_data.get("litellm_settings", {})
                callbacks = litellm_settings.get("callbacks", [])
                model_list = config_data.get("model_list", [])
        except (yaml.YAMLError, OSError):
            pass

    # Extract hooks, proxy URL, and MITM config from ccproxy.yaml
    hooks = []
    proxy_url = None
    mitm_config = {}
    forward_port = 8081
    if ccproxy_config.exists():
        try:
            with ccproxy_config.open() as f:
                ccproxy_data = yaml.safe_load(f)
            if ccproxy_data:
                ccproxy_section = ccproxy_data.get("ccproxy", {})
                hooks = ccproxy_section.get("hooks", [])
                mitm_config = ccproxy_section.get("mitm", {})
                forward_port = mitm_config.get("port", 8081)
                # Get proxy URL from litellm config section
                litellm_section = ccproxy_data.get("litellm", {})
                host = os.environ.get("HOST", litellm_section.get("host", "127.0.0.1"))
                port = int(os.environ.get("PORT", litellm_section.get("port", 4000)))
                proxy_url = f"http://{host}:{port}"
        except (yaml.YAMLError, OSError):
            pass

    # Check MITM status for both modes
    reverse_running, reverse_pid = mitm_is_running(config_dir, ProxyMode.REVERSE)
    forward_running, forward_pid = mitm_is_running(config_dir, ProxyMode.FORWARD)
    mitm_enabled = mitm_config.get("enabled", False)

    # Get ports - main port is always the entry point (4000 by default)
    main_port = 4000
    litellm_actual_port = main_port  # Default: LiteLLM on main port

    # Read actual LiteLLM port from state file (when MITM is running)
    litellm_port_file = config_dir / ".litellm_port"
    if litellm_port_file.exists():
        with contextlib.suppress(ValueError, OSError):
            litellm_actual_port = int(litellm_port_file.read_text().strip())

    # Build status data
    status_data = {
        "proxy": proxy_running,
        "url": proxy_url,
        "config": config_paths,
        "callbacks": callbacks,
        "hooks": hooks,
        "model_list": model_list,
        "log": str(log_file) if log_file.exists() else None,
        "mitm": {
            "enabled": mitm_enabled,
            "reverse": {
                "running": reverse_running,
                "pid": reverse_pid,
                "port": main_port,
            },
            "forward": {
                "running": forward_running,
                "pid": forward_pid,
                "port": forward_port,
            },
            "litellm_port": litellm_actual_port,
        },
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

        # MITM status - show both proxies
        mitm_info = status_data["mitm"]
        reverse_info = mitm_info["reverse"]
        forward_info = mitm_info["forward"]
        litellm_port = mitm_info["litellm_port"]

        mitm_parts = []

        # Reverse proxy status
        if reverse_info["running"]:
            reverse_port = reverse_info["port"]
            reverse_status = (
                f"[green]reverse[/green] on [cyan]{reverse_port}[/cyan] → litellm on [cyan]{litellm_port}[/cyan]"
            )
            if reverse_info["pid"]:
                reverse_status += f" [dim](pid: {reverse_info['pid']})[/dim]"
            mitm_parts.append(reverse_status)
        else:
            mitm_parts.append("[dim]reverse: stopped[/dim]")

        # Forward proxy status
        if forward_info["running"]:
            forward_port = forward_info["port"]
            forward_status = f"[green]forward[/green] on [cyan]{forward_port}[/cyan] → providers"
            if forward_info["pid"]:
                forward_status += f" [dim](pid: {forward_info['pid']})[/dim]"
            mitm_parts.append(forward_status)
        else:
            mitm_parts.append("[dim]forward: stopped[/dim]")

        mitm_display = "\n".join(mitm_parts)
        table.add_row("mitm", mitm_display)

        # Config files
        if status_data["config"]:
            config_display = "\n".join(f"[cyan]{key}[/cyan]: {value}" for key, value in status_data["config"].items())
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

        # Hooks table
        if status_data["hooks"]:
            hooks_table = Table(show_header=True, show_lines=True)
            hooks_table.add_column("#", style="dim", width=3)
            hooks_table.add_column("Hook", style="cyan")
            hooks_table.add_column("Parameters", style="yellow")

            for i, hook in enumerate(status_data["hooks"], 1):
                if isinstance(hook, str):
                    # Simple string format - extract function name
                    hook_name = hook.split(".")[-1]
                    hook_path = hook
                    params_display = "[dim]none[/dim]"
                else:
                    # Dict format with params
                    hook_path = hook.get("hook", "")
                    hook_name = hook_path.split(".")[-1] if hook_path else ""
                    params = hook.get("params", {})
                    if params:
                        params_display = ", ".join(f"{k}={v}" for k, v in params.items())
                    else:
                        params_display = "[dim]none[/dim]"

                hooks_table.add_row(str(i), f"[bold]{hook_name}[/bold]\n[dim]{hook_path}[/dim]", params_display)

            console.print(Panel(hooks_table, title="[bold]Hooks[/bold]", border_style="green"))

        # Model deployments table
        if status_data["model_list"]:
            models_table = Table(show_header=True, show_lines=True, expand=True)
            models_table.add_column("Model Name", style="cyan", no_wrap=True)
            models_table.add_column("Provider Model", style="yellow", no_wrap=True)
            models_table.add_column("API Base", style="dim", no_wrap=True)

            # Build lookup for resolving model aliases
            model_lookup = {m.get("model_name", ""): m for m in status_data["model_list"]}

            for model in status_data["model_list"]:
                model_name = model.get("model_name", "")
                litellm_params = model.get("litellm_params", {})
                provider_model = litellm_params.get("model", "")
                api_base = litellm_params.get("api_base")

                # Resolve API base from target model if this is an alias
                if not api_base and provider_model in model_lookup:
                    target = model_lookup[provider_model]
                    api_base = target.get("litellm_params", {}).get("api_base")

                # Shorten API base to just the hostname
                if api_base:
                    from urllib.parse import urlparse

                    parsed = urlparse(api_base)
                    api_base_display = parsed.netloc or api_base
                else:
                    api_base_display = "[dim]default[/dim]"

                models_table.add_row(model_name, provider_model, api_base_display)

            console.print(Panel(models_table, title="[bold]Model Deployments[/bold]", border_style="magenta"))


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
        start_litellm(config_dir, args=cmd.args, detach=cmd.detach, mitm=cmd.mitm)

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
        # Check if MITM is running before stopping (check reverse mode)
        from ccproxy.mitm import ProxyMode
        from ccproxy.mitm.process import is_running as mitm_is_running

        mitm_was_running, _ = mitm_is_running(config_dir, ProxyMode.REVERSE)

        # Stop the server first
        pid_file = config_dir / "litellm.lock"
        if pid_file.exists():
            print("Stopping LiteLLM server...")
            stop_litellm(config_dir)
        else:
            print("No server running, starting fresh...")

        # Wait for clean shutdown
        time.sleep(1)

        # Start the server with same MITM state
        print("Starting LiteLLM server...")
        start_litellm(config_dir, args=cmd.args, detach=cmd.detach, mitm=mitm_was_running)

    elif isinstance(cmd, Logs):
        view_logs(config_dir, follow=cmd.follow, lines=cmd.lines)

    elif isinstance(cmd, Status):
        show_status(config_dir, json_output=cmd.json)

    elif isinstance(cmd, StatuslineOutput):
        handle_statusline_output(config_dir)

    elif isinstance(cmd, (StatuslineInstall, StatuslineUninstall, StatuslineStatus)):
        from ccproxy.statusline import (
            install_statusline,
            show_statusline_status,
            uninstall_statusline,
        )

        # Extract Claude config dir from global config_dir if different
        claude_config_dir = Path.home() / ".claude"

        if isinstance(cmd, StatuslineInstall):
            success = install_statusline(
                force=cmd.force,
                use_bun=cmd.use_bun,
                claude_config_dir=claude_config_dir,
            )
            sys.exit(0 if success else 1)

        elif isinstance(cmd, StatuslineUninstall):
            success = uninstall_statusline(claude_config_dir=claude_config_dir)
            sys.exit(0 if success else 1)

        elif isinstance(cmd, StatuslineStatus):
            show_statusline_status(claude_config_dir=claude_config_dir)


def entry_point() -> None:
    """Entry point for the ccproxy command."""
    # Handle 'run' and 'statusline' subcommands specially
    # - 'run': avoid tyro parsing command arguments (ccproxy run claude -p foo)
    # - 'statusline' (no subcommand): route to StatuslineOutput
    # - 'statusline <subcommand>': rewrite to statusline-<subcommand> for tyro
    args = sys.argv[1:]

    # Check for 'statusline' with subcommand
    subcommands = {"start", "stop", "restart", "install", "logs", "status", "run", "statusline"}
    statusline_subcommands = {"install", "uninstall", "status"}

    statusline_idx = None
    run_idx = None

    for i, arg in enumerate(args):
        if arg == "statusline":
            # Check if next arg is a statusline subcommand
            if i + 1 < len(args) and args[i + 1] in statusline_subcommands:
                # Rewrite "statusline install" -> "statusline-install"
                subcommand = args[i + 1]
                new_args = args[:i] + [f"statusline-{subcommand}"] + args[i + 2 :]
                sys.argv = [sys.argv[0]] + new_args
                break
            # Check for flags (--help, --force, etc.)
            elif i + 1 < len(args) and args[i + 1].startswith("-"):
                # Has flags but no subcommand - error case, let tyro handle it
                pass
            else:
                # Standalone 'statusline' with no subcommand
                statusline_idx = i
            break
        elif arg == "run":
            run_idx = i
            break
        # Stop if we hit a different subcommand
        if arg in subcommands:
            break

    # Handle standalone 'ccproxy statusline' (no subcommand)
    if statusline_idx is not None:
        # Route to StatuslineOutput
        args_before = args[:statusline_idx]

        # Parse config_dir from args if present
        config_dir = Path.home() / ".ccproxy"
        try:
            if "--config-dir" in args_before:
                idx = args_before.index("--config-dir")
                if idx + 1 < len(args_before):
                    config_dir = Path(args_before[idx + 1])
        except (ValueError, IndexError):
            pass

        # Call statusline output directly
        handle_statusline_output(config_dir)
        sys.exit(0)

    # Handle 'run' subcommand
    if run_idx is not None:
        # Extract command after 'run'
        command_args = args[run_idx + 1 :]

        # Only insert '--' if not already present (backwards compatibility)
        if command_args and command_args[0] != "--":
            # Rebuild argv: keep everything up to and including 'run', then '--' to escape the rest
            sys.argv = [sys.argv[0]] + args[: run_idx + 1] + ["--"] + command_args

    tyro.cli(main)


if __name__ == "__main__":
    entry_point()
