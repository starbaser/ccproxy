"""End-to-end tests for the gemini_cli hook against the live Gemini API.

Skipped by default (excluded via ``-m "not e2e"`` in pyproject.toml). Run with::

    uv run pytest -m e2e tests/test_gemini_cli_e2e.py

Prereqs:
    * ccproxy running on the URL specified by ``CCPROXY_E2E_URL``
      (default ``http://127.0.0.1:4001`` — dev instance).
      Start with ``just up``.
    * Valid Gemini OAuth creds at ``~/.gemini/oauth_creds.json``.
      Run ``gemini -p ""`` once if missing.

These tests catch regressions caused by external changes:
    * Google deprecating or modifying ``v1internal``
    * ``cloudcode-pa.googleapis.com`` rate limit / capacity changes
    * OAuth token format / scope changes
    * Response envelope structure drift
    * Capacity tier degradation from user-agent fingerprint changes
"""

from __future__ import annotations

import base64
import os
import time
from pathlib import Path

import httpx
import pytest

CCPROXY_BASE = os.environ.get("CCPROXY_E2E_URL", "http://127.0.0.1:4001")
GEMINI_CREDS = Path.home() / ".gemini" / "oauth_creds.json"
SENTINEL_KEY = "sk-ant-oat-ccproxy-gemini"
MODEL = os.environ.get("CCPROXY_E2E_GEMINI_MODEL", "gemini-3.1-pro-preview")

# 32x32 solid red PNG. Large enough that Gemini accepts it as an image
# (1x1 PNGs are rejected as "Provided image is not valid"). Generated with
# Pillow as RGB(220, 20, 20) and embedded — no test-time dependency.
_RED_32X32_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAIAAAD8GO2jAAAAK0lEQVR4nO3NQQEAAATAQGTQ"
    "P5kwSvC7BdjldMdn9XoHAAAAAAAAAAAAhy3gIwFE6inHLwAAAABJRU5ErkJggg=="
)
RED_32X32_PNG = base64.b64decode(_RED_32X32_PNG_B64)


def _ccproxy_reachable() -> bool:
    try:
        httpx.head(CCPROXY_BASE, timeout=2)
    except httpx.HTTPError:
        return False
    return True


pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not GEMINI_CREDS.exists(), reason=f"{GEMINI_CREDS} not found"),
    pytest.mark.skipif(not _ccproxy_reachable(), reason=f"ccproxy not reachable at {CCPROXY_BASE}"),
]


@pytest.fixture
def client():
    from google import genai
    from google.genai import types

    return genai.Client(
        api_key=SENTINEL_KEY,
        http_options=types.HttpOptions(base_url=f"{CCPROXY_BASE}/gemini"),
    )


@pytest.fixture(autouse=True)
def _space_requests():
    """cloudcode-pa rate-limits aggressively; space requests across tests."""
    yield
    time.sleep(2)


def _call_with_retry(fn, *, retries: int = 2, backoff: float = 3.0):
    """Call ``fn`` retrying on cloudcode-pa transient errors (429/5xx).

    Skips the test entirely if transients persist past ``retries`` — these
    are external environmental issues (rate limit, backend flake), not code
    regressions. A code regression would surface as a 4xx (other than 429),
    malformed body, or wrong response shape.
    """
    from google.genai import errors

    for attempt in range(retries + 1):
        try:
            return fn()
        except errors.ClientError as e:
            if e.code == 429 and attempt < retries:
                time.sleep(backoff * (attempt + 1))
                continue
            if e.code == 429:
                pytest.skip(f"cloudcode-pa rate limit (429) persisted across {retries + 1} attempts")
            raise
        except errors.ServerError as e:
            if attempt < retries:
                time.sleep(backoff * (attempt + 1))
                continue
            pytest.skip(f"cloudcode-pa server error ({e.code}) persisted across {retries + 1} attempts")
    raise AssertionError("unreachable")


def test_non_streaming_text_request(client) -> None:
    """Round-trips a text request through ccproxy → cloudcode-pa → back.

    Verifies: sentinel resolution, envelope wrap, project resolution, path
    rewrite, response unwrap. Failure here typically signals an external
    change (token expired, model deprecated, envelope schema drift).
    """
    response = _call_with_retry(
        lambda: client.models.generate_content(
            model=MODEL,
            contents="Reply with exactly the single word: pong",
        )
    )
    assert response.text is not None
    assert "pong" in response.text.lower()


def test_streaming_text_request(client) -> None:
    """Streaming response: each SSE chunk's v1internal envelope must unwrap.

    A regression in EnvelopeUnwrapStream or in the cloudcode-pa response
    schema would surface here as empty/malformed chunks.
    """

    def _stream():
        chunks: list[str] = []
        count = 0
        for chunk in client.models.generate_content_stream(
            model=MODEL,
            contents="Count from 1 to 5, one number per line.",
        ):
            count += 1
            if chunk.text:
                chunks.append(chunk.text)
        return count, chunks

    chunks_received, text_collected = _call_with_retry(_stream)

    assert chunks_received > 0, "no SSE chunks received"
    full = "".join(text_collected)
    for n in ("1", "2", "3", "4", "5"):
        assert n in full, f"missing {n!r} in streamed response: {full!r}"


def test_image_payload(client) -> None:
    """Multi-byte inline image data flows through unchanged.

    The Glass-equivalent capability: large base64 image payloads in
    ``contents[].parts[].inlineData`` survive the envelope wrap and
    reach Gemini intact.
    """
    from google.genai import types

    response = _call_with_retry(
        lambda: client.models.generate_content(
            model=MODEL,
            contents=[
                "What color is this image? Reply with one word.",
                types.Part.from_bytes(data=RED_32X32_PNG, mime_type="image/png"),
            ],
        )
    )
    assert response.text is not None
    assert "red" in response.text.lower()


def test_native_v1internal_client_passthrough() -> None:
    """Glass-style native v1internal request passes through idempotently.

    The hook detects already-wrapped bodies (``request`` key, no ``contents``)
    and skips the envelope step. Validates that Glass's pattern still works.
    """
    body = {
        "model": MODEL,
        "request": {
            "contents": [{"role": "user", "parts": [{"text": "Reply with: ok"}]}],
            "generationConfig": {"maxOutputTokens": 32, "temperature": 0.0},
        },
    }
    headers = {"x-api-key": SENTINEL_KEY, "Content-Type": "application/json"}
    url = f"{CCPROXY_BASE}/v1internal:generateContent"

    retries = 2
    for attempt in range(retries + 1):
        resp = httpx.post(url, json=body, headers=headers, timeout=30)
        if resp.status_code < 500 and resp.status_code != 429:
            break
        if attempt < retries:
            time.sleep(3.0 * (attempt + 1))
            continue
        pytest.skip(f"cloudcode-pa transient {resp.status_code} persisted across {retries + 1} attempts")

    assert resp.status_code == 200, f"got {resp.status_code}: {resp.text}"
    data = resp.json()
    assert "candidates" in data, f"no candidates in response: {data}"
    text = data["candidates"][0]["content"]["parts"][0].get("text", "")
    assert "ok" in text.lower()
