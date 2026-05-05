"""Tests for the cloudcode-pa envelope-unwrap primitives."""

from __future__ import annotations

import json

from ccproxy.hooks.gemini_envelope import EnvelopeUnwrapStream, unwrap_buffered


class TestEnvelopeUnwrapStream:
    def test_buffered_response_unwraps_envelope(self) -> None:
        stream = EnvelopeUnwrapStream()
        chunk = b'data: {"response": {"candidates": [{"content": {"parts": [{"text": "hi"}]}}]}}\n\n'

        out = stream(chunk)

        assert isinstance(out, bytes)
        parsed = json.loads(out.split(b"data: ", 1)[1].rstrip(b"\n\n"))
        assert "candidates" in parsed
        assert parsed["candidates"][0]["content"]["parts"][0]["text"] == "hi"

    def test_crlf_separator_unwraps_envelope(self) -> None:
        """cloudcode-pa uses CRLF (\\r\\n\\r\\n) — must be handled."""
        stream = EnvelopeUnwrapStream()
        chunk = b'data: {"response": {"candidates": [{"x": 1}]}}\r\n\r\n'

        out = stream(chunk)

        assert b'"x": 1' in out
        assert b"response" not in out
        assert out.endswith(b"\r\n\r\n")

    def test_multiple_chunks_unwrapped_independently(self) -> None:
        stream = EnvelopeUnwrapStream()
        chunk1 = b'data: {"response": {"candidates": [{"a": 1}]}}\n\n'
        chunk2 = b'data: {"response": {"candidates": [{"b": 2}]}}\n\n'

        out1 = stream(chunk1)
        out2 = stream(chunk2)

        assert b'"a": 1' in out1 and b"response" not in out1
        assert b'"b": 2' in out2 and b"response" not in out2

    def test_partial_chunk_buffered_until_double_newline(self) -> None:
        stream = EnvelopeUnwrapStream()
        out1 = stream(b'data: {"response": {"x":')
        out2 = stream(b" 1}}\n\n")

        assert out1 == b""
        assert b'"x": 1' in out2

    def test_done_marker_passes_through(self) -> None:
        stream = EnvelopeUnwrapStream()
        out = stream(b"data: [DONE]\n\n")
        assert b"[DONE]" in out

    def test_unparseable_json_passes_through(self) -> None:
        stream = EnvelopeUnwrapStream()
        out = stream(b"data: not-valid-json\n\n")
        assert b"not-valid-json" in out

    def test_chunk_without_response_field_passes_through(self) -> None:
        stream = EnvelopeUnwrapStream()
        out = stream(b'data: {"candidates": [{"x": 1}]}\n\n')
        parsed = json.loads(out.split(b"data: ", 1)[1].rstrip(b"\n\n"))
        assert parsed == {"candidates": [{"x": 1}]}

    def test_raw_body_accumulates_input_chunks(self) -> None:
        stream = EnvelopeUnwrapStream()
        stream(b'data: {"response": {"a": 1}}\n\n')
        stream(b'data: {"response": {"b": 2}}\n\n')

        raw = stream.raw_body
        assert b'{"response": {"a": 1}}' in raw
        assert b'{"response": {"b": 2}}' in raw

    def test_empty_input_returns_empty(self) -> None:
        stream = EnvelopeUnwrapStream()
        assert stream(b"") == b""


class TestUnwrapBuffered:
    def test_strips_envelope_returns_inner_object(self) -> None:
        content = b'{"response": {"candidates": [{"content": {"parts": [{"text": "hi"}]}}]}}'

        out = unwrap_buffered(content)

        parsed = json.loads(out)
        assert parsed == {"candidates": [{"content": {"parts": [{"text": "hi"}]}}]}

    def test_missing_envelope_key_returns_input_unchanged(self) -> None:
        content = b'{"foo": "bar"}'

        out = unwrap_buffered(content)

        assert out == content

    def test_unparseable_json_returns_input_unchanged(self) -> None:
        content = b"not json"

        out = unwrap_buffered(content)

        assert out == content

    def test_empty_bytes_returns_input_unchanged(self) -> None:
        out = unwrap_buffered(b"")

        assert out == b""

    def test_non_dict_inner_returns_input_unchanged(self) -> None:
        content = b'{"response": "string-not-dict"}'

        out = unwrap_buffered(content)

        assert out == content
