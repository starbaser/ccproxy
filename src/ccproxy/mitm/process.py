"""Process management for mitmproxy traffic capture."""

import logging
import os
import subprocess
import sys
from enum import Enum
from pathlib import Path

from ccproxy.process import is_process_running as shared_is_process_running
from ccproxy.process import stop_process as shared_stop_process
from ccproxy.process import write_pid

logger = logging.getLogger(__name__)


class ProxyMode(Enum):
    """Mitmproxy operating mode."""

    REVERSE = "reverse"
    """Reverse proxy mode - sits in front of LiteLLM"""

    FORWARD = "forward"
    """Forward proxy mode - sits behind LiteLLM for provider API calls"""


def get_pid_file(config_dir: Path, mode: ProxyMode = ProxyMode.REVERSE) -> Path:
    """Get the path to the mitmproxy PID file for a specific mode.

    Args:
        config_dir: Configuration directory
        mode: Proxy mode (REVERSE or FORWARD)

    Returns:
        Path to .mitm.lock or .mitm-forward.lock file
    """
    if mode == ProxyMode.FORWARD:
        return config_dir / ".mitm-forward.lock"
    return config_dir / ".mitm.lock"


def get_log_file(config_dir: Path, mode: ProxyMode = ProxyMode.REVERSE) -> Path:
    """Get the path to the mitmproxy log file for a specific mode.

    Args:
        config_dir: Configuration directory
        mode: Proxy mode (REVERSE or FORWARD)

    Returns:
        Path to mitm.log or mitm-forward.log file
    """
    if mode == ProxyMode.FORWARD:
        return config_dir / "mitm-forward.log"
    return config_dir / "mitm.log"


def is_running(config_dir: Path, mode: ProxyMode = ProxyMode.REVERSE) -> tuple[bool, int | None]:
    """Check if mitmproxy is currently running for a specific mode.

    Args:
        config_dir: Configuration directory
        mode: Proxy mode to check (REVERSE or FORWARD)

    Returns:
        Tuple of (is_running, pid or None)
    """
    pid_file = get_pid_file(config_dir, mode)
    return shared_is_process_running(pid_file)


def start_mitm(
    config_dir: Path,
    port: int = 4000,
    litellm_port: int = 4001,
    mode: ProxyMode = ProxyMode.REVERSE,
    detach: bool = False,
) -> None:
    """Start the mitmproxy traffic capture proxy.

    Args:
        config_dir: Configuration directory for PID and log files
        port: Port for mitmproxy to listen on
        litellm_port: Port where LiteLLM is running (only used in REVERSE mode)
        mode: Proxy mode (REVERSE or FORWARD)
        detach: Run in background mode
    """
    # Check if already running
    running, pid = is_running(config_dir, mode)
    if running:
        logger.error(f"Mitmproxy ({mode.value}) is already running with PID {pid}")
        sys.exit(1)

    # Get paths
    pid_file = get_pid_file(config_dir, mode)
    log_file = get_log_file(config_dir, mode)

    # Get the bin directory from the current Python interpreter's location
    venv_bin = Path(sys.executable).parent
    mitmdump_path = venv_bin / "mitmdump"

    if not mitmdump_path.exists():
        logger.error(f"mitmdump not found at {mitmdump_path}")
        logger.error("Make sure mitmproxy is installed: uv add mitmproxy")
        sys.exit(1)

    # Get addon script path
    script_path = Path(__file__).parent / "script.py"
    if not script_path.exists():
        logger.error(f"Addon script not found at {script_path}")
        sys.exit(1)

    # Build mitmdump command based on mode
    if mode == ProxyMode.REVERSE:
        # Reverse mode forwards requests directly to LiteLLM without CONNECT tunneling
        cmd = [
            str(mitmdump_path),
            "--mode",
            f"reverse:http://localhost:{litellm_port}",
            "--listen-port",
            str(port),
            "--set",
            "stream_large_bodies=1m",
            "-s",
            str(script_path),
        ]
    else:
        # Forward mode is the default mitmproxy mode
        cmd = [
            str(mitmdump_path),
            "--listen-port",
            str(port),
            "--set",
            "stream_large_bodies=1m",
            "-s",
            str(script_path),
        ]

    # Pass environment to subprocess
    env = os.environ.copy()
    env["CCPROXY_MITM_PORT"] = str(port)
    env["CCPROXY_MITM_MODE"] = mode.value
    env["CCPROXY_CONFIG_DIR"] = str(config_dir)
    if mode == ProxyMode.REVERSE:
        env["CCPROXY_LITELLM_PORT"] = str(litellm_port)

    if detach:
        # Run in background mode
        mode_desc = f"{mode.value} mode"
        if mode == ProxyMode.REVERSE:
            logger.info(f"Starting mitmproxy in {mode_desc} on port {port} → LiteLLM on port {litellm_port}")
        else:
            logger.info(f"Starting mitmproxy in {mode_desc} on port {port}")
        logger.info(f"Log file: {log_file}")

        try:
            with log_file.open("w") as log:
                # S603: Command construction is safe - we control the mitmdump path
                process = subprocess.Popen(  # noqa: S603
                    cmd,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,  # Detach from parent process group
                    env=env,
                )

            # Save PID
            write_pid(pid_file, process.pid)
            logger.info(f"Mitmproxy ({mode.value}) started with PID {process.pid}")

        except FileNotFoundError:
            logger.error("mitmdump command not found")
            logger.error("Please ensure mitmproxy is installed: uv add mitmproxy")
            sys.exit(1)

    else:
        # Run in foreground
        mode_desc = f"{mode.value} mode"
        if mode == ProxyMode.REVERSE:
            logger.info(f"Starting mitmproxy in {mode_desc} on port {port} → LiteLLM on port {litellm_port}")
        else:
            logger.info(f"Starting mitmproxy in {mode_desc} on port {port}")

        try:
            # S603: Command construction is safe - we control the mitmdump path
            result = subprocess.run(cmd, env=env)  # noqa: S603
            sys.exit(result.returncode)
        except FileNotFoundError:
            logger.error("mitmdump command not found")
            logger.error("Please ensure mitmproxy is installed: uv add mitmproxy")
            sys.exit(1)
        except KeyboardInterrupt:
            sys.exit(130)


def stop_mitm(config_dir: Path, mode: ProxyMode | None = None) -> bool:
    """Stop the mitmproxy traffic capture proxy.

    Args:
        config_dir: Configuration directory containing the PID file
        mode: Specific proxy mode to stop, or None to stop all modes

    Returns:
        True if at least one proxy was stopped successfully, False otherwise
    """
    if mode is not None:
        # Stop specific mode
        pid_file = get_pid_file(config_dir, mode)

        # Check if PID file exists
        if not pid_file.exists():
            logger.error(f"No mitmproxy ({mode.value}) server is running (PID file not found)")
            return False

        return shared_stop_process(pid_file)

    # Stop all modes
    stopped_any = False
    for proxy_mode in ProxyMode:
        pid_file = get_pid_file(config_dir, proxy_mode)
        if pid_file.exists():
            logger.info(f"Stopping mitmproxy ({proxy_mode.value})...")
            if shared_stop_process(pid_file):
                stopped_any = True

    if not stopped_any:
        logger.error("No mitmproxy servers are running")

    return stopped_any


def get_mitm_status(config_dir: Path) -> dict[str, dict[str, bool | int | str | None]]:
    """Get the status of all mitmproxy servers.

    Args:
        config_dir: Configuration directory

    Returns:
        Dictionary with status information for each mode
    """
    status: dict[str, dict[str, bool | int | str | None]] = {}

    for mode in ProxyMode:
        running, pid = is_running(config_dir, mode)

        mode_status: dict[str, bool | int | str | None] = {
            "running": running,
            "pid": pid,
        }

        if running:
            # Add additional information when running
            pid_file = get_pid_file(config_dir, mode)
            log_file = get_log_file(config_dir, mode)

            mode_status["pid_file"] = str(pid_file)
            mode_status["log_file"] = str(log_file) if log_file.exists() else None

        status[mode.value] = mode_status

    return status
