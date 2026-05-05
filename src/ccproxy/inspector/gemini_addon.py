"""Response-side Gemini orchestration.

Two responsibilities, both gated on
``flow.metadata["ccproxy.oauth_provider"] == "gemini"``:

- **Capacity fallback** — sticky-retry the original model on
  ``RESOURCE_EXHAUSTED`` (HTTP 429 / 503), then walk a configured fallback
  chain. Reads :attr:`~ccproxy.config.CCProxyConfig.gemini_capacity` for
  parameters; runs first in :meth:`response` so a successful retry replaces
  ``flow.response`` before envelope unwrap looks at it. Streaming flows are
  supported via deferred stream setup in :meth:`responseheaders`.
- **Envelope unwrap** — strip cloudcode-pa's ``{response: {...}}`` wrapper
  from successful responses. Streaming flows install
  :class:`~ccproxy.hooks.gemini_envelope.EnvelopeUnwrapStream` in
  :meth:`responseheaders`; buffered flows call
  :func:`~ccproxy.hooks.gemini_envelope.unwrap_buffered` from :meth:`response`.

The wrap on the request side is applied by the ``gemini_cli`` outbound hook;
this addon owns every response-side counterpart.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

import httpx
from mitmproxy import http

from ccproxy.config import get_config
from ccproxy.flows.store import InspectorMeta
from ccproxy.hooks.gemini_envelope import EnvelopeUnwrapStream, unwrap_buffered

logger = logging.getLogger(__name__)


_CAPACITY_STATUS_CODES: tuple[int, ...] = (429, 503)

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h)?\s*$")
_DURATION_FACTORS: dict[str, float] = {
    "ms": 0.001,
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
}


def _parse_duration(s: str) -> float | None:
    """Parse a Google duration string into seconds.

    Accepts ``"9s"``, ``"500ms"``, ``"2m"``, ``"1h"``, or a bare number
    (treated as seconds). Returns ``None`` for unparseable inputs.
    """
    if not isinstance(s, str) or not s:
        return None
    match = _DURATION_RE.match(s)
    if not match:
        return None
    value, suffix = match.groups()
    factor = _DURATION_FACTORS[suffix] if suffix else 1.0
    return float(value) * factor


def _extract_retry_delay(body: Any) -> float | None:
    """Walk ``error.details[]`` for a ``RetryInfo`` entry and parse its retryDelay."""
    if not isinstance(body, dict):
        return None
    err = body.get("error")
    if not isinstance(err, dict):
        return None
    details = err.get("details")
    if not isinstance(details, list):
        return None
    for entry in details:
        if not isinstance(entry, dict):
            continue
        type_url = str(entry.get("@type", ""))
        if "RetryInfo" not in type_url:
            continue
        delay = entry.get("retryDelay")
        if isinstance(delay, str):
            return _parse_duration(delay)
    return None


def _is_capacity_exhausted(body: Any) -> bool:
    if not isinstance(body, dict):
        return False
    err = body.get("error", {})
    if not isinstance(err, dict):
        return False
    return err.get("code") in _CAPACITY_STATUS_CODES and err.get("status") == "RESOURCE_EXHAUSTED"


class GeminiAddon:
    """mitmproxy addon: Gemini capacity fallback + response envelope unwrap."""

    @staticmethod
    def _is_gemini_flow(flow: http.HTTPFlow) -> bool:
        return flow.metadata.get("ccproxy.oauth_provider") == "gemini"

    @staticmethod
    def _capacity_enabled() -> bool:
        cfg = get_config().gemini_capacity
        return cfg.enabled and bool(cfg.fallback_models)

    async def responseheaders(self, flow: http.HTTPFlow) -> None:
        """Install ``EnvelopeUnwrapStream`` for streaming Gemini redirect flows.

        :class:`~ccproxy.inspector.addon.InspectorAddon`'s ``responseheaders``
        runs first and may have:

        a. installed an SSE transformer for transform-mode (LiteLLM) — leave it alone
        b. set ``stream=True`` for non-Gemini SSE — leave it alone

        For Gemini redirect-mode streaming flows the InspectorAddon returns
        without touching ``flow.response.stream``; this addon defers stream
        setup on a capacity error when fallback is configured (so the body
        buffers for retry), and otherwise installs
        :class:`~ccproxy.hooks.gemini_envelope.EnvelopeUnwrapStream` so each
        SSE event is unwrapped on the way back.
        """
        if not flow.response or not self._is_gemini_flow(flow):
            return

        content_type = flow.response.headers.get("content-type", "")
        if "text/event-stream" not in content_type:
            return

        record = flow.metadata.get(InspectorMeta.RECORD)
        transform = getattr(record, "transform", None) if record else None
        if not transform or transform.mode != "redirect" or not transform.is_streaming:
            return

        if flow.response.status_code in _CAPACITY_STATUS_CODES and self._capacity_enabled():
            # Defer stream setup so mitmproxy buffers the error body for retry.
            logger.info(
                "Deferring stream setup for %d to allow capacity fallback retry (flow=%s)",
                flow.response.status_code,
                flow.id,
            )
            return

        unwrap_stream = EnvelopeUnwrapStream()
        flow.response.stream = unwrap_stream
        flow.metadata["ccproxy.sse_transformer"] = unwrap_stream

    async def response(self, flow: http.HTTPFlow) -> None:
        """Run capacity fallback first, then unwrap the envelope on success.

        The capacity-fallback retry replaces ``flow.response`` if a fallback
        model succeeds; envelope unwrap then looks at the (possibly replaced)
        response. Streaming flows were already unwrapped chunk-by-chunk by
        :class:`~ccproxy.hooks.gemini_envelope.EnvelopeUnwrapStream` installed
        in :meth:`responseheaders`; error responses (status >= 400) are left
        alone so callers above can read the original error body.
        """
        if not flow.response or not self._is_gemini_flow(flow):
            return

        if flow.response.status_code in _CAPACITY_STATUS_CODES and self._capacity_enabled():
            await self._try_fallback_models(flow)

        response = flow.response
        if not response or response.status_code >= 400:
            return

        record = flow.metadata.get(InspectorMeta.RECORD)
        transform = getattr(record, "transform", None) if record else None
        if not transform or transform.is_streaming:
            return

        response.content = unwrap_buffered(response.content or b"")

    # ----- capacity fallback orchestrator --------------------------------

    @staticmethod
    async def _attempt_request(
        flow: http.HTTPFlow,
        model: str,
        request_body: dict[str, Any],
    ) -> httpx.Response | None:
        retry_body = {**request_body, "model": model}
        new_body = json.dumps(retry_body).encode()
        retry_headers = {
            k: v
            for k, v in flow.request.headers.items()  # type: ignore[no-untyped-call]
            if k.lower() not in {"content-length", "content-encoding", "transfer-encoding"}
        }
        try:
            # timeout=None: ccproxy does not enforce per-request timeouts on LLM
            # calls (slow inference is the norm). Matches OAuthAddon retry.
            async with httpx.AsyncClient(timeout=None) as client:  # noqa: S113
                return await client.request(
                    method=flow.request.method,
                    url=flow.request.pretty_url,
                    headers=retry_headers,
                    content=new_body,
                )
        except httpx.HTTPError:
            logger.warning(
                "gemini_capacity_fallback: %s network error",
                model,
                exc_info=True,
            )
            return None

    @staticmethod
    def _stamp_success_response(flow: http.HTTPFlow, resp: httpx.Response) -> None:
        content = resp.content
        if "text/event-stream" in resp.headers.get("content-type", ""):
            # Streaming retry: unwrap v1internal envelopes from each event so
            # the client sees the standard Gemini chunk format. The full body
            # is in hand, so a single pass through the stream transformer
            # flushes everything (events end at \r\n\r\n / \n\n).
            unwrap = EnvelopeUnwrapStream()
            out = unwrap(resp.content)
            content = bytes(out) if isinstance(out, bytes) else b"".join(out)
        assert flow.response is not None
        flow.response.status_code = resp.status_code
        flow.response.headers.clear()
        for key, value in resp.headers.multi_items():
            flow.response.headers.add(key, value)
        flow.response.content = content

    @staticmethod
    def _resolve_delay(
        last_capacity_body: Any,
        attempt_index: int,
        fresh_candidate: bool,
    ) -> float:
        """Determine sleep before the next attempt.

        Honours upstream ``RetryInfo.retryDelay`` when present. Otherwise the
        first attempt of a candidate has no preceding sleep, and subsequent
        attempts use exponential backoff (1s, 2s, 4s, ...). When moving to a
        fresh candidate the prior body's retryDelay is ignored — that delay
        was about a different model's capacity.
        """
        if fresh_candidate and attempt_index == 0:
            return 0.0
        server_delay = _extract_retry_delay(last_capacity_body)
        if server_delay is not None:
            return server_delay
        if attempt_index == 0:
            return 0.0
        return 2.0 ** (attempt_index - 1)

    async def _try_fallback_models(self, flow: http.HTTPFlow) -> bool:
        """Sticky retry on the original model, then walk the fallback chain.

        Returns True if a retry succeeded (``flow.response`` has been replaced);
        False otherwise.
        """
        params = get_config().gemini_capacity
        if not params.enabled or not params.fallback_models:
            return False
        if flow.response is None or flow.response.status_code not in _CAPACITY_STATUS_CODES:
            return False

        try:
            err_body = json.loads(flow.response.content or b"{}")
        except (ValueError, TypeError):
            return False
        if not _is_capacity_exhausted(err_body):
            return False

        try:
            request_body = json.loads(flow.request.content or b"{}")
        except (ValueError, TypeError):
            return False

        original_model = str(request_body.get("model", ""))
        if not original_model:
            return False

        deadline = time.monotonic() + params.total_retry_budget_seconds
        last_capacity_body: Any = err_body

        candidates: list[tuple[str, int]] = [(original_model, params.sticky_retry_attempts)]
        candidates.extend((m, 1) for m in params.fallback_models if m != original_model)

        for candidate_idx, (model, attempts) in enumerate(candidates):
            if attempts <= 0:
                continue
            fresh_candidate = candidate_idx > 0
            for attempt_index in range(attempts):
                delay = self._resolve_delay(
                    last_capacity_body,
                    attempt_index,
                    fresh_candidate=fresh_candidate and attempt_index == 0,
                )

                if delay > params.terminal_delay_threshold_seconds:
                    logger.warning(
                        "gemini_capacity_fallback: server retryDelay %.1fs exceeds "
                        "terminal threshold %.1fs, halting retry chain",
                        delay,
                        params.terminal_delay_threshold_seconds,
                    )
                    return False

                if delay > params.sticky_retry_max_delay_seconds:
                    logger.info(
                        "gemini_capacity_fallback: server retryDelay %.1fs exceeds "
                        "per-model cap %.1fs on %s, moving to next candidate",
                        delay,
                        params.sticky_retry_max_delay_seconds,
                        model,
                    )
                    break

                if time.monotonic() + delay > deadline:
                    logger.warning(
                        "gemini_capacity_fallback: total retry budget %.1fs exhausted",
                        params.total_retry_budget_seconds,
                    )
                    return False

                if delay > 0:
                    logger.info(
                        "gemini_capacity_fallback: sleeping %.2fs before %s attempt %d",
                        delay,
                        model,
                        attempt_index + 1,
                    )
                    await asyncio.sleep(delay)

                logger.info(
                    "gemini_capacity_fallback: %s attempt %d/%d (original=%s)",
                    model,
                    attempt_index + 1,
                    attempts,
                    original_model,
                )
                resp = await self._attempt_request(flow, model, request_body)
                if resp is None:
                    continue

                if 200 <= resp.status_code < 300:
                    logger.info(
                        "gemini_capacity_fallback: %s succeeded after %s exhausted",
                        model,
                        original_model,
                    )
                    self._stamp_success_response(flow, resp)
                    return True

                if resp.status_code not in _CAPACITY_STATUS_CODES:
                    logger.warning(
                        "gemini_capacity_fallback: %s returned %d, stopping retry chain",
                        model,
                        resp.status_code,
                    )
                    return False

                try:
                    last_capacity_body = resp.json()
                except (ValueError, TypeError):
                    last_capacity_body = {}

                if not _is_capacity_exhausted(last_capacity_body):
                    logger.warning(
                        "gemini_capacity_fallback: %s capacity error not RESOURCE_EXHAUSTED, stopping",
                        model,
                    )
                    return False

        logger.warning(
            "gemini_capacity_fallback: all candidates exhausted for %s",
            original_model,
        )
        return False
