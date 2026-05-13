"""Response-side Perplexity orchestration.

One responsibility, gated on
``flow.metadata["ccproxy.oauth_provider"] == "perplexity_pro"``:

**L1 cache capture** — parse the upstream Perplexity SSE response after it
completes and persist the captured ``backend_uuid`` /
``read_write_token`` / ``context_uuid`` / ``thread_url_slug`` into the
:class:`~ccproxy.lightllm.pplx_threads.PerplexityThreadStore` keyed by
``flow.metadata["ccproxy.conversation_id"]`` (the SHA12 stamped by
:class:`~ccproxy.inspector.addon.InspectorAddon`).

The next-turn ``pplx_thread_inject`` hook reads this cache as Mode 2
(organic in-session continuation) when the client did not supply an
explicit ``metadata.ccproxy_pplx_thread``. This gives zero-friction
multi-turn for naive OpenAI SDK clients without requiring ccproxy to
hold authoritative state — Perplexity remains the source of truth,
this is just a hot-path latency optimization.

Decoupled from :class:`PerplexityProIterator` to keep concerns clean:
the iterator transforms wire format; this addon captures persistent
state. Both observe the same SSE events but for different purposes.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from mitmproxy import http

from ccproxy.lightllm.pplx import (
    PERPLEXITY_PROVIDER_NAME,
    _PPLX_ID_FIELDS,
    _extract_deltas,
    _parse_sse_line,
    StreamState,
)
from ccproxy.lightllm.pplx_threads import get_pplx_thread_store

logger = logging.getLogger(__name__)


class PerplexityAddon:
    """mitmproxy addon: capture thread identifiers from Perplexity SSE into L1."""

    @staticmethod
    def _is_pplx_flow(flow: http.HTTPFlow) -> bool:
        return (
            flow.metadata.get("ccproxy.oauth_provider") == PERPLEXITY_PROVIDER_NAME
        )

    async def response(self, flow: http.HTTPFlow) -> None:
        """Parse the upstream Perplexity SSE body and save IDs to the L1 cache.

        Reads from the ``SseTransformer.raw_body`` accumulated during streaming
        (when the InspectorAddon installed one), or falls back to
        ``flow.response.content`` for buffered flows. Silently no-ops on parse
        failure, missing IDs, or absence of a ``conversation_id`` to key by.
        """
        if flow.response is None or not self._is_pplx_flow(flow):
            return

        raw_body = self._extract_raw_body(flow)
        if not raw_body:
            return

        conv_id = flow.metadata.get("ccproxy.conversation_id")
        if not isinstance(conv_id, str) or not conv_id:
            return

        ids = self._scan_for_ids(raw_body)
        if not ids:
            return

        backend_uuid = ids.get("backend_uuid")
        context_uuid = ids.get("context_uuid")
        if not backend_uuid or not context_uuid:
            return

        store = get_pplx_thread_store()
        store.save(
            conversation_id=conv_id,
            backend_uuid=backend_uuid,
            read_write_token=ids.get("read_write_token"),
            context_uuid=context_uuid,
            thread_url_slug=ids.get("thread_url_slug"),
        )
        flow.metadata["ccproxy.pplx.captured_ids"] = dict(ids)
        logger.debug(
            "pplx L1 cache populated: conv_id=%s backend_uuid=%s slug=%s",
            conv_id[:8],
            backend_uuid[:8],
            ids.get("thread_url_slug"),
        )

    @staticmethod
    def _extract_raw_body(flow: http.HTTPFlow) -> bytes:
        # Preferred source: FlowRecord.provider_response.body — stashed by
        # InspectorAddon.response BEFORE the route layer rewrites
        # flow.response.content with the OpenAI-format JSON. This is the
        # only access path for non-streaming flows since by the time we run
        # the response.content has already been transformed.
        from ccproxy.flows.store import InspectorMeta

        record = flow.metadata.get(InspectorMeta.RECORD)
        provider_resp = getattr(record, "provider_response", None) if record else None
        if provider_resp is not None:
            body = getattr(provider_resp, "body", None)
            if isinstance(body, bytes) and body:
                return body
        # Streaming flows that never went through the route's transform_response:
        # the SseTransformer keeps the raw_body tee.
        transformer = flow.metadata.get("ccproxy.sse_transformer")
        if transformer is not None and hasattr(transformer, "raw_body"):
            raw = transformer.raw_body
            if isinstance(raw, bytes) and raw:
                return raw
        if flow.response is not None:
            try:
                return flow.response.content or b""
            except Exception:
                return b""
        return b""

    @staticmethod
    def _scan_for_ids(raw_body: bytes) -> dict[str, str] | None:
        """Parse SSE events from the raw body; return the accumulated identifier map.

        Iterates events lazily using the same parser as the LiteLLM iterator
        so streaming and buffered flows share identical extraction logic.
        Late events overwrite earlier values (read_write_token and
        thread_url_slug typically arrive on the final event per
        ``threads-history.md:24-44``).
        """
        try:
            text = raw_body.decode("utf-8", errors="replace")
        except Exception:
            return None

        state = StreamState()
        for line in text.splitlines():
            event = _parse_sse_line(line)
            if event is None:
                continue
            try:
                _extract_deltas(event, state)
            except Exception:
                pass

        ids = {k: v for k, v in state.ids.items() if k in _PPLX_ID_FIELDS and isinstance(v, str)}
        return ids or None
