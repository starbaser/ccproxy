"""Tests for the startup outbound-reachability probe."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from ccproxy.config import CCProxyConfig
from ccproxy.inspector.readiness import (
    ReadinessError,
    verify_or_shutdown,
    verify_outbound_reachability,
)


def _config(**overrides: object) -> CCProxyConfig:
    defaults: dict[str, object] = {
        "readiness_probe_url": "https://canary.example.com/",
        "readiness_probe_timeout_seconds": 5.0,
    }
    defaults.update(overrides)
    return CCProxyConfig(**defaults)  # type: ignore[arg-type]


def _mock_async_client_with(behaviour: object) -> MagicMock:
    """Build a patched AsyncClient whose .head() returns or raises ``behaviour``."""
    instance = MagicMock()
    if isinstance(behaviour, BaseException):
        instance.head = AsyncMock(side_effect=behaviour)
    else:
        instance.head = AsyncMock(return_value=behaviour)
    instance.__aenter__ = AsyncMock(return_value=instance)
    instance.__aexit__ = AsyncMock(return_value=None)
    return instance


@pytest.mark.asyncio
class TestVerifyOutboundReachability:
    async def test_success_on_any_http_response(self, caplog: pytest.LogCaptureFixture) -> None:
        """Any HTTP response (even 404) proves the stack works → success."""
        config = _config()
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 404
        client = _mock_async_client_with(resp)

        with (
            patch("httpx.AsyncClient", return_value=client),
            caplog.at_level(logging.INFO, logger="ccproxy.inspector.readiness"),
        ):
            await verify_outbound_reachability(config)

        assert any(
            "Outbound readiness OK" in r.message and "HTTP 404" in r.message
            for r in caplog.records
        )
        client.head.assert_awaited_once_with(
            "https://canary.example.com/", follow_redirects=False,
        )

    async def test_connect_error_raises(self) -> None:
        config = _config()
        client = _mock_async_client_with(httpx.ConnectError("dns failed"))

        with (
            patch("httpx.AsyncClient", return_value=client),
            pytest.raises(ReadinessError, match="connect error"),
        ):
            await verify_outbound_reachability(config)

    async def test_connect_timeout_raises(self) -> None:
        config = _config()
        client = _mock_async_client_with(httpx.ConnectTimeout("timed out"))

        with (
            patch("httpx.AsyncClient", return_value=client),
            pytest.raises(ReadinessError, match="connect timeout"),
        ):
            await verify_outbound_reachability(config)

    async def test_read_timeout_raises_not_a_success(self) -> None:
        """ReadTimeout means the server never replied — that is a failure, not reachability."""
        config = _config()
        client = _mock_async_client_with(httpx.ReadTimeout("hung"))

        with (
            patch("httpx.AsyncClient", return_value=client),
            pytest.raises(ReadinessError, match="read timeout"),
        ):
            await verify_outbound_reachability(config)

    async def test_generic_http_error_raises(self) -> None:
        config = _config()
        client = _mock_async_client_with(httpx.ProtocolError("bad framing"))

        with (
            patch("httpx.AsyncClient", return_value=client),
            pytest.raises(ReadinessError, match="ProtocolError"),
        ):
            await verify_outbound_reachability(config)

    async def test_uses_configured_url(self) -> None:
        config = _config(readiness_probe_url="https://custom.example.org/ping")
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        client = _mock_async_client_with(resp)

        with patch("httpx.AsyncClient", return_value=client):
            await verify_outbound_reachability(config)

        client.head.assert_awaited_once_with(
            "https://custom.example.org/ping", follow_redirects=False,
        )

    async def test_uses_configured_timeout(self) -> None:
        config = _config(readiness_probe_timeout_seconds=2.5)
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        client = _mock_async_client_with(resp)

        with patch("httpx.AsyncClient", return_value=client) as client_cls:
            await verify_outbound_reachability(config)

        timeout = client_cls.call_args.kwargs["timeout"]
        assert isinstance(timeout, httpx.Timeout)
        assert timeout.read == 2.5

    async def test_error_message_includes_timeout_value(self) -> None:
        config = _config(readiness_probe_timeout_seconds=7.0)
        client = _mock_async_client_with(httpx.ReadTimeout("slow"))

        with (
            patch("httpx.AsyncClient", return_value=client),
            pytest.raises(ReadinessError, match=r"7\.0s"),
        ):
            await verify_outbound_reachability(config)


@pytest.mark.asyncio
class TestVerifyOrShutdown:
    async def test_success_does_not_call_cleanup(self) -> None:
        config = _config()
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 200
        client = _mock_async_client_with(resp)
        cleanup = AsyncMock()

        with patch("httpx.AsyncClient", return_value=client):
            await verify_or_shutdown(config, cleanup)

        cleanup.assert_not_awaited()

    async def test_failure_calls_cleanup_and_reraises(self) -> None:
        config = _config()
        client = _mock_async_client_with(httpx.ConnectError("no route"))
        cleanup = AsyncMock()

        with (
            patch("httpx.AsyncClient", return_value=client),
            pytest.raises(ReadinessError),
        ):
            await verify_or_shutdown(config, cleanup)

        cleanup.assert_awaited_once()

    async def test_cleanup_exception_is_swallowed_but_original_raised(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """If the cleanup itself raises, log and still surface the original ReadinessError."""
        config = _config()
        client = _mock_async_client_with(httpx.ConnectError("no route"))

        async def broken_cleanup() -> None:
            raise RuntimeError("cleanup broke")

        with (
            patch("httpx.AsyncClient", return_value=client),
            caplog.at_level(logging.ERROR, logger="ccproxy.inspector.readiness"),
            pytest.raises(ReadinessError),
        ):
            await verify_or_shutdown(config, broken_cleanup)

        assert any(
            "Cleanup after readiness failure itself raised" in r.message
            for r in caplog.records
        )
