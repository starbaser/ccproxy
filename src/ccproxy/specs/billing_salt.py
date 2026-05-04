"""Anthropic billing-header signing constants.

Both the salt (SHA-256 ``cc_version`` suffix ingredient) and the cch seed
(xxhash64 initialization) are reverse-engineered from the upstream client
binary, so neither is committed. Users supply them under
``shaping.providers.anthropic.billing.{salt,seed}`` in ``ccproxy.yaml``.
The values can be literals or ``${VAR}`` env references (expanded at
config load time — see ``ccproxy.config._expand_env_refs``). When either
is unset, ``regenerate_billing_header`` no-ops with a warning.
"""

from __future__ import annotations

import logging

from ccproxy.config import AnthropicShapingConfig, get_config

logger = logging.getLogger(__name__)


def _billing_config() -> tuple[str | None, str | None]:
    """Return ``(salt, seed_raw)`` from the Anthropic shaping profile."""
    profile = get_config().shaping.providers.get("anthropic")
    if not isinstance(profile, AnthropicShapingConfig):
        return (None, None)
    return (profile.billing.salt, profile.billing.seed)


def get_billing_salt() -> str | None:
    """Return the configured billing salt, or ``None`` if unset."""
    salt, _ = _billing_config()
    return salt or None


def get_billing_cch_seed() -> int | None:
    """Return the configured xxhash64 cch seed as an int, or ``None`` if unset.

    Always parsed as hex. Accepts ``"0x6E52..."`` or bare ``"6E52..."``.
    An unparseable value warns and returns ``None``.
    """
    _, raw = _billing_config()
    if not raw:
        return None
    cleaned = raw[2:] if raw.lower().startswith("0x") else raw
    try:
        return int(cleaned, 16)
    except ValueError:
        logger.warning("billing.seed=%r is not valid hex", raw)
        return None
