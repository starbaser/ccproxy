"""ccproxy CLI."""

from __future__ import annotations

import json
import logging
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
from rich import print
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ccproxy.tools.flows import Flows, handle_flows
from ccproxy.utils import get_templates_dir

logger = logging.getLogger(__name__)


# Subcommand definitions using attrs
@attrs.define
class Start:
    """Start the ccproxy inspector server."""

    args: Annotated[list[str] | None, tyro.conf.Positional] = None
    """Additional arguments (reserved for future use)."""


@attrs.define
class Install:
    """Install ccproxy configuration files."""

    force: bool = False
    """Overwrite existing configuration."""


@attrs.define
class Run:
    """Run a command with ccproxy environment.

    Usage: ccproxy run [--inspect] -- <command> [args...]"""

    command: Annotated[list[str], tyro.conf.Positional] = attrs.Factory(list)  # pyright: ignore[reportUnknownVariableType]
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
    """Show ccproxy status.

    When service flags (--proxy, --inspect) are specified,
    runs in health check mode with bitmask exit codes:

      0 = all healthy
      1 = proxy down
      2 = inspect down
      3 = both down

    Examples:
        ccproxy status --proxy --inspect  # All must be running
        ccproxy status --proxy            # Just check proxy
    """

    json: bool = False
    """Output status as JSON with boolean values."""

    proxy: bool = False
    """Check if proxy is running."""

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
    | Annotated[Flows, tyro.conf.subcommand(name="flows")]
)


def setup_logging(config_dir: Path, debug: bool = False, *, log_file: bool = False) -> Path | None:
    """Configure unified logging with tagged namespaces and optional file output.

    In systemd mode (INVOCATION_ID set), logs to stderr only (journal captures).
    When log_file=True and not systemd, also logs to {config_dir}/ccproxy.log
    (truncated on restart).

    Returns the log file path if created, None otherwise.
    """
    root = logging.getLogger()
    root.handlers.clear()

    level = logging.DEBUG if debug else logging.INFO
    root.setLevel(level)

    fmt = logging.Formatter(
        "%(asctime)s %(name)-30s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    root.addHandler(stream)

    log_path: Path | None = None
    if log_file and not os.environ.get("INVOCATION_ID"):
        log_path = config_dir / "ccproxy.log"
        fh = logging.FileHandler(str(log_path), mode="w", encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)

    logging.getLogger("LiteLLM").setLevel(logging.WARNING)  # suppress litellm import noise
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    return log_path


def install_config(config_dir: Path, force: bool = False) -> None:
    """Install ccproxy template configuration files.

    Args:
        config_dir: Directory to install configuration files to
        force: Whether to overwrite existing configuration files
    """
    config_dir.mkdir(parents=True, exist_ok=True)

    try:
        templates_dir = get_templates_dir()
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    template_files = [
        "ccproxy.yaml",
    ]

    installed = 0
    for filename in template_files:
        src = templates_dir / filename
        dst = config_dir / filename

        if not src.exists():
            print(f"  Warning: Template {filename} not found", file=sys.stderr)
            continue
        if dst.exists() and not force:
            print(f"  Skipping {filename} (already exists, use --force to overwrite)")
            continue
        shutil.copy2(src, dst)
        print(f"  Installed {filename}")
        installed += 1

    if installed:
        print(f"\nConfiguration installed to: {config_dir}")
        print("\nNext steps:")
        print(f"  1. Edit {config_dir}/ccproxy.yaml")
        print("  2. Start with: ccproxy start")
    else:
        print(f"\nNothing to install. Config files already exist in {config_dir}.")


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

    Without --inspect: sets ANTHROPIC_BASE_URL etc. to point at ccproxy's
    reverse proxy listener so SDK clients route through the inspector.

    With --inspect: confines the subprocess in a WireGuard namespace jail
    for transparent traffic capture (all traffic routes through mitmweb).
    """
    from ccproxy.config import get_config

    ccproxy_config_path = config_dir / "ccproxy.yaml"
    if not ccproxy_config_path.exists():
        print(f"Error: Configuration not found at {ccproxy_config_path}", file=sys.stderr)
        print("Run 'ccproxy install' first to set up configuration.", file=sys.stderr)
        sys.exit(1)

    cfg = get_config()
    host, port = cfg.host, cfg.port

    # Set up environment for the subprocess
    env = os.environ.copy()

    # Inspect mode: route subprocess traffic through a WireGuard namespace for transparent capture.
    # No base URL env vars — traffic routes through the mitmweb addon pipeline.
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
                "Start ccproxy first: ccproxy start",
                file=sys.stderr,
            )
            sys.exit(1)

        wg_client_conf = wg_conf_file.read_text()

        confdir = cfg.inspector.mitmproxy.confdir
        inspector_confdir: Path | None = Path(confdir) if confdir else None

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


async def _run_inspect(
    config_dir: Path,
    main_port: int,
) -> int:
    """Run the inspector lifecycle: mitmweb + WireGuard namespace.

    Embeds mitmweb in-process via WebMaster with two listeners (reverse
    proxy + WireGuard CLI). The three-stage addon chain (inbound → transform
    → outbound) handles all request routing via lightllm — no LiteLLM
    subprocess.

    Returns 0 on clean shutdown.
    """
    import asyncio

    from ccproxy.config import get_config
    from ccproxy.inspector import get_wg_client_conf, run_inspector
    from ccproxy.inspector.namespace import check_namespace_capabilities

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

    # Set TLS keylog path before any mitmproxy module that reads
    # MITMPROXY_SSLKEYLOGFILE is imported. mitmproxy.net.tls evaluates
    # this env var at module import time (module-level global), triggered
    # by the WebMaster import inside run_inspector() below.
    tls_keylog_path = config_dir / "tls.keylog"
    os.environ["MITMPROXY_SSLKEYLOGFILE"] = str(tls_keylog_path)

    pid = os.getpid()
    wg_cli_keypair_path = config_dir / f"wireguard-cli.{pid}.conf"

    (config_dir / ".inspector-wireguard-client.conf").unlink(missing_ok=True)

    builtin_print(
        f"Starting inspector: mitmweb reverse@{main_port} "
        f"+ wg-cli (auto-port), UI@{inspector.port}"
    )

    master, master_task, web_token = await run_inspector(
        wg_cli_conf_path=wg_cli_keypair_path,
        reverse_port=main_port,
    )

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, master.shutdown)

    try:
        wg_cli_conf = get_wg_client_conf(master, wg_cli_keypair_path)
        if wg_cli_conf:
            (config_dir / ".inspector-wireguard-client.conf").write_text(wg_cli_conf)
        else:
            logger.warning("Failed to retrieve CLI WireGuard client config")

        # Export WireGuard keys for Wireshark decryption
        wg_keylog_path = config_dir / "wg.keylog"
        keylog_lines: list[str] = []
        if wg_cli_keypair_path.exists():
            try:
                kp_data = json.loads(wg_cli_keypair_path.read_text())
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

        builtin_print(f"TLS keylog: {tls_keylog_path}")
        builtin_print("  Wireshark: Edit → Preferences → Protocols → TLS → (Pre)-Master-Secret log filename")

        web_url = f"http://{inspector.mitmproxy.web_host}:{inspector.port}/?token={web_token}"
        builtin_print(f"Inspector UI: {web_url}")

        # Block until shutdown (SIGTERM or SIGINT)
        await master_task

    finally:
        master.shutdown()  # type: ignore[no-untyped-call]
        try:
            await master_task
        except Exception:
            pass
        loop.remove_signal_handler(signal.SIGTERM)

        wg_cli_keypair_path.unlink(missing_ok=True)

    return 0


def start_server(
    config_dir: Path,
) -> None:
    """Start the ccproxy inspector server.

    Runs mitmweb with the three-stage addon chain (inbound → transform →
    outbound). All request routing is handled via lightllm.

    Runs in the foreground. Use process-compose or systemd for supervision.
    """
    import asyncio

    from ccproxy.config import get_config
    from ccproxy.preflight import run_preflight_checks

    main_port = get_config().port
    ports_to_check = [main_port, get_config().inspector.port]
    run_preflight_checks(ports=ports_to_check, config_dir=config_dir)

    exit_code = asyncio.run(_run_inspect(
        config_dir=config_dir,
        main_port=main_port,
    ))
    sys.exit(exit_code)


def view_logs(follow: bool = False, lines: int = 100, config_dir: Path | None = None) -> None:
    """View ccproxy logs from journal, process-compose, or log file."""
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
            "-n",
            str(lines),
        ]
        if follow:
            pc_cmd.append("-f")
        try:
            proc = subprocess.run(pc_cmd)  # noqa: S603
            sys.exit(proc.returncode)
        except KeyboardInterrupt:
            sys.exit(0)

    if config_dir:
        log_path = config_dir / "ccproxy.log"
        if log_path.exists():
            tail_cmd = ["tail", "-n", str(lines)]
            if follow:
                tail_cmd.append("-f")
            tail_cmd.append(str(log_path))
            try:
                proc = subprocess.run(tail_cmd)  # noqa: S603
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
    """Show ccproxy status."""
    import socket

    def _check_alive(check_host: str, check_port: int, timeout: float = 0.5) -> bool:
        try:
            with socket.create_connection((check_host, check_port), timeout=timeout):
                return True
        except OSError:
            return False

    from ccproxy.config import get_config

    cfg = get_config()
    host, main_port = cfg.host, cfg.port
    inspect_port = cfg.inspector.port
    hooks = cfg.hooks

    # Check configuration files
    ccproxy_config = config_dir / "ccproxy.yaml"
    config_paths: dict[str, str] = {}
    if ccproxy_config.exists():
        config_paths["ccproxy.yaml"] = str(ccproxy_config)

    proxy_url = f"http://{host}:{main_port}"

    # Detect running state via TCP probes
    proxy_running = _check_alive(host, main_port)
    combined_running = _check_alive("127.0.0.1", inspect_port)

    # Build inspector URL — resolve web_password from config if set
    inspect_url: str | None = None
    if combined_running:
        from ccproxy.config import CredentialSource

        base = f"http://127.0.0.1:{inspect_port}"
        web_password_cfg = cfg.inspector.mitmproxy.web_password
        if isinstance(web_password_cfg, str):
            inspect_url = f"{base}/?token={web_password_cfg}"
        elif web_password_cfg is not None:
            source = web_password_cfg if isinstance(web_password_cfg, CredentialSource) else CredentialSource(**web_password_cfg)
            resolved = source.resolve("mitmweb web_password")
            inspect_url = f"{base}/?token={resolved}" if resolved else base
        else:
            inspect_url = base

    status_data: dict[str, Any] = {
        "proxy": proxy_running,
        "url": proxy_url,
        "config": config_paths,
        "hooks": hooks,
        "log": str(config_dir / "ccproxy.log") if (config_dir / "ccproxy.log").exists() else None,
        "inspector": {
            "running": combined_running,
            "entry_port": main_port,
            "inspect_port": inspect_port,
            "inspect_url": inspect_url,
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

        # Inspector status
        inspector_info = status_data["inspector"]

        if inspector_info["running"]:
            entry_port = inspector_info["entry_port"]
            inspect_status = f"[green]listening[/green]@[cyan]{entry_port}[/cyan]"
            if inspector_info.get("inspect_url"):
                inspect_status += f"\n[green]ui[/green] → [cyan]{inspector_info['inspect_url']}[/cyan]"
        else:
            inspect_status = "[dim]stopped[/dim]"

        table.add_row("inspector", inspect_status)

        # Config files
        if status_data["config"]:
            config_display = "\n".join(f"[cyan]{key}[/cyan]: {value}" for key, value in status_data["config"].items())
        else:
            config_display = "[red]No config files found[/red]"
        table.add_row("config", config_display)

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



def main(
    cmd: Annotated[Command, tyro.conf.arg(name="")],
    *,
    config_dir: Annotated[Path | None, tyro.conf.arg(help="Configuration directory", metavar="PATH")] = None,
) -> None:
    """ccproxy - Intercept and route Claude Code requests to LLM providers.

    Transparent mitmproxy-based pipeline with DAG-driven hooks for OAuth
    injection, model transformation, and identity management.
    """
    if config_dir is None:
        env_config_dir = os.environ.get("CCPROXY_CONFIG_DIR")
        config_dir = Path(env_config_dir) if env_config_dir else Path.home() / ".ccproxy"

    os.environ.setdefault("CCPROXY_CONFIG_DIR", str(config_dir))
    from ccproxy.config import get_config

    config = get_config()
    setup_logging(config_dir, debug=config.debug, log_file=isinstance(cmd, Start))

    # Handle each command type
    if isinstance(cmd, Start):
        start_server(config_dir)

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
            print("  --inspect, -i       Route subprocess traffic through a WireGuard namespace jail")
            print("                      for transparent capture of all TCP/UDP traffic.")
            print("                      Requires ccproxy start to be running.")
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
        view_logs(follow=cmd.follow, lines=cmd.lines, config_dir=config_dir)

    elif isinstance(cmd, Status):
        show_status(
            config_dir,
            json_output=cmd.json,
            check_proxy=cmd.proxy,
            check_inspect=cmd.inspect,
        )

    elif isinstance(cmd, DagViz):
        handle_dag_viz(cmd)

    elif isinstance(cmd, Flows):  # pyright: ignore[reportUnnecessaryIsInstance]
        handle_flows(cmd, config_dir)


def handle_dag_viz(cmd: DagViz) -> None:
    """Handle dag-viz subcommand to visualize the pipeline DAG."""
    # Import all hooks to register them
    from ccproxy.hooks import (  # noqa: F401
        add_beta_headers,  # pyright: ignore[reportUnusedImport]
        extract_session_id,  # pyright: ignore[reportUnusedImport]
        forward_oauth,  # pyright: ignore[reportUnusedImport]
        inject_claude_code_identity,  # pyright: ignore[reportUnusedImport]
        inject_mcp_notifications,  # pyright: ignore[reportUnusedImport]
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
        "flows",
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
