"""Merge a compliance profile onto a pipeline Context.

All merge operations are idempotent — applying a profile twice
produces the same result as applying it once.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import TYPE_CHECKING, Any

from ccproxy.compliance.models import ComplianceProfile

if TYPE_CHECKING:
    from ccproxy.pipeline.context import Context

logger = logging.getLogger(__name__)


def merge_profile(ctx: Context, profile: ComplianceProfile) -> None:
    """Apply a compliance profile to a pipeline context.

    Adds missing headers, merges body envelope fields, wraps system
    prompt, and synthesizes session metadata. Does not overwrite
    values the user explicitly set.
    """
    _merge_headers(ctx, profile)
    _merge_session_metadata(ctx, profile)
    _merge_body_fields(ctx, profile)
    _merge_system(ctx, profile)


def _merge_headers(ctx: Context, profile: ComplianceProfile) -> None:
    """Add profile headers that are missing from the request."""
    for feature in profile.headers:
        existing = ctx.get_header(feature.name)
        if not existing:
            ctx.set_header(feature.name, feature.value)
            logger.debug("Compliance: added header %s", feature.name)


# Body fields that are feature config, not compliance — never stamped
_BODY_MERGE_EXCLUSIONS = frozenset({
    "thinking",
    "context_management",
    "output_config",
})


def _merge_body_fields(ctx: Context, profile: ComplianceProfile) -> None:
    """Add compliance-relevant body envelope fields that are missing.

    Skips feature config fields (thinking, context_management, output_config)
    which are user choices, not compliance requirements.
    """
    body = ctx._body
    for feature in profile.body_fields:
        if feature.path in _BODY_MERGE_EXCLUSIONS:
            continue
        if feature.path not in body:
            body[feature.path] = feature.value
            logger.debug("Compliance: added body field %s", feature.path)


def _merge_system(ctx: Context, profile: ComplianceProfile) -> None:
    """Wrap the user's system prompt in the profile's learned structure."""
    if profile.system is None:
        return

    profile_blocks = profile.system.structure
    if not profile_blocks:
        return

    current = ctx.system

    if current is None:
        ctx.system = profile_blocks
        return

    if isinstance(current, str):
        ctx.system = [*profile_blocks, {"type": "text", "text": current}]
        return

    if isinstance(current, list):
        if _system_has_prefix(current, profile_blocks):
            return
        ctx.system = [*profile_blocks, *current]


def _system_has_prefix(current: list[dict[str, Any]], prefix: list[dict[str, Any]]) -> bool:
    """Check if current system blocks already start with the profile prefix."""
    if len(current) < len(prefix):
        return False

    for i, pblock in enumerate(prefix):
        cblock = current[i]
        if pblock.get("type") != cblock.get("type"):
            return False
        if pblock.get("text") != cblock.get("text"):
            return False

    return True


def _merge_session_metadata(ctx: Context, profile: ComplianceProfile) -> None:
    """Synthesize session metadata from profile identity fields.

    Uses device_id and account_uuid from the profile, generates a
    fresh session_id. Only applies if metadata.user_id is absent.
    """
    # Find identity fields in profile body features
    device_id: str | None = None
    account_uuid: str | None = None

    for feature in profile.body_fields:
        if feature.path == "metadata" and isinstance(feature.value, dict):
            user_id_raw = feature.value.get("user_id")
            if user_id_raw:
                identity_out: dict[str, Any] = {}
                _extract_identity(str(user_id_raw), identity_out)
                device_id = identity_out.get("device_id")
                account_uuid = identity_out.get("account_uuid")

    if not device_id and not account_uuid:
        return

    metadata = ctx._body.setdefault("metadata", {})
    if metadata.get("user_id"):
        return

    identity: dict[str, Any] = {}
    if device_id:
        identity["device_id"] = device_id
    if account_uuid:
        identity["account_uuid"] = account_uuid
    identity["session_id"] = str(uuid.uuid4())

    metadata["user_id"] = json.dumps(identity)
    logger.debug("Compliance: synthesized session metadata")


def _extract_identity(user_id_str: str, out: dict[str, Any]) -> None:
    """Parse identity fields from a user_id JSON string."""
    try:
        data = json.loads(user_id_str)
        if isinstance(data, dict):
            if "device_id" in data:
                out["device_id"] = data["device_id"]
            if "account_uuid" in data:
                out["account_uuid"] = data["account_uuid"]
    except (json.JSONDecodeError, TypeError):
        pass
