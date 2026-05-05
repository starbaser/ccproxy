"""Dynamic shaping hooks — DAG-ordered operations that can't be expressed as field injection.

Each hook is decorated with ``@hook(reads=..., writes=...)`` for DAG ordering
and receives ``(ctx, params) -> Context`` where ``ctx`` is the shape context.
The incoming pipeline context is available via ``params["incoming_ctx"]``.

Registered via dotted paths in ``shaping.providers.{name}.shape_hooks``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from typing import Any

import xxhash
from glom import assign, glom

from ccproxy.pipeline.context import Context
from ccproxy.pipeline.hook import hook
from ccproxy.specs import get_billing_cch_seed, get_billing_salt
from ccproxy.utils import extract_first_user_text

logger = logging.getLogger(__name__)

_BILLING_HEADER_PREFIX = "x-anthropic-billing-header"

# cch is xxhash64 of the serialized request body with a literal
# ``cch=00000;`` placeholder, masked to 20 bits → 5 lowercase hex.
_CCH_MASK = 0xFFFFF
_CCH_PLACEHOLDER = "00000"

# In-place rewrite tokens. ``cc_version=X.Y.Z.<3hex>`` — only the suffix
# changes; the major-version part stays as the shape captured it.
_VERSION_SUFFIX_RE = re.compile(r"(cc_version=[0-9]+(?:\.[0-9]+)*)\.[0-9a-f]{3}")
_CCH_RE = re.compile(r"cch=[0-9a-f]+")
# Byte-level placeholder substitution on the serialized body. Scoped to the
# billing header value (``[^"]*?`` stops at the JSON string terminator) so
# user message content can never spuriously match.
_CCH_BYTES_RE = re.compile(rb'(x-anthropic-billing-header:[^"]*?\bcch=)(00000)(;)')


@hook(reads=["user_prompt_id"], writes=["user_prompt_id"])
def regenerate_user_prompt_id(ctx: Context, params: dict[str, Any]) -> Context:
    """Re-roll ``user_prompt_id`` if the shape carries one."""
    if glom(ctx._body, "user_prompt_id", default=None) is not None:
        assign(ctx._body, "user_prompt_id", uuid.uuid4().hex[:13])
    return ctx


@hook(reads=["metadata.user_id"], writes=["metadata.user_id"])
def regenerate_session_id(ctx: Context, params: dict[str, Any]) -> Context:
    """Re-roll ``metadata.user_id.session_id`` if the shape carries one."""
    metadata = glom(ctx._body, "metadata", default=None)
    if not isinstance(metadata, dict):
        return ctx
    user_id_raw = glom(metadata, "user_id", default=None)
    if not isinstance(user_id_raw, str):
        return ctx
    try:
        identity: Any = json.loads(user_id_raw)
    except (json.JSONDecodeError, TypeError):
        return ctx
    if not isinstance(identity, dict):
        return ctx
    if "device_id" in identity or "account_uuid" in identity:
        identity["session_id"] = str(uuid.uuid4())
        metadata["user_id"] = json.dumps(identity)
    return ctx


def _compute_suffix(text: str, salt: str, version: str) -> str:
    """3-hex ``cc_version`` suffix.

    ``sha256(salt + sampled + version).hex[:3]`` where ``sampled`` is the
    text characters at indices 4, 7, 20 (padded with ``"0"`` for short
    messages). Confirmed by both Go reimplementations of the leaked
    claude-code source.
    """
    sampled = "".join(text[i] if i < len(text) else "0" for i in (4, 7, 20))
    return hashlib.sha256(f"{salt}{sampled}{version}".encode()).hexdigest()[:3]


def _find_billing_block_index(system: list[Any]) -> int | None:
    """Return the index of the first billing block in ``system``, or None."""
    for i, block in enumerate(system):
        if (
            isinstance(block, dict)
            and isinstance(block.get("text"), str)
            and block["text"].startswith(_BILLING_HEADER_PREFIX)
        ):
            return i
    return None


@hook(reads=["messages"], writes=["system"])
def regenerate_billing_header(ctx: Context, params: dict[str, Any]) -> Context:
    """Re-sign the shape's ``x-anthropic-billing-header`` against the incoming first user message.

    Two-phase signing:

    1. **In ``_body`` (typed layer)** — parse ``cc_version`` from the shape's
       existing billing block, look up the configured ``billing_salt``,
       compute the SHA-256 ``cc_version`` suffix against the incoming first
       user message, and stamp ``cch=00000;`` as a placeholder. The shape's
       ``cc_entrypoint``, formatting, position, and block extras (e.g.
       ``cache_control``) survive verbatim.

    2. **On serialized bytes (wire layer)** — force-commit to flush ``_body``
       through ``json.dumps``, then xxhash64 the resulting bytes with the
       configured seed masked to 20 bits, and substitute the ``cch=00000;``
       placeholder with the real 5-hex digest. Mirrors the upstream native
       algorithm: the JS layer ships a placeholder and the native HTTP stack
       swaps it for the real hash before send.

    The version comes from the shape (not config) because the shape's
    User-Agent and other release-pinned headers also come from the shape —
    everything advertised upstream stays internally consistent.

    Self-gates (no-op + warning):
    - ``messages`` absent or not a list (Gemini shape replays).
    - No existing billing block in the shape's ``system`` array.
    - Billing block missing the parseable ``cc_version`` or ``cch`` token.
    - No ``billing_salt`` configured.
    """
    messages = glom(ctx._body, "messages", default=None)
    if not isinstance(messages, list):
        return ctx

    system = glom(ctx._body, "system", default=None)
    if not isinstance(system, list):
        return ctx

    idx = _find_billing_block_index(system)
    if idx is None:
        logger.warning(
            "no billing header in shape; skipping billing-header regeneration "
            "(re-capture the shape from a real Claude client)",
        )
        return ctx

    original_text: str = system[idx]["text"]
    version_match = _VERSION_SUFFIX_RE.search(original_text)
    cch_match = _CCH_RE.search(original_text)
    if version_match is None or cch_match is None:
        logger.warning("billing header missing expected tokens; skipping regeneration")
        return ctx

    version = version_match.group(1).removeprefix("cc_version=")
    salt = get_billing_salt()
    seed = get_billing_cch_seed()
    if salt is None or seed is None:
        missing = ", ".join(name for name, value in (("salt", salt), ("seed", seed)) if value is None)
        logger.warning(
            "shaping.providers.anthropic.billing.%s unset; skipping billing-header regeneration",
            missing,
        )
        return ctx

    text = extract_first_user_text(messages=messages)
    suffix = _compute_suffix(text, salt, version)

    # Phase 1: stamp cc_version suffix + cch=00000 placeholder into _body.
    placeholder_text = _VERSION_SUFFIX_RE.sub(f"cc_version={version}.{suffix}", original_text, count=1)
    placeholder_text = _CCH_RE.sub(f"cch={_CCH_PLACEHOLDER}", placeholder_text, count=1)
    new_block = {**system[idx], "text": placeholder_text}
    new_system = list(system)
    new_system[idx] = new_block
    assign(ctx._body, "system", new_system)

    # Phase 2: serialize, xxhash64 over the bytes (with placeholder), substitute.
    ctx.commit()
    request = ctx._resolve_request()
    if request is None:  # defensive: every Context has either flow or _request
        return ctx
    body_bytes: bytes = request.content or b""
    if not _CCH_BYTES_RE.search(body_bytes):
        logger.warning("cch=00000 placeholder missing after commit; skipping cch sign")
        return ctx
    digest = xxhash.xxh64(body_bytes, seed=seed).intdigest() & _CCH_MASK
    cch_bytes = f"{digest:05x}".encode()
    signed_bytes = _CCH_BYTES_RE.sub(rb"\g<1>" + cch_bytes + rb"\g<3>", body_bytes, count=1)
    request.content = signed_bytes
    # Re-parse so the outer commit re-serializes to the same bytes.
    try:
        ctx._body = json.loads(signed_bytes)
    except (json.JSONDecodeError, TypeError):
        logger.warning("signed body failed to round-trip as JSON; leaving wire bytes intact")
    return ctx
