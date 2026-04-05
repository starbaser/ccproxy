"""Process management for inspector traffic capture."""

from __future__ import annotations

import logging
import os
import secrets
import socket
import subprocess
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ccproxy.config import InspectorConfig
    from ccproxy.inspector.mitmproxy_options import MitmproxyOptions

logger = logging.getLogger(__name__)


def _find_free_udp_port() -> int:
    """Find an available UDP port by binding to port 0."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]



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


def _build_mitmproxy_set_args(opts: MitmproxyOptions) -> list[str]:
    """Convert MitmproxyOptions fields to mitmproxy --set arguments.

    Web UI fields (web_host, web_password, web_open_browser) are excluded —
    they use dedicated CLI flags handled by the caller.
    """
    from ccproxy.inspector.mitmproxy_options import MitmproxyOptions

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

    return env


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
    config: InspectorConfig,
    litellm_port: int,
    *,
    reverse_port: int | None = None,
    forward_port: int | None = None,
) -> tuple[subprocess.Popen[bytes], str]:
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
        Tuple of (running subprocess, web API auth token)
    """

    mitm_bin = _resolve_mitmproxy_binary(web=True)
    script_path = _resolve_addon_script()

    rev_port = reverse_port or config.port
    fwd_port = forward_port or 8081
    wg_spec = (
        f"wireguard:{config.wireguard_conf_path}"
        if config.wireguard_conf_path
        else "wireguard"
    )
    wg_port = _find_free_udp_port()

    cmd = [
        str(mitm_bin),
        "--mode", f"reverse:http://localhost:{litellm_port}@{rev_port}",
        "--mode", f"regular@{fwd_port}",
        "--mode", f"{wg_spec}@{wg_port}",
        "-s", str(script_path),
        *_build_mitmproxy_set_args(config.mitmproxy),
        "--web-port", str(config.port),
        "--web-host", config.mitmproxy.web_host,
    ]

    web_token = config.mitmproxy.web_password or secrets.token_hex(16)
    cmd += ["--set", f"web_password={web_token}"]

    env = _build_env(
        config_dir,
        reverse_port=rev_port,
        forward_port=fwd_port,
        litellm_port=litellm_port,
    )

    description = (
        f"mitmweb: reverse@{rev_port} → LiteLLM@{litellm_port}, "
        f"regular@{fwd_port}, wireguard@{wg_port}, "
        f"UI@{config.port}"
    )

    return _launch_process(cmd, env, description), web_token


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
