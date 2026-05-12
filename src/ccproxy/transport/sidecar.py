"""In-process HTTP sidecar that forwards requests via curl-cffi impersonation.

mitmproxy reverse-proxies through this sidecar when a flow needs TLS+HTTP/2
fingerprint impersonation. The two-header contract on the incoming request:

- ``X-CCProxy-Target-Url`` — real upstream URL (scheme + host + path).
- ``X-CCProxy-Impersonate`` — ``curl-cffi`` impersonate profile name.

The sidecar strips those, forwards everything else through the cached
``httpx.AsyncClient`` from :mod:`ccproxy.transport.dispatch`, and streams the
response body back chunk-by-chunk. mitmproxy's existing streaming pipeline
handles relaying chunks to the client unchanged.

Lifecycle: :class:`Sidecar` binds 127.0.0.1 on an OS-picked port at
:meth:`Sidecar.start`. :attr:`Sidecar.port` exposes the bound port for the
``TransportOverrideAddon`` to rewrite ``flow.request`` against.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import AsyncIterator
from urllib.parse import urlsplit

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from ccproxy import transport

logger = logging.getLogger(__name__)

TARGET_URL_HEADER = "x-ccproxy-target-url"
IMPERSONATE_HEADER = "x-ccproxy-impersonate"

_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)
"""Hop-by-hop headers per RFC 7230 §6.1 plus ``host``/``content-length``,
which are set by the outbound client based on the rewritten target."""


def _filter_headers(headers: list[tuple[bytes, bytes]], drop: frozenset[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in headers:
        name = k.decode("latin-1").lower()
        if name in drop:
            continue
        out[k.decode("latin-1")] = v.decode("latin-1")
    return out


def _filter_response_headers(headers: list[tuple[bytes, bytes]]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for k, v in headers:
        name = k.decode("latin-1").lower()
        if name in _HOP_BY_HOP:
            continue
        out.append((k.decode("latin-1"), v.decode("latin-1")))
    return out


async def _handle(request: Request) -> Response:
    """Forward one request through the impersonating transport."""
    target_url = request.headers.get(TARGET_URL_HEADER)
    profile = request.headers.get(IMPERSONATE_HEADER)
    if not target_url or not profile:
        return Response(
            f"missing {TARGET_URL_HEADER} or {IMPERSONATE_HEADER}",
            status_code=400,
        )

    parsed = urlsplit(target_url)
    host = parsed.hostname
    if host is None:
        return Response(f"invalid target URL: {target_url!r}", status_code=400)

    drop = _HOP_BY_HOP | {TARGET_URL_HEADER, IMPERSONATE_HEADER}
    fwd_headers = _filter_headers(list(request.headers.raw), drop)
    body = await request.body()

    try:
        client = await transport.get_client(host=host, profile=profile)
    except transport.UnknownFingerprintProfileError as e:
        return Response(str(e), status_code=400)

    try:
        upstream = await client.send(
            client.build_request(
                method=request.method,
                url=target_url,
                headers=fwd_headers,
                content=body,
            ),
            stream=True,
        )
    except Exception as e:
        logger.warning("sidecar: transport error for %s: %s", target_url, e)
        return Response(f"transport error: {e}", status_code=502)

    async def body_stream() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(
        body_stream(),
        status_code=upstream.status_code,
        headers=dict(_filter_response_headers(list(upstream.headers.raw))),
    )


def _build_app() -> Starlette:
    methods = ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]
    return Starlette(routes=[Route("/{path:path}", _handle, methods=methods)])


class Sidecar:
    """In-process HTTP sidecar lifecycle.

    Run :meth:`start` once during inspector boot; :attr:`port` is then the
    bound TCP port to rewrite ``flow.request`` destinations against. Call
    :meth:`stop` during shutdown — it ends the server cleanly and joins the
    background task.
    """

    def __init__(self) -> None:
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None
        self._port: int | None = None
        self._sock: socket.socket | None = None

    @property
    def port(self) -> int:
        if self._port is None:
            raise RuntimeError("sidecar not started")
        return self._port

    async def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        self._sock = sock
        self._port = sock.getsockname()[1]

        config = uvicorn.Config(
            app=_build_app(),
            log_level="warning",
            lifespan="off",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(
            self._server.serve(sockets=[sock]),
            name="ccproxy-sidecar",
        )

        deadline = asyncio.get_running_loop().time() + 5.0
        while not self._server.started:
            if asyncio.get_running_loop().time() > deadline:
                raise RuntimeError("sidecar failed to bind within 5s")
            if self._task.done():
                exc = self._task.exception()
                raise RuntimeError(f"sidecar serve() exited prematurely: {exc!r}") from exc
            await asyncio.sleep(0.01)

        logger.info("sidecar listening on 127.0.0.1:%d", self._port)

    async def stop(self) -> None:
        if self._server is None or self._task is None:
            return
        self._server.should_exit = True
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except TimeoutError:
            logger.warning("sidecar: shutdown timeout, cancelling")
            self._task.cancel()
        finally:
            self._server = None
            self._task = None
            self._sock = None
            self._port = None
