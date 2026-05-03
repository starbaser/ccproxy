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

from glom import assign, glom

from ccproxy.pipeline.context import Context
from ccproxy.pipeline.hook import hook
from ccproxy.specs import get_billing_salt_for_version
from ccproxy.utils import extract_first_user_text

logger = logging.getLogger(__name__)

_BILLING_HEADER_PREFIX = "x-anthropic-billing-header"

# The two content-derived tokens in the captured header. Each is replaced
# in-place with the value computed against the *incoming* first user message;
# everything else (version major, cc_entrypoint, formatting) stays as the
# shape captured it.
_VERSION_SUFFIX_RE = re.compile(r"(cc_version=[0-9]+(?:\.[0-9]+)*)\.[0-9a-f]{3}")
_CCH_RE = re.compile(r"cch=[0-9a-f]+")


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


def _compute_cch(text: str) -> str:
    """First 5 hex of ``sha256(text)``. Mirrors signing.ts:32-34."""
    return hashlib.sha256(text.encode()).hexdigest()[:5]


def _compute_suffix(text: str, salt: str, version: str) -> str:
    """3-hex suffix of ``sha256(salt + sampled + version)``.

    ``sampled`` is text characters at indices 4, 7, 20 padded with ``"0"``
    when the message is shorter. Mirrors signing.ts:42-51.
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

    Parses ``cc_version`` from the shape's existing billing block, looks up
    the matching salt in ``{config_dir}/billing_salts.json``, then rewrites
    the block in place: only the 3-hex ``cc_version`` suffix and the 5-hex
    ``cch`` token are replaced. ``cc_entrypoint``, formatting, position,
    and block extras like ``cache_control`` survive verbatim.

    The version comes from the shape (not config) because the shape carries
    the version embedded in the captured Claude client's release; the salt
    must pair with that exact version per Anthropic's server-side validation.

    Self-gates (no-op + warning):
    - ``messages`` absent or not a list (Gemini shape replays).
    - No existing billing block in the shape's ``system`` array.
    - Billing block missing the parseable ``cc_version`` or ``cch`` token.
    - No salt configured for the shape's version in
      ``{config_dir}/billing_salts.json``.
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
    salt = get_billing_salt_for_version(version)
    if salt is None:
        logger.warning(
            "no billing salt configured for cc_version=%s in billing_salts.json; "
            "skipping billing-header regeneration",
            version,
        )
        return ctx

    text = extract_first_user_text(messages=messages)
    cch = _compute_cch(text)
    suffix = _compute_suffix(text, salt, version)

    new_text = _VERSION_SUFFIX_RE.sub(f"cc_version={version}.{suffix}", original_text, count=1)
    new_text = _CCH_RE.sub(f"cch={cch}", new_text, count=1)

    new_block = {**system[idx], "text": new_text}
    new_system = list(system)
    new_system[idx] = new_block
    assign(ctx._body, "system", new_system)
    return ctx
