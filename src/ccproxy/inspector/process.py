"""In-process mitmproxy management for inspector traffic capture.

Embeds mitmweb via the WebMaster API.
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

from ccproxy.config import CredentialSource, MitmproxyOptions, get_config

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
    Exposes an asyncio.Event that external code can await.
    """

    def __init__(self) -> None:
        self.event = asyncio.Event()

    async def running(self) -> None:
        self.event.set()


def _build_opts(
    wg_cli_conf_path: Path,
    reverse_port: int,
    wg_cli_port: int,
) -> Any:
    # deferred: heavy mitmproxy Options import
    from mitmproxy.options import Options

    config = get_config()
    inspector = config.inspector

    opts = Options(
        mode=[
            f"reverse:http://localhost:1@{reverse_port}",
            f"wireguard:{wg_cli_conf_path}@{wg_cli_port}",
        ],
    )

    # Many options (web_*, stream_large_bodies, body_size_limit, etc.) are
    # registered by addons inside WebMaster.__init__, not on Options() itself.
    # Defer ALL non-mode options so they resolve after addon registration.
    deferred: dict[str, Any] = {}
    for field_name in MitmproxyOptions.model_fields:
        if field_name == "web_password":
            continue
        value = getattr(inspector.mitmproxy, field_name)
        if value is not None:
            deferred[field_name] = value

    deferred["web_port"] = inspector.port
    deferred["store_streamed_bodies"] = True

    opts.update_defer(**deferred)  # type: ignore[no-untyped-call]

    return opts


def _make_pipeline_router(name: str, hook_entries: list[Any]) -> Any:
    """Build a DAG-driven pipeline router from config hook entries."""
    # deferred: heavy pipeline + hook registry chain
    from ccproxy.inspector.pipeline import build_executor, register_pipeline_routes
    from ccproxy.inspector.router import InspectorRouter

    router = InspectorRouter(
        name=name,
        request_passthrough=True,
        response_passthrough=True,
    )
    executor = build_executor(hook_entries)
    register_pipeline_routes(router, executor)
    return router


def _make_transform_router() -> Any:
    # deferred: heavy mitmproxy router chain
    from ccproxy.inspector.router import InspectorRouter
    from ccproxy.inspector.routes.transform import register_transform_routes

    router = InspectorRouter(
        name="ccproxy_transform",
        request_passthrough=True,
        response_passthrough=True,
    )
    register_transform_routes(router)
    return router


def _build_addons(
    wg_cli_port: int,
) -> list[Any]:
    """Addon order: InspectorAddon (OTel, flow records) → inbound pipeline (OAuth,
    session extraction) → transform (lightllm) → outbound pipeline
    (beta headers, identity injection).
    """
    # deferred: heavy mitmproxy addon chain
    from mitmproxy import contentviews

    from ccproxy.inspector.addon import InspectorAddon
    from ccproxy.inspector.contentview import ClientRequestContentview, ProviderResponseContentview
    from ccproxy.inspector.multi_har_saver import MultiHARSaver
    from ccproxy.inspector.shape_capturer import ShapeCapturer

    contentviews.add(ClientRequestContentview())
    contentviews.add(ProviderResponseContentview())

    config = get_config()
    otel = config.otel
    hooks_cfg = config.hooks

    addon = InspectorAddon(
        traffic_source=os.environ.get("CCPROXY_TRAFFIC_SOURCE") or None,
        wg_cli_port=wg_cli_port,
    )

    try:
        # deferred: optional OTel dependency
        from ccproxy.inspector.telemetry import InspectorTracer

        tracer = InspectorTracer(
            enabled=otel.enabled,
            otlp_endpoint=otel.endpoint,
            service_name=otel.service_name,
            provider_map=config.inspector.provider_map,
        )
        addon.set_tracer(tracer)
        if otel.enabled:
            logger.info("OTel tracing enabled, exporting to %s", otel.endpoint)
    except Exception as e:
        logger.warning("Failed to initialize OTel tracer: %s", e)

    # Initialize shape store (fail-fast if path is unwritable)
    if config.shaping.enabled:
        try:
            # deferred: optional shaping subsystem
            from ccproxy.shaping.store import get_store

            get_store()
            logger.info("Shape store initialized")
        except Exception as e:
            logger.warning("Failed to initialize shape store: %s", e)

    # Split hooks config into inbound/outbound stages
    inbound_hooks = hooks_cfg.get("inbound", []) if isinstance(hooks_cfg, dict) else hooks_cfg
    outbound_hooks = hooks_cfg.get("outbound", []) if isinstance(hooks_cfg, dict) else []

    addons: list[Any] = [addon, MultiHARSaver(), ShapeCapturer()]

    if inbound_hooks:
        addons.append(_make_pipeline_router("ccproxy_inbound", inbound_hooks))

    addons.append(_make_transform_router())

    if outbound_hooks:
        addons.append(_make_pipeline_router("ccproxy_outbound", outbound_hooks))

    return addons


def get_wg_client_conf(master: WebMaster, keypair_path: Path) -> str | None:
    """Extract a WireGuard client config from the running proxyserver.

    Matches the WireGuardServerInstance whose mode.data path resolves to
    the given keypair_path. Returns the WireGuard INI client config string
    or None if not found.
    """
    # deferred: heavy mitmproxy server import
    from mitmproxy.proxy.mode_servers import WireGuardServerInstance

    proxyserver = master.addons.get("proxyserver")  # type: ignore[no-untyped-call]
    resolved = keypair_path.resolve()

    for server_instance in proxyserver.servers:  # pyright: ignore[reportUnknownMemberType,reportOptionalMemberAccess,reportUnknownVariableType]
        if not isinstance(server_instance, WireGuardServerInstance):
            continue
        if Path(server_instance.mode.data).resolve() == resolved:
            return server_instance.client_conf()

    return None


def get_listen_port(server_instance: ServerInstance) -> int | None:  # type: ignore[type-arg]
    addrs = server_instance.listen_addrs
    if addrs:
        return int(addrs[0][1])
    return None


async def run_inspector(
    *,
    wg_cli_conf_path: Path,
    reverse_port: int,
) -> tuple[WebMaster, asyncio.Task[None], str]:
    """Start the inspector in-process via mitmproxy's WebMaster API.

    Creates a WebMaster with two listeners (reverse + WireGuard), registers
    all addons, and waits for servers to bind. Returns after the running()
    hook fires — all ports are bound and WG configs are readable.
    """
    # deferred: heavy mitmproxy WebMaster import
    from mitmproxy.tools.web.master import WebMaster

    config = get_config()
    inspector = config.inspector

    wg_cli_port = _find_free_udp_port()
    web_password_cfg = inspector.mitmproxy.web_password
    if isinstance(web_password_cfg, str):
        web_token = web_password_cfg
    elif web_password_cfg is not None:
        if isinstance(web_password_cfg, CredentialSource):
            source = web_password_cfg
        else:
            source = CredentialSource(**web_password_cfg)
        web_token = source.resolve("mitmweb web_password") or secrets.token_hex(16)
        logger.info("Resolved mitmweb web_password from credential source")
    else:
        web_token = secrets.token_hex(16)
        logger.info("Generated random mitmweb web_password")

    opts = _build_opts(
        wg_cli_conf_path,
        reverse_port,
        wg_cli_port,
    )

    master = WebMaster(opts, with_termlog=False)

    # web_password must be set via opts.update() AFTER WebMaster creation —
    # update_defer doesn't trigger WebAuth.configure for this option.
    opts.update(web_password=web_token)

    ready = ReadySignal()
    addons = _build_addons(wg_cli_port)
    master.addons.add(ready, *addons)  # type: ignore[no-untyped-call]

    master_task = asyncio.create_task(master.run())

    try:
        await asyncio.wait_for(ready.event.wait(), timeout=15)
    except TimeoutError as err:
        master.shutdown()  # type: ignore[no-untyped-call]
        await master_task
        raise RuntimeError("mitmweb failed to start (timeout waiting for servers to bind)") from err

    logger.info(
        "Inspector running: reverse@%d, wg-cli@%d, UI@%d",
        reverse_port,
        wg_cli_port,
        inspector.port,
    )

    return master, master_task, web_token


def get_inspector_status() -> dict[str, dict[str, bool | str | None]]:
    """Get the status of the inspector process via TCP port probe."""
    config = get_config()
    inspector_cfg = getattr(config, "inspector", None)
    port: int = getattr(inspector_cfg, "port", 8083)

    running = _check_port_alive("127.0.0.1", port)
    status: dict[str, bool | str | None] = {"running": running}

    return {"inspector": status}
