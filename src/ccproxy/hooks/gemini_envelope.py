"""cloudcode-pa envelope-unwrap primitives.

Two surfaces share the same conceptual operation — strip the
``{response: {...}}`` wrapper cloudcode-pa adds around standard Gemini
responses:

- :class:`EnvelopeUnwrapStream` — stateful SSE stream transformer used as
  ``flow.response.stream`` for streaming flows.
- :func:`unwrap_buffered` — free function for already-buffered response
  bodies.

Both forms live here so any consumer (the outbound hook, the capacity
fallback retry, the response-side addon) can import a single source of
truth.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable

logger = logging.getLogger(__name__)


def _split_event(buf: bytes) -> tuple[bytes, bytes, bytes]:
    """Split ``buf`` at the first SSE event boundary (``\\r\\n\\r\\n`` or ``\\n\\n``).

    Returns ``(event, separator, rest)``. If no boundary is present, returns
    ``(buf, b"", b"")`` so the caller can buffer until more data arrives.
    """
    crlf_idx = buf.find(b"\r\n\r\n")
    lf_idx = buf.find(b"\n\n")

    if crlf_idx == -1 and lf_idx == -1:
        return buf, b"", b""

    if crlf_idx != -1 and (lf_idx == -1 or crlf_idx <= lf_idx):
        return buf[:crlf_idx], b"\r\n\r\n", buf[crlf_idx + 4 :]
    return buf[:lf_idx], b"\n\n", buf[lf_idx + 2 :]


class EnvelopeUnwrapStream:
    """Stateful SSE stream transformer that unwraps the v1internal envelope.

    cloudcode-pa emits chunks like ``data: {"response": {"candidates": [...]}}``.
    Standard Gemini SDK clients expect ``data: {"candidates": [...]}``. This
    transformer parses each event and unwraps the inner ``response`` object.

    Mirrors the protocol of :class:`ccproxy.lightllm.dispatch.SseTransformer`:
    a callable ``(bytes) -> bytes | Iterable[bytes]`` installed as
    ``flow.response.stream``. Tees raw input chunks for ``raw_body`` capture.
    """

    def __init__(self) -> None:
        self._buf = b""
        self._raw_chunks: list[bytes] = []

    def __call__(self, data: bytes) -> bytes | Iterable[bytes]:
        self._raw_chunks.append(data)

        if data == b"":
            return b""

        self._buf += data
        out = bytearray()

        while True:
            event, sep, rest = _split_event(self._buf)
            if not sep:
                break
            self._buf = rest
            out += self._process_event(event) + sep

        return bytes(out)

    def _process_event(self, event: bytes) -> bytes:
        payloads: list[bytes] = []
        prefix_lines: list[bytes] = []
        for line in event.split(b"\n"):
            stripped = line.strip()
            if stripped.startswith(b"data:"):
                payloads.append(stripped[5:].strip())
            elif stripped:
                prefix_lines.append(stripped)

        if not payloads:
            return event

        raw = b"\n".join(payloads)
        if raw == b"[DONE]":
            return event

        try:
            chunk = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug("gemini_cli: skipping unparseable SSE chunk")
            return event

        inner = chunk.get("response") if isinstance(chunk, dict) else None
        unwrapped = inner if isinstance(inner, dict) else chunk

        out = bytearray()
        for line in prefix_lines:
            out += line + b"\n"
        out += b"data: " + json.dumps(unwrapped).encode()
        return bytes(out)

    @property
    def raw_body(self) -> bytes:
        """Reassembled raw provider response body (pre-unwrap)."""
        return b"".join(self._raw_chunks)


def unwrap_buffered(content: bytes) -> bytes:
    """Strip cloudcode-pa's {response: {...}} envelope from a buffered body.

    Returns the inner ``response`` object as JSON bytes. Returns the input
    unchanged on parse failure or when the envelope key is absent. Mirrors
    the silent-fail behavior of InspectorAddon._unwrap_gemini_response.
    """
    if not content:
        return content
    try:
        body = json.loads(content)
    except (ValueError, TypeError):
        return content
    inner = body.get("response") if isinstance(body, dict) else None
    if isinstance(inner, dict):
        return json.dumps(inner).encode()
    return content


__all__ = ["EnvelopeUnwrapStream", "unwrap_buffered"]
