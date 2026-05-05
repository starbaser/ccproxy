"""Tests for GeminiAddon's capacity-fallback retry orchestrator (Phase E.3)."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from ccproxy.config import (
    CCProxyConfig,
    GeminiCapacityFallbackConfig,
    set_config_instance,
)
from ccproxy.flows.store import FlowRecord, InspectorMeta, TransformMeta
from ccproxy.inspector import gemini_addon as gemini_addon_module
from ccproxy.inspector.gemini_addon import (
    GeminiAddon,
    _extract_retry_delay,
    _parse_duration,
)


def _set_capacity(**overrides: Any) -> None:
    """Configure the gemini_capacity block on a fresh CCProxyConfig instance."""
    overrides.setdefault("enabled", True)
    set_config_instance(CCProxyConfig(gemini_capacity=GeminiCapacityFallbackConfig(**overrides)))


@pytest.fixture(autouse=True)
def patch_sleep() -> AsyncMock:
    """Mock asyncio.sleep so retry tests don't actually wait."""
    with patch("ccproxy.inspector.gemini_addon.asyncio.sleep", new_callable=AsyncMock) as mock:
        yield mock


def _make_flow(
    *,
    status: int = 429,
    response_body: dict[str, Any] | None = None,
    request_model: str = "gemini-3.1-pro-preview",
    is_streaming: bool = False,
) -> MagicMock:
    flow = MagicMock()
    flow.id = "test-flow"
    flow.request.method = "POST"
    flow.request.pretty_url = "https://cloudcode-pa.googleapis.com/v1internal:generateContent"
    flow.request.headers = {"authorization": "Bearer test", "content-type": "application/json"}
    flow.request.content = json.dumps(
        {
            "model": request_model,
            "request": {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
        }
    ).encode()

    flow.response = MagicMock()
    flow.response.status_code = status
    flow.response.content = json.dumps(
        response_body
        or {
            "error": {
                "code": status,
                "message": "No capacity available",
                "status": "RESOURCE_EXHAUSTED",
            }
        }
    ).encode()
    flow.response.headers = MagicMock()

    record = FlowRecord(direction="inbound")
    record.transform = TransformMeta(
        provider="gemini",
        model=request_model,
        request_data={},
        is_streaming=is_streaming,
    )
    flow.metadata = {InspectorMeta.RECORD: record, "ccproxy.oauth_provider": "gemini"}
    return flow


def _capacity_response(status: int, retry_delay: str | None = None) -> MagicMock:
    body: dict[str, Any] = {"error": {"code": status, "status": "RESOURCE_EXHAUSTED"}}
    if retry_delay is not None:
        body["error"]["details"] = [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry_delay}]
    resp = MagicMock()
    resp.status_code = status
    resp.content = json.dumps(body).encode()
    resp.json = MagicMock(return_value=body)
    return resp


def _success_response(content: bytes = b'{"candidates":[{}]}') -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.content = content
    resp.headers.get = MagicMock(return_value="application/json")
    resp.headers.multi_items = MagicMock(return_value=[("content-type", "application/json")])
    return resp


class TestParseDuration:
    def test_parse_duration_seconds_milliseconds_minutes(self) -> None:
        assert _parse_duration("9s") == 9.0
        assert _parse_duration("500ms") == 0.5
        assert _parse_duration("2m") == 120.0
        assert _parse_duration("1h") == 3600.0
        assert _parse_duration("0.5s") == 0.5
        assert _parse_duration("3") == 3.0

    def test_parse_duration_unparseable_returns_none(self) -> None:
        assert _parse_duration("garbage") is None
        assert _parse_duration("") is None
        assert _parse_duration("9 seconds") is None


class TestExtractRetryDelay:
    def test_extract_retry_delay_walks_error_details(self) -> None:
        body = {
            "error": {
                "code": 429,
                "status": "RESOURCE_EXHAUSTED",
                "details": [
                    {"@type": "type.googleapis.com/google.rpc.QuotaFailure"},
                    {
                        "@type": "type.googleapis.com/google.rpc.RetryInfo",
                        "retryDelay": "12s",
                    },
                ],
            }
        }
        assert _extract_retry_delay(body) == 12.0

    def test_extract_retry_delay_no_retry_info_returns_none(self) -> None:
        body = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}}
        assert _extract_retry_delay(body) is None

    def test_extract_retry_delay_non_dict_returns_none(self) -> None:
        assert _extract_retry_delay(None) is None
        assert _extract_retry_delay([]) is None


class TestTryFallbackGuards:
    @pytest.mark.asyncio
    async def test_no_op_when_capacity_disabled(self) -> None:
        _set_capacity(enabled=False, fallback_models=["gemini-2.5-pro"])
        flow = _make_flow()
        addon = GeminiAddon()
        result = await addon._try_fallback_models(flow)
        assert result is False

    @pytest.mark.asyncio
    async def test_no_op_when_no_fallback_models(self) -> None:
        _set_capacity(enabled=True, fallback_models=[])
        flow = _make_flow()
        addon = GeminiAddon()
        result = await addon._try_fallback_models(flow)
        assert result is False

    @pytest.mark.asyncio
    async def test_no_op_when_status_not_capacity(self) -> None:
        _set_capacity(fallback_models=["gemini-2.5-pro"], sticky_retry_attempts=0)
        flow = _make_flow(status=500)
        addon = GeminiAddon()
        result = await addon._try_fallback_models(flow)
        assert result is False

    @pytest.mark.asyncio
    async def test_no_op_when_capacity_status_not_resource_exhausted(self) -> None:
        _set_capacity(fallback_models=["gemini-2.5-pro"], sticky_retry_attempts=0)
        flow = _make_flow(
            status=429,
            response_body={"error": {"code": 429, "status": "QUOTA_EXCEEDED"}},
        )
        addon = GeminiAddon()
        result = await addon._try_fallback_models(flow)
        assert result is False

    @pytest.mark.asyncio
    async def test_503_resource_exhausted_triggers_retry(self) -> None:
        """503 capacity errors should be retried just like 429."""
        _set_capacity(fallback_models=["gemini-2.5-pro"], sticky_retry_attempts=0)
        flow = _make_flow(status=503)
        addon = GeminiAddon()

        success = _success_response()
        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.request = AsyncMock(return_value=success)
            result = await addon._try_fallback_models(flow)

        assert result is True
        assert flow.response.status_code == 200


class TestStickyRetry:
    @pytest.mark.asyncio
    async def test_sticky_retry_honors_server_retry_delay(self, patch_sleep: AsyncMock) -> None:
        _set_capacity(fallback_models=["gemini-2.5-pro"], sticky_retry_attempts=2)
        flow = _make_flow(
            status=429,
            response_body={
                "error": {
                    "code": 429,
                    "status": "RESOURCE_EXHAUSTED",
                    "details": [
                        {
                            "@type": "type.googleapis.com/google.rpc.RetryInfo",
                            "retryDelay": "7s",
                        }
                    ],
                }
            },
        )
        addon = GeminiAddon()

        success = _success_response()
        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.request = AsyncMock(return_value=success)
            result = await addon._try_fallback_models(flow)

        assert result is True
        patch_sleep.assert_awaited_with(7.0)

    @pytest.mark.asyncio
    async def test_sticky_retry_succeeds_on_second_attempt(self, patch_sleep: AsyncMock) -> None:
        _set_capacity(fallback_models=["gemini-2.5-pro"], sticky_retry_attempts=3)
        flow = _make_flow()
        addon = GeminiAddon()

        exhausted = _capacity_response(429, retry_delay="2s")
        success = _success_response(b'{"candidates":[{"text":"ok"}]}')
        request_mock = AsyncMock(side_effect=[exhausted, success])

        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.request = request_mock
            result = await addon._try_fallback_models(flow)

        assert result is True
        assert request_mock.call_count == 2
        models_tried = [json.loads(call.kwargs["content"])["model"] for call in request_mock.call_args_list]
        assert models_tried == ["gemini-3.1-pro-preview", "gemini-3.1-pro-preview"]
        assert patch_sleep.await_count == 1

    @pytest.mark.asyncio
    async def test_sticky_retry_exhausted_falls_through_to_fallback(self, patch_sleep: AsyncMock) -> None:
        _set_capacity(
            fallback_models=["gemini-2.5-pro"],
            sticky_retry_attempts=2,
        )
        flow = _make_flow()
        addon = GeminiAddon()

        exhausted = _capacity_response(429, retry_delay="1s")
        success = _success_response()
        request_mock = AsyncMock(side_effect=[exhausted, exhausted, success])

        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.request = request_mock
            result = await addon._try_fallback_models(flow)

        assert result is True
        assert request_mock.call_count == 3
        models_tried = [json.loads(call.kwargs["content"])["model"] for call in request_mock.call_args_list]
        assert models_tried == [
            "gemini-3.1-pro-preview",
            "gemini-3.1-pro-preview",
            "gemini-2.5-pro",
        ]


class TestDelayCaps:
    @pytest.mark.asyncio
    async def test_terminal_delay_stops_chain(self, patch_sleep: AsyncMock) -> None:
        """retryDelay > terminal threshold halts the entire chain."""
        _set_capacity(
            fallback_models=["gemini-2.5-pro", "gemini-2.5-flash"],
            sticky_retry_attempts=3,
            terminal_delay_threshold_seconds=300.0,
        )
        flow = _make_flow(
            response_body={
                "error": {
                    "code": 429,
                    "status": "RESOURCE_EXHAUSTED",
                    "details": [
                        {
                            "@type": "type.googleapis.com/google.rpc.RetryInfo",
                            "retryDelay": "600s",
                        }
                    ],
                }
            }
        )
        addon = GeminiAddon()

        request_mock = AsyncMock()
        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.request = request_mock
            result = await addon._try_fallback_models(flow)

        assert result is False
        assert request_mock.call_count == 0
        patch_sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_per_model_cap_falls_through(self, patch_sleep: AsyncMock) -> None:
        """retryDelay between per-model cap and terminal skips remaining sticky attempts."""
        _set_capacity(
            fallback_models=["gemini-2.5-pro"],
            sticky_retry_attempts=3,
            sticky_retry_max_delay_seconds=60.0,
            terminal_delay_threshold_seconds=300.0,
        )
        flow = _make_flow(
            response_body={
                "error": {
                    "code": 429,
                    "status": "RESOURCE_EXHAUSTED",
                    "details": [
                        {
                            "@type": "type.googleapis.com/google.rpc.RetryInfo",
                            "retryDelay": "120s",
                        }
                    ],
                }
            }
        )
        addon = GeminiAddon()

        success = _success_response()
        request_mock = AsyncMock(return_value=success)
        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.request = request_mock
            result = await addon._try_fallback_models(flow)

        assert result is True
        models_tried = [json.loads(call.kwargs["content"])["model"] for call in request_mock.call_args_list]
        assert models_tried == ["gemini-2.5-pro"]

    @pytest.mark.asyncio
    async def test_total_budget_exhausted_returns_false(self, patch_sleep: AsyncMock) -> None:
        """When the wall-clock budget would be exceeded, return False."""
        _set_capacity(
            fallback_models=["gemini-2.5-pro"],
            sticky_retry_attempts=3,
            total_retry_budget_seconds=5.0,
        )
        flow = _make_flow(
            response_body={
                "error": {
                    "code": 429,
                    "status": "RESOURCE_EXHAUSTED",
                    "details": [
                        {
                            "@type": "type.googleapis.com/google.rpc.RetryInfo",
                            "retryDelay": "10s",
                        }
                    ],
                }
            }
        )
        addon = GeminiAddon()

        clock = [1000.0]

        def fake_monotonic() -> float:
            return clock[0]

        request_mock = AsyncMock()
        with (
            patch("ccproxy.inspector.gemini_addon.time.monotonic", side_effect=fake_monotonic),
            patch("httpx.AsyncClient") as mock_client,
        ):
            mock_client.return_value.__aenter__.return_value.request = request_mock
            result = await addon._try_fallback_models(flow)

        assert result is False
        assert request_mock.call_count == 0

    @pytest.mark.asyncio
    async def test_no_retry_delay_uses_exponential_backoff(self, patch_sleep: AsyncMock) -> None:
        """Without a retryDelay, sleep is exponential: 1s, 2s, 4s. The first
        attempt of a candidate runs immediately; subsequent attempts back off."""
        _set_capacity(
            fallback_models=["gemini-2.5-pro"],
            sticky_retry_attempts=4,
            sticky_retry_max_delay_seconds=60.0,
        )
        flow = _make_flow()
        addon = GeminiAddon()

        exhausted = _capacity_response(429)
        success = _success_response()
        request_mock = AsyncMock(side_effect=[exhausted, exhausted, exhausted, success])
        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.request = request_mock
            result = await addon._try_fallback_models(flow)

        assert result is True
        delays = [call.args[0] for call in patch_sleep.await_args_list]
        assert delays == [1.0, 2.0, 4.0]


class TestFallbackChainBehavior:
    @pytest.mark.asyncio
    async def test_succeeds_on_first_fallback_replaces_response(self, patch_sleep: AsyncMock) -> None:
        _set_capacity(
            fallback_models=["gemini-2.5-pro", "gemini-2.5-flash"],
            sticky_retry_attempts=0,
        )
        flow = _make_flow()
        addon = GeminiAddon()

        success = _success_response(b'{"candidates":[{"text":"ok"}]}')
        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.request = AsyncMock(return_value=success)
            result = await addon._try_fallback_models(flow)

        assert result is True
        assert flow.response.status_code == 200
        assert flow.response.content == b'{"candidates":[{"text":"ok"}]}'
        assert mock_client.return_value.__aenter__.return_value.request.call_count == 1

    @pytest.mark.asyncio
    async def test_walks_chain_on_consecutive_capacity_errors(self, patch_sleep: AsyncMock) -> None:
        _set_capacity(
            fallback_models=["gemini-2.5-pro", "gemini-2.5-flash"],
            sticky_retry_attempts=0,
        )
        flow = _make_flow()
        addon = GeminiAddon()

        exhausted = _capacity_response(429)
        success = _success_response()
        request_mock = AsyncMock(side_effect=[exhausted, success])
        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.request = request_mock
            result = await addon._try_fallback_models(flow)

        assert result is True
        assert request_mock.call_count == 2
        models_tried = [json.loads(call.kwargs["content"])["model"] for call in request_mock.call_args_list]
        assert models_tried == ["gemini-2.5-pro", "gemini-2.5-flash"]

    @pytest.mark.asyncio
    async def test_stops_on_non_capacity_error(self, patch_sleep: AsyncMock) -> None:
        _set_capacity(
            fallback_models=["gemini-2.5-pro", "gemini-2.5-flash"],
            sticky_retry_attempts=0,
        )
        flow = _make_flow()
        addon = GeminiAddon()

        server_err = MagicMock()
        server_err.status_code = 500
        server_err.content = b'{"error":"oops"}'

        request_mock = AsyncMock(return_value=server_err)
        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.request = request_mock
            result = await addon._try_fallback_models(flow)

        assert result is False
        assert request_mock.call_count == 1

    @pytest.mark.asyncio
    async def test_skips_network_error_continues_chain(self, patch_sleep: AsyncMock) -> None:
        _set_capacity(
            fallback_models=["gemini-2.5-pro", "gemini-2.5-flash"],
            sticky_retry_attempts=0,
        )
        flow = _make_flow()
        addon = GeminiAddon()

        success = _success_response()
        request_mock = AsyncMock(side_effect=[httpx.ConnectError("boom"), success])
        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.request = request_mock
            result = await addon._try_fallback_models(flow)

        assert result is True
        assert request_mock.call_count == 2

    @pytest.mark.asyncio
    async def test_returns_false_when_all_fallbacks_exhausted(self, patch_sleep: AsyncMock) -> None:
        _set_capacity(
            fallback_models=["gemini-2.5-pro", "gemini-2.5-flash"],
            sticky_retry_attempts=0,
        )
        flow = _make_flow()
        addon = GeminiAddon()

        exhausted = _capacity_response(429)
        request_mock = AsyncMock(return_value=exhausted)
        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.request = request_mock
            result = await addon._try_fallback_models(flow)

        assert result is False
        assert request_mock.call_count == 2

    @pytest.mark.asyncio
    async def test_skips_fallback_matching_original_model(self, patch_sleep: AsyncMock) -> None:
        _set_capacity(
            fallback_models=["gemini-3.1-pro-preview", "gemini-2.5-pro"],
            sticky_retry_attempts=0,
        )
        flow = _make_flow(request_model="gemini-3.1-pro-preview")
        addon = GeminiAddon()

        success = _success_response()
        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.request = AsyncMock(return_value=success)
            result = await addon._try_fallback_models(flow)

        assert result is True
        sent_body = json.loads(mock_client.return_value.__aenter__.return_value.request.call_args.kwargs["content"])
        assert sent_body["model"] == "gemini-2.5-pro"

    @pytest.mark.asyncio
    async def test_request_body_dict_not_mutated_across_retries(self, patch_sleep: AsyncMock) -> None:
        """Regression: ``_attempt_request`` must not mutate the caller's dict.

        The retry uses a defensive copy (``{**request_body, "model": model}``).
        Verifies the dict parsed from ``flow.request.content`` survives a
        4-attempt walk through the sticky retries plus two fallback candidates
        with its original ``model`` field intact.
        """
        _set_capacity(
            fallback_models=["gemini-2.5-pro", "gemini-2.5-flash"],
            sticky_retry_attempts=2,
        )
        flow = _make_flow()
        addon = GeminiAddon()

        captured: list[dict[str, Any]] = []
        original_attempt_request = gemini_addon_module.GeminiAddon._attempt_request

        async def spy_attempt_request(flow: Any, model: str, request_body: dict[str, Any]) -> Any:
            captured.append(request_body)
            return await original_attempt_request(flow, model, request_body)

        exhausted = _capacity_response(429)
        success = _success_response()
        request_mock = AsyncMock(side_effect=[exhausted, exhausted, exhausted, success])

        with (
            patch.object(GeminiAddon, "_attempt_request", side_effect=spy_attempt_request),
            patch("httpx.AsyncClient") as mock_client,
        ):
            mock_client.return_value.__aenter__.return_value.request = request_mock
            result = await addon._try_fallback_models(flow)

        assert result is True
        assert request_mock.call_count == 4

        models_tried = [json.loads(call.kwargs["content"])["model"] for call in request_mock.call_args_list]
        assert models_tried == [
            "gemini-3.1-pro-preview",
            "gemini-3.1-pro-preview",
            "gemini-2.5-pro",
            "gemini-2.5-flash",
        ]

        assert len(captured) == 4
        request_body = captured[0]
        assert all(rb is request_body for rb in captured)
        snapshot = json.dumps(request_body, sort_keys=True)
        assert request_body["model"] == "gemini-3.1-pro-preview"
        assert json.dumps(request_body, sort_keys=True) == snapshot

    @pytest.mark.asyncio
    async def test_streaming_flows_retry_with_envelope_unwrap(self, patch_sleep: AsyncMock) -> None:
        """Streaming capacity errors are retried; SSE retry body has v1internal unwrapped."""
        _set_capacity(fallback_models=["gemini-2.5-pro"], sticky_retry_attempts=0)
        flow = _make_flow(is_streaming=True)
        addon = GeminiAddon()

        sse_resp = MagicMock()
        sse_resp.status_code = 200
        sse_resp.content = b'data: {"response": {"candidates": [{"x": 1}]}}\r\n\r\n'
        sse_resp.headers.get = MagicMock(return_value="text/event-stream")
        sse_resp.headers.multi_items = MagicMock(return_value=[("content-type", "text/event-stream")])

        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.request = AsyncMock(return_value=sse_resp)
            result = await addon._try_fallback_models(flow)

        assert result is True
        assert b'"x": 1' in flow.response.content
        assert b'"response"' not in flow.response.content


class TestResponseEntrypointBypass:
    """``GeminiAddon.response`` calls ``_try_fallback_models`` only when capacity
    is enabled and configured. These tests exercise the addon entrypoint."""

    @pytest.mark.asyncio
    async def test_capacity_disabled_passes_429_through(self) -> None:
        """Master switch off → addon does not retry, leaves response intact."""
        _set_capacity(enabled=False, fallback_models=["gemini-2.5-pro"])
        flow = _make_flow()
        addon = GeminiAddon()

        with patch("httpx.AsyncClient") as mock_client:
            request_mock = AsyncMock()
            mock_client.return_value.__aenter__.return_value.request = request_mock
            await addon.response(flow)

        assert request_mock.await_count == 0
        assert flow.response.status_code == 429

    @pytest.mark.asyncio
    async def test_capacity_enabled_no_fallback_models_passes_through(self) -> None:
        """Empty fallback_models list → no retry, no upstream call."""
        _set_capacity(enabled=True, fallback_models=[])
        flow = _make_flow()
        addon = GeminiAddon()

        with patch("httpx.AsyncClient") as mock_client:
            request_mock = AsyncMock()
            mock_client.return_value.__aenter__.return_value.request = request_mock
            await addon.response(flow)

        assert request_mock.await_count == 0
        assert flow.response.status_code == 429

    @pytest.mark.asyncio
    async def test_capacity_retries_via_response_entrypoint(self) -> None:
        """Enabled + configured + 429 → addon.response triggers fallback retry."""
        _set_capacity(
            enabled=True,
            fallback_models=["gemini-2.5-pro"],
            sticky_retry_attempts=0,
        )
        flow = _make_flow()
        addon = GeminiAddon()

        success = _success_response(b'{"candidates":[{"text":"ok"}]}')
        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.request = AsyncMock(return_value=success)
            await addon.response(flow)

        assert flow.response.status_code == 200


class TestResponseHeadersDeferEntrypoint:
    """The capacity-defer branch on streaming flows lives on GeminiAddon."""

    @pytest.mark.asyncio
    async def test_503_in_responseheaders_defers_stream(self) -> None:
        """503 + gemini + capacity enabled → no stream installed (deferred)."""
        _set_capacity(enabled=True, fallback_models=["gemini-2.5-pro"])

        flow = MagicMock()
        flow.id = "f1"
        flow.response = MagicMock()
        flow.response.status_code = 503
        flow.response.headers = {"content-type": "text/event-stream"}
        flow.response.stream = None
        record = FlowRecord(direction="inbound")
        record.transform = TransformMeta(
            provider="gemini",
            model="gemini-3.1-pro-preview",
            request_data={},
            is_streaming=True,
        )
        flow.metadata = {InspectorMeta.RECORD: record, "ccproxy.oauth_provider": "gemini"}

        addon = GeminiAddon()
        await addon.responseheaders(flow)

        assert flow.response.stream is None
