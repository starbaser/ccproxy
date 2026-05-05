"""Retry Gemini requests with sticky same-model retries and fallback models.

cloudcode-pa returns capacity errors with HTTP 429 or 503 and
``status: RESOURCE_EXHAUSTED`` (and ``reason: MODEL_CAPACITY_EXHAUSTED``) when
the requested model has no capacity available. This module first retries the
same model a configurable number of times (honouring the upstream
``RetryInfo.retryDelay``), then walks a configured fallback chain. This
mirrors the official Gemini CLI's quota-error handling.

Configured via the standard hook system, with a Pydantic params schema::

    hooks:
      outbound:
        - hook: ccproxy.hooks.gemini_capacity_fallback
          params:
            fallback_models:
              - gemini-3-flash-preview
              - gemini-2.5-pro
              - gemini-2.5-flash
            sticky_retry_attempts: 3
            sticky_retry_max_delay_seconds: 60.0
            terminal_delay_threshold_seconds: 300.0
            total_retry_budget_seconds: 120.0

The hook system itself is request-side only, so the @hook function below
just records the configured params. The actual retry runs from the addon's
response phase — see :func:`try_fallback_models` invoked from
``ccproxy.inspector.addon.InspectorAddon.response``.

Streaming flows are supported because ``InspectorAddon.responseheaders``
defers stream setup for capacity errors when fallbacks are configured —
by the time :func:`try_fallback_models` runs, the error body is fully
buffered.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import BaseModel, Field

from ccproxy.pipeline.hook import hook

if TYPE_CHECKING:
    from mitmproxy import http

    from ccproxy.pipeline.context import Context

logger = logging.getLogger(__name__)

_CAPACITY_STATUS_CODES: tuple[int, ...] = (429, 503)


class GeminiCapacityFallbackParams(BaseModel):
    fallback_models: list[str] = Field(default_factory=list)
    """Models to try in order after sticky retries on the original are exhausted."""

    sticky_retry_attempts: int = Field(default=3, ge=0, le=10)
    """Number of same-model retries on the original model before falling through."""

    sticky_retry_max_delay_seconds: float = Field(default=60.0, gt=0)
    """Per-attempt cap on retryDelay. If the server asks for longer, skip remaining
    sticky attempts on this model and move to the next candidate."""

    terminal_delay_threshold_seconds: float = Field(default=300.0, gt=0)
    """Hard ceiling. retryDelay above this halts the entire retry chain — server
    is signaling sustained outage, fallback models would also fail."""

    total_retry_budget_seconds: float = Field(default=120.0, gt=0)
    """Wall-clock budget for the entire retry chain across all candidates."""


_configured_params: GeminiCapacityFallbackParams | None = None


@hook(reads=[], writes=[], model=GeminiCapacityFallbackParams)
def gemini_capacity_fallback(ctx: Context, params: dict[str, Any]) -> Context:
    """Records the configured fallback params. No request-side mutation.

    The retry logic itself runs from the addon's response phase — this
    function only stores the params for that handler to consume.
    """
    global _configured_params
    incoming = GeminiCapacityFallbackParams(**params)
    if _configured_params is None or incoming.model_dump() != _configured_params.model_dump():
        _configured_params = incoming
        logger.info(
            "gemini_capacity_fallback: configured fallback chain: %s",
            incoming.fallback_models,
        )
    return ctx


def has_fallback_configured() -> bool:
    """Whether any fallback models are configured.

    Used by ``InspectorAddon.responseheaders`` to decide whether to defer
    stream setup on a capacity error so the body can be buffered for retry.
    """
    return _configured_params is not None and bool(_configured_params.fallback_models)


def reset_config() -> None:
    """Clear the configured params (for tests)."""
    global _configured_params
    _configured_params = None


def _is_capacity_exhausted(body: Any) -> bool:
    if not isinstance(body, dict):
        return False
    err = body.get("error", {})
    if not isinstance(err, dict):
        return False
    return err.get("code") in _CAPACITY_STATUS_CODES and err.get("status") == "RESOURCE_EXHAUSTED"


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
        # calls (slow inference is the norm). Matches addon.py 401 retry.
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


def _stamp_success_response(flow: http.HTTPFlow, resp: httpx.Response) -> None:
    content = resp.content
    if "text/event-stream" in resp.headers.get("content-type", ""):
        # Streaming retry: unwrap v1internal envelopes from each event so the
        # client sees the standard Gemini chunk format. The full body is in
        # hand, so a single pass through the stream transformer flushes
        # everything (events end at \r\n\r\n / \n\n).
        from ccproxy.hooks.gemini_cli import EnvelopeUnwrapStream

        unwrap = EnvelopeUnwrapStream()
        out = unwrap(resp.content)
        content = bytes(out) if isinstance(out, bytes) else b"".join(out)
    assert flow.response is not None
    flow.response.status_code = resp.status_code
    flow.response.headers.clear()
    for key, value in resp.headers.multi_items():
        flow.response.headers.add(key, value)
    flow.response.content = content


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


async def try_fallback_models(flow: http.HTTPFlow) -> bool:
    """Sticky retry on the original model, then walk the fallback chain.

    Called from ``InspectorAddon.response`` when a capacity error lands on a
    Gemini flow. Returns True if a retry succeeded (``flow.response`` has
    been replaced); False otherwise.
    """
    params = _configured_params
    if params is None or not params.fallback_models:
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
            delay = _resolve_delay(
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
            resp = await _attempt_request(flow, model, request_body)
            if resp is None:
                continue

            if 200 <= resp.status_code < 300:
                logger.info(
                    "gemini_capacity_fallback: %s succeeded after %s exhausted",
                    model,
                    original_model,
                )
                _stamp_success_response(flow, resp)
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
