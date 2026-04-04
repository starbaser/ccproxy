"""Process management for mitmproxy traffic capture."""

import logging
import os
import socket
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def ensure_prisma_client(database_url: str) -> bool:
    """Ensure Prisma client is generated for the current environment.

    Prisma requires a generated client (build-time step). When ccproxy is installed
    via `uv tool install`, the client may not exist. This function auto-generates
    it if needed.

    Args:
        database_url: PostgreSQL connection URL (used for schema introspection)

    Returns:
        True if client is ready, False if generation failed
    """
    # Try importing and instantiating Prisma - if it works, client is ready
    try:
        from prisma import Prisma  # type: ignore[attr-defined]

        Prisma()
        return True
    except Exception:
        pass

    # Client not generated - find schema and run prisma generate
    import ccproxy

    # Try multiple schema locations (dev vs installed)
    pkg_dir = Path(ccproxy.__file__).parent
    candidates = [
        pkg_dir.parent.parent / "prisma" / "schema.prisma",  # Dev: src/../prisma/
        pkg_dir / "prisma" / "schema.prisma",  # Installed: bundled with package
    ]

    schema_path = None
    for candidate in candidates:
        if candidate.exists():
            schema_path = candidate
            break

    if not schema_path:
        logger.warning("Prisma schema not found, cannot auto-generate client")
        return False

    logger.info("Auto-generating Prisma client for MITM storage...")
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url

    # Ensure the bin directory containing prisma-client-py is on PATH.
    # Prisma CLI spawns /bin/sh to run the generator, which won't inherit
    # Nix store paths unless explicitly added.
    exe_bin_dir = str(Path(sys.executable).parent)
    env["PATH"] = exe_bin_dir + os.pathsep + env.get("PATH", "")

    try:
        result = subprocess.run(
            [sys.executable, "-m", "prisma", "generate", "--schema", str(schema_path)],
            env=env,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            logger.info("Prisma client generated successfully")
            return True
        logger.error(f"Prisma generate failed: {result.stderr}")
        return False
    except Exception as e:
        logger.error(f"Failed to run prisma generate: {e}")
        return False


def get_log_file(config_dir: Path) -> Path:
    """Get the path to the mitmproxy log file.

    Args:
        config_dir: Configuration directory

    Returns:
        Path to log file
    """
    return config_dir / "mitm.log"


def _check_port_alive(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _resolve_mitm_binary(web: bool = False) -> Path:
    """Resolve the mitmproxy binary path from the current Python environment.

    Args:
        web: Use mitmweb instead of mitmdump

    Returns:
        Path to the binary

    Raises:
        SystemExit: If binary not found
    """
    venv_bin = Path(sys.executable).parent
    binary_name = "mitmweb" if web else "mitmdump"
    binary_path = venv_bin / binary_name

    if not binary_path.exists():
        logger.error(f"{binary_name} not found at {binary_path}")
        logger.error("Make sure mitmproxy is installed: uv add mitmproxy")
        sys.exit(1)

    return binary_path


def _resolve_addon_script() -> Path:
    """Resolve the mitmproxy addon script path.

    Returns:
        Path to script.py

    Raises:
        SystemExit: If script not found
    """
    script_path = Path(__file__).parent / "script.py"
    if not script_path.exists():
        logger.error(f"Addon script not found at {script_path}")
        sys.exit(1)
    return script_path


def _resolve_confdir(confdir: Path | None) -> str:
    """Resolve mitmproxy confdir for CA certificate store."""
    return str(Path(confdir).expanduser()) if confdir else str(Path.home() / ".mitmproxy")


def _auto_generate_prisma(config_dir: Path | None = None) -> None:
    """Auto-generate Prisma client if database is configured."""
    database_url = os.environ.get("CCPROXY_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not database_url and config_dir:
        database_url = _resolve_database_url(config_dir)
    if database_url and not ensure_prisma_client(database_url):
        logger.warning("Prisma client generation failed - traces will not be persisted")


def _build_env(
    config_dir: Path,
    *,
    reverse_port: int | None = None,
    forward_port: int | None = None,
    litellm_port: int | None = None,
) -> dict[str, str]:
    """Build environment variables for the mitmweb subprocess."""
    env = os.environ.copy()
    env["CCPROXY_CONFIG_DIR"] = str(config_dir)

    if reverse_port is not None:
        env["CCPROXY_MITM_REVERSE_PORT"] = str(reverse_port)
    if forward_port is not None:
        env["CCPROXY_MITM_FORWARD_PORT"] = str(forward_port)
    if litellm_port is not None:
        env["CCPROXY_LITELLM_PORT"] = str(litellm_port)

    # Ensure database URL is available — resolve from ccproxy.yaml if not in env
    if "CCPROXY_DATABASE_URL" not in env and "DATABASE_URL" not in env:
        database_url = _resolve_database_url(config_dir)
        if database_url:
            env["CCPROXY_DATABASE_URL"] = database_url

    return env


def _resolve_database_url(config_dir: Path) -> str | None:
    """Resolve database URL from ccproxy.yaml config."""
    import re

    config_path = config_dir / "ccproxy.yaml"
    if not config_path.exists():
        return None
    try:
        import yaml

        with config_path.open() as f:
            data = yaml.safe_load(f)
        url = data.get("ccproxy", {}).get("mitm", {}).get("database_url")
        if not url:
            return None
        # Expand ${VAR:-default} patterns
        return re.sub(
            r"\$\{([^}:]+)(?::-(.*?))?\}",
            lambda m: os.environ.get(m.group(1), m.group(2) or ""),
            url,
        )
    except Exception:
        return None


def _launch_process(
    cmd: list[str],
    env: dict[str, str],
    log_file: Path,
    description: str,
) -> subprocess.Popen[bytes]:
    """Launch a mitmproxy subprocess and return the Popen object.

    Args:
        cmd: Command and arguments
        env: Environment variables
        log_file: Log file path for subprocess output
        description: Human-readable description for log messages

    Returns:
        The running subprocess as a Popen object
    """
    logger.info("Starting %s", description)
    logger.info("Log file: %s", log_file)

    try:
        log = log_file.open("w")
        process = subprocess.Popen(  # noqa: S603
            cmd,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=False,
            env=env,
        )
        logger.info("Mitmproxy started with PID %d", process.pid)
        return process
    except FileNotFoundError:
        logger.error("mitmproxy command not found")
        sys.exit(1)


def start_mitm(
    config_dir: Path,
    reverse_port: int = 4002,
    forward_port: int = 4003,
    litellm_port: int = 4001,
    web: bool = False,
    inspect_port: int = 8083,
    confdir: Path | None = None,
    wireguard_port: int = 51820,
    wireguard_conf_path: Path | None = None,
) -> subprocess.Popen[bytes]:
    """Start the combined mitmproxy process (reverse + forward in one process).

    Uses mitmproxy multi-mode to serve both reverse and forward proxy
    listeners from a single process with a unified addon pipeline.

    Args:
        config_dir: Configuration directory for log files
        reverse_port: Port for client-facing reverse proxy
        forward_port: Port for LiteLLM-outbound forward proxy
        litellm_port: Port where LiteLLM is running
        web: Use mitmweb (browser UI) instead of mitmdump
        inspect_port: Port for mitmweb web UI (only used when web=True)
        confdir: mitmproxy confdir for CA certs (defaults to ~/.mitmproxy)
        wireguard_port: Port for WireGuard transparent proxy listener
        wireguard_conf_path: Optional path to WireGuard config file

    Returns:
        The running subprocess as a Popen object
    """
    _auto_generate_prisma(config_dir)

    log_file = get_log_file(config_dir)
    mitm_bin = _resolve_mitm_binary(web=web)
    script_path = _resolve_addon_script()
    mitm_confdir = _resolve_confdir(confdir)

    cmd = [
        str(mitm_bin),
        "--mode",
        f"reverse:http://localhost:{litellm_port}@{reverse_port}",
        "--mode",
        f"regular@{forward_port}",
        "--mode",
        f"{'wireguard:' + str(wireguard_conf_path) if wireguard_conf_path else 'wireguard'}@{wireguard_port}",
        "--set",
        f"confdir={mitm_confdir}",
        "--set",
        "stream_large_bodies=1m",
        "--set",
        "ssl_insecure=true",
        "-s",
        str(script_path),
    ]

    if web:
        import secrets

        web_token = secrets.token_hex(16)
        (config_dir / ".mitm-web-token").write_text(web_token)
        cmd += [
            "--web-port",
            str(inspect_port),
            "--web-host",
            "127.0.0.1",
            "--set",
            f"web_password={web_token}",
        ]

    env = _build_env(
        config_dir,
        reverse_port=reverse_port,
        forward_port=forward_port,
        litellm_port=litellm_port,
    )

    description = (
        f"mitmproxy combined mode: "
        f"reverse@{reverse_port} → LiteLLM@{litellm_port}, "
        f"forward@{forward_port}"
    )
    if web:
        description += f", inspect UI@{inspect_port}"

    return _launch_process(cmd, env, log_file, description)


def get_mitm_status(config_dir: Path) -> dict[str, dict[str, bool | str | None]]:
    """Get the status of mitmproxy via TCP port probes.

    Args:
        config_dir: Configuration directory

    Returns:
        Dictionary with status information
    """
    from ccproxy.config import get_config

    config = get_config()
    mitm_cfg = getattr(config, "mitm", None)

    reverse_port: int = getattr(mitm_cfg, "reverse_port", None) or 4002
    forward_port: int = getattr(mitm_cfg, "forward_port", None) or 4003

    running = _check_port_alive("127.0.0.1", reverse_port) or _check_port_alive(
        "127.0.0.1", forward_port
    )

    status: dict[str, bool | str | None] = {"running": running}
    if running:
        log = get_log_file(config_dir)
        status["log_file"] = str(log) if log.exists() else None

    return {"combined": status}
