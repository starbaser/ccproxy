"""Tests for ccproxy.transport.sidecar.

Covers: lifecycle (start/stop/port), two-header contract, profile validation,
target-URL validation, happy-path forwarding, streaming, hop-by-hop stripping,
and transport error handling.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from ccproxy.transport import UnknownFingerprintProfileError, reset_cache
from ccproxy.transport.sidecar import (
    IMPERSONATE_HEADER,
    TARGET_URL_HEADER,
    Sidecar,
)

# ---------------------------------------------------------------------------
# Autouse cleanup: reset the dispatch cache between tests.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_transport_cache():
    reset_cache()
    yield
    reset_cache()


# ---------------------------------------------------------------------------
# Async transport that delegates to a swappable handler.
# The sidecar calls client.send(..., stream=True) and then iterates aiter_raw().
# We need a transport that properly supports streaming responses.
# ---------------------------------------------------------------------------


class _AsyncChunkedStream(httpx.AsyncByteStream):
    """AsyncByteStream that yields pre-set chunks."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk


class _CallableAsyncTransport(httpx.AsyncBaseTransport):
    """Async transport that dispatches to a user-supplied handler.

    The handler receives an :class:`httpx.Request` and must return an
    :class:`httpx.Response`. To test streaming, return a ``Response`` built
    with ``stream=_AsyncChunkedStream([...])``.
    """

    def __init__(self) -> None:
        self.handler: Callable[[httpx.Request], httpx.Response] | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        assert self.handler is not None, "handler not set before request"
        return self.handler(request)


# ---------------------------------------------------------------------------
# Shared fixture: Sidecar + pluggable transport
# ---------------------------------------------------------------------------


@pytest.fixture
async def running_sidecar():
    """Start a Sidecar with a swappable async transport. Yield (sidecar, transport).

    Tests set ``transport.handler = lambda req: httpx.Response(...)`` before
    issuing HTTP calls to the sidecar.
    """
    async_transport = _CallableAsyncTransport()
    mock_client = httpx.AsyncClient(transport=async_transport)

    sidecar = Sidecar()
    with patch("ccproxy.transport.sidecar.transport") as mock_transport_module:
        mock_transport_module.get_client = AsyncMock(return_value=mock_client)
        mock_transport_module.UnknownFingerprintProfileError = UnknownFingerprintProfileError
        await sidecar.start()
        try:
            yield sidecar, async_transport
        finally:
            await sidecar.stop()
            await mock_client.aclose()


# ---------------------------------------------------------------------------
# Helper: a default "200 OK" handler for tests that only care about status.
# ---------------------------------------------------------------------------


def _ok_handler(content: bytes = b"{}") -> Callable[[httpx.Request], httpx.Response]:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=content)

    return _handler


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_target_url_header_value(self) -> None:
        assert TARGET_URL_HEADER == "x-ccproxy-target-url"

    def test_impersonate_header_value(self) -> None:
        assert IMPERSONATE_HEADER == "x-ccproxy-impersonate"


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestSidecarLifecycle:
    async def test_port_raises_before_start(self) -> None:
        sidecar = Sidecar()
        with pytest.raises(RuntimeError, match="sidecar not started"):
            _ = sidecar.port

    async def test_start_binds_port(self) -> None:
        sidecar = Sidecar()
        with patch("ccproxy.transport.sidecar.transport") as m:
            m.get_client = AsyncMock()
            m.UnknownFingerprintProfileError = UnknownFingerprintProfileError
            await sidecar.start()
            try:
                port = sidecar.port
                assert isinstance(port, int)
                assert 1 <= port <= 65535
            finally:
                await sidecar.stop()

    async def test_port_is_reachable_after_start(self) -> None:
        sidecar = Sidecar()
        with patch("ccproxy.transport.sidecar.transport") as m:
            m.get_client = AsyncMock()
            m.UnknownFingerprintProfileError = UnknownFingerprintProfileError
            await sidecar.start()
            try:
                async with httpx.AsyncClient() as client:
                    # No contract headers → expect 400, not a connection error
                    resp = await client.get(f"http://127.0.0.1:{sidecar.port}/test")
                    assert resp.status_code == 400
            finally:
                await sidecar.stop()

    async def test_port_raises_after_stop(self) -> None:
        sidecar = Sidecar()
        with patch("ccproxy.transport.sidecar.transport") as m:
            m.get_client = AsyncMock()
            m.UnknownFingerprintProfileError = UnknownFingerprintProfileError
            await sidecar.start()
            await sidecar.stop()
            with pytest.raises(RuntimeError, match="sidecar not started"):
                _ = sidecar.port

    async def test_stop_on_unstarted_sidecar_is_noop(self) -> None:
        sidecar = Sidecar()
        await sidecar.stop()  # must not raise

    async def test_double_stop_is_safe(self) -> None:
        sidecar = Sidecar()
        with patch("ccproxy.transport.sidecar.transport") as m:
            m.get_client = AsyncMock()
            m.UnknownFingerprintProfileError = UnknownFingerprintProfileError
            await sidecar.start()
            await sidecar.stop()
            await sidecar.stop()  # second stop must not raise

    async def test_each_start_binds_unique_port(self) -> None:
        ports: set[int] = set()
        for _ in range(2):
            sidecar = Sidecar()
            with patch("ccproxy.transport.sidecar.transport") as m:
                m.get_client = AsyncMock()
                m.UnknownFingerprintProfileError = UnknownFingerprintProfileError
                await sidecar.start()
                ports.add(sidecar.port)
                await sidecar.stop()
        # Two independently started sidecars get distinct ports.
        assert len(ports) == 2


# ---------------------------------------------------------------------------
# Two-header contract — 400 responses
# ---------------------------------------------------------------------------


class TestTwoHeaderContract:
    async def test_missing_target_url_returns_400(self, running_sidecar) -> None:
        sidecar, _ = running_sidecar
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"http://127.0.0.1:{sidecar.port}/v1/messages",
                headers={IMPERSONATE_HEADER: "chrome131"},
            )
        assert resp.status_code == 400
        assert TARGET_URL_HEADER in resp.text

    async def test_missing_impersonate_returns_400(self, running_sidecar) -> None:
        sidecar, _ = running_sidecar
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"http://127.0.0.1:{sidecar.port}/v1/messages",
                headers={TARGET_URL_HEADER: "https://api.anthropic.com/v1/messages"},
            )
        assert resp.status_code == 400
        assert IMPERSONATE_HEADER in resp.text

    async def test_both_headers_missing_returns_400(self, running_sidecar) -> None:
        sidecar, _ = running_sidecar
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"http://127.0.0.1:{sidecar.port}/v1/messages")
        assert resp.status_code == 400

    async def test_error_body_mentions_missing_headers(self, running_sidecar) -> None:
        sidecar, _ = running_sidecar
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"http://127.0.0.1:{sidecar.port}/v1/messages")
        # Both header names should be referenced in the error
        assert TARGET_URL_HEADER in resp.text or IMPERSONATE_HEADER in resp.text


# ---------------------------------------------------------------------------
# Invalid target URL
# ---------------------------------------------------------------------------


class TestInvalidTargetUrl:
    async def test_url_without_hostname_returns_400(self, running_sidecar) -> None:
        sidecar, _ = running_sidecar
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"http://127.0.0.1:{sidecar.port}/v1/messages",
                headers={
                    TARGET_URL_HEADER: "/just/a/path",
                    IMPERSONATE_HEADER: "chrome131",
                },
            )
        assert resp.status_code == 400
        assert "invalid target URL" in resp.text

    async def test_invalid_url_body_includes_target(self, running_sidecar) -> None:
        sidecar, _ = running_sidecar
        bad_url = "///no-host-here"
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"http://127.0.0.1:{sidecar.port}/v1/messages",
                headers={
                    TARGET_URL_HEADER: bad_url,
                    IMPERSONATE_HEADER: "chrome131",
                },
            )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Invalid fingerprint profile
# ---------------------------------------------------------------------------


class TestInvalidProfile:
    async def test_unknown_profile_returns_400(self) -> None:
        """When get_client raises UnknownFingerprintProfileError the sidecar returns 400."""
        sidecar = Sidecar()
        with patch("ccproxy.transport.sidecar.transport") as m:
            m.UnknownFingerprintProfileError = UnknownFingerprintProfileError
            m.get_client = AsyncMock(
                side_effect=UnknownFingerprintProfileError("totally_bogus_xyz not found")
            )
            await sidecar.start()
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(
                        f"http://127.0.0.1:{sidecar.port}/v1/messages",
                        headers={
                            TARGET_URL_HEADER: "https://api.anthropic.com/v1/messages",
                            IMPERSONATE_HEADER: "totally_bogus_xyz",
                        },
                    )
                assert resp.status_code == 400
                assert "totally_bogus_xyz" in resp.text
            finally:
                await sidecar.stop()


# ---------------------------------------------------------------------------
# Happy-path forwarding
# ---------------------------------------------------------------------------


class TestHappyPathForwarding:
    async def test_status_code_propagates(self, running_sidecar) -> None:
        sidecar, async_transport = running_sidecar

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                201,
                stream=_AsyncChunkedStream([b'{"ok":true}']),
            )

        async_transport.handler = handler
        async with httpx.AsyncClient() as client, client.stream(
            "POST",
            f"http://127.0.0.1:{sidecar.port}/v1/messages",
            headers={
                TARGET_URL_HEADER: "https://api.anthropic.com/v1/messages",
                IMPERSONATE_HEADER: "chrome131",
            },
            content=b'{"model":"claude-3"}',
        ) as resp:
            assert resp.status_code == 201
            await resp.aread()

    async def test_response_body_propagates(self, running_sidecar) -> None:
        sidecar, async_transport = running_sidecar
        expected_body = b'{"id":"msg-123","type":"message"}'

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                stream=_AsyncChunkedStream([expected_body]),
            )

        async_transport.handler = handler
        async with httpx.AsyncClient() as client, client.stream(
            "POST",
            f"http://127.0.0.1:{sidecar.port}/v1/messages",
            headers={
                TARGET_URL_HEADER: "https://api.anthropic.com/v1/messages",
                IMPERSONATE_HEADER: "chrome131",
            },
            content=b"{}",
        ) as resp:
            body = await resp.aread()
        assert body == expected_body

    async def test_response_header_propagates(self, running_sidecar) -> None:
        sidecar, async_transport = running_sidecar

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"x-request-id": "req-abc"},
                stream=_AsyncChunkedStream([b"{}"]),
            )

        async_transport.handler = handler
        async with httpx.AsyncClient() as client, client.stream(
            "POST",
            f"http://127.0.0.1:{sidecar.port}/v1/messages",
            headers={
                TARGET_URL_HEADER: "https://api.anthropic.com/v1/messages",
                IMPERSONATE_HEADER: "chrome131",
            },
            content=b"{}",
        ) as resp:
            await resp.aread()
        assert resp.headers.get("x-request-id") == "req-abc"

    async def test_method_forwarded(self, running_sidecar) -> None:
        sidecar, async_transport = running_sidecar
        received_method: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            received_method.append(request.method)
            return httpx.Response(200, stream=_AsyncChunkedStream([b"{}"]))

        async_transport.handler = handler
        async with httpx.AsyncClient() as client, client.stream(
            "POST",
            f"http://127.0.0.1:{sidecar.port}/v1/messages",
            headers={
                TARGET_URL_HEADER: "https://api.anthropic.com/v1/messages",
                IMPERSONATE_HEADER: "chrome131",
            },
            content=b"{}",
        ) as resp:
            await resp.aread()
        assert received_method == ["POST"]

    async def test_custom_request_header_forwarded(self, running_sidecar) -> None:
        sidecar, async_transport = running_sidecar
        received_headers: list[dict[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            received_headers.append(dict(request.headers))
            return httpx.Response(200, stream=_AsyncChunkedStream([b"{}"]))

        async_transport.handler = handler
        async with httpx.AsyncClient() as client, client.stream(
            "POST",
            f"http://127.0.0.1:{sidecar.port}/v1/messages",
            headers={
                TARGET_URL_HEADER: "https://api.anthropic.com/v1/messages",
                IMPERSONATE_HEADER: "chrome131",
                "x-custom-header": "custom-value",
                "authorization": "Bearer mytoken",
            },
            content=b"{}",
        ) as resp:
            await resp.aread()
        assert len(received_headers) == 1
        hdrs = received_headers[0]
        assert hdrs.get("x-custom-header") == "custom-value"
        assert hdrs.get("authorization") == "Bearer mytoken"

    async def test_request_body_forwarded(self, running_sidecar) -> None:
        sidecar, async_transport = running_sidecar
        received_body: list[bytes] = []

        def handler(request: httpx.Request) -> httpx.Response:
            received_body.append(request.content)
            return httpx.Response(200, stream=_AsyncChunkedStream([b"{}"]))

        async_transport.handler = handler
        payload = b'{"model":"claude-3","messages":[{"role":"user","content":"hi"}]}'
        async with httpx.AsyncClient() as client, client.stream(
            "POST",
            f"http://127.0.0.1:{sidecar.port}/v1/messages",
            headers={
                TARGET_URL_HEADER: "https://api.anthropic.com/v1/messages",
                IMPERSONATE_HEADER: "chrome131",
            },
            content=payload,
        ) as resp:
            await resp.aread()
        assert received_body == [payload]


# ---------------------------------------------------------------------------
# Hop-by-hop header stripping
# ---------------------------------------------------------------------------


class TestHopByHopStripping:
    async def test_contract_headers_not_forwarded(self, running_sidecar) -> None:
        """TARGET_URL_HEADER and IMPERSONATE_HEADER are not forwarded upstream."""
        sidecar, async_transport = running_sidecar
        received_headers: list[dict[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            received_headers.append({k.lower(): v for k, v in request.headers.items()})
            return httpx.Response(200, stream=_AsyncChunkedStream([b"{}"]))

        async_transport.handler = handler
        async with httpx.AsyncClient() as client, client.stream(
            "POST",
            f"http://127.0.0.1:{sidecar.port}/v1/messages",
            headers={
                TARGET_URL_HEADER: "https://api.anthropic.com/v1/messages",
                IMPERSONATE_HEADER: "chrome131",
            },
            content=b"{}",
        ) as resp:
            await resp.aread()
        hdrs = received_headers[0]
        assert TARGET_URL_HEADER not in hdrs
        assert IMPERSONATE_HEADER not in hdrs

    async def test_proxy_authorization_not_forwarded(self, running_sidecar) -> None:
        """Hop-by-hop proxy-authorization header is stripped and not forwarded upstream.

        We use proxy-authorization rather than 'connection' because httpx itself
        adds its own connection header on every HTTP/1.1 request; testing for the
        absence of a header that httpx re-adds would produce a false failure.
        """
        sidecar, async_transport = running_sidecar
        received_headers: list[dict[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            received_headers.append({k.lower(): v for k, v in request.headers.items()})
            return httpx.Response(200, stream=_AsyncChunkedStream([b"{}"]))

        async_transport.handler = handler
        async with httpx.AsyncClient() as client, client.stream(
            "POST",
            f"http://127.0.0.1:{sidecar.port}/v1/messages",
            headers={
                TARGET_URL_HEADER: "https://api.anthropic.com/v1/messages",
                IMPERSONATE_HEADER: "chrome131",
                "proxy-authorization": "Basic abc123",
            },
            content=b"{}",
        ) as resp:
            await resp.aread()
        assert "proxy-authorization" not in received_headers[0]

    async def test_transfer_encoding_not_forwarded(self, running_sidecar) -> None:
        sidecar, async_transport = running_sidecar
        received_headers: list[dict[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            received_headers.append({k.lower(): v for k, v in request.headers.items()})
            return httpx.Response(200, stream=_AsyncChunkedStream([b"{}"]))

        async_transport.handler = handler
        async with httpx.AsyncClient() as client, client.stream(
            "POST",
            f"http://127.0.0.1:{sidecar.port}/v1/messages",
            headers={
                TARGET_URL_HEADER: "https://api.anthropic.com/v1/messages",
                IMPERSONATE_HEADER: "chrome131",
                "transfer-encoding": "chunked",
            },
            content=b"{}",
        ) as resp:
            await resp.aread()
        assert "transfer-encoding" not in received_headers[0]

    async def test_hop_by_hop_response_headers_stripped(self, running_sidecar) -> None:
        """Hop-by-hop headers in the upstream response are stripped before relaying.

        The upstream transport returns raw headers that include hop-by-hop entries;
        the sidecar's _filter_response_headers must strip them. We use the raw-tuple
        form so httpx doesn't swallow the headers before the sidecar sees them.
        """
        sidecar, async_transport = running_sidecar

        def handler(request: httpx.Request) -> httpx.Response:
            # Use raw header tuples so httpx preserves them in response.headers.raw
            return httpx.Response(
                200,
                headers=[
                    (b"transfer-encoding", b"chunked"),
                    (b"connection", b"keep-alive"),
                    (b"proxy-authenticate", b"Basic realm=test"),
                    (b"x-custom", b"kept"),
                ],
                stream=_AsyncChunkedStream([b"{}"]),
            )

        async_transport.handler = handler
        async with httpx.AsyncClient() as client, client.stream(
            "POST",
            f"http://127.0.0.1:{sidecar.port}/v1/messages",
            headers={
                TARGET_URL_HEADER: "https://api.anthropic.com/v1/messages",
                IMPERSONATE_HEADER: "chrome131",
            },
            content=b"{}",
        ) as resp:
            resp_hdrs = {k.lower(): v for k, v in resp.headers.items()}
            await resp.aread()

        # Hop-by-hop headers from upstream are stripped
        assert "proxy-authenticate" not in resp_hdrs
        # Non-hop-by-hop custom header survives
        assert resp_hdrs.get("x-custom") == "kept"


# ---------------------------------------------------------------------------
# Transport error → 502
# ---------------------------------------------------------------------------


class TestTransportError:
    async def test_connect_error_returns_502(self) -> None:
        sidecar = Sidecar()
        with patch("ccproxy.transport.sidecar.transport") as m:
            m.UnknownFingerprintProfileError = UnknownFingerprintProfileError

            async def _bad_send(request: httpx.Request, **kwargs: object) -> httpx.Response:
                raise httpx.ConnectError("oops")

            # Build an async transport that raises on send
            class ErrorTransport(httpx.AsyncBaseTransport):
                async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                    raise httpx.ConnectError("oops")

            error_client = httpx.AsyncClient(transport=ErrorTransport())
            m.get_client = AsyncMock(return_value=error_client)

            await sidecar.start()
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.post(
                        f"http://127.0.0.1:{sidecar.port}/v1/messages",
                        headers={
                            TARGET_URL_HEADER: "https://api.anthropic.com/v1/messages",
                            IMPERSONATE_HEADER: "chrome131",
                        },
                        content=b"{}",
                    )
                assert resp.status_code == 502
                assert "transport error" in resp.text
                assert "oops" in resp.text
            finally:
                await sidecar.stop()
                await error_client.aclose()

    async def test_connect_error_message_includes_target_url(self) -> None:
        sidecar = Sidecar()
        with patch("ccproxy.transport.sidecar.transport") as m:
            m.UnknownFingerprintProfileError = UnknownFingerprintProfileError

            class ErrorTransport(httpx.AsyncBaseTransport):
                async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                    raise httpx.ConnectError("connection refused")

            error_client = httpx.AsyncClient(transport=ErrorTransport())
            m.get_client = AsyncMock(return_value=error_client)

            await sidecar.start()
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.post(
                        f"http://127.0.0.1:{sidecar.port}/v1/messages",
                        headers={
                            TARGET_URL_HEADER: "https://api.anthropic.com/v1/messages",
                            IMPERSONATE_HEADER: "chrome131",
                        },
                        content=b"{}",
                    )
                assert resp.status_code == 502
                assert "connection refused" in resp.text
            finally:
                await sidecar.stop()
                await error_client.aclose()


# ---------------------------------------------------------------------------
# Streaming response
# ---------------------------------------------------------------------------


class TestStreamingResponse:
    async def test_streaming_chunks_delivered(self, running_sidecar) -> None:
        """Upstream streaming response is fully delivered to the client."""
        sidecar, async_transport = running_sidecar
        chunk_a = b"data: first chunk\n\n"
        chunk_b = b"data: second chunk\n\n"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_AsyncChunkedStream([chunk_a, chunk_b]),
            )

        async_transport.handler = handler
        received = bytearray()
        async with httpx.AsyncClient() as client, client.stream(
            "POST",
            f"http://127.0.0.1:{sidecar.port}/v1/messages",
            headers={
                TARGET_URL_HEADER: "https://api.anthropic.com/v1/messages",
                IMPERSONATE_HEADER: "chrome131",
            },
            content=b"{}",
        ) as resp:
            async for chunk in resp.aiter_bytes():
                received.extend(chunk)

        assert chunk_a in bytes(received)
        assert chunk_b in bytes(received)

    async def test_streaming_status_code_propagates(self, running_sidecar) -> None:
        sidecar, async_transport = running_sidecar

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                206,
                stream=_AsyncChunkedStream([b"data: chunk\n\n"]),
            )

        async_transport.handler = handler
        async with httpx.AsyncClient() as client, client.stream(
            "GET",
            f"http://127.0.0.1:{sidecar.port}/v1/messages",
            headers={
                TARGET_URL_HEADER: "https://api.anthropic.com/v1/messages",
                IMPERSONATE_HEADER: "chrome131",
            },
        ) as resp:
            assert resp.status_code == 206
            async for _ in resp.aiter_bytes():
                pass

    async def test_streaming_delivers_correct_chunk_count(self, running_sidecar) -> None:
        sidecar, async_transport = running_sidecar
        chunks = [b"chunk-%d\n" % i for i in range(5)]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                stream=_AsyncChunkedStream(chunks),
            )

        async_transport.handler = handler
        received_bytes = bytearray()
        async with httpx.AsyncClient() as client, client.stream(
            "POST",
            f"http://127.0.0.1:{sidecar.port}/v1/messages",
            headers={
                TARGET_URL_HEADER: "https://api.anthropic.com/v1/messages",
                IMPERSONATE_HEADER: "chrome131",
            },
            content=b"{}",
        ) as resp:
            async for chunk in resp.aiter_bytes():
                received_bytes.extend(chunk)

        expected_total = b"".join(chunks)
        assert bytes(received_bytes) == expected_total


# ---------------------------------------------------------------------------
# Parametrized: missing-header combinations always return 400
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MissingHeaderCase:
    name: str
    """Descriptive name for the test scenario."""

    headers: dict[str, str]
    """Headers to send (may omit one or both contract headers)."""


MISSING_HEADER_CASES: list[MissingHeaderCase] = [
    MissingHeaderCase(
        name="no_headers",
        headers={},
    ),
    MissingHeaderCase(
        name="only_target_url",
        headers={TARGET_URL_HEADER: "https://api.anthropic.com/v1/messages"},
    ),
    MissingHeaderCase(
        name="only_impersonate",
        headers={IMPERSONATE_HEADER: "chrome131"},
    ),
]


@pytest.mark.parametrize(
    "case",
    [pytest.param(c, id=c.name) for c in MISSING_HEADER_CASES],
)
async def test_missing_header_yields_400(case: MissingHeaderCase, running_sidecar) -> None:
    sidecar, _ = running_sidecar
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"http://127.0.0.1:{sidecar.port}/v1/messages",
            headers=case.headers,
        )
    assert resp.status_code == 400
