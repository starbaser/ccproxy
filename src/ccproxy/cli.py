"""ccproxy CLI."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from builtins import print as builtin_print
from pathlib import Path
from typing import Annotated, Any

import tyro
from pydantic import BaseModel, Field
from rich import print
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ccproxy.tools.flows import (
    Flows,
    FlowsClear,
    FlowsDiff,
    FlowsDump,
    FlowsList,
    handle_flows,
)
from ccproxy.utils import get_templates_dir

logger = logging.getLogger(__name__)


class Start(BaseModel):
    """Start the ccproxy inspector server."""

    args: Annotated[list[str] | None, tyro.conf.Positional] = None
    """Additional arguments (reserved for future use)."""


class Install(BaseModel):
    """Install ccproxy configuration files."""

    force: bool = False
    """Overwrite existing configuration."""


class Run(BaseModel):
    """Run a command with ccproxy environment.

    Usage: ccproxy run [--inspect] -- <command> [args...]"""

    command: Annotated[list[str], tyro.conf.Positional] = Field(default_factory=list)
    """Command and arguments to execute with proxy settings."""


class Logs(BaseModel):
    """View ccproxy logs from journal or process-compose."""

    follow: Annotated[bool, tyro.conf.arg(aliases=["-f"])] = False
    """Follow log output (like tail -f)."""

    lines: Annotated[int, tyro.conf.arg(aliases=["-n"])] = 100
    """Number of lines to show (default: 100)."""


class Status(BaseModel):
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

    json_output: Annotated[bool, tyro.conf.arg(name="json")] = False
    """Output status as JSON with boolean values."""

    proxy: bool = False
    """Check if proxy is running."""

    inspect: bool = False
    """Check if inspector stack (mitmweb) is running."""


Command = (
    Annotated[Start, tyro.conf.subcommand(name="start")]
    | Annotated[Install, tyro.conf.subcommand(name="install")]
    | Annotated[Run, tyro.conf.subcommand(name="run")]
    | Annotated[Logs, tyro.conf.subcommand(name="logs")]
    | Annotated[Status, tyro.conf.subcommand(name="status")]
    | Flows
)


def setup_logging(
    config_dir: Path,
    log_level: str = "INFO",
    *,
    log_file: Path | None = None,
    use_journal: bool = False,
    verbose: bool = True,
) -> Path | None:
    """Configure unified logging with optional file output.

    The effective root level is ``log_level`` when ``verbose=True``,
    otherwise ``max(log_level, WARNING)`` — one-shot CLI commands without
    ``-v`` still surface warnings and errors but suppress INFO/DEBUG noise.

    Primary handler:
      - ``use_journal=True``: ``systemd.journal.JournalHandler`` with
        ``SYSLOG_IDENTIFIER=ccproxy`` (requires the ``journal`` optional extra).
      - Otherwise: ``StreamHandler(sys.stderr)``.

    When the journal handler cannot be constructed (missing ``systemd-python``
    or no systemd socket), falls back to stderr and emits a warning log.

    When ``log_file`` is provided and not running under systemd
    (``INVOCATION_ID`` unset), also logs to that path (truncated on restart).

    Returns the log file path if a FileHandler was installed, None otherwise.
    """
    root = logging.getLogger()
    root.handlers.clear()

    level = getattr(logging, log_level.upper(), logging.INFO)
    if not verbose:
        level = max(level, logging.WARNING)
    root.setLevel(level)

    fmt = logging.Formatter(
        "%(asctime)s %(name)-30s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    handler: logging.Handler
    journal_fallback_reason: str | None = None
    if use_journal:
        try:
            from systemd.journal import JournalHandler  # type: ignore[import-not-found]

            handler = JournalHandler(SYSLOG_IDENTIFIER="ccproxy")
        except Exception as exc:  # ImportError or runtime socket errors
            handler = logging.StreamHandler(sys.stderr)
            journal_fallback_reason = f"{type(exc).__name__}: {exc}"
    else:
        handler = logging.StreamHandler(sys.stderr)

    handler.setFormatter(fmt)
    root.addHandler(handler)

    log_path: Path | None = None
    if log_file is not None and not os.environ.get("INVOCATION_ID"):
        log_path = log_file
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(str(log_path), mode="w", encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)

    if journal_fallback_reason is not None:
        logger.warning(
            "use_journal requested but JournalHandler unavailable (%s); falling back to stderr",
            journal_fallback_reason,
        )

    return log_path


def install_config(config_dir: Path, force: bool = False) -> None:
    """Install ccproxy template configuration files."""
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
        content = proxy_ca_data + "\n" + base_ca_data
        fd, tmp_path = tempfile.mkstemp(dir=str(config_dir), prefix=".ca-bundle-")
        try:
            os.write(fd, content.encode())
            os.close(fd)
            Path(tmp_path).rename(combined_bundle)
        except BaseException:
            with contextlib.suppress(OSError):
                os.close(fd)
            Path(tmp_path).unlink(missing_ok=True)
            raise
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
                "\nCannot create network namespace for --inspect mode. All prerequisites above must be satisfied.",
                file=sys.stderr,
            )
            sys.exit(1)
        wg_conf_file = config_dir / ".inspector-wireguard-client.conf"
        if not wg_conf_file.exists():
            print(
                "Error: No WireGuard configuration found. Start ccproxy first: ccproxy start",
                file=sys.stderr,
            )
            sys.exit(1)

        wg_client_conf = wg_conf_file.read_text()

        confdir = cfg.inspector.mitmproxy.confdir
        inspector_confdir: Path | None = Path(confdir) if confdir else None

        # Trust mitmproxy's CA so TLS interception works transparently
        combined_bundle = _ensure_combined_ca_bundle(config_dir, env.get("SSL_CERT_FILE"), confdir=inspector_confdir)
        if combined_bundle:
            bundle = str(combined_bundle)
            env["SSL_CERT_FILE"] = bundle
            env["NODE_EXTRA_CA_CERTS"] = bundle
            env["REQUESTS_CA_BUNDLE"] = bundle
            env["CURL_CA_BUNDLE"] = bundle

        ctx = None
        try:
            ctx = create_namespace(wg_client_conf, proxy_port=port)
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
            "\nCannot create network namespace for --inspect mode. All prerequisites above must be satisfied.",
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

    logger.info(
        "Starting inspector: mitmweb reverse@%d + wg-cli (auto-port), UI@%d",
        main_port,
        inspector.port,
    )

    master, master_task, web_token = await run_inspector(
        wg_cli_conf_path=wg_cli_keypair_path,
        reverse_port=main_port,
    )

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, master.shutdown)

    if get_config().verify_readiness_on_startup:
        import contextlib as _contextlib

        from ccproxy.inspector.readiness import verify_or_shutdown

        async def _cleanup() -> None:
            master.shutdown()  # type: ignore[no-untyped-call]
            with _contextlib.suppress(Exception):
                await master_task
            loop.remove_signal_handler(signal.SIGTERM)
            wg_cli_keypair_path.unlink(missing_ok=True)

        await verify_or_shutdown(get_config(), _cleanup)

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
            logger.info("WireGuard keylog: %s", wg_keylog_path)
            logger.info("  Wireshark: -o wg.keylog_file:%s", wg_keylog_path)

        logger.info("TLS keylog: %s", tls_keylog_path)
        logger.info("  Wireshark: Edit → Preferences → Protocols → TLS → (Pre)-Master-Secret log filename")

        web_url = f"http://{inspector.mitmproxy.web_host}:{inspector.port}/?token={web_token}"
        logger.info("Inspector UI: %s", web_url)

        # Block until shutdown (SIGTERM or SIGINT)
        await master_task

    finally:
        import contextlib

        master.shutdown()  # type: ignore[no-untyped-call]
        with contextlib.suppress(Exception):
            await master_task
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

    exit_code = asyncio.run(
        _run_inspect(
            config_dir=config_dir,
            main_port=main_port,
        )
    )
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
        from ccproxy.config import get_config

        log_path = get_config().resolved_log_file
        if log_path is not None and log_path.exists():
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
            source = (
                web_password_cfg
                if isinstance(web_password_cfg, CredentialSource)
                else CredentialSource(**web_password_cfg)
            )
            resolved = source.resolve("mitmweb web_password")
            inspect_url = f"{base}/?token={resolved}" if resolved else base
        else:
            inspect_url = base

    log_path = cfg.resolved_log_file
    status_data: dict[str, Any] = {
        "proxy": proxy_running,
        "url": proxy_url,
        "config": config_paths,
        "hooks": hooks,
        "log": str(log_path) if log_path is not None and log_path.exists() else None,
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
        console = Console()

        table = Table(show_header=False, show_lines=True)
        table.add_column("Key", style="white", width=15)
        table.add_column("Value", style="yellow")

        url = status_data.get("url") or "http://127.0.0.1:4000"
        if status_data["proxy"]:
            proxy_status = f"[cyan]{url}[/cyan] [green]true[/green]"
        else:
            proxy_status = f"[dim]{url}[/dim] [red]false[/red]"
        table.add_row("proxy", proxy_status)

        inspector_info = status_data["inspector"]

        if inspector_info["running"]:
            entry_port = inspector_info["entry_port"]
            inspect_status = f"[green]listening[/green]@[cyan]{entry_port}[/cyan]"
            if inspector_info.get("inspect_url"):
                inspect_status += f"\n[green]ui[/green] → [cyan]{inspector_info['inspect_url']}[/cyan]"
        else:
            inspect_status = "[dim]stopped[/dim]"

        table.add_row("inspector", inspect_status)

        if status_data["config"]:
            config_display = "\n".join(f"[cyan]{key}[/cyan]: {value}" for key, value in status_data["config"].items())
        else:
            config_display = "[red]No config files found[/red]"
        table.add_row("config", config_display)

        log_display = status_data["log"] if status_data["log"] else "[yellow]No log file[/yellow]"
        table.add_row("log", log_display)

        console.print(Panel(table, title="[bold]ccproxy Status[/bold]", border_style="blue"))

        if status_data["hooks"]:
            from ccproxy.pipeline.executor import PipelineExecutor
            from ccproxy.pipeline.loader import load_hooks
            from ccproxy.pipeline.render import render_pipeline

            hooks_cfg = status_data["hooks"]
            inbound_specs = load_hooks(hooks_cfg.get("inbound", []))
            outbound_specs = load_hooks(hooks_cfg.get("outbound", []))
            inbound_exec = PipelineExecutor(hooks=inbound_specs)
            outbound_exec = PipelineExecutor(hooks=outbound_specs)
            pipeline = render_pipeline(inbound_exec, outbound_exec)
            console.print(
                Panel(pipeline, title="[bold]Pipeline[/bold]", border_style="green")
            )


def main(
    cmd: Annotated[Command, tyro.conf.arg(name="")],
    *,
    config_dir: Annotated[Path | None, tyro.conf.arg(help="Configuration directory", metavar="PATH")] = None,
    verbose: Annotated[
        bool,
        tyro.conf.arg(
            aliases=["-v"],
            help="Show INFO/DEBUG log output on CLI commands (daemon logs unconditionally)",
        ),
    ] = False,
) -> None:
    """ccproxy - Intercept and route Claude Code requests to LLM providers.

    Transparent mitmproxy-based pipeline with DAG-driven hooks for OAuth
    injection, model transformation, and identity management.
    """
    if config_dir is None:
        env_config_dir = os.environ.get("CCPROXY_CONFIG_DIR")
        config_dir = Path(env_config_dir) if env_config_dir else Path.home() / ".ccproxy"

    os.environ.setdefault("CCPROXY_CONFIG_DIR", str(config_dir))

    # Tyro wraps nested subcommand unions (like Flows) in a DummyWrapper when
    # the outer parameter is Annotated[Command, tyro.conf.arg(name="")]. The
    # real parsed subcommand lives at cmd.__tyro_dummy_inner__ — unwrap it so
    # the isinstance dispatch below sees the concrete class.
    if hasattr(cmd, "__tyro_dummy_inner__"):
        cmd = cmd.__tyro_dummy_inner__  # type: ignore[attr-defined]
    from ccproxy.config import get_config

    config = get_config()
    is_daemon = isinstance(cmd, Start)
    # LOG_LEVEL env var overrides config.log_level — standard convention
    # used across Django / FastAPI / uvicorn. Python's stdlib has no
    # built-in env var support for logging; LOG_LEVEL is the de-facto name.
    log_level = os.environ.get("LOG_LEVEL") or config.log_level
    setup_logging(
        config_dir,
        log_level=log_level,
        log_file=config.resolved_log_file if is_daemon else None,
        use_journal=config.use_journal and is_daemon,
        verbose=is_daemon or verbose,
    )

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
            json_output=cmd.json_output,
            check_proxy=cmd.proxy,
            check_inspect=cmd.inspect,
        )

    elif isinstance(cmd, FlowsList | FlowsDump | FlowsDiff | FlowsClear):
        handle_flows(cmd, config_dir)


def entry_point() -> None:
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
