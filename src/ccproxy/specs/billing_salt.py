"""Read user-supplied Anthropic billing salts from ``{config_dir}/billing_salts.json``.

Anthropic rotates the billing salt across claude-code releases, and each
salt is paired with the version embedded in that same release. The
``regenerate_billing_header`` hook needs the salt that pairs with the
version it's about to publish.

The salts live in ``{ccproxy_config_dir}/billing_salts.json`` — a JSON map
``{cc_version: salt}``. The path is fixed (no config field, no env var):
the user already controls config location via ``CCPROXY_CONFIG_DIR``, and
the salts file sits next to ``ccproxy.yaml``::

    {
      "2.1.26": "0123456789ab",
      "2.1.87": "fedcba987654"
    }

This file is not committed (``.gitignore`` excludes it). The user populates
it by extracting salts from their installed claude-code binary. When the
file is absent or doesn't contain the version embedded in the shape's
captured billing header, the regenerator hook no-ops with a warning.

Future work: extract salts at runtime from the user's installed claude-code
binary. When that lands, ``load_billing_salts`` is the only function to
update — call sites stay identical. Reference for the legacy ``cli.js``
anchor-search pattern: ``community/cchistory/src/core/cli-patcher.ts``.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from ccproxy.config import get_config_dir

logger = logging.getLogger(__name__)


_SALTS_FILENAME = "billing_salts.json"

_salts_cache: dict[str, str] | None = None
_salts_cache_mtime: float | None = None
_salts_cache_lock = threading.Lock()


def _salts_path() -> Path:
    return get_config_dir() / _SALTS_FILENAME


def load_billing_salts() -> dict[str, str]:
    """Return the version → salt map from ``{config_dir}/billing_salts.json``.

    Returns an empty dict when the file is missing, unparseable, or its
    JSON root isn't an object. Caches by mtime so live edits are picked
    up without restart.
    """
    global _salts_cache, _salts_cache_mtime

    path = _salts_path()
    if not path.is_file():
        return {}

    try:
        mtime = path.stat().st_mtime
    except OSError as exc:
        logger.debug("billing salts file stat failed: %s", exc)
        return {}

    with _salts_cache_lock:
        if _salts_cache is not None and _salts_cache_mtime == mtime:
            return _salts_cache

        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("billing salts file %s unreadable: %s", path, exc)
            return {}

        if not isinstance(data, dict):
            logger.warning("billing salts file %s is not a JSON object", path)
            return {}

        loaded = {str(k): str(v) for k, v in data.items() if isinstance(v, str)}
        _salts_cache = loaded
        _salts_cache_mtime = mtime
        return loaded


def clear_salts_cache() -> None:
    """Reset the in-memory salts cache (test cleanup)."""
    global _salts_cache, _salts_cache_mtime
    with _salts_cache_lock:
        _salts_cache = None
        _salts_cache_mtime = None


def get_billing_salt_for_version(version: str) -> str | None:
    """Return the salt that pairs with ``version``, or ``None`` if absent."""
    return load_billing_salts().get(version)
