"""Pre-flight checks for ccproxy startup.

Ensures a clean environment before launching processes:
- Detects and kills orphaned ccproxy/mitmweb processes
- Verifies required ports are available
- Enforces single-instance constraint
"""

import logging
import os
import re
import signal
import socket
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_CCPROXY_PATTERNS: list[tuple[str, str]] = []


def _is_ccproxy_process(cmdline: str) -> bool:
    """Check if a command line string matches a ccproxy-managed process."""
    return any(binary in cmdline and marker in cmdline for binary, marker in _CCPROXY_PATTERNS)


def _read_proc_cmdline(pid: int) -> str | None:
    """Read and decode /proc/<pid>/cmdline, returning None on failure."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        return raw.replace(b"\0", b" ").decode("utf-8", errors="replace").strip()
    except (OSError, PermissionError):
        return None


def _find_inode_pids() -> dict[int, int]:
    """Build a mapping of socket inode → PID from /proc/*/fd/ symlinks."""
    inode_to_pid: dict[int, int] = {}
    proc = Path("/proc")

    try:
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            fd_dir = entry / "fd"
            try:
                for fd_link in fd_dir.iterdir():
                    try:
                        target = str(fd_link.readlink())
                        m = re.match(r"socket:\[(\d+)\]", target)
                        if m:
                            inode_to_pid[int(m.group(1))] = pid
                    except (OSError, ValueError):
                        continue
            except (OSError, PermissionError):
                continue
    except OSError:
        pass

    return inode_to_pid


def _is_udp_port_in_use(port: int) -> int | None:
    """Check if a UDP port is in use by reading /proc/net/udp.

    Returns the PID using the port, or None if the port is free.
    """
    hex_port = f"{port:04X}"
    bound_inodes: set[int] = set()

    for udp_path in ("/proc/net/udp", "/proc/net/udp6"):
        try:
            with Path(udp_path).open() as f:
                for line in f:
                    fields = line.split()
                    if len(fields) < 10:
                        continue
                    local_addr = fields[1]
                    port_hex = local_addr.rsplit(":", 1)[-1]
                    if port_hex == hex_port:
                        bound_inodes.add(int(fields[9]))
        except OSError:
            continue

    if not bound_inodes:
        return None

    inode_to_pid = _find_inode_pids()
    for inode in bound_inodes:
        pid = inode_to_pid.get(inode)
        if pid is not None:
            return pid

    # Inode found but couldn't resolve to PID (permission issue)
    return -1


def get_port_pid(port: int, host: str = "127.0.0.1") -> tuple[int | None, str | None]:
    """Find which process is listening on a port.

    Parses /proc/net/tcp{,6} and correlates socket inodes to PIDs.
    Falls back to a socket bind test if /proc is unavailable.

    Returns:
        (pid, cmdline_snippet) if occupied, (None, None) if free.
        pid=-1 means occupied but PID unknown (fallback path).
    """
    hex_port = f"{port:04X}"
    # 0100007F = 127.0.0.1, 00000000 = 0.0.0.0
    listen_addrs = {"0100007F", "00000000"}
    if host == "0.0.0.0":
        listen_addrs = {"00000000"}

    listening_inodes: set[int] = set()

    for tcp_path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with Path(tcp_path).open() as f:
                for line in f:
                    fields = line.split()
                    if len(fields) < 10:
                        continue
                    local_addr = fields[1]
                    state = fields[3]
                    # state 0A = LISTEN
                    if state != "0A":
                        continue
                    addr_hex, port_hex = local_addr.split(":")
                    if port_hex == hex_port:
                        # For tcp6, check if it's a v4-mapped address or wildcard
                        if tcp_path.endswith("6"):
                            # ::ffff:127.0.0.1 or :: (wildcard)
                            if addr_hex in (
                                "00000000000000000000FFFF0100007F",
                                "00000000000000000000000000000000",
                            ):
                                listening_inodes.add(int(fields[9]))
                        elif addr_hex in listen_addrs:
                            listening_inodes.add(int(fields[9]))
        except OSError:
            continue

    if not listening_inodes:
        # Double-check with socket bind as a safety net
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((host, port))
                return None, None
        except OSError:
            return -1, "unknown"

    inode_to_pid = _find_inode_pids()
    for inode in listening_inodes:
        pid = inode_to_pid.get(inode)
        if pid is not None:
            cmdline = _read_proc_cmdline(pid)
            snippet = (cmdline[:80] + "...") if cmdline and len(cmdline) > 80 else cmdline
            return pid, snippet

    # Inode found but couldn't resolve to PID (permission issue)
    return -1, "unknown"


def find_ccproxy_processes(exclude_pid: int | None = None) -> list[tuple[int, str]]:
    """Scan /proc for orphaned ccproxy-managed processes."""
    exclude = {exclude_pid, os.getppid()} if exclude_pid else {os.getppid()}
    results: list[tuple[int, str]] = []

    try:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            if pid in exclude:
                continue
            cmdline = _read_proc_cmdline(pid)
            if cmdline and _is_ccproxy_process(cmdline):
                results.append((pid, cmdline))
    except OSError as e:
        logger.warning(f"Error scanning /proc: {e}")

    return results


def kill_stale_processes(processes: list[tuple[int, str]]) -> int:
    """Kill a list of processes with SIGTERM → SIGKILL fallback."""
    killed = 0
    for pid, cmdline in processes:
        snippet = (cmdline[:80] + "...") if len(cmdline) > 80 else cmdline
        try:
            logger.warning(f"Killing stale process PID {pid}: {snippet}")
            os.kill(pid, signal.SIGTERM)
            time.sleep(0.3)
            try:
                os.kill(pid, 0)
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            killed += 1
        except ProcessLookupError:
            killed += 1  # Already dead
        except PermissionError:
            logger.error(f"No permission to kill PID {pid}")
        except OSError as e:
            logger.error(f"Failed to kill PID {pid}: {e}")

    return killed


def _cleanup_stale_wireguard_confs(config_dir: Path) -> None:
    """Remove wireguard.{pid}.conf files whose owning process no longer exists."""
    for wg_file in config_dir.glob("wireguard.*.conf"):
        stem = wg_file.stem
        parts = stem.split(".")
        if len(parts) == 2 and parts[1].isdigit():
            pid = int(parts[1])
            if not Path(f"/proc/{pid}").exists():
                logger.info("Removing stale WireGuard keypair: %s (PID %d dead)", wg_file.name, pid)
                wg_file.unlink(missing_ok=True)


def run_preflight_checks(
    ports: list[int] | None = None,
    udp_ports: list[int] | None = None,
    config_dir: Path | None = None,
) -> None:
    """Run pre-flight checks before starting ccproxy.

    Verifies required TCP and UDP ports are free; kills stale ccproxy processes
    found on those TCP ports. Only targets processes on the specific configured
    ports — other ccproxy instances are left alone.

    Raises:
        SystemExit: On unrecoverable conflicts.
    """
    logger.debug("Running pre-flight checks...")

    if config_dir is not None:
        _cleanup_stale_wireguard_confs(config_dir)

    # TCP port availability — kill stale ccproxy processes on configured ports
    for port in ports or []:
        pid, snippet = get_port_pid(port)
        if pid is None:
            logger.debug(f"Port {port} is available")
            continue

        if pid == -1:
            print(f"Error: Port {port} is already in use (could not identify process)")
            raise SystemExit(1)

        # Check if the port holder is a stale ccproxy process we missed
        cmdline = _read_proc_cmdline(pid)
        if cmdline and _is_ccproxy_process(cmdline):
            logger.warning(f"Port {port} held by stale ccproxy process (PID {pid})")
            kill_stale_processes([(pid, cmdline)])
            time.sleep(0.3)
            check_pid, _ = get_port_pid(port)
            if check_pid is not None:
                print(f"Error: Failed to free port {port} (PID {pid} still holding it)")
                raise SystemExit(1)
        else:
            name = snippet or "unknown"
            print(f"Error: Port {port} is occupied by another process (PID {pid}: {name})")
            print(f"Stop it first, e.g.: kill {pid}")
            raise SystemExit(1)

    # UDP port availability
    for port in udp_ports or []:
        pid = _is_udp_port_in_use(port)
        if pid is None:
            logger.debug(f"UDP port {port} is available")
            continue

        if pid == -1:
            print(f"Error: UDP port {port} is already in use (could not identify process)")
            raise SystemExit(1)

        cmdline = _read_proc_cmdline(pid)
        snippet = (cmdline[:80] + "...") if cmdline and len(cmdline) > 80 else cmdline
        name = snippet or "unknown"
        print(f"Error: UDP port {port} is occupied by another process (PID {pid}: {name})")
        print(f"Stop it first, e.g.: kill {pid}")
        raise SystemExit(1)

    logger.debug("Pre-flight checks passed")
