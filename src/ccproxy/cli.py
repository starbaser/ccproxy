"""ccproxy CLI for managing the LiteLLM proxy server - Tyro implementation."""

from __future__ import annotations

import contextlib
import json
import logging
import logging.config
import os
import shutil
import signal
import subprocess
import sys
from builtins import print as builtin_print
from pathlib import Path
from typing import Annotated, Any

import attrs
import tyro
import yaml
from rich import print
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

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
                data: dict[str, Any] = yaml.safe_load(f) or {}
            general: dict[str, Any] = data.get("general_settings", {})
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
            litellm: dict[str, Any] = data.get("litellm", {})
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

    inspect: Annotated[bool, tyro.conf.arg(aliases=["-i"])] = False
    """Start mitmproxy for traffic capture with browser-based flow inspection."""


@attrs.define
class Install:
    """Install ccproxy configuration files."""

    force: bool = False
    """Overwrite existing configuration."""


@attrs.define
class Run:
    """Run a command with ccproxy environment.

    Usage: ccproxy run [--inspect] -- <command> [args...]"""

    command: Annotated[list[str], tyro.conf.Positional] = attrs.Factory(list)
    """Command and arguments to execute with proxy settings."""


@attrs.define
class Logs:
    """View ccproxy logs from journal or process-compose."""

    follow: Annotated[bool, tyro.conf.arg(aliases=["-f"])] = False
    """Follow log output (like tail -f)."""

    lines: Annotated[int, tyro.conf.arg(aliases=["-n"])] = 100
    """Number of lines to show (default: 100)."""


@attrs.define
class Status:
    """Show the status of LiteLLM proxy and ccproxy configuration.

    When service flags (--proxy, --inspect) are specified,
    runs in health check mode with bitmask exit codes:

      0 = all healthy
      1 = proxy down
      2 = inspect down
      3 = both down

    Examples:
        ccproxy status --proxy --inspect  # All must be running
        ccproxy status --proxy            # Just check LiteLLM
    """

    json: bool = False
    """Output status as JSON with boolean values."""

    proxy: bool = False
    """Check if LiteLLM proxy is running."""

    inspect: bool = False
    """Check if inspector stack (mitmweb) is running."""


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
    | Annotated[Logs, tyro.conf.subcommand(name="logs")]
    | Annotated[Status, tyro.conf.subcommand(name="status")]
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


def _ensure_combined_ca_bundle(
    config_dir: Path, base_ssl_cert: str | None = None, confdir: Path | None = None
) -> Path | None:
    """Build a combined CA bundle with mitmproxy's CA + system CAs.

    mitmproxy intercepts TLS and re-signs with its own CA. Subprocesses need
    to trust both the mitmproxy CA and real upstream CAs.

    Args:
        config_dir: Configuration directory for storing the bundle
        base_ssl_cert: Base SSL_CERT_FILE path (uses system default if None)
        confdir: mitmproxy confdir override (defaults to ~/.mitmproxy)

    Returns:
        Path to combined bundle, or None if mitmproxy CA not found
    """
    search_dirs: list[Path] = []
    if confdir:
        search_dirs.append(Path(confdir))
    search_dirs.append(Path.home() / ".mitmproxy")

    proxy_ca: Path | None = None
    for d in search_dirs:
        candidate = d / "mitmproxy-ca-cert.pem"
        if candidate.exists():
            proxy_ca = candidate
            break

    if proxy_ca is None:
        return None

    combined_bundle = config_dir / "combined-ca-bundle.pem"
    base_ca = base_ssl_cert or os.environ.get("SSL_CERT_FILE", "/etc/ssl/certs/ca-certificates.crt")
    try:
        proxy_ca_data = proxy_ca.read_text()
        base_ca_data = Path(base_ca).read_text() if Path(base_ca).exists() else ""
        combined_bundle.write_text(proxy_ca_data + "\n" + base_ca_data)
        return combined_bundle
    except OSError:
        return None


def run_with_proxy(
    config_dir: Path,
    command: list[str],
    inspect: bool = False,
) -> None:
    """Run a command with ccproxy environment variables set.

    The main port (default 4000) is always the entry point:
    - Without --inspect: LiteLLM runs on port 4000
    - With --inspect: mitmweb runs on port 4000, forwards to LiteLLM on a random port

    Args:
        config_dir: Configuration directory
        command: Command and arguments to execute
        inspect: Route subprocess traffic through a WireGuard namespace for transparent capture
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

    # Inspect mode: route subprocess traffic through a WireGuard namespace for transparent capture.
    # No base URL env vars — the inspector addon forwards LLM API domain traffic to LiteLLM.
    if inspect:
        from ccproxy.inspector.namespace import (
            check_namespace_capabilities,
            cleanup_namespace,
            create_namespace,
            run_in_namespace,
        )

        problems = check_namespace_capabilities()
        if problems:
            for p in problems:
                print(f"Error: {p}", file=sys.stderr)
            print(
                "\nCannot create network namespace for --inspect mode. "
                "All prerequisites above must be satisfied.",
                file=sys.stderr,
            )
            sys.exit(1)
        wg_conf_file = config_dir / ".inspector-wireguard-client.conf"
        if not wg_conf_file.exists():
            print(
                "Error: No WireGuard configuration found. "
                "Start ccproxy with --inspect first: ccproxy start --inspect",
                file=sys.stderr,
            )
            sys.exit(1)

        wg_client_conf = wg_conf_file.read_text()

        inspector_confdir: Path | None = None
        ccproxy_config_path = config_dir / "ccproxy.yaml"
        if ccproxy_config_path.exists():
            import yaml

            with ccproxy_config_path.open() as f:
                cfg: dict[str, Any] = yaml.safe_load(f) or {}
            inspect_section: dict[str, Any] = cfg.get("ccproxy", {}).get("inspector", {})
            cert_dir = inspect_section.get("cert_dir")
            if cert_dir:
                inspector_confdir = Path(cert_dir).expanduser()

        # Trust mitmproxy's CA so TLS interception works transparently
        combined_bundle = _ensure_combined_ca_bundle(
            config_dir, env.get("SSL_CERT_FILE"), confdir=inspector_confdir
        )
        if combined_bundle:
            bundle = str(combined_bundle)
            env["SSL_CERT_FILE"] = bundle
            env["NODE_EXTRA_CA_CERTS"] = bundle
            env["REQUESTS_CA_BUNDLE"] = bundle
            env["CURL_CA_BUNDLE"] = bundle

        ctx = None
        try:
            ctx = create_namespace(wg_client_conf)
            exit_code = run_in_namespace(ctx, command, env)
            sys.exit(exit_code)
        except RuntimeError as e:
            print(f"Error: Namespace setup failed: {e}", file=sys.stderr)
            sys.exit(1)
        finally:
            if ctx:
                cleanup_namespace(ctx)

    # Non-inspect: point SDKs directly at the proxy
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
        sys.exit(130)


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
                config: dict[str, Any] | None = yaml.safe_load(f)
                if config and "ccproxy" in config and "handler" in config["ccproxy"]:
                    handler_import = config["ccproxy"]["handler"]
        except Exception:
            logger.debug("Could not load ccproxy config for handler import, using default")

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
                        "  2. Restart the proxy: [dim]ccproxy start[/dim]\n\n"
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


async def _run_inspect(
    config_dir: Path,
    litellm_port: int,
    litellm_cmd: list[str],
    env: dict[str, str],
    main_port: int,
) -> int:
    """Run the full inspect lifecycle: mitmweb + namespaces + LiteLLM.

    Embeds mitmweb in-process via WebMaster, creates WireGuard namespaces,
    and runs LiteLLM inside the gateway namespace. Returns LiteLLM's exit code.

    InspectorConfig and OtelConfig are read from the singleton.
    """
    import asyncio

    from ccproxy.config import get_config
    from ccproxy.inspector import get_wg_client_conf, run_inspector
    from ccproxy.inspector.namespace import (
        check_namespace_capabilities,
        cleanup_namespace,
        create_gateway_namespace,
        run_in_namespace_async,
    )

    problems = check_namespace_capabilities()
    if problems:
        for p in problems:
            builtin_print(f"Error: {p}", file=sys.stderr)
        builtin_print(
            "\nCannot create network namespace for --inspect mode. "
            "All prerequisites above must be satisfied.",
            file=sys.stderr,
        )
        sys.exit(1)

    inspector = get_config().inspector

    pid = os.getpid()
    wg_cli_keypair_path = config_dir / f"wireguard-cli.{pid}.conf"
    wg_gateway_keypair_path = config_dir / f"wireguard-gateway.{pid}.conf"

    (config_dir / ".inspector-wireguard-client.conf").unlink(missing_ok=True)

    builtin_print(
        f"Starting inspector: mitmweb reverse@{main_port} "
        f"+ wg-cli (auto-port) + wg-gateway (auto-port), UI@{inspector.port}"
    )

    master, master_task, web_token = await run_inspector(
        litellm_port,
        wg_cli_conf_path=wg_cli_keypair_path,
        wg_gateway_conf_path=wg_gateway_keypair_path,
        reverse_port=main_port,
    )

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, master.shutdown)

    gateway_ctx = None
    exit_code = 1

    try:
        # WG client configs — direct in-process access
        wg_cli_conf = get_wg_client_conf(master, wg_cli_keypair_path)
        if wg_cli_conf:
            (config_dir / ".inspector-wireguard-client.conf").write_text(wg_cli_conf)
        else:
            logger.warning("Failed to retrieve CLI WireGuard client config")

        wg_gateway_conf = get_wg_client_conf(master, wg_gateway_keypair_path)
        if not wg_gateway_conf:
            builtin_print("Error: Failed to retrieve gateway WireGuard config", file=sys.stderr)
            return 1

        # Build combined CA bundle (mitmproxy CA cert exists after servers bind)
        confdir_path = Path(inspector.mitmproxy.confdir) if inspector.mitmproxy.confdir else None
        combined_bundle = _ensure_combined_ca_bundle(
            config_dir,
            env.get("SSL_CERT_FILE"),
            confdir=confdir_path,
        )
        if combined_bundle:
            bundle = str(combined_bundle)
            env["SSL_CERT_FILE"] = bundle
            env["REQUESTS_CA_BUNDLE"] = bundle
            env["CURL_CA_BUNDLE"] = bundle
            env["NODE_EXTRA_CA_CERTS"] = bundle
        else:
            logger.warning(
                "mitmproxy CA certificate not found — "
                "LiteLLM may fail SSL verification inside the gateway namespace"
            )

        # Export WireGuard keys for Wireshark decryption
        wg_keylog_path = config_dir / "wg.keylog"
        keylog_lines: list[str] = []
        for kp_path in (wg_cli_keypair_path, wg_gateway_keypair_path):
            if kp_path.exists():
                try:
                    kp_data = json.loads(kp_path.read_text())
                    for key_field in ("server_key", "client_key"):
                        key_val = kp_data.get(key_field)
                        if key_val:
                            keylog_lines.append(f"LOCAL_STATIC_PRIVATE_KEY = {key_val}")
                except (ValueError, OSError):
                    pass
        if keylog_lines:
            wg_keylog_path.write_text("\n".join(keylog_lines) + "\n")
            builtin_print(f"WireGuard keylog: {wg_keylog_path}")
            builtin_print(f"  Wireshark: -o wg.keylog_file:{wg_keylog_path}")

        web_url = f"http://{inspector.mitmproxy.web_host}:{inspector.port}/?token={web_token}"
        builtin_print(f"Inspector UI: {web_url}")
        try:
            subprocess.Popen(  # noqa: S603
                ["xdg-open", web_url],  # noqa: S607
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            logger.debug("xdg-open not found; open the inspector URL manually")

        # Create gateway namespace and run LiteLLM inside it
        gateway_ctx = create_gateway_namespace(wg_gateway_conf, litellm_port)
        exit_code = await run_in_namespace_async(gateway_ctx, litellm_cmd, env)

    finally:
        master.shutdown()  # type: ignore[no-untyped-call]
        await master_task
        loop.remove_signal_handler(signal.SIGTERM)

        if gateway_ctx is not None:
            cleanup_namespace(gateway_ctx)
        wg_cli_keypair_path.unlink(missing_ok=True)
        wg_gateway_keypair_path.unlink(missing_ok=True)

    return exit_code


def start_litellm(
    config_dir: Path,
    args: list[str] | None = None,
    inspect: bool = False,
) -> None:
    """Start the LiteLLM proxy server with ccproxy configuration.

    Runs in the foreground. Use process-compose or systemd for supervision.

    Args:
        config_dir: Configuration directory containing config files
        args: Additional arguments to pass to litellm command
        inspect: Start mitmproxy with browser-based flow inspection
    """
    from ccproxy.utils import find_available_port

    config_path = config_dir / "config.yaml"
    if not config_path.exists():
        print(f"Error: Configuration not found at {config_path}", file=sys.stderr)
        print("Run 'ccproxy install' first to set up configuration.", file=sys.stderr)
        sys.exit(1)

    litellm_host, main_port = _read_proxy_settings(config_dir)

    ccproxy_config_path = config_dir / "ccproxy.yaml"
    ccproxy_config: dict[str, Any] | None = None
    if ccproxy_config_path.exists():
        with ccproxy_config_path.open() as f:
            ccproxy_config = yaml.safe_load(f)

    from ccproxy.preflight import run_preflight_checks

    ports_to_check = [main_port]
    if inspect:
        from ccproxy.config import get_config

        ports_to_check.append(get_config().inspector.port)
    run_preflight_checks(ports=ports_to_check, config_dir=config_dir)

    try:
        generate_handler_file(config_dir)
    except Exception as e:
        print(f"Error generating handler file: {e}", file=sys.stderr)
        sys.exit(1)

    if inspect:
        litellm_port = find_available_port()
        litellm_port_file = config_dir / ".litellm_port"
        litellm_port_file.write_text(str(litellm_port))
    else:
        litellm_port = main_port
        litellm_port_file = config_dir / ".litellm_port"
        if litellm_port_file.exists():
            litellm_port_file.unlink()

    env = os.environ.copy()
    env["CCPROXY_CONFIG_DIR"] = str(config_dir.absolute())

    if ccproxy_config_path.exists() and ccproxy_config:
        litellm_env = ccproxy_config.get("litellm", {}).get("environment", {})
        for key, value in litellm_env.items():
            expanded = _expand_env_vars(str(value))
            env[key] = expanded
            os.environ[key] = expanded

    if "SSL_CERT_FILE" not in env or not Path(env["SSL_CERT_FILE"]).exists():
        ssl_cert = None
        try:
            import certifi

            ssl_cert = certifi.where()
        except ImportError:
            pass
        if ssl_cert and Path(ssl_cert).exists():
            env["SSL_CERT_FILE"] = ssl_cert
        elif Path("/etc/ssl/certs/ca-certificates.crt").exists():
            env["SSL_CERT_FILE"] = "/etc/ssl/certs/ca-certificates.crt"

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

    litellm_cmd = [
        str(litellm_path),
        "--config",
        str(config_path),
        "--host",
        # In inspect mode, LiteLLM runs inside a gateway namespace where
        # slirp4netns hostfwd delivers traffic to the tap0 IP (10.0.2.100).
        # Bind to 0.0.0.0 so LiteLLM accepts on all namespace interfaces.
        "0.0.0.0" if inspect else litellm_host,
        "--port",
        str(litellm_port),
    ]

    if args:
        litellm_cmd.extend(args)

    if inspect:
        import asyncio

        exit_code = asyncio.run(_run_inspect(
            config_dir=config_dir,
            litellm_port=litellm_port,
            litellm_cmd=litellm_cmd,
            env=env,
            main_port=main_port,
        ))
        sys.exit(exit_code)

    try:
        # S603: Command construction is safe - we control the litellm path
        result = subprocess.run(litellm_cmd, env=env)  # noqa: S603
        sys.exit(result.returncode)
    except FileNotFoundError:
        print("Error: litellm command not found.", file=sys.stderr)
        print(
            "Please ensure LiteLLM is installed: pip install litellm",
            file=sys.stderr,
        )
        sys.exit(1)
    except KeyboardInterrupt:
        pass


def view_logs(follow: bool = False, lines: int = 100) -> None:
    """View ccproxy logs from journal or process-compose."""
    if shutil.which("systemctl"):
        result = subprocess.run(
            ["systemctl", "--user", "is-active", "ccproxy.service"],  # noqa: S607
            capture_output=True,
            text=True,
        )
        if result.stdout.strip() in ("active", "activating"):
            jctl_cmd: list[str] = [
                "journalctl",
                "--user",
                "-u",
                "ccproxy.service",
                "-n",
                str(lines),
            ]
            if follow:
                jctl_cmd.append("-f")
            try:
                proc = subprocess.run(jctl_cmd)  # noqa: S603
                sys.exit(proc.returncode)
            except KeyboardInterrupt:
                sys.exit(0)

    pc_socket = Path("/tmp/process-compose-ccproxy.sock")  # noqa: S108
    if pc_socket.exists() and shutil.which("process-compose"):
        pc_cmd: list[str] = [
            "process-compose",
            "--unix-socket",
            str(pc_socket),
            "process",
            "logs",
            "ccproxy",
        ]
        if follow:
            pc_cmd.append("-f")
        try:
            proc = subprocess.run(pc_cmd)  # noqa: S603
            sys.exit(proc.returncode)
        except KeyboardInterrupt:
            sys.exit(0)

    print(
        "No active ccproxy service found.\n"
        "Run 'systemctl --user status ccproxy.service' or "
        "'process-compose attach' to inspect.",
        file=sys.stderr,
    )
    sys.exit(1)


def show_status(
    config_dir: Path,
    json_output: bool = False,
    check_proxy: bool = False,
    check_inspect: bool = False,
) -> None:
    """Show the status of LiteLLM proxy and ccproxy configuration.

    Args:
        config_dir: Configuration directory to check
        json_output: Output status as JSON with boolean values
        check_proxy: Health check - require LiteLLM proxy running
        check_inspect: Health check - require inspector stack running

    When any check_* flag is True, exits 0 only if ALL specified services
    are healthy, otherwise exits 1. No output is produced in check mode.
    """
    import socket

    def _check_alive(check_host: str, check_port: int, timeout: float = 0.5) -> bool:
        try:
            with socket.create_connection((check_host, check_port), timeout=timeout):
                return True
        except OSError:
            return False

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
                config_data: dict[str, Any] = yaml.safe_load(f)
            if config_data:
                litellm_settings: dict[str, Any] = config_data.get("litellm_settings", {})
                callbacks = litellm_settings.get("callbacks", [])
                model_list = config_data.get("model_list", [])
        except (yaml.YAMLError, OSError):
            pass

    # Extract hooks and inspect config from ccproxy.yaml
    hooks: list[Any] = []
    inspect_config: dict[str, Any] = {}
    if ccproxy_config.exists():
        try:
            with ccproxy_config.open() as f:
                ccproxy_data: dict[str, Any] = yaml.safe_load(f)
            if ccproxy_data:
                ccproxy_section: dict[str, Any] = ccproxy_data.get("ccproxy", {})
                hooks = ccproxy_section.get("hooks", [])
                inspect_config = ccproxy_section.get("inspector", {})
        except (yaml.YAMLError, OSError):
            pass

    host, main_port = _read_proxy_settings(config_dir)
    proxy_url = f"http://{host}:{main_port}"

    # Detect running state via TCP probes
    proxy_running = _check_alive(host, main_port)
    inspect_port = inspect_config.get("port", 8083)
    combined_running = _check_alive("127.0.0.1", inspect_port)
    litellm_actual_port = main_port

    litellm_port_file = config_dir / ".litellm_port"
    if litellm_port_file.exists():
        with contextlib.suppress(ValueError, OSError):
            litellm_actual_port = int(litellm_port_file.read_text().strip())

    status_data: dict[str, Any] = {
        "proxy": proxy_running,
        "url": proxy_url,
        "config": config_paths,
        "callbacks": callbacks,
        "hooks": hooks,
        "model_list": model_list,
        "log": None,
        "inspector": {
            "running": combined_running,
            "entry_port": main_port,
            "inspect_port": inspect_port,
            "inspect_url": f"http://127.0.0.1:{inspect_port}" if combined_running else None,
            "litellm_port": litellm_actual_port,
        },
    }

    # Health check mode: exit with bitmask code indicating failed services
    # Bit 0 (1): proxy, Bit 1 (2): inspect stack
    if check_proxy or check_inspect:
        exit_code = 0
        if check_proxy and not proxy_running:
            exit_code |= 1
        if check_inspect and not combined_running:
            exit_code |= 2
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

        # Inspector status — inspect stack
        inspector_info = status_data["inspector"]
        litellm_port = inspector_info["litellm_port"]

        inspector_parts = []

        if inspector_info["running"]:
            entry_port = inspector_info["entry_port"]
            inspect_status = f"[green]inspect[/green]@[cyan]{entry_port}[/cyan] → litellm@[cyan]{litellm_port}[/cyan]"
            if inspector_info.get("inspect_url"):
                inspect_status += f"\n[green]ui[/green] → [cyan]{inspector_info['inspect_url']}[/cyan]"
            inspector_parts.append(inspect_status)
        else:
            inspector_parts.append("[dim]stopped[/dim]")

        inspector_display = "\n".join(inspector_parts)
        table.add_row("inspector", inspector_display)

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
                    params_display = ", ".join(f"{k}={v}" for k, v in params.items()) if params else "[dim]none[/dim]"

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
                model_entry: dict[str, Any] = model if isinstance(model, dict) else {}
                model_name: str = model_entry.get("model_name", "")
                litellm_params: dict[str, Any] = model_entry.get("litellm_params", {})
                provider_model: str = litellm_params.get("model", "")
                api_base: str | None = litellm_params.get("api_base")

                # Resolve API base from target model if this is an alias
                if not api_base and provider_model in model_lookup:
                    target: dict[str, Any] = model_lookup[provider_model]
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
        config_dir = Path(env_config_dir) if env_config_dir else Path.home() / ".ccproxy"

    # Setup logging with 100-character text width
    setup_logging()

    # Handle each command type
    if isinstance(cmd, Start):
        start_litellm(config_dir, args=cmd.args, inspect=cmd.inspect)

    elif isinstance(cmd, Install):
        install_config(config_dir, force=cmd.force)

    elif isinstance(cmd, Run):
        # Tyro's greedy Positional consumes all args including flags.
        # Extract --inspect/-i and --help/-h manually from the command list.
        args = list(cmd.command)
        if not args or args == ["-h"] or args == ["--help"]:
            print("usage: ccproxy run [--inspect] -- <command> [args...]")
            print()
            print("Run a command with ccproxy environment.")
            print()
            print("options:")
            print("  --inspect, -i       Route subprocess traffic through a WireGuard namespace")
            print("                      for transparent capture of all TCP/UDP traffic.")
            print("                      Requires ccproxy start --inspect to be running.")
            print("  command ...         Command and arguments to execute with proxy settings")
            sys.exit(0)

        # Extract --inspect / -i from args
        inspect = False
        filtered: list[str] = []
        i = 0
        while i < len(args):
            if args[i] in ("--inspect", "-i"):
                inspect = True
                i += 1
            elif args[i] == "--":
                filtered.extend(args[i + 1 :])
                break
            else:
                filtered.append(args[i])
                i += 1

        if not filtered:
            print("Error: No command specified to run", file=sys.stderr)
            sys.exit(1)
        run_with_proxy(config_dir, filtered, inspect=inspect)

    elif isinstance(cmd, Logs):
        view_logs(follow=cmd.follow, lines=cmd.lines)

    elif isinstance(cmd, Status):
        show_status(
            config_dir,
            json_output=cmd.json,
            check_proxy=cmd.proxy,
            check_inspect=cmd.inspect,
        )

    elif isinstance(cmd, DagViz):
        handle_dag_viz(cmd)


def handle_dag_viz(cmd: DagViz) -> None:
    """Handle dag-viz subcommand to visualize the pipeline DAG."""
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
    from ccproxy.pipeline import PipelineExecutor
    from ccproxy.pipeline.hook import get_registry

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
                    console.print(f"  Group {i + 1}: {next(iter(group))}")

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
    # Handle 'run' subcommand specially to avoid tyro parsing command arguments
    # (e.g., ccproxy run claude -p foo)
    args = sys.argv[1:]

    subcommands = {
        "start",
        "install",
        "logs",
        "status",
        "run",
    }

    run_idx = None

    for i, arg in enumerate(args):
        if arg == "run":
            run_idx = i
            break
        # Stop if we hit a different subcommand
        if arg in subcommands:
            break

    # Handle 'run' subcommand
    if run_idx is not None:
        # Extract command after 'run'
        command_args = args[run_idx + 1 :]

        # Only insert '--' if not already present (backwards compatibility)
        if command_args and command_args[0] != "--":
            # Rebuild argv: keep everything up to and including 'run', then '--' to escape the rest
            sys.argv = [sys.argv[0], *args[: run_idx + 1], "--", *command_args]

    tyro.cli(main)


if __name__ == "__main__":
    entry_point()
