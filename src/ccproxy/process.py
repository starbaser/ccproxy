"""Shared process management utilities."""

import logging
import os
import signal
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def is_process_running(pid_file: Path) -> tuple[bool, int | None]:
    """Check if process is running, clean up stale PID file if not.

    Args:
        pid_file: Path to PID file

    Returns:
        Tuple of (is_running, pid or None)
    """
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


def read_pid(pid_file: Path) -> int | None:
    """Read PID from file, return None if invalid/missing.

    Args:
        pid_file: Path to PID file

    Returns:
        PID as integer or None if invalid/missing
    """
    if not pid_file.exists():
        return None

    try:
        return int(pid_file.read_text().strip())
    except (ValueError, OSError):
        return None


def stop_process(pid_file: Path, graceful_timeout: float = 0.5) -> bool:
    """Stop process: SIGTERM → wait → SIGKILL. Returns True if stopped.

    Args:
        pid_file: Path to PID file
        graceful_timeout: Seconds to wait for graceful shutdown

    Returns:
        True if process was stopped, False if not running or error
    """
    if not pid_file.exists():
        return False

    pid = read_pid(pid_file)
    if pid is None:
        return False

    try:
        # Check if process is running
        os.kill(pid, 0)

        # Process exists, attempt graceful shutdown
        logger.info(f"Stopping process (PID: {pid})...")
        os.kill(pid, signal.SIGTERM)

        # Wait for graceful shutdown
        time.sleep(graceful_timeout)

        # Check if still running
        try:
            os.kill(pid, 0)
            # Still running, force kill
            os.kill(pid, signal.SIGKILL)
            logger.info(f"Force killed process (PID: {pid})")
        except ProcessLookupError:
            logger.info(f"Process stopped successfully (PID: {pid})")

        # Remove PID file
        pid_file.unlink()
        return True

    except ProcessLookupError:
        # Process is not running, clean up stale PID file
        logger.warning(f"Process was not running (stale PID: {pid})")
        pid_file.unlink()
        return False
    except OSError as e:
        logger.error(f"Error stopping process: {e}")
        return False


def write_pid(pid_file: Path, pid: int) -> None:
    """Write PID to file.

    Args:
        pid_file: Path to PID file
        pid: Process ID to write
    """
    pid_file.write_text(str(pid))
