"""Network namespace confinement for transparent traffic capture.

Creates an isolated network namespace with a WireGuard client routed through
mitmproxy's WireGuard server. All traffic from the confined process flows
through the tunnel and is captured transparently.

Requires: unshare, nsenter, slirp4netns, ip, wg (all rootless on Linux 5.6+
with unprivileged_userns_clone=1).
"""

import dataclasses
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


def check_namespace_capabilities() -> list[str]:
    """Validate prerequisites for namespace-based inspection.

    Returns empty list if all capabilities are present, or a list of
    human-readable problem descriptions.
    """
    problems = []

    userns_path = Path("/proc/sys/kernel/unprivileged_userns_clone")
    if userns_path.exists():
        try:
            val = userns_path.read_text().strip()
            if val != "1":
                problems.append(
                    "Unprivileged user namespaces disabled "
                    "(kernel.unprivileged_userns_clone=0). "
                    "Enable with: sysctl -w kernel.unprivileged_userns_clone=1"
                )
        except OSError:
            pass

    required_tools = {
        "slirp4netns": "nix profile install nixpkgs#slirp4netns",
        "unshare": "nix profile install nixpkgs#util-linux",
        "nsenter": "nix profile install nixpkgs#util-linux",
        "ip": "nix profile install nixpkgs#iproute2",
        "wg": "nix profile install nixpkgs#wireguard-tools",
    }
    for tool, install_hint in required_tools.items():
        if not shutil.which(tool):
            problems.append(f"{tool} not found. Install with: {install_hint}")

    return problems


@dataclasses.dataclass
class NamespaceContext:
    """Tracks resources for a confined network namespace."""

    ns_pid: int
    """PID of the sleep-infinity sentinel process inside the namespace."""

    slirp_proc: subprocess.Popen[bytes]
    """The slirp4netns bridge process."""

    exit_w: int
    """Write end of the exit-fd pipe. Close to trigger clean slirp4netns shutdown."""

    wg_conf_path: Path
    """Temp file with the modified WireGuard client config."""

    api_socket: Path | None = None
    """slirp4netns API socket path (for cleanup)."""


def _rewrite_wg_endpoint(client_conf: str, gateway: str, wg_port: int) -> str:
    """Rewrite the Endpoint and strip wg-quick-only fields.

    Replaces the original Endpoint with the slirp4netns gateway address and
    removes Address/DNS lines (wg-quick extensions not understood by `wg setconf`).
    """
    # Strip wg-quick-only fields that `wg setconf` doesn't understand
    conf = re.sub(r"^(?:Address|DNS)\s*=.*\n?", "", client_conf, flags=re.MULTILINE)
    # Rewrite endpoint to the namespace-reachable gateway
    return re.sub(
        r"^Endpoint\s*=\s*.*$",
        f"Endpoint = {gateway}:{wg_port}",
        conf,
        flags=re.MULTILINE,
    )


def create_namespace(wg_client_conf: str, wg_port: int) -> NamespaceContext:
    """Create a user+net namespace with WireGuard routing through mitmproxy.

    Network topology (slirp4netns --configure):
      - Namespace TAP IP: 10.0.2.100/24
      - Gateway (host): 10.0.2.2
      - DNS forwarder: 10.0.2.3

    Args:
        wg_client_conf: WireGuard client config INI from mitmweb
        wg_port: WireGuard server port on the host

    Returns:
        NamespaceContext with all resources for cleanup

    Raises:
        RuntimeError: If namespace setup fails at any step
    """
    gateway = "10.0.2.2"

    # Write modified client config with namespace-reachable endpoint
    modified_conf = _rewrite_wg_endpoint(wg_client_conf, gateway, wg_port)
    conf_fd, conf_path_str = tempfile.mkstemp(suffix=".conf", prefix="ccproxy-wg-")
    conf_path = Path(conf_path_str)
    try:
        with os.fdopen(conf_fd, "w") as f:
            f.write(modified_conf)
    except Exception:
        conf_path.unlink(missing_ok=True)
        raise

    # Start sentinel process in a new user+net namespace
    try:
        sentinel = subprocess.Popen(
            ["unshare", "--user", "--map-root-user", "--net", "--pid", "--fork",
             "sleep", "infinity"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        conf_path.unlink(missing_ok=True)
        raise RuntimeError("Failed to create network namespace (unshare)")

    ns_pid = sentinel.pid

    # Create pipes for slirp4netns lifecycle management
    ready_r, ready_w = os.pipe()
    exit_r, exit_w = os.pipe()

    try:
        # Start slirp4netns bridge
        slirp_cmd = [
            "slirp4netns",
            "--configure",
            "--mtu=65520",
            f"--ready-fd={ready_w}",
            f"--exit-fd={exit_r}",
            str(ns_pid),
            "tap0",
        ]
        slirp_proc = subprocess.Popen(
            slirp_cmd,
            pass_fds=(ready_w, exit_r),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # Close the FDs that slirp4netns now owns
        os.close(ready_w)
        ready_w = -1
        os.close(exit_r)
        exit_r = -1

        # Block until slirp4netns signals readiness
        with os.fdopen(ready_r, "r") as ready_file:
            ready_data = ready_file.read()
        ready_r = -1  # fdopen closed it

        if not ready_data.strip():
            raise RuntimeError("slirp4netns failed to become ready")

        logger.debug("slirp4netns ready, configuring WireGuard in namespace")

        # Configure WireGuard inside the namespace
        # lo and tap0 are already configured by slirp4netns --configure
        wg_setup = (
            f"ip link add wg0 type wireguard && "
            f"wg setconf wg0 {conf_path} && "
            f"ip addr add 10.0.0.1/32 dev wg0 && "
            f"ip link set wg0 up && "
            f"ip route del default && "
            f"ip route add default dev wg0"
        )
        result = subprocess.run(
            ["nsenter", "-t", str(ns_pid), "--net", "--user", "--preserve-credentials", "--",
             "sh", "-c", wg_setup],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            raise RuntimeError(f"WireGuard setup failed in namespace: {stderr}")

        logger.info("Namespace created: WireGuard tunnel active via %s:%d", gateway, wg_port)

        return NamespaceContext(
            ns_pid=ns_pid,
            slirp_proc=slirp_proc,
            exit_w=exit_w,
            wg_conf_path=conf_path,
        )

    except Exception:
        # Cleanup on failure
        _safe_close(exit_w)
        _safe_close(exit_r)
        _safe_close(ready_r)
        _safe_close(ready_w)
        _safe_kill(ns_pid)
        conf_path.unlink(missing_ok=True)
        raise


def run_in_namespace(ctx: NamespaceContext, command: list[str], env: dict[str, str]) -> int:
    """Run a command inside the confined namespace.

    Args:
        ctx: Active namespace context from create_namespace()
        command: Command and arguments to execute
        env: Environment variables for the subprocess

    Returns:
        Exit code of the confined process
    """
    nsenter_cmd = [
        "nsenter",
        "-t", str(ctx.ns_pid),
        "--net", "--user", "--preserve-credentials",
        "--", *command,
    ]
    try:
        proc = subprocess.Popen(nsenter_cmd, env=env)
        return proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        try:
            return proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            return 130


def cleanup_namespace(ctx: NamespaceContext) -> None:
    """Tear down a confined namespace and all associated resources.

    Uses exit-fd for clean slirp4netns shutdown (preferred over SIGTERM
    which leaves the API socket file behind).
    """
    # Close exit-fd pipe → slirp4netns detects HUP, exits cleanly
    _safe_close(ctx.exit_w)
    ctx.exit_w = -1

    # Wait for slirp4netns to exit
    try:
        ctx.slirp_proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        ctx.slirp_proc.kill()
        ctx.slirp_proc.wait(timeout=2)

    # Kill the namespace sentinel
    _safe_kill(ctx.ns_pid)

    # Clean up temp files
    ctx.wg_conf_path.unlink(missing_ok=True)
    if ctx.api_socket:
        ctx.api_socket.unlink(missing_ok=True)


def _safe_close(fd: int) -> None:
    """Close a file descriptor, ignoring errors."""
    if fd >= 0:
        try:
            os.close(fd)
        except OSError:
            pass


def _safe_kill(pid: int) -> None:
    """Kill a process, ignoring errors if already dead."""
    try:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
    except (ProcessLookupError, ChildProcessError, OSError):
        pass
