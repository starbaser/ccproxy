"""In-process mitmproxy management for inspector traffic capture.

Embeds mitmweb via the WebMaster API instead of launching a subprocess.
Addons are registered as Python objects with direct access to ccproxy config.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import socket
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mitmproxy.proxy.mode_servers import ServerInstance
    from mitmproxy.tools.web.master import WebMaster

logger = logging.getLogger(__name__)


def _find_free_udp_port() -> int:
    """Find an available UDP port by binding to port 0."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("", 0))
        return int(s.getsockname()[1])


def _check_port_alive(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class ReadySignal:
    """Mitmproxy addon that signals when servers are bound and running.

    mitmproxy's RunningHook fires after setup_servers() completes — all
    listeners (reverse, WireGuard) are bound by the time running() is called.
    This addon bridges that internal hook into an asyncio.Event that external
    code can await.
    """

    def __init__(self) -> None:
        self.event = asyncio.Event()

    async def running(self) -> None:
        self.event.set()


def _build_opts(
    litellm_port: int,
    wg_cli_conf_path: Path,
    wg_gateway_conf_path: Path,
    reverse_port: int,
    wg_cli_port: int,
    wg_gateway_port: int,
    web_token: str,
) -> Any:
    """Build mitmproxy Options from the singleton config."""
    from mitmproxy.options import Options

    from ccproxy.config import MitmproxyOptions, get_config

    config = get_config()
    inspector = config.inspector

    opts = Options(
        mode=[
            f"reverse:http://localhost:{litellm_port}@{reverse_port}",
            f"wireguard:{wg_cli_conf_path}@{wg_cli_port}",
            f"wireguard:{wg_gateway_conf_path}@{wg_gateway_port}",
        ],
    )

    # Many options (web_*, stream_large_bodies, body_size_limit, etc.) are
    # registered by addons inside WebMaster.__init__, not on Options() itself.
    # Defer ALL non-mode options so they resolve after addon registration.
    deferred: dict[str, Any] = {
        "web_port": inspector.port,
        "web_host": inspector.mitmproxy.web_host,
        "web_open_browser": inspector.mitmproxy.web_open_browser,
        "web_password": web_token,
    }
    for field_name in MitmproxyOptions.model_fields:
        value = getattr(inspector.mitmproxy, field_name)
        if value is not None:
            deferred[field_name] = value

    opts.update_defer(**deferred)  # type: ignore[no-untyped-call]

    return opts


def _make_inbound_router() -> Any:
    from ccproxy.inspector.router import InspectorRouter
    from ccproxy.inspector.routes.inbound import register_inbound_routes

    router = InspectorRouter(
        name="ccproxy_inbound", request_passthrough=True, response_passthrough=True,
    )
    register_inbound_routes(router)
    return router


def _make_outbound_router() -> Any:
    from ccproxy.inspector.router import InspectorRouter
    from ccproxy.inspector.routes.outbound import register_outbound_routes

    router = InspectorRouter(
        name="ccproxy_outbound", request_passthrough=True, response_passthrough=True,
    )
    register_outbound_routes(router)
    return router


def _build_addons(
    litellm_port: int,
    wg_cli_port: int,
    wg_gateway_port: int,
) -> list[Any]:
    """Build the addon chain from the singleton config.

    Order matters: InspectorAddon (OTel spans) must fire first, then
    inbound router (OAuth), outbound router (beta headers), then optional
    PcapAddon.
    """
    from ccproxy.config import get_config
    from ccproxy.inspector.addon import InspectorAddon

    config = get_config()
    inspector = config.inspector
    otel = config.otel

    addon = InspectorAddon(
        config=inspector,
        traffic_source=os.environ.get("CCPROXY_TRAFFIC_SOURCE") or None,
        wg_cli_port=wg_cli_port,
        wg_gateway_port=wg_gateway_port,
        litellm_port=litellm_port,
    )

    try:
        from ccproxy.inspector.telemetry import InspectorTracer

        tracer = InspectorTracer(
            enabled=otel.enabled,
            otlp_endpoint=otel.endpoint,
            service_name=otel.service_name,
        )
        addon.set_tracer(tracer)
        if otel.enabled:
            logger.info("OTel tracing enabled, exporting to %s", otel.endpoint)
    except Exception as e:
        logger.warning("Failed to initialize OTel tracer: %s", e)

    addons: list[Any] = [
        addon,
        _make_inbound_router(),
        _make_outbound_router(),
    ]

    pcap_file = os.environ.get("CCPROXY_PCAP_FILE")
    pcap_pipe = os.environ.get("CCPROXY_PCAP_PIPE")
    if pcap_file or pcap_pipe:
        from ccproxy.inspector.pcap import PcapAddon

        addons.append(PcapAddon(pcap_file=pcap_file, pcap_pipe=pcap_pipe))

    return addons


def get_wg_client_conf(master: WebMaster, keypair_path: Path) -> str | None:
    """Extract a WireGuard client config from the running proxyserver.

    Matches the WireGuardServerInstance whose mode.data path resolves to
    the given keypair_path. Returns the WireGuard INI client config string
    or None if not found.
    """
    from mitmproxy.proxy.mode_servers import WireGuardServerInstance

    proxyserver = master.addons.get("proxyserver")  # type: ignore[no-untyped-call]
    resolved = keypair_path.resolve()

    for server_instance in proxyserver.servers:
        if not isinstance(server_instance, WireGuardServerInstance):
            continue
        if Path(server_instance.mode.data).resolve() == resolved:
            return server_instance.client_conf()

    return None


def get_listen_port(server_instance: ServerInstance) -> int | None:  # type: ignore[type-arg]
    """Get the actual bound port from a running server instance."""
    addrs = server_instance.listen_addrs
    if addrs:
        return int(addrs[0][1])
    return None


async def run_inspector(
    litellm_port: int,
    *,
    wg_cli_conf_path: Path,
    wg_gateway_conf_path: Path,
    reverse_port: int,
) -> tuple[WebMaster, asyncio.Task[None], str]:
    """Start the inspector in-process via mitmproxy's WebMaster API.

    Reads InspectorConfig and OtelConfig from the singleton. Creates and
    starts a WebMaster with three listeners (reverse + 2x WireGuard),
    registers all addons directly, and waits for servers to bind.

    Returns after the running() hook fires — all ports are bound and
    WG configs are readable.

    The caller is responsible for:
    - Namespace setup using get_wg_client_conf()
    - Calling master.shutdown() when done
    - Awaiting the master_task for clean shutdown

    Returns:
        (master, master_task, web_token)
    """
    from mitmproxy.tools.web.master import WebMaster

    from ccproxy.config import get_config

    config = get_config()
    inspector = config.inspector

    wg_cli_port = _find_free_udp_port()
    wg_gateway_port = _find_free_udp_port()
    web_token = inspector.mitmproxy.web_password or secrets.token_hex(16)

    opts = _build_opts(
        litellm_port,
        wg_cli_conf_path, wg_gateway_conf_path,
        reverse_port, wg_cli_port, wg_gateway_port,
        web_token,
    )

    master = WebMaster(opts, with_termlog=True)

    ready = ReadySignal()
    addons = _build_addons(litellm_port, wg_cli_port, wg_gateway_port)
    master.addons.add(ready, *addons)  # type: ignore[no-untyped-call]

    master_task = asyncio.create_task(master.run())

    try:
        await asyncio.wait_for(ready.event.wait(), timeout=15)
    except TimeoutError as err:
        master.shutdown()  # type: ignore[no-untyped-call]
        await master_task
        raise RuntimeError("mitmweb failed to start (timeout waiting for servers to bind)") from err

    logger.info(
        "Inspector running: reverse@%d → LiteLLM@%d, wg-cli@%d, wg-gateway@%d, UI@%d",
        reverse_port, litellm_port, wg_cli_port, wg_gateway_port, inspector.port,
    )

    return master, master_task, web_token


def get_inspector_status() -> dict[str, dict[str, bool | str | None]]:
    """Get the status of the inspector process via TCP port probe."""
    from ccproxy.config import get_config

    config = get_config()
    inspector_cfg = getattr(config, "inspector", None)
    port: int = getattr(inspector_cfg, "port", 8083)

    running = _check_port_alive("127.0.0.1", port)
    status: dict[str, bool | str | None] = {"running": running}

    return {"inspector": status}
