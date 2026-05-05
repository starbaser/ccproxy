"""Tests for GeminiAddon — response-side envelope unwrap (Phase E.2).

Capacity-fallback responsibility moves into this addon in Wave 6 (Phase E.3);
those tests live in ``test_gemini_capacity_fallback.py`` until then.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from ccproxy.flows.store import FlowRecord, InspectorMeta, TransformMeta
from ccproxy.hooks.gemini_envelope import EnvelopeUnwrapStream
from ccproxy.inspector.gemini_addon import GeminiAddon


def _make_gemini_flow(
    *,
    is_streaming: bool = True,
    mode: str = "redirect",
    status_code: int = 200,
    content: bytes | None = None,
    content_type: str = "text/event-stream",
    oauth_provider: str | None = "gemini",
    transform_provider: str = "gemini",
    include_transform: bool = True,
) -> MagicMock:
    """Build a mock flow approximating a Gemini-routed request/response."""
    flow = MagicMock()
    flow.id = "flow-test-1"
    metadata: dict[str, object] = {}
    if oauth_provider is not None:
        metadata["ccproxy.oauth_provider"] = oauth_provider

    if include_transform:
        record = FlowRecord(direction="inbound")
        record.transform = TransformMeta(
            provider=transform_provider,
            model="gemini-2.5-flash",
            request_data={},
            is_streaming=is_streaming,
            mode=mode,  # type: ignore[arg-type]
        )
        metadata[InspectorMeta.RECORD] = record

    flow.metadata = metadata
    flow.response = MagicMock()
    flow.response.status_code = status_code
    flow.response.headers = {"content-type": content_type}
    flow.response.content = content
    flow.response.stream = None
    return flow


# ----------------------------------------------------------------------------
# responseheaders — streaming setup
# ----------------------------------------------------------------------------


class TestResponseHeadersStreamingInstall:
    """Tests for GeminiAddon.responseheaders streaming install path."""

    @pytest.mark.asyncio
    async def test_installs_envelope_unwrap_for_streaming_redirect(self) -> None:
        """Streaming Gemini redirect flow installs EnvelopeUnwrapStream."""
        flow = _make_gemini_flow(is_streaming=True, mode="redirect", status_code=200)
        addon = GeminiAddon()

        with patch(
            "ccproxy.hooks.gemini_capacity_fallback.has_fallback_configured",
            return_value=False,
        ):
            await addon.responseheaders(flow)

        assert isinstance(flow.response.stream, EnvelopeUnwrapStream)
        assert flow.metadata.get("ccproxy.sse_transformer") is flow.response.stream

    @pytest.mark.asyncio
    async def test_no_install_for_transform_mode(self) -> None:
        """Streaming Gemini transform-mode is left to InspectorAddon's lightllm path."""
        flow = _make_gemini_flow(is_streaming=True, mode="transform", status_code=200)
        addon = GeminiAddon()

        await addon.responseheaders(flow)

        assert flow.response.stream is None
        assert "ccproxy.sse_transformer" not in flow.metadata

    @pytest.mark.asyncio
    async def test_no_install_when_capacity_fallback_deferring(self) -> None:
        """When InspectorAddon is buffering for a fallback retry, GeminiAddon stays out."""
        flow = _make_gemini_flow(is_streaming=True, mode="redirect", status_code=429)
        addon = GeminiAddon()

        with patch(
            "ccproxy.hooks.gemini_capacity_fallback.has_fallback_configured",
            return_value=True,
        ):
            await addon.responseheaders(flow)

        assert flow.response.stream is None
        assert "ccproxy.sse_transformer" not in flow.metadata

    @pytest.mark.asyncio
    async def test_install_on_429_when_no_fallback_configured(self) -> None:
        """A 429 with no fallback chain configured still gets the unwrap stream."""
        flow = _make_gemini_flow(is_streaming=True, mode="redirect", status_code=429)
        addon = GeminiAddon()

        with patch(
            "ccproxy.hooks.gemini_capacity_fallback.has_fallback_configured",
            return_value=False,
        ):
            await addon.responseheaders(flow)

        assert isinstance(flow.response.stream, EnvelopeUnwrapStream)

    @pytest.mark.asyncio
    async def test_no_install_for_503_when_fallback_configured(self) -> None:
        """503 also triggers the capacity-defer path when fallbacks are configured."""
        flow = _make_gemini_flow(is_streaming=True, mode="redirect", status_code=503)
        addon = GeminiAddon()

        with patch(
            "ccproxy.hooks.gemini_capacity_fallback.has_fallback_configured",
            return_value=True,
        ):
            await addon.responseheaders(flow)

        assert flow.response.stream is None

    @pytest.mark.asyncio
    async def test_no_install_for_non_gemini_oauth_flow(self) -> None:
        """A flow without ``ccproxy.oauth_provider == "gemini"`` is left alone."""
        flow = _make_gemini_flow(is_streaming=True, mode="redirect", oauth_provider="anthropic")
        addon = GeminiAddon()

        await addon.responseheaders(flow)

        assert flow.response.stream is None

    @pytest.mark.asyncio
    async def test_no_install_for_non_streaming_response(self) -> None:
        """Non-streaming responses do not get an SSE transformer installed."""
        flow = _make_gemini_flow(is_streaming=False, mode="redirect", content_type="application/json")
        addon = GeminiAddon()

        await addon.responseheaders(flow)

        assert flow.response.stream is None

    @pytest.mark.asyncio
    async def test_no_install_when_no_response(self) -> None:
        """A flow without ``flow.response`` is a no-op."""
        flow = MagicMock()
        flow.metadata = {"ccproxy.oauth_provider": "gemini"}
        flow.response = None
        addon = GeminiAddon()

        await addon.responseheaders(flow)

    @pytest.mark.asyncio
    async def test_no_install_when_no_record(self) -> None:
        """A streaming Gemini flow without a FlowRecord is left alone."""
        flow = _make_gemini_flow(is_streaming=True, mode="redirect", include_transform=False)
        addon = GeminiAddon()

        await addon.responseheaders(flow)

        assert flow.response.stream is None

    @pytest.mark.asyncio
    async def test_no_install_when_record_has_no_transform(self) -> None:
        """A FlowRecord without a transform is left alone."""
        record = FlowRecord(direction="inbound")
        record.transform = None
        flow = MagicMock()
        flow.metadata = {InspectorMeta.RECORD: record, "ccproxy.oauth_provider": "gemini"}
        flow.response = MagicMock()
        flow.response.status_code = 200
        flow.response.headers = {"content-type": "text/event-stream"}
        flow.response.stream = None
        addon = GeminiAddon()

        await addon.responseheaders(flow)

        assert flow.response.stream is None


# ----------------------------------------------------------------------------
# response — buffered unwrap
# ----------------------------------------------------------------------------


class TestResponseBufferedUnwrap:
    """Tests for GeminiAddon.response buffered envelope unwrap path."""

    @pytest.mark.asyncio
    async def test_unwraps_buffered_success_envelope(self) -> None:
        """Buffered Gemini success unwraps the {response: {...}} envelope."""
        inner = {"candidates": [{"content": "hello"}]}
        flow = _make_gemini_flow(
            is_streaming=False,
            mode="redirect",
            status_code=200,
            content=json.dumps({"response": inner}).encode(),
            content_type="application/json",
        )
        addon = GeminiAddon()

        await addon.response(flow)

        assert json.loads(flow.response.content) == inner

    @pytest.mark.asyncio
    async def test_skips_error_response(self) -> None:
        """Errors (status >= 400) are left alone so the original body surfaces."""
        original = json.dumps({"response": {"inner": True}}).encode()
        flow = _make_gemini_flow(
            is_streaming=False,
            mode="redirect",
            status_code=500,
            content=original,
            content_type="application/json",
        )
        addon = GeminiAddon()

        await addon.response(flow)

        assert flow.response.content == original

    @pytest.mark.asyncio
    async def test_skips_streaming_flow(self) -> None:
        """Streaming flows were already unwrapped chunk-by-chunk by EnvelopeUnwrapStream."""
        original = json.dumps({"response": {"inner": True}}).encode()
        flow = _make_gemini_flow(
            is_streaming=True,
            mode="redirect",
            status_code=200,
            content=original,
            content_type="text/event-stream",
        )
        addon = GeminiAddon()

        await addon.response(flow)

        assert flow.response.content == original

    @pytest.mark.asyncio
    async def test_skips_non_gemini_flow(self) -> None:
        """A flow with a non-gemini ``ccproxy.oauth_provider`` is left alone."""
        original = json.dumps({"response": {"inner": True}}).encode()
        flow = _make_gemini_flow(
            is_streaming=False,
            mode="redirect",
            status_code=200,
            content=original,
            content_type="application/json",
            oauth_provider="anthropic",
        )
        addon = GeminiAddon()

        await addon.response(flow)

        assert flow.response.content == original

    @pytest.mark.asyncio
    async def test_no_op_when_envelope_key_absent(self) -> None:
        """A buffered Gemini body without ``response`` key is left unchanged."""
        original = json.dumps({"other": "data"}).encode()
        flow = _make_gemini_flow(
            is_streaming=False,
            mode="redirect",
            status_code=200,
            content=original,
            content_type="application/json",
        )
        addon = GeminiAddon()

        await addon.response(flow)

        assert flow.response.content == original

    @pytest.mark.asyncio
    async def test_no_op_on_invalid_json(self) -> None:
        """Invalid JSON in the body is left unchanged (graceful no-op)."""
        original = b"not-json{{{"
        flow = _make_gemini_flow(
            is_streaming=False,
            mode="redirect",
            status_code=200,
            content=original,
            content_type="application/json",
        )
        addon = GeminiAddon()

        await addon.response(flow)

        assert flow.response.content == original

    @pytest.mark.asyncio
    async def test_no_op_when_no_response(self) -> None:
        """A flow without ``flow.response`` is a no-op."""
        flow = MagicMock()
        flow.metadata = {"ccproxy.oauth_provider": "gemini"}
        flow.response = None
        addon = GeminiAddon()

        await addon.response(flow)

    @pytest.mark.asyncio
    async def test_no_op_when_no_transform(self) -> None:
        """A flow without a FlowRecord transform is left alone."""
        flow = _make_gemini_flow(
            is_streaming=False,
            mode="redirect",
            status_code=200,
            content=json.dumps({"response": {"inner": True}}).encode(),
            content_type="application/json",
            include_transform=False,
        )
        addon = GeminiAddon()
        original = flow.response.content

        await addon.response(flow)

        assert flow.response.content == original

    @pytest.mark.asyncio
    async def test_handles_empty_body(self) -> None:
        """Empty body unwraps to empty without raising."""
        flow = _make_gemini_flow(
            is_streaming=False,
            mode="redirect",
            status_code=200,
            content=b"",
            content_type="application/json",
        )
        addon = GeminiAddon()

        await addon.response(flow)

        assert flow.response.content == b""

    @pytest.mark.asyncio
    async def test_handles_none_body(self) -> None:
        """``None`` body coerces to ``b""`` without raising."""
        flow = _make_gemini_flow(
            is_streaming=False,
            mode="redirect",
            status_code=200,
            content=None,
            content_type="application/json",
        )
        addon = GeminiAddon()

        await addon.response(flow)

        assert flow.response.content == b""


# ----------------------------------------------------------------------------
# Addon-chain ordering regression
# ----------------------------------------------------------------------------


class TestAddonChainOrdering:
    """Regression: GeminiAddon.response runs after InspectorAddon and unwraps."""

    @pytest.mark.asyncio
    async def test_buffered_gemini_success_unwraps_through_addon(self) -> None:
        """Integration-style: a buffered Gemini 200 with envelope unwraps via GeminiAddon.

        Proves the envelope unwrap responsibility now lives on GeminiAddon. Not
        a true multi-addon dispatch (mitmproxy owns that), but anchors the
        post-extraction contract: once InspectorAddon has snapshotted and
        capacity-fallback has done nothing for a 200, GeminiAddon strips the
        envelope so downstream consumers see the canonical Gemini shape.
        """
        inner = {"candidates": [{"content": "ok"}]}
        flow = _make_gemini_flow(
            is_streaming=False,
            mode="redirect",
            status_code=200,
            content=json.dumps({"response": inner}).encode(),
            content_type="application/json",
        )
        gemini = GeminiAddon()

        await gemini.response(flow)

        assert json.loads(flow.response.content) == inner
