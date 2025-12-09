"""Process management for mitmproxy traffic capture."""

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

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

    if not pid_file.exists():
        return False, None

    try:
        pid = int(pid_file.read_text().strip())

        # Check if process is actually running
        try:
            os.kill(pid, 0)  # This doesn't kill, just checks if process exists
            return True, pid
        except ProcessLookupError:
            # Process is not running, clean up stale PID file
            pid_file.unlink()
            return False, None

    except (ValueError, OSError):
        # Invalid PID file
        return False, None


def start_mitm(
    config_dir: Path,
    port: int = 8081,
    upstream: str = "http://localhost:4000",
    detach: bool = False,
) -> None:
    """Start the mitmproxy traffic capture proxy.

    Args:
        config_dir: Configuration directory for PID and log files
        port: Port for mitmproxy to listen on
        upstream: Upstream proxy URL (LiteLLM)
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

    # Build mitmdump command
    # Use upstream mode to forward traffic to LiteLLM
    cmd = [
        str(mitmdump_path),
        "--mode",
        f"upstream:{upstream}",
        "--listen-port",
        str(port),
        "--set",
        "stream_large_bodies=1m",  # Stream large bodies
        "-s",
        str(script_path),  # Load CCProxy addon
    ]

    # Pass environment to subprocess (needed for DATABASE_URL)
    env = os.environ.copy()
    env["CCPROXY_MITM_PORT"] = str(port)
    env["CCPROXY_MITM_UPSTREAM"] = upstream

    if detach:
        # Run in background mode
        logger.info(f"Starting mitmproxy in background on port {port}")
        logger.info(f"Upstream: {upstream}")
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
            pid_file.write_text(str(process.pid))
            logger.info(f"Mitmproxy started with PID {process.pid}")

        except FileNotFoundError:
            logger.error("mitmdump command not found")
            logger.error("Please ensure mitmproxy is installed: uv add mitmproxy")
            sys.exit(1)

    else:
        # Run in foreground
        logger.info(f"Starting mitmproxy on port {port}")
        logger.info(f"Upstream: {upstream}")

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

    try:
        pid = int(pid_file.read_text().strip())

        # Check if process is still running
        try:
            os.kill(pid, 0)  # Check if process exists

            # Process exists, kill it
            logger.info(f"Stopping mitmproxy server (PID: {pid})...")
            os.kill(pid, signal.SIGTERM)  # Graceful shutdown

            # Wait a moment for graceful shutdown
            time.sleep(0.5)

            # Check if still running
            try:
                os.kill(pid, 0)
                # Still running, force kill
                os.kill(pid, signal.SIGKILL)
                logger.info(f"Force killed mitmproxy server (PID: {pid})")
            except ProcessLookupError:
                logger.info(f"Mitmproxy server stopped successfully (PID: {pid})")

            # Remove PID file
            pid_file.unlink()
            return True

        except ProcessLookupError:
            # Process is not running, clean up stale PID file
            logger.warning(f"Mitmproxy server was not running (stale PID: {pid})")
            pid_file.unlink()
            return False

    except (ValueError, OSError) as e:
        logger.error(f"Error reading PID file: {e}")
        return False


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
