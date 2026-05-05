"""Response-side Gemini orchestration.

Envelope unwrap responsibility (this commit):

- :meth:`GeminiAddon.responseheaders` — installs
  :class:`~ccproxy.hooks.gemini_envelope.EnvelopeUnwrapStream` for streaming
  Gemini redirect flows so each SSE chunk is unwrapped on the way back.
- :meth:`GeminiAddon.response` — calls
  :func:`~ccproxy.hooks.gemini_envelope.unwrap_buffered` on buffered Gemini
  responses, stripping cloudcode-pa's ``{response: {...}}`` envelope.

Capacity-fallback responsibility lands in Wave 6 (Phase E.3); the
``ccproxy.hooks.gemini_capacity_fallback`` module currently still owns the
defer-on-429 branch in :class:`~ccproxy.inspector.addon.InspectorAddon` and
the ``try_fallback_models`` retry routine. This addon coordinates with that
defer branch via the same status-code + ``has_fallback_configured()`` check
in :meth:`responseheaders` so it does not install ``EnvelopeUnwrapStream``
when the InspectorAddon is buffering for a retry.

Triggered by ``flow.metadata["ccproxy.oauth_provider"] == "gemini"`` (set by
the request-side ``forward_oauth`` hook). The envelope wrap was applied by
the request-side ``gemini_cli`` hook; this addon owns the response-side
counterpart.
"""

from __future__ import annotations

import logging

from mitmproxy import http

from ccproxy.flows.store import InspectorMeta
from ccproxy.hooks.gemini_envelope import EnvelopeUnwrapStream, unwrap_buffered

logger = logging.getLogger(__name__)


class GeminiAddon:
    """mitmproxy addon: Gemini envelope unwrap (capacity fallback added in Wave 6)."""

    @staticmethod
    def _is_gemini_flow(flow: http.HTTPFlow) -> bool:
        return flow.metadata.get("ccproxy.oauth_provider") == "gemini"

    async def responseheaders(self, flow: http.HTTPFlow) -> None:
        """Install ``EnvelopeUnwrapStream`` for streaming Gemini redirect flows.

        :class:`~ccproxy.inspector.addon.InspectorAddon`'s ``responseheaders``
        runs first and may have:

        a. installed an SSE transformer for transform-mode (LiteLLM) — leave it alone
        b. deferred stream setup for capacity-fallback retry — honor that and skip
        c. set ``stream=True`` for non-Gemini SSE — leave it alone

        For Gemini redirect-mode streaming the InspectorAddon returns without
        touching ``flow.response.stream``; this addon installs
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

        # Capacity-defer in InspectorAddon: don't install if it is buffering
        # for a fallback-model retry. This conditional disappears in Wave 6
        # when GeminiAddon owns the capacity-fallback path too.
        # deferred: optional capacity-fallback hook
        from ccproxy.hooks.gemini_capacity_fallback import (
            _CAPACITY_STATUS_CODES,
            has_fallback_configured,
        )

        if flow.response.status_code in _CAPACITY_STATUS_CODES and has_fallback_configured():
            return  # InspectorAddon's defer branch is in charge of this flow

        unwrap_stream = EnvelopeUnwrapStream()
        flow.response.stream = unwrap_stream
        flow.metadata["ccproxy.sse_transformer"] = unwrap_stream

    async def response(self, flow: http.HTTPFlow) -> None:
        """Unwrap cloudcode-pa's ``{response: {...}}`` envelope on buffered success bodies.

        Streaming flows were already unwrapped chunk-by-chunk by the
        :class:`~ccproxy.hooks.gemini_envelope.EnvelopeUnwrapStream` installed
        in :meth:`responseheaders`; error responses (status >= 400) are left
        alone so capacity-fallback callers and surfaces above can read the
        original error body.
        """
        response = flow.response
        if not response or not self._is_gemini_flow(flow):
            return
        if response.status_code >= 400:
            return

        record = flow.metadata.get(InspectorMeta.RECORD)
        transform = getattr(record, "transform", None) if record else None
        if not transform or transform.is_streaming:
            return

        response.content = unwrap_buffered(response.content or b"")
