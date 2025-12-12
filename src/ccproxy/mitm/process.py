"""Process management for mitmproxy traffic capture."""

import logging
import os
import subprocess
import sys
from pathlib import Path

from ccproxy.process import is_process_running as shared_is_process_running
from ccproxy.process import stop_process as shared_stop_process
from ccproxy.process import write_pid

logger = logging.getLogger(__name__)


def get_pid_file(config_dir: Path) -> Path:
    """Get the path to the mitmproxy PID file.

    Args:
        config_dir: Configuration directory

    Returns:
        Path to .mitm.lock file
    """
    return config_dir / ".mitm.lock"


def get_log_file(config_dir: Path) -> Path:
    """Get the path to the mitmproxy log file.

    Args:
        config_dir: Configuration directory

    Returns:
        Path to mitm.log file
    """
    return config_dir / "mitm.log"


def is_running(config_dir: Path) -> tuple[bool, int | None]:
    """Check if mitmproxy is currently running.

    Args:
        config_dir: Configuration directory

    Returns:
        Tuple of (is_running, pid or None)
    """
    pid_file = get_pid_file(config_dir)
    return shared_is_process_running(pid_file)


def start_mitm(
    config_dir: Path,
    port: int = 4000,
    litellm_port: int = 4001,
    detach: bool = False,
) -> None:
    """Start the mitmproxy traffic capture proxy in reverse proxy mode.

    MITM sits in front of LiteLLM, forwarding requests transparently.

    Args:
        config_dir: Configuration directory for PID and log files
        port: Port for mitmproxy to listen on (main port, e.g., 4000)
        litellm_port: Port where LiteLLM is running
        detach: Run in background mode
    """
    # Check if already running
    running, pid = is_running(config_dir)
    if running:
        logger.error(f"Mitmproxy is already running with PID {pid}")
        sys.exit(1)

    # Get paths
    pid_file = get_pid_file(config_dir)
    log_file = get_log_file(config_dir)

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

    # Build mitmdump command in reverse proxy mode
    # Reverse mode forwards requests directly to LiteLLM without CONNECT tunneling
    cmd = [
        str(mitmdump_path),
        "--mode", f"reverse:http://localhost:{litellm_port}",
        "--listen-port", str(port),
        "--set", "stream_large_bodies=1m",
        "-s", str(script_path),
    ]

    # Pass environment to subprocess
    env = os.environ.copy()
    env["CCPROXY_MITM_PORT"] = str(port)
    env["CCPROXY_LITELLM_PORT"] = str(litellm_port)
    env["CCPROXY_CONFIG_DIR"] = str(config_dir)

    if detach:
        # Run in background mode
        logger.info(f"Starting mitmproxy in reverse mode on port {port}")
        logger.info(f"Forwarding to LiteLLM on port {litellm_port}")
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
            logger.info(f"Mitmproxy started with PID {process.pid}")

        except FileNotFoundError:
            logger.error("mitmdump command not found")
            logger.error("Please ensure mitmproxy is installed: uv add mitmproxy")
            sys.exit(1)

    else:
        # Run in foreground
        logger.info(f"Starting mitmproxy in reverse mode on port {port}")
        logger.info(f"Forwarding to LiteLLM on port {litellm_port}")

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


def stop_mitm(config_dir: Path) -> bool:
    """Stop the mitmproxy traffic capture proxy.

    Args:
        config_dir: Configuration directory containing the PID file

    Returns:
        True if stopped successfully, False otherwise
    """
    pid_file = get_pid_file(config_dir)

    # Check if PID file exists
    if not pid_file.exists():
        logger.error("No mitmproxy server is running (PID file not found)")
        return False

    return shared_stop_process(pid_file)


def get_mitm_status(config_dir: Path) -> dict[str, bool | int | str | None]:
    """Get the status of the mitmproxy server.

    Args:
        config_dir: Configuration directory

    Returns:
        Dictionary with status information
    """
    running, pid = is_running(config_dir)

    status: dict[str, bool | int | str | None] = {
        "running": running,
        "pid": pid,
    }

    if running:
        # Add additional information when running
        pid_file = get_pid_file(config_dir)
        log_file = get_log_file(config_dir)

        status["pid_file"] = str(pid_file)
        status["log_file"] = str(log_file) if log_file.exists() else None

    return status
