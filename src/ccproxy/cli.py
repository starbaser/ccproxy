"""ccproxy CLI for managing the LiteLLM proxy server - Tyro implementation."""

import contextlib
import json
import logging
import logging.config
import os
import select
import shutil
import signal
import subprocess
import sys
import time
from builtins import print as builtin_print
from pathlib import Path
from typing import Annotated, Literal

import attrs
import tyro
import yaml
from rich import print
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ccproxy.process import is_process_running, write_pid
from ccproxy.utils import get_templates_dir

logger = logging.getLogger(__name__)


def _read_proxy_settings(config_dir: Path) -> tuple[str, int]:
    """Read host and port from the config directory.

    Checks config.yaml general_settings first (LiteLLM's canonical location),
    then falls back to ccproxy.yaml litellm section (legacy global config).
    Env vars HOST/PORT override both.
    """
    host = "127.0.0.1"
    port = 4000
    host_set = False
    port_set = False

    # Primary: config.yaml general_settings (per-project and modern configs)
    config_yaml = config_dir / "config.yaml"
    if config_yaml.exists():
        try:
            with config_yaml.open() as f:
                data = yaml.safe_load(f) or {}
            general = data.get("general_settings", {})
            if "host" in general:
                host = general["host"]
                host_set = True
            if "port" in general:
                port = int(general["port"])
                port_set = True
        except (yaml.YAMLError, OSError, ValueError):
            pass

    # Fallback: ccproxy.yaml litellm section
    ccproxy_yaml = config_dir / "ccproxy.yaml"
    if ccproxy_yaml.exists():
        try:
            with ccproxy_yaml.open() as f:
                data = yaml.safe_load(f) or {}
            litellm = data.get("litellm", {})
            if not host_set:
                host = litellm.get("host", host)
            if not port_set:
                port = int(litellm.get("port", port))
        except (yaml.YAMLError, OSError, ValueError):
            pass

    host = os.environ.get("HOST", host)
    port = int(os.environ.get("PORT", str(port)))
    return host, port


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
    """Run a command with ccproxy environment.

    Usage: ccproxy run [--shadow] [--shadow-port PORT] -- <command> [args...]"""

    shadow: Annotated[bool, tyro.conf.arg(aliases=["-s"])] = False
    """Route all subprocess HTTP/HTTPS through MITM shadow proxy for capture.
    Sets HTTP_PROXY/HTTPS_PROXY to route non-localhost traffic through a
    dedicated forward proxy instance. API calls still flow through the
    primary proxy via ANTHROPIC_BASE_URL. Note: Node.js does not natively
    honor HTTP_PROXY; this captures traffic from curl, Python, and other
    tools that respect standard proxy env vars."""

    shadow_port: int = 8082
    """Port for the shadow forward proxy (only used with --shadow)."""

    command: Annotated[list[str], tyro.conf.Positional] = attrs.Factory(list)
    """Command and arguments to execute with proxy settings."""


@attrs.define
class Stop:
    """Stop the LiteLLM proxy server."""


@attrs.define
class Restart:
    """Restart the LiteLLM proxy server (stop then start).

    MITM state is preserved automatically from the running configuration.
    """

    args: Annotated[list[str] | None, tyro.conf.Positional] = None
    """Additional arguments to pass to litellm command."""

    detach: Annotated[bool, tyro.conf.arg(aliases=["-d"])] = False
    """Run in background and save PID to litellm.lock."""


LogSource = Literal["litellm", "mitm", "forward", "all"]


@attrs.define
class Logs:
    """View the LiteLLM log file."""

    source: Annotated[LogSource, tyro.conf.Positional] = "litellm"
    """Log source to view: litellm, mitm, forward, or all."""

    follow: Annotated[bool, tyro.conf.arg(aliases=["-f"])] = False
    """Follow log output (like tail -f)."""

    lines: Annotated[int, tyro.conf.arg(aliases=["-n"])] = 100
    """Number of lines to show (default: 100)."""


@attrs.define
class Status:
    """Show the status of LiteLLM proxy and ccproxy configuration.

    When service flags (--proxy, --reverse, --forward) are specified,
    runs in health check mode with bitmask exit codes:

      0 = all healthy    4 = forward down
      1 = proxy down     5 = proxy+forward
      2 = reverse down   6 = reverse+forward
      3 = proxy+reverse  7 = all down

    Examples:
        ccproxy status --proxy --reverse --forward  # All must be running
        ccproxy status --proxy                      # Just check LiteLLM
    """

    json: bool = False
    """Output status as JSON with boolean values."""

    proxy: bool = False
    """Check if LiteLLM proxy is running."""

    reverse: bool = False
    """Check if MITM reverse proxy is running."""

    forward: bool = False
    """Check if MITM forward proxy is running."""


@attrs.define
class StatuslineOutput:
    """Output routing status for the statusline widget."""


@attrs.define
class StatuslineInstall:
    """Install the statusline widget and configure Claude Code integration."""

    force: bool = False
    """Overwrite existing configuration."""

    use_bun: bool = False
    """Use bunx instead of npx."""


@attrs.define
class StatuslineUninstall:
    """Remove statusline configuration."""


@attrs.define
class StatuslineStatus:
    """Show statusline installation status."""


@attrs.define
class DbSql:
    """Execute SQL queries against the MITM traces database."""

    query: Annotated[str | None, tyro.conf.Positional] = None
    """SQL query to execute (inline)."""

    file: Annotated[Path | None, tyro.conf.arg(aliases=["-f"])] = None
    """Read SQL from file."""

    json: Annotated[bool, tyro.conf.arg(aliases=["-j"])] = False
    """Output results as JSON."""

    csv: Annotated[bool, tyro.conf.arg(aliases=["-c"])] = False
    """Output results as CSV."""


@attrs.define
class DbPrompt:
    """Convert a MITM trace to formatted markdown showing the conversation."""

    trace_id: Annotated[str, tyro.conf.Positional]
    """Trace ID to convert."""

    output: Annotated[Path | None, tyro.conf.arg(aliases=["-o"])] = None
    """Output file path. Defaults to stdout."""

    direction: Annotated[str, tyro.conf.arg(aliases=["-d"])] = "forward"
    """Proxy direction filter: 'forward' (default), 'reverse', or 'both'."""

    include_headers: Annotated[bool, tyro.conf.arg(aliases=["-H"])] = False
    """Include HTTP headers in output."""

    raw: Annotated[bool, tyro.conf.arg(aliases=["-r"])] = False
    """Output raw JSON bodies instead of formatted markdown."""


@attrs.define
class DagViz:
    """Visualize the hook pipeline DAG (Directed Acyclic Graph).

    Shows hook execution order and dependencies based on reads/writes declarations.
    """

    output: Annotated[str, tyro.conf.arg(aliases=["-o"])] = "ascii"
    """Output format: ascii, mermaid, json."""

    validate: Annotated[bool, tyro.conf.arg(aliases=["-v"])] = False
    """Validate the DAG and report any issues."""


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
    | Annotated[DbSql, tyro.conf.subcommand(name="db-sql")]
    | Annotated[DbPrompt, tyro.conf.subcommand(name="db-prompt")]
    | Annotated[DagViz, tyro.conf.subcommand(name="dag-viz")]
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


def _ensure_combined_ca_bundle(config_dir: Path, base_ssl_cert: str | None = None) -> Path | None:
    """Build a combined CA bundle with mitmproxy's CA + system CAs.

    mitmproxy intercepts TLS and re-signs with its own CA. Subprocesses need
    to trust both the mitmproxy CA and real upstream CAs.

    Args:
        config_dir: Configuration directory for storing the bundle
        base_ssl_cert: Base SSL_CERT_FILE path (uses system default if None)

    Returns:
        Path to combined bundle, or None if mitmproxy CA not found
    """
    mitm_ca = Path.home() / ".mitmproxy" / "mitmproxy-ca-cert.pem"
    if not mitm_ca.exists():
        return None

    combined_bundle = config_dir / "combined-ca-bundle.pem"
    base_ca = base_ssl_cert or os.environ.get("SSL_CERT_FILE", "/etc/ssl/certs/ca-certificates.crt")
    try:
        mitm_ca_data = mitm_ca.read_text()
        base_ca_data = Path(base_ca).read_text() if Path(base_ca).exists() else ""
        combined_bundle.write_text(mitm_ca_data + "\n" + base_ca_data)
        return combined_bundle
    except OSError:
        return None


def run_with_proxy(
    config_dir: Path,
    command: list[str],
    shadow: bool = False,
    shadow_port: int = 8082,
) -> None:
    """Run a command with ccproxy environment variables set.

    The main port (default 4000) is always the entry point:
    - Without MITM: LiteLLM runs on port 4000
    - With MITM: MITM runs on port 4000, forwards to LiteLLM on a random port

    Args:
        config_dir: Configuration directory
        command: Command and arguments to execute
        shadow: Enable shadow proxy for blanket HTTP traffic capture
        shadow_port: Port for the shadow forward proxy
    """
    # Load config to get proxy settings
    ccproxy_config_path = config_dir / "ccproxy.yaml"
    if not ccproxy_config_path.exists():
        print(f"Error: Configuration not found at {ccproxy_config_path}", file=sys.stderr)
        print("Run 'ccproxy install' first to set up configuration.", file=sys.stderr)
        sys.exit(1)

    host, port = _read_proxy_settings(config_dir)

    # Set up environment for the subprocess
    env = os.environ.copy()

    proxy_url = f"http://{host}:{port}"
    env["OPENAI_API_BASE"] = proxy_url
    env["OPENAI_BASE_URL"] = proxy_url
    env["ANTHROPIC_BASE_URL"] = proxy_url

    # Shadow mode: route all non-localhost HTTP through a dedicated forward proxy
    shadow_started = False
    if shadow:
        from ccproxy.mitm.process import ProxyMode, is_running, start_mitm, stop_mitm

        running, _ = is_running(config_dir, ProxyMode.SHADOW)
        if not running:
            logger.info("Starting shadow proxy on port %d...", shadow_port)
            start_mitm(config_dir, port=shadow_port, mode=ProxyMode.SHADOW, detach=True)
            shadow_started = True

        shadow_proxy_url = f"http://127.0.0.1:{shadow_port}"
        env["HTTP_PROXY"] = shadow_proxy_url
        env["HTTPS_PROXY"] = shadow_proxy_url
        env["NO_PROXY"] = "localhost,127.0.0.1,::1"
        env["no_proxy"] = "localhost,127.0.0.1,::1"

        # Ensure SSL trust for mitmproxy-signed certs
        combined_bundle = _ensure_combined_ca_bundle(config_dir, env.get("SSL_CERT_FILE"))
        if combined_bundle:
            env["SSL_CERT_FILE"] = str(combined_bundle)
            env["NODE_EXTRA_CA_CERTS"] = str(combined_bundle)
            env["REQUESTS_CA_BUNDLE"] = str(combined_bundle)
        else:
            print(
                "Warning: mitmproxy CA not found (~/.mitmproxy/mitmproxy-ca-cert.pem). "
                "HTTPS capture may fail. Run 'ccproxy start --mitm' once to generate it.",
                file=sys.stderr,
            )

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
    finally:
        if shadow_started:
            from ccproxy.mitm.process import ProxyMode, stop_mitm

            stop_mitm(config_dir, mode=ProxyMode.SHADOW)


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


def start_litellm(
    config_dir: Path,
    args: list[str] | None = None,
    detach: bool = False,
    mitm: bool = False,
) -> None:
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

    # Read proxy host/port from config.yaml general_settings
    litellm_host, main_port = _read_proxy_settings(config_dir)
    forward_port = 8081  # Forward proxy port for provider API calls

    # Load ccproxy.yaml for MITM forward port
    ccproxy_config_path = config_dir / "ccproxy.yaml"
    ccproxy_config = None
    if ccproxy_config_path.exists():
        with ccproxy_config_path.open() as f:
            ccproxy_config = yaml.safe_load(f)
            if ccproxy_config:
                mitm_section = ccproxy_config.get("ccproxy", {}).get("mitm", {})
                forward_port = mitm_section.get("port", 8081)

    # Pre-flight: kill orphans, verify ports are free
    from ccproxy.preflight import run_preflight_checks

    ports_to_check = [main_port, forward_port] if mitm else [main_port]
    run_preflight_checks(config_dir, ports=ports_to_check)

    # Generate the handler file before starting LiteLLM
    try:
        generate_handler_file(config_dir)
    except Exception as e:
        print(f"Error generating handler file: {e}", file=sys.stderr)
        sys.exit(1)

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

    # Apply environment variables from ccproxy.yaml litellm.environment
    # Set in both os.environ (for MITM inheritance) and env dict (for LiteLLM subprocess)
    if ccproxy_config_path.exists() and ccproxy_config:
        litellm_env = ccproxy_config.get("litellm", {}).get("environment", {})
        for key, value in litellm_env.items():
            # Expand ${VAR} and ${VAR:-default} patterns
            expanded = _expand_env_vars(str(value))
            env[key] = expanded
            os.environ[key] = expanded

    # Ensure SSL_CERT_FILE is set for the litellm subprocess.
    # aiohttp creates a module-level SSL context at import time via ssl.create_default_context(),
    # and litellm has fallback code paths that create bare ClientSession() without explicit SSL
    # context. On NixOS (and other non-standard layouts), the compiled-in OpenSSL default path
    # (/etc/ssl/cert.pem) doesn't exist. Setting SSL_CERT_FILE before subprocess launch ensures
    # all code paths find valid CA certificates.
    if "SSL_CERT_FILE" not in env:
        try:
            import certifi

            env["SSL_CERT_FILE"] = certifi.where()
        except ImportError:
            pass

    # When MITM is enabled, route LiteLLM's outbound traffic through forward proxy.
    # mitmproxy intercepts TLS (MITM), so litellm sees mitmproxy-signed certs.
    # Build a combined CA bundle with mitmproxy's CA + system/certifi CAs so the
    # SSL context trusts both the proxy-issued certs and real upstream certs.
    if mitm:
        forward_proxy_url = f"http://localhost:{forward_port}"
        env["HTTPS_PROXY"] = forward_proxy_url
        env["HTTP_PROXY"] = forward_proxy_url

        combined_bundle = _ensure_combined_ca_bundle(config_dir, env.get("SSL_CERT_FILE"))
        if combined_bundle:
            env["SSL_CERT_FILE"] = str(combined_bundle)

    # Build litellm command using the bundled version from the same venv
    venv_bin = Path(sys.executable).parent
    litellm_path = venv_bin / "litellm"

    if not litellm_path.exists():
        print(
            f"Error: litellm not found in virtual environment at {litellm_path}",
            file=sys.stderr,
        )
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
        start_mitm(
            config_dir,
            port=main_port,
            litellm_port=litellm_port,
            mode=ProxyMode.REVERSE,
            detach=True,
        )

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
            console = Console()
            console.print(f"[dim]Proxy already running (PID {pid}), attaching to logs...[/dim]")
            view_logs(config_dir, source="all", follow=True)
            sys.exit(0)

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
            print(
                "Please ensure LiteLLM is installed: pip install litellm",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        # Execute litellm command in foreground
        try:
            # S603: Command construction is safe - we control the litellm path
            result = subprocess.run(cmd, env=env)  # noqa: S603
            sys.exit(result.returncode)
        except FileNotFoundError:
            print("Error: litellm command not found.", file=sys.stderr)
            print(
                "Please ensure LiteLLM is installed: pip install litellm",
                file=sys.stderr,
            )
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
    from ccproxy.mitm.process import ProxyMode
    from ccproxy.mitm.process import is_running as mitm_is_running
    from ccproxy.process import read_pid

    reverse_running, _ = mitm_is_running(config_dir, ProxyMode.REVERSE)
    forward_running, _ = mitm_is_running(config_dir, ProxyMode.FORWARD)
    shadow_running, _ = mitm_is_running(config_dir, ProxyMode.SHADOW)
    if reverse_running or forward_running or shadow_running:
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


def get_log_paths(config_dir: Path, source: LogSource) -> list[tuple[str, Path]]:
    """Get (tag, path) tuples for the specified source.

    Args:
        config_dir: Configuration directory containing log files
        source: Log source to retrieve

    Returns:
        List of (tag, path) tuples for the log files
    """
    paths = []
    if source in ("litellm", "all"):
        paths.append(("litellm", config_dir / "litellm.log"))
    if source in ("mitm", "all"):
        paths.append(("mitm", config_dir / "mitm.log"))
    if source in ("forward", "all"):
        paths.append(("forward", config_dir / "mitm-forward.log"))
    return paths


def view_logs(config_dir: Path, source: LogSource = "litellm", follow: bool = False, lines: int = 100) -> None:
    """View log files using system pager.

    Args:
        config_dir: Configuration directory containing the log files
        source: Log source to view (litellm, mitm, forward, or all)
        follow: Follow log output (like tail -f)
        lines: Number of lines to show
    """
    log_paths = get_log_paths(config_dir, source)

    # Check if log files exist
    existing_logs = [(tag, path) for tag, path in log_paths if path.exists()]

    if not existing_logs:
        print("[red]No log files found[/red]", file=sys.stderr)
        print("[dim]Expected log files:[/dim]", file=sys.stderr)
        for tag, path in log_paths:
            print(f"  {tag}: {path}", file=sys.stderr)
        sys.exit(1)

    if follow:
        # Single file: use plain tail -f
        if len(existing_logs) == 1:
            _, log_file = existing_logs[0]
            try:
                # S603, S607: tail is a standard system command, file path is validated
                result = subprocess.run(["tail", "-f", str(log_file)])  # noqa: S603, S607
                sys.exit(result.returncode)
            except KeyboardInterrupt:
                sys.exit(0)
            except FileNotFoundError:
                print("[red]Error: 'tail' command not found[/red]", file=sys.stderr)
                sys.exit(1)

        # Multiple files: multiplex with colored tags
        colors = {
            "litellm": "\033[36m",  # cyan
            "mitm": "\033[32m",  # green
            "forward": "\033[33m",  # yellow
        }
        reset = "\033[0m"

        # Start tail processes for each file
        processes = []
        for tag, log_file in existing_logs:
            try:
                # S603, S607: tail is a standard system command, file path is validated
                proc = subprocess.Popen(  # noqa: S603
                    ["tail", "-f", str(log_file)],  # noqa: S607
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    bufsize=1,
                    universal_newlines=True,
                )
                processes.append((tag, proc))
            except FileNotFoundError:
                print("[red]Error: 'tail' command not found[/red]", file=sys.stderr)
                sys.exit(1)

        try:
            # Multiplex output from all processes
            while True:
                for tag, proc in processes:
                    # Use select to check if data is available (non-blocking)
                    if proc.stdout and select.select([proc.stdout], [], [], 0.1)[0]:
                        line = proc.stdout.readline()
                        if line:
                            color = colors.get(tag, "")
                            # Print with colored tag prefix
                            print(f"{color}[{tag}]{reset} {line}", end="")

        except KeyboardInterrupt:
            # Clean up processes
            for _, proc in processes:
                proc.terminate()
            sys.exit(0)

    else:
        # Non-follow mode: read last N lines
        if len(existing_logs) == 1:
            # Single file: use existing pager logic
            _, log_file = existing_logs[0]
            pager = os.environ.get("PAGER", "less")

            try:
                with log_file.open("r") as f:
                    all_lines = f.readlines()
                    tail_lines = all_lines[-lines:] if len(all_lines) > lines else all_lines
                    content = "".join(tail_lines)

                    if not content.strip():
                        print("[yellow]Log file is empty[/yellow]")
                        sys.exit(0)

                    if len(tail_lines) > 20 or pager == "cat":
                        # S603: pager comes from PAGER env var, standard practice for CLI tools
                        process = subprocess.Popen([pager], stdin=subprocess.PIPE)  # noqa: S603
                        process.communicate(content.encode())
                        sys.exit(process.returncode)
                    else:
                        print(content, end="")
                        sys.exit(0)

            except OSError as e:
                print(f"[red]Error reading log file: {e}[/red]", file=sys.stderr)
                sys.exit(1)

        else:
            # Multiple files: show last N lines from each with headers
            pager = os.environ.get("PAGER", "less")
            all_content = []

            for tag, log_file in existing_logs:
                try:
                    with log_file.open("r") as f:
                        file_lines = f.readlines()
                        tail_lines = file_lines[-lines:] if len(file_lines) > lines else file_lines

                        if tail_lines:
                            # Add header for this log file
                            all_content.append(f"==> {tag} <==\n")
                            all_content.extend(tail_lines)
                            all_content.append("\n")

                except OSError as e:
                    print(f"[yellow]Warning: Could not read {tag}: {e}[/yellow]", file=sys.stderr)

            if not all_content:
                print("[yellow]All log files are empty[/yellow]")
                sys.exit(0)

            content = "".join(all_content)

            if len(all_content) > 20 or pager == "cat":
                # S603: pager comes from PAGER env var, standard practice for CLI tools
                process = subprocess.Popen([pager], stdin=subprocess.PIPE)  # noqa: S603
                process.communicate(content.encode())
                sys.exit(process.returncode)
            else:
                print(content, end="")
                sys.exit(0)


def handle_statusline_output(config_dir: Path) -> None:
    """Output routing status for ccstatusline widget.

    Args:
        config_dir: Configuration directory to get proxy settings
    """
    from ccproxy.statusline import format_status_output, query_status

    _, port = _read_proxy_settings(config_dir)

    # Query proxy and format output
    status = query_status(port=port, timeout=0.1)
    proxy_reachable = status is not None
    output = format_status_output(status, proxy_reachable=proxy_reachable)

    # Always print output (ON or OFF)
    builtin_print(output)


def show_status(
    config_dir: Path,
    json_output: bool = False,
    check_proxy: bool = False,
    check_reverse: bool = False,
    check_forward: bool = False,
) -> None:
    """Show the status of LiteLLM proxy and ccproxy configuration.

    Args:
        config_dir: Configuration directory to check
        json_output: Output status as JSON with boolean values
        check_proxy: Health check - require LiteLLM proxy running
        check_reverse: Health check - require MITM reverse proxy running
        check_forward: Health check - require MITM forward proxy running

    When any check_* flag is True, exits 0 only if ALL specified services
    are healthy, otherwise exits 1. No output is produced in check mode.
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

    # Extract hooks and MITM config from ccproxy.yaml
    hooks = []
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
        except (yaml.YAMLError, OSError):
            pass

    # Read proxy host/port from config.yaml general_settings
    host, main_port = _read_proxy_settings(config_dir)
    proxy_url = f"http://{host}:{main_port}"

    # Check MITM status for all modes
    reverse_running, reverse_pid = mitm_is_running(config_dir, ProxyMode.REVERSE)
    forward_running, forward_pid = mitm_is_running(config_dir, ProxyMode.FORWARD)
    shadow_running, shadow_pid = mitm_is_running(config_dir, ProxyMode.SHADOW)
    mitm_enabled = mitm_config.get("enabled", False)
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
            "shadow": {
                "running": shadow_running,
                "pid": shadow_pid,
                "port": 8082,
            },
            "litellm_port": litellm_actual_port,
        },
    }

    # Health check mode: exit with bitmask code indicating failed services
    # Bit 0 (1): proxy, Bit 1 (2): reverse, Bit 2 (4): forward
    if check_proxy or check_reverse or check_forward:
        exit_code = 0
        if check_proxy and not proxy_running:
            exit_code |= 1
        if check_reverse and not reverse_running:
            exit_code |= 2
        if check_forward and not forward_running:
            exit_code |= 4
        sys.exit(exit_code)

    if json_output:
        builtin_print(json.dumps(status_data, indent=2))
    else:
        # Rich table output
        console = Console()

        table = Table(show_header=False, show_lines=True)
        table.add_column("Key", style="white", width=15)
        table.add_column("Value", style="yellow")

        # Proxy status with URL
        url = status_data.get("url") or "http://127.0.0.1:4000"
        if status_data["proxy"]:
            proxy_status = f"[cyan]{url}[/cyan] [green]true[/green]"
        else:
            proxy_status = f"[dim]{url}[/dim] [red]false[/red]"
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

        # Shadow proxy status
        shadow_info = mitm_info["shadow"]
        if shadow_info["running"]:
            shadow_port = shadow_info["port"]
            shadow_status = f"[green]shadow[/green] on [cyan]{shadow_port}[/cyan] → all HTTP capture"
            if shadow_info["pid"]:
                shadow_status += f" [dim](pid: {shadow_info['pid']})[/dim]"
            mitm_parts.append(shadow_status)

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

                hooks_table.add_row(
                    str(i),
                    f"[bold]{hook_name}[/bold]\n[dim]{hook_path}[/dim]",
                    params_display,
                )

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

            console.print(
                Panel(
                    models_table,
                    title="[bold]Model Deployments[/bold]",
                    border_style="magenta",
                )
            )


# === Database SQL Command Handlers ===


def get_database_url(config_dir: Path) -> str | None:
    """Get database URL from config or environment.

    Checks in order:
    1. CCPROXY_DATABASE_URL environment variable
    2. DATABASE_URL environment variable
    3. ccproxy.yaml mitm.database_url config

    Args:
        config_dir: Configuration directory containing ccproxy.yaml

    Returns:
        Database URL string or None if not configured
    """
    if url := os.environ.get("CCPROXY_DATABASE_URL") or os.environ.get("DATABASE_URL"):
        return url

    ccproxy_yaml = config_dir / "ccproxy.yaml"
    if ccproxy_yaml.exists():
        with ccproxy_yaml.open() as f:
            data = yaml.safe_load(f)
        if data and "ccproxy" in data:
            mitm = data["ccproxy"].get("mitm", {})
            if url := mitm.get("database_url"):
                return _expand_env_vars(url) if "${" in url else url
    return None


async def execute_sql(database_url: str, query: str) -> tuple[list[dict], list[str]]:
    """Execute SQL query and return results.

    Args:
        database_url: PostgreSQL connection string
        query: SQL query to execute

    Returns:
        Tuple of (rows as list of dicts, column names)
    """
    import asyncpg

    conn = await asyncpg.connect(database_url)
    try:
        result = await conn.fetch(query)
        if not result:
            return [], []
        columns = list(result[0].keys())
        rows = [dict(row) for row in result]
        return rows, columns
    finally:
        await conn.close()


def resolve_sql_input(cmd: DbSql) -> str | None:
    """Resolve SQL query from inline argument, file, or stdin.

    Args:
        cmd: DbSql command with query sources

    Returns:
        SQL query string or None if no input provided
    """
    if cmd.query:
        return cmd.query
    if cmd.file:
        return cmd.file.read_text()
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    return None


def format_table(rows: list[dict], columns: list[str], console: Console) -> None:
    """Format query results as Rich table with styling.

    Args:
        rows: List of row dictionaries
        columns: Column names in order
        console: Rich console for output
    """
    from rich.box import ROUNDED

    table = Table(
        box=ROUNDED,
        show_header=True,
        header_style="bold cyan",
        row_styles=["", "dim"],
        expand=False,
        caption=f"[dim]{len(rows)} row(s)[/dim]",
    )
    for col in columns:
        table.add_column(col, overflow="fold")
    for row in rows:
        table.add_row(*[str(row.get(c, "")) for c in columns])
    console.print(table)


def format_json_output(rows: list[dict], _console: Console) -> None:
    """Format query results as JSON output.

    Args:
        rows: List of row dictionaries
        _console: Unused; retained for API consistency with format_table_output
    """
    import json as json_module

    def serialize_value(obj):
        """Custom serializer for database values.

        Handles bytes objects (bytea fields) by decoding them as UTF-8 strings.
        This ensures proper JSON escaping of special characters including newlines.
        """
        if isinstance(obj, bytes):
            return obj.decode("utf-8", errors="replace")
        return str(obj)

    json_str = json_module.dumps(rows, indent=2, default=serialize_value)
    builtin_print(json_str)


def format_csv_output(rows: list[dict], columns: list[str]) -> None:
    """Format query results as CSV to stdout.

    Args:
        rows: List of row dictionaries
        columns: Column names in order
    """
    import csv
    import io

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=columns)
    writer.writeheader()
    writer.writerows(rows)
    builtin_print(output.getvalue(), end="")


def handle_db_sql(config_dir: Path, cmd: DbSql) -> None:
    """Handle the db sql command.

    Args:
        config_dir: Configuration directory
        cmd: DbSql command instance
    """
    import asyncio

    console = Console(stderr=True)

    if cmd.json and cmd.csv:
        console.print("[red]Error:[/red] --json and --csv are mutually exclusive")
        sys.exit(1)

    sql = resolve_sql_input(cmd)
    if not sql:
        console.print("[red]Error:[/red] No SQL query provided")
        console.print('Usage: ccproxy db sql "SELECT ..." or --file query.sql or pipe via stdin')
        sys.exit(1)

    database_url = get_database_url(config_dir)
    if not database_url:
        console.print("[red]Error:[/red] No database_url configured")
        console.print("Set in ccproxy.yaml under ccproxy.mitm.database_url")
        console.print("Or set CCPROXY_DATABASE_URL or DATABASE_URL environment variable")
        sys.exit(1)

    try:
        rows, columns = asyncio.run(execute_sql(database_url, sql))
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    if not rows:
        if not cmd.json and not cmd.csv:
            console.print("[dim]No results[/dim]")
        elif cmd.json:
            builtin_print("[]")
        return

    out = Console()
    if cmd.json:
        format_json_output(rows, out)
    elif cmd.csv:
        format_csv_output(rows, columns)
    else:
        format_table(rows, columns, out)


# === Database Prompt Command Handlers ===


async def fetch_trace(database_url: str, trace_id: str) -> dict | None:
    """Fetch a single trace by ID.

    Args:
        database_url: PostgreSQL connection string
        trace_id: UUID of the trace

    Returns:
        Trace record as dict or None if not found
    """
    import asyncpg

    conn = await asyncpg.connect(database_url)
    try:
        result = await conn.fetchrow(
            'SELECT * FROM "CCProxy_HttpTraces" WHERE trace_id = $1',
            trace_id,
        )
        return dict(result) if result else None
    finally:
        await conn.close()


def parse_anthropic_request(body: bytes | None) -> dict:
    """Parse Anthropic Messages API request body.

    Args:
        body: Raw request body bytes

    Returns:
        Parsed request with: model, system, messages, settings
    """
    if not body:
        return {"error": "Empty request body"}

    try:
        data = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        return {"error": f"Failed to parse JSON: {e}"}

    return {
        "model": data.get("model", "unknown"),
        "system": data.get("system"),
        "messages": data.get("messages", []),
        "max_tokens": data.get("max_tokens"),
        "temperature": data.get("temperature"),
        "thinking": data.get("thinking"),
        "tools": data.get("tools"),
        "metadata": data.get("metadata"),
        "stream": data.get("stream", False),
    }


def parse_streaming_response(text: str) -> dict:
    """Parse SSE streaming response into consolidated content.

    Args:
        text: Raw SSE text with "event: X\\ndata: {...}" lines

    Returns:
        Consolidated response content
    """
    content_blocks: list[dict] = []
    usage: dict | None = None
    stop_reason: str | None = None
    model: str | None = None

    for line in text.split("\n"):
        if not line.startswith("data: "):
            continue

        try:
            event = json.loads(line[6:])
        except json.JSONDecodeError:
            continue

        event_type = event.get("type")

        if event_type == "message_start":
            msg = event.get("message", {})
            model = msg.get("model")
            usage = msg.get("usage")
        elif event_type == "content_block_start":
            block = event.get("content_block", {})
            content_blocks.append(block)
        elif event_type == "content_block_delta":
            delta = event.get("delta", {})
            idx = event.get("index", 0)
            if idx < len(content_blocks):
                if delta.get("type") == "text_delta":
                    content_blocks[idx]["text"] = content_blocks[idx].get("text", "") + delta.get("text", "")
                elif delta.get("type") == "thinking_delta":
                    content_blocks[idx]["thinking"] = content_blocks[idx].get("thinking", "") + delta.get(
                        "thinking", ""
                    )
        elif event_type == "message_delta":
            delta = event.get("delta", {})
            stop_reason = delta.get("stop_reason")
            if event.get("usage"):
                usage = {**(usage or {}), **event["usage"]}

    return {
        "content": content_blocks,
        "stop_reason": stop_reason,
        "usage": usage,
        "model": model,
        "streaming": True,
    }


def parse_anthropic_response(body: bytes | None, content_type: str | None) -> dict:
    """Parse Anthropic Messages API response body.

    Handles both streaming (text/event-stream) and non-streaming responses.

    Args:
        body: Raw response body bytes
        content_type: Response content-type header

    Returns:
        Parsed response with: content, usage, stop_reason
    """
    if not body:
        return {"error": "Empty response body"}

    is_streaming = content_type and "event-stream" in content_type

    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as e:
        return {"error": f"Failed to decode response: {e}"}

    if is_streaming:
        return parse_streaming_response(text)

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return {"error": f"Failed to parse JSON: {e}"}

    return {
        "content": data.get("content", []),
        "stop_reason": data.get("stop_reason"),
        "usage": data.get("usage"),
        "model": data.get("model"),
    }


def format_content_block(block: dict) -> list[str]:
    """Format a single content block.

    Args:
        block: Content block dict with type field

    Returns:
        List of markdown lines
    """
    lines: list[str] = []
    block_type = block.get("type", "unknown")

    if block_type == "text":
        text = block.get("text", "")
        lines.append(text)

    elif block_type == "thinking":
        thinking = block.get("thinking", "")
        lines.append("<details>")
        lines.append("<summary>Thinking</summary>")
        lines.append("")
        lines.append(thinking)
        lines.append("")
        lines.append("</details>")

    elif block_type == "tool_use":
        name = block.get("name", "unknown")
        tool_id = block.get("id", "")
        tool_input = block.get("input", {})
        lines.append(f"**Tool Use: {name}** (id: `{tool_id}`)")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(tool_input, indent=2))
        lines.append("```")

    elif block_type == "tool_result":
        tool_id = block.get("tool_use_id", "")
        content = block.get("content")
        is_error = block.get("is_error", False)

        error_marker = " [ERROR]" if is_error else ""
        lines.append(f"**Tool Result{error_marker}** (id: `{tool_id}`)")
        lines.append("")

        if isinstance(content, str):
            lines.append("```")
            truncated = content[:2000] + ("..." if len(content) > 2000 else "")
            lines.append(truncated)
            lines.append("```")
        elif isinstance(content, list):
            for sub_block in content:
                lines.extend(format_content_block(sub_block))

    elif block_type == "image":
        source = block.get("source", {})
        media_type = source.get("media_type", "image/*")
        lines.append(f"*[Image: {media_type}]*")

    else:
        lines.append(f"*[{block_type}]*")
        lines.append("```json")
        lines.append(json.dumps(block, indent=2)[:500])
        lines.append("```")

    return lines


def format_trace_markdown(
    trace: dict,
    request: dict,
    response: dict,
    include_headers: bool = False,
) -> str:
    """Format trace data as markdown document.

    Args:
        trace: Raw trace record from database
        request: Parsed request data
        response: Parsed response data
        include_headers: Whether to include HTTP headers

    Returns:
        Formatted markdown string
    """
    lines: list[str] = []

    # Title and metadata table
    lines.append(f"# MITM Trace: {trace['trace_id']}")
    lines.append("")

    # Metadata table
    lines.append("## Metadata")
    lines.append("")
    lines.append("| Field | Value |")
    lines.append("|-------|-------|")
    lines.append(f"| Trace ID | `{trace['trace_id']}` |")
    direction_label = "Forward (LiteLLM→Provider)" if trace.get("proxy_direction") == 1 else "Reverse (Client→LiteLLM)"
    lines.append(f"| Direction | {direction_label} |")
    lines.append(f"| Session ID | `{trace.get('session_id') or 'N/A'}` |")
    lines.append(f"| Model | `{request.get('model', 'unknown')}` |")
    lines.append(f"| URL | `{trace.get('url', 'N/A')}` |")
    lines.append(f"| Status | {trace.get('status_code', 'N/A')} |")

    duration = trace.get("duration_ms")
    if duration is not None:
        lines.append(f"| Duration | {duration:.2f}ms |")
    else:
        lines.append("| Duration | N/A |")

    lines.append(f"| Start Time | {trace.get('start_time', 'N/A')} |")

    # Request settings
    if request.get("max_tokens") or request.get("temperature") is not None or request.get("thinking"):
        lines.append("")
        lines.append("### Request Settings")
        lines.append("")
        if request.get("max_tokens"):
            lines.append(f"- **max_tokens:** {request['max_tokens']}")
        if request.get("temperature") is not None:
            lines.append(f"- **temperature:** {request['temperature']}")
        if request.get("thinking"):
            budget = request["thinking"].get("budget_tokens", "N/A")
            lines.append(f"- **thinking:** enabled (budget: {budget})")
        if request.get("stream"):
            lines.append("- **streaming:** enabled")

    # Usage stats from response
    if response.get("usage"):
        lines.append("")
        lines.append("### Token Usage")
        lines.append("")
        usage = response["usage"]
        lines.append(f"- **Input tokens:** {usage.get('input_tokens', 'N/A')}")
        lines.append(f"- **Output tokens:** {usage.get('output_tokens', 'N/A')}")
        if usage.get("cache_read_input_tokens"):
            lines.append(f"- **Cache read:** {usage['cache_read_input_tokens']}")
        if usage.get("cache_creation_input_tokens"):
            lines.append(f"- **Cache creation:** {usage['cache_creation_input_tokens']}")

    # HTTP Headers (optional)
    if include_headers:
        lines.append("")
        lines.append("## HTTP Headers")
        lines.append("")
        lines.append("### Request Headers")
        lines.append("```")
        for k, v in (trace.get("request_headers") or {}).items():
            if k.lower() in ("authorization", "x-api-key"):
                v = v[:20] + "..." if len(str(v)) > 20 else "[REDACTED]"
            lines.append(f"{k}: {v}")
        lines.append("```")

        lines.append("")
        lines.append("### Response Headers")
        lines.append("```")
        for k, v in (trace.get("response_headers") or {}).items():
            lines.append(f"{k}: {v}")
        lines.append("```")

    # System message
    lines.append("")
    lines.append("## System Message")
    lines.append("")
    system = request.get("system")
    if system:
        if isinstance(system, str):
            lines.append(system)
        elif isinstance(system, list):
            for block in system:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        lines.append(block.get("text", ""))
                    if block.get("cache_control"):
                        lines.append(f"*[cache_control: {block['cache_control']}]*")
    else:
        lines.append("*No system message*")

    # Tools (if any)
    if request.get("tools"):
        lines.append("")
        lines.append("## Tools")
        lines.append("")
        lines.append(f"*{len(request['tools'])} tools defined*")
        lines.append("")
        for tool in request["tools"]:
            name = tool.get("name", "unknown")
            desc = tool.get("description", "")[:100]
            lines.append(f"- **{name}**: {desc}...")

    # Conversation
    lines.append("")
    lines.append("## Conversation")
    lines.append("")

    for msg in request.get("messages", []):
        role = msg.get("role", "unknown")
        content = msg.get("content")

        lines.append(f"### {role.title()}")
        lines.append("")

        if isinstance(content, str):
            lines.append(content)
        elif isinstance(content, list):
            for block in content:
                lines.extend(format_content_block(block))

        lines.append("")

    # Assistant response
    if response.get("content"):
        lines.append("### Assistant (Response)")
        lines.append("")
        for block in response["content"]:
            lines.extend(format_content_block(block))
        lines.append("")

        if response.get("stop_reason"):
            lines.append(f"*Stop reason: {response['stop_reason']}*")

    # Errors
    if response.get("error"):
        lines.append("")
        lines.append("## Error")
        lines.append("")
        lines.append(f"**{response['error']}**")

    return "\n".join(lines)


def handle_db_prompt(config_dir: Path, cmd: DbPrompt) -> None:
    """Handle the db prompt command.

    Args:
        config_dir: Configuration directory
        cmd: DbPrompt command instance
    """
    import asyncio
    from datetime import datetime

    console = Console(stderr=True)

    # Validate direction
    valid_directions = {"forward", "reverse", "both"}
    if cmd.direction not in valid_directions:
        console.print(f"[red]Error:[/red] Invalid direction '{cmd.direction}'. Use: {', '.join(valid_directions)}")
        sys.exit(1)

    # Get database URL
    database_url = get_database_url(config_dir)
    if not database_url:
        console.print("[red]Error:[/red] No database_url configured")
        console.print("Set in ccproxy.yaml under ccproxy.mitm.database_url")
        console.print("Or set CCPROXY_DATABASE_URL or DATABASE_URL environment variable")
        sys.exit(1)

    # Fetch trace
    try:
        trace = asyncio.run(fetch_trace(database_url, cmd.trace_id))
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    if not trace:
        console.print(f"[red]Error:[/red] Trace not found: {cmd.trace_id}")
        sys.exit(1)

    # Filter by direction
    trace_direction = "forward" if trace.get("proxy_direction") == 1 else "reverse"
    if cmd.direction != "both" and trace_direction != cmd.direction:
        console.print(
            f"[yellow]Warning:[/yellow] Trace direction is '{trace_direction}' but filter is '{cmd.direction}'"
        )

    # Parse request and response
    request = parse_anthropic_request(trace.get("request_body"))
    response = parse_anthropic_response(
        trace.get("response_body"),
        trace.get("response_content_type"),
    )

    # Format output
    if cmd.raw:
        # Convert non-serializable types for JSON output
        trace_serializable = {}
        for k, v in trace.items():
            if isinstance(v, bytes):
                trace_serializable[k] = v.decode("utf-8", errors="replace")
            elif isinstance(v, datetime):
                trace_serializable[k] = v.isoformat()
            else:
                trace_serializable[k] = v

        output = json.dumps(
            {
                "trace": trace_serializable,
                "parsed_request": request,
                "parsed_response": response,
            },
            indent=2,
            default=str,
        )
    else:
        output = format_trace_markdown(trace, request, response, cmd.include_headers)

    # Write output
    if cmd.output:
        cmd.output.write_text(output)
        console.print(f"[green]Written to:[/green] {cmd.output}")
    else:
        builtin_print(output)


def main(
    cmd: Annotated[Command, tyro.conf.arg(name="")],
    *,
    config_dir: Annotated[Path | None, tyro.conf.arg(help="Configuration directory", metavar="PATH")] = None,
) -> None:
    """ccproxy - Intercept and route Claude Code requests to LLM providers.

    Intelligent request routing via LiteLLM proxy based on token count,
    model type, tool usage, or custom rules.
    """
    if config_dir is None:
        env_config_dir = os.environ.get("CCPROXY_CONFIG_DIR")
        if env_config_dir:
            config_dir = Path(env_config_dir)
        else:
            config_dir = Path.home() / ".ccproxy"

    # Setup logging with 100-character text width
    setup_logging()

    # Handle each command type
    if isinstance(cmd, Start):
        start_litellm(config_dir, args=cmd.args, detach=cmd.detach, mitm=cmd.mitm)

    elif isinstance(cmd, Install):
        install_config(config_dir, force=cmd.force)

    elif isinstance(cmd, Run):
        # Tyro's greedy Positional consumes --help/-h before tyro can intercept
        if not cmd.command or cmd.command in (["-h"], ["--help"]):
            print("usage: ccproxy run [--shadow] [--shadow-port PORT] -- <command> [args...]")
            print()
            print("Run a command with ccproxy environment.")
            print()
            print("options:")
            print("  --shadow, -s        Route all subprocess HTTP/HTTPS through MITM shadow")
            print("                      proxy for capture. API calls still flow through the")
            print("                      primary proxy via ANTHROPIC_BASE_URL.")
            print("  --shadow-port PORT  Port for the shadow forward proxy (default: 8082)")
            print("  command ...         Command and arguments to execute with proxy settings")
            sys.exit(0 if cmd.command else 1)
        run_with_proxy(config_dir, cmd.command, shadow=cmd.shadow, shadow_port=cmd.shadow_port)

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
        view_logs(config_dir, source=cmd.source, follow=cmd.follow, lines=cmd.lines)

    elif isinstance(cmd, Status):
        show_status(
            config_dir,
            json_output=cmd.json,
            check_proxy=cmd.proxy,
            check_reverse=cmd.reverse,
            check_forward=cmd.forward,
        )

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

    elif isinstance(cmd, DbSql):
        handle_db_sql(config_dir, cmd)

    elif isinstance(cmd, DbPrompt):
        handle_db_prompt(config_dir, cmd)

    elif isinstance(cmd, DagViz):
        handle_dag_viz(cmd)


def handle_dag_viz(cmd: DagViz) -> None:
    """Handle dag-viz subcommand to visualize the pipeline DAG."""
    from ccproxy.pipeline import PipelineExecutor
    from ccproxy.pipeline.hook import get_registry

    # Import all hooks to register them
    from ccproxy.hooks import (  # noqa: F401  # pyright: ignore[reportUnusedImport]
        add_beta_headers,
        capture_headers,
        extract_session_id,
        forward_oauth,
        inject_claude_code_identity,
        model_router,
        rule_evaluator,
    )

    # Get registered hooks
    registry = get_registry()
    all_specs = registry.get_all_specs()

    if not all_specs:
        print("[red]No hooks registered in pipeline[/red]")
        sys.exit(1)

    hook_specs = list(all_specs.values())

    # Create executor (this builds the DAG)
    try:
        executor = PipelineExecutor(hooks=hook_specs)
    except Exception as e:
        print(f"[red]Error building DAG: {e}[/red]")
        sys.exit(1)

    # Validate if requested
    if cmd.validate:
        warnings = executor.dag.validate()
        if warnings:
            print("[yellow]DAG Validation Warnings:[/yellow]")
            for w in warnings:
                print(f"  • {w}")
        else:
            print("[green]DAG validation passed - no issues found[/green]")
        print()

    # Output based on format
    if cmd.output == "mermaid":
        print(executor.to_mermaid())
    elif cmd.output == "json":
        import json as json_mod

        dag_data = {
            "execution_order": executor.get_execution_order(),
            "parallel_groups": [list(g) for g in executor.get_parallel_groups()],
            "hooks": {
                name: {
                    "reads": list(spec.reads),
                    "writes": list(spec.writes),
                    "dependencies": list(executor.dag.get_dependencies(name)),
                }
                for name, spec in all_specs.items()
            },
        }
        print(json_mod.dumps(dag_data, indent=2))
    else:
        # Default: ASCII
        console = Console()

        # Title
        console.print(Panel("[bold cyan]Pipeline Hook DAG[/bold cyan]", expand=False))

        # Execution order
        order = executor.get_execution_order()
        console.print("\n[bold]Execution Order:[/bold]")
        console.print(f"  {' → '.join(order)}")

        # Parallel groups
        groups = executor.get_parallel_groups()
        if any(len(g) > 1 for g in groups):
            console.print("\n[bold]Parallel Execution Groups:[/bold]")
            for i, group in enumerate(groups):
                if len(group) > 1:
                    console.print(f"  Group {i + 1}: {', '.join(sorted(group))} [dim](can run in parallel)[/dim]")
                else:
                    console.print(f"  Group {i + 1}: {list(group)[0]}")

        # Hook details table
        console.print("\n[bold]Hook Dependencies:[/bold]")
        table = Table(show_header=True, header_style="bold")
        table.add_column("Hook", style="cyan")
        table.add_column("Reads", style="green")
        table.add_column("Writes", style="yellow")
        table.add_column("Depends On", style="magenta")

        for name in order:
            spec = all_specs[name]
            deps = executor.dag.get_dependencies(name)
            table.add_row(
                name,
                ", ".join(sorted(spec.reads)) or "-",
                ", ".join(sorted(spec.writes)) or "-",
                ", ".join(sorted(deps)) or "-",
            )

        console.print(table)

        # ASCII diagram
        console.print("\n[bold]DAG Visualization:[/bold]")
        console.print(executor.to_ascii())


def entry_point() -> None:
    """Entry point for the ccproxy command."""
    # Handle 'run' and 'statusline' subcommands specially
    # - 'run': avoid tyro parsing command arguments (ccproxy run claude -p foo)
    # - 'statusline' (no subcommand): route to StatuslineOutput
    # - 'statusline <subcommand>': rewrite to statusline-<subcommand> for tyro
    args = sys.argv[1:]

    # Check for 'statusline' and 'db' with subcommands
    subcommands = {
        "start",
        "stop",
        "restart",
        "install",
        "logs",
        "status",
        "run",
        "statusline",
        "db",
    }
    statusline_subcommands = {"install", "uninstall", "status"}
    db_subcommands = {"sql", "prompt"}

    statusline_idx = None
    run_idx = None

    for i, arg in enumerate(args):
        if arg == "db":
            # Check if next arg is a db subcommand
            if i + 1 < len(args) and args[i + 1] in db_subcommands:
                # Rewrite "db sql" -> "db-sql"
                subcommand = args[i + 1]
                new_args = args[:i] + [f"db-{subcommand}"] + args[i + 2 :]
                sys.argv = [sys.argv[0]] + new_args
            break
        elif arg == "statusline":
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
