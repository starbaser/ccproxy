"""Network namespace confinement for transparent traffic capture.

Creates an isolated network namespace with a WireGuard client routed through
mitmproxy's WireGuard server. All traffic from the confined process flows
through the tunnel and is captured transparently.

Requires: unshare, nsenter, slirp4netns, ip, wg (all rootless on Linux 5.6+
with unprivileged_userns_clone=1).
"""

import dataclasses
import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
from pathlib import Path

from ccproxy.inspector.process import _pipe_output

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

    port_forwarder: "PortForwarder | None" = None
    """Background thread forwarding namespace listen ports to the host."""


def _parse_proc_net_tcp(path: Path) -> set[int]:
    """Return TCP LISTEN ports on localhost or wildcard from a /proc/net/tcp file.

    The sentinel PID's /proc/{pid}/net/tcp exposes the namespace's socket table.
    """
    ports: set[int] = set()
    try:
        content = path.read_text()
    except OSError:
        return ports

    for line in content.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 4:
            continue
        state = parts[3]
        if state != "0A":  # LISTEN
            continue
        host_hex, port_hex = parts[1].split(":")
        if host_hex not in ("0100007F", "00000000"):  # localhost, wildcard
            continue
        port = int(port_hex, 16)
        if port < 1024:
            continue
        ports.add(port)

    return ports


def _slirp_add_hostfwd(api_socket: Path, port: int) -> bool:
    """Forward host 127.0.0.1:port → namespace 10.0.2.100:port via slirp4netns API."""
    request = json.dumps({
        "execute": "add_hostfwd",
        "arguments": {
            "proto": "tcp",
            "host_addr": "127.0.0.1",
            "host_port": port,
            "guest_addr": "10.0.2.100",
            "guest_port": port,
        },
    }).encode()

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(2.0)
            s.connect(str(api_socket))
            s.sendall(request + b"\n")
            data = b""
            while b"\n" not in data:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
    except OSError as e:
        logger.warning("slirp4netns API unavailable for port %d: %s", port, e)
        return False

    try:
        response = json.loads(data.strip())
    except json.JSONDecodeError:
        logger.warning("slirp4netns returned malformed JSON for port %d", port)
        return False

    if "error" in response:
        logger.warning(
            "slirp4netns refused hostfwd for port %d: %s",
            port,
            response["error"].get("desc", response["error"]),
        )
        return False

    logger.info("Port forwarding active: host 127.0.0.1:%d → namespace 127.0.0.1:%d", port, port)
    return True


class PortForwarder:
    """Monitors namespace TCP sockets and forwards new LISTEN ports to the host."""

    def __init__(self, ns_pid: int, api_socket: Path, poll_interval: float = 0.5) -> None:
        self._proc_tcp_path = Path(f"/proc/{ns_pid}/net/tcp")
        self._api_socket = api_socket
        self._poll_interval = poll_interval
        self._stop_event = threading.Event()
        self._attempted: set[int] = set()
        self._thread = threading.Thread(target=self._run, daemon=True, name="port-forwarder")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def _run(self) -> None:
        logger.debug("PortForwarder started")
        while not self._stop_event.wait(self._poll_interval):
            try:
                self._poll()
            except Exception:
                logger.debug("PortForwarder poll error", exc_info=True)
        logger.debug("PortForwarder stopped")

    def _poll(self) -> None:
        current = _parse_proc_net_tcp(self._proc_tcp_path)
        for port in current - self._attempted:
            self._attempted.add(port)
            _slirp_add_hostfwd(self._api_socket, port)


def _rewrite_wg_endpoint(client_conf: str, gateway: str) -> str:
    """Rewrite the Endpoint and strip wg-quick-only fields.

    Replaces the Endpoint host with the slirp4netns gateway address (preserving
    the port mitmweb chose) and removes Address/DNS lines (wg-quick extensions
    not understood by `wg setconf`).
    """
    # Strip wg-quick-only fields that `wg setconf` doesn't understand
    conf = re.sub(r"^(?:Address|DNS)\s*=.*\n?", "", client_conf, flags=re.MULTILINE)
    # Rewrite endpoint host to the namespace-reachable gateway, keep the port
    def _replace_endpoint(m: re.Match[str]) -> str:
        port = m.group(1)
        return f"Endpoint = {gateway}:{port}"
    return re.sub(
        r"^Endpoint\s*=\s*\S+:(\d+)\s*$",
        _replace_endpoint,
        conf,
        flags=re.MULTILINE,
    )


def create_namespace(wg_client_conf: str) -> NamespaceContext:
    """Create a user+net namespace with WireGuard routing through mitmproxy.

    Network topology (slirp4netns --configure):
      - Namespace TAP IP: 10.0.2.100/24
      - Gateway (host): 10.0.2.2
      - DNS forwarder: 10.0.2.3

    Args:
        wg_client_conf: WireGuard client config INI from mitmweb (contains
            the server endpoint with the auto-assigned port)

    Returns:
        NamespaceContext with all resources for cleanup

    Raises:
        RuntimeError: If namespace setup fails at any step
    """
    gateway = "10.0.2.2"

    # Rewrite endpoint host to the slirp4netns gateway (port preserved from config)
    modified_conf = _rewrite_wg_endpoint(wg_client_conf, gateway)
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
    api_socket_path = Path(tempfile.gettempdir()) / f"ccproxy-slirp-{ns_pid}.sock"

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
            f"--api-socket={api_socket_path}",
            str(ns_pid),
            "tap0",
        ]
        slirp_proc = subprocess.Popen(
            slirp_cmd,
            pass_fds=(ready_w, exit_r),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        _pipe_output(slirp_proc, "slirp4netns")

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

        logger.info("Namespace created: WireGuard tunnel active via %s", gateway)

        # Set up iptables DNAT so slirp4netns hostfwd traffic reaches localhost servers
        if shutil.which("iptables"):
            dnat_cmd = (
                "iptables -t nat -A PREROUTING -i tap0 -p tcp "
                "-j DNAT --to-destination 127.0.0.1"
            )
            dnat_result = subprocess.run(
                ["nsenter", "-t", str(ns_pid), "--net", "--user",
                 "--preserve-credentials", "--", "sh", "-c", dnat_cmd],
                capture_output=True,
                text=True,
            )
            if dnat_result.returncode != 0:
                logger.warning(
                    "iptables DNAT setup failed (port forwarding disabled): %s",
                    dnat_result.stderr.strip(),
                )
            else:
                logger.debug("iptables DNAT rule installed on tap0")
        else:
            logger.warning(
                "iptables not found — OAuth callback port forwarding unavailable"
            )

        # Start port monitor to dynamically forward namespace listen ports to host
        forwarder = PortForwarder(ns_pid=ns_pid, api_socket=api_socket_path)
        forwarder.start()

        return NamespaceContext(
            ns_pid=ns_pid,
            slirp_proc=slirp_proc,
            exit_w=exit_w,
            wg_conf_path=conf_path,
            api_socket=api_socket_path,
            port_forwarder=forwarder,
        )

    except Exception:
        # Cleanup on failure
        _safe_close(exit_w)
        _safe_close(exit_r)
        _safe_close(ready_r)
        _safe_close(ready_w)
        _safe_kill(ns_pid)
        conf_path.unlink(missing_ok=True)
        api_socket_path.unlink(missing_ok=True)
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
    if ctx.port_forwarder is not None:
        ctx.port_forwarder.stop()

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
