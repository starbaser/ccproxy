"""Process management for inspector traffic capture."""

import logging
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

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

    logger.info("Auto-generating Prisma client for inspector storage...")
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


def _pipe_output(proc: subprocess.Popen[bytes], tag: str) -> threading.Thread:
    """Forward subprocess stdout to stderr with a [tag] prefix."""
    def reader() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stderr.buffer.write(f"[{tag}] ".encode() + line)
            sys.stderr.buffer.flush()

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    return t


def _check_port_alive(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _resolve_mitmproxy_binary(web: bool = False) -> Path:
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


_WEB_FIELDS = {"web_host", "web_password", "web_open_browser"}


def _build_mitmproxy_set_args(opts: "MitmproxyOptions") -> list[str]:
    """Convert MitmproxyOptions fields to mitmproxy --set arguments.

    Web UI fields (web_host, web_password, web_open_browser) are excluded —
    they use dedicated CLI flags handled by the caller.
    """
    from ccproxy.inspector.mitmproxy_options import MitmproxyOptions  # noqa: F811

    args: list[str] = []
    for field_name in MitmproxyOptions.model_fields:
        if field_name in _WEB_FIELDS:
            continue
        value = getattr(opts, field_name)
        if value is None:
            continue
        if isinstance(value, list):
            if value:
                args += ["--set", f"{field_name}={','.join(value)}"]
            continue
        if isinstance(value, bool):
            args += ["--set", f"{field_name}={'true' if value else 'false'}"]
        else:
            args += ["--set", f"{field_name}={value}"]
    return args


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
        env["CCPROXY_INSPECTOR_REVERSE_PORT"] = str(reverse_port)
    if forward_port is not None:
        env["CCPROXY_INSPECTOR_FORWARD_PORT"] = str(forward_port)
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
            data: dict[str, Any] = yaml.safe_load(f)
        url = data.get("ccproxy", {}).get("inspector", {}).get("database_url")
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
    description: str,
) -> subprocess.Popen[bytes]:
    """Launch a mitmproxy subprocess and return the Popen object.

    Args:
        cmd: Command and arguments
        env: Environment variables
        description: Human-readable description for log messages

    Returns:
        The running subprocess as a Popen object
    """
    logger.info("Starting %s", description)

    try:
        process = subprocess.Popen(        # noqa: S603
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=False,
            env=env,
        )
        logger.info("Mitmproxy started with PID %d", process.pid)
        _pipe_output(process, "inspector")
        return process
    except FileNotFoundError:
        logger.error("mitmproxy command not found")
        sys.exit(1)


def start_inspector(
    config_dir: Path,
    config: "InspectorConfig",
    litellm_port: int,
    *,
    reverse_port: int | None = None,
    forward_port: int | None = None,
) -> subprocess.Popen[bytes]:
    """Start the mitmweb inspector process.

    Launches mitmweb with three --mode listeners: reverse (client-facing),
    regular (LiteLLM outbound via HTTPS_PROXY), and wireguard (namespace
    transparent capture).

    Args:
        config_dir: Runtime configuration directory
        config: InspectorConfig with all inspector settings
        litellm_port: Port where LiteLLM is running (runtime-derived)
        reverse_port: Override for reverse listener port (defaults to config.port)
        forward_port: Override for regular listener port (defaults to auto-assigned)

    Returns:
        The running subprocess as a Popen object
    """
    from ccproxy.config import InspectorConfig  # noqa: F811

    _auto_generate_prisma(config_dir)

    mitm_bin = _resolve_mitmproxy_binary(web=True)
    script_path = _resolve_addon_script()

    rev_port = reverse_port or config.port
    fwd_port = forward_port or 8081
    wg_spec = (
        f"wireguard:{config.wireguard_conf_path}"
        if config.wireguard_conf_path
        else "wireguard"
    )

    cmd = [
        str(mitm_bin),
        "--mode", f"reverse:http://localhost:{litellm_port}@{rev_port}",
        "--mode", f"regular@{fwd_port}",
        "--mode", f"{wg_spec}@{config.wireguard_port}",
        "-s", str(script_path),
        *_build_mitmproxy_set_args(config.mitmproxy),
        "--web-port", str(config.port),
        "--web-host", config.mitmproxy.web_host,
    ]

    if config.mitmproxy.web_password is not None:
        cmd += ["--set", f"web_password={config.mitmproxy.web_password}"]

    env = _build_env(
        config_dir,
        reverse_port=rev_port,
        forward_port=fwd_port,
        litellm_port=litellm_port,
    )

    description = (
        f"mitmweb: reverse@{rev_port} → LiteLLM@{litellm_port}, "
        f"regular@{fwd_port}, wireguard@{config.wireguard_port}, "
        f"UI@{config.port}"
    )

    return _launch_process(cmd, env, description)


def get_inspector_status() -> dict[str, dict[str, bool | str | None]]:
    """Get the status of the inspector process via TCP port probe.

    Probes the mitmweb UI port (InspectorConfig.port) to determine
    whether the inspector is running.

    Returns:
        Dictionary with status information
    """
    from ccproxy.config import get_config

    config = get_config()
    inspector_cfg = getattr(config, "inspector", None)
    port: int = getattr(inspector_cfg, "port", 8083)

    running = _check_port_alive("127.0.0.1", port)
    status: dict[str, bool | str | None] = {"running": running}

    return {"inspector": status}
