"""Gemini OAuth auto-refresh hook — workaround for google-gemini/gemini-cli#21691.

Gemini CLI's OAuth refresh path has an upstream bug: when Google returns a new
access_token, the payload does not include refresh_token, and the CLI overwrites
``~/.gemini/oauth_creds.json`` entirely — wiping the persisted refresh_token. At
the next expiry (~1hr later), the CLI fails with ``API Error: No refresh token is
set`` and gets stuck in a ``Failed to clear OAuth credentials`` loop, blocking
recovery.

This hook works around the bug by:

1. Stashing the current refresh_token (in memory + on disk) before any refresh.
2. Running ``gemini -m gemini-2.5-flash -p hi`` to trigger Gemini CLI's refresh.
3. If ``oauth_creds.json`` is missing refresh_token after the CLI runs, merging
   the stashed refresh_token back in atomically.
4. Reloading ccproxy's token cache so ``forward_oauth`` picks up the new
   access_token.

If we reach a state where we have no stash AND the CLI fails with the bug's
signature errors, the hook logs a prominent warning telling the user to
``rm ~/.gemini/oauth_creds.json`` and re-auth via browser. The request then
falls through to the original 401.

This is a Gemini-specific workaround — it is NOT a generic OAuth refresh pattern.
See the upstream bug for the root cause:
  https://github.com/google-gemini/gemini-cli/issues/21691
"""

from __future__ import annotations

import json
import logging
import os
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from ccproxy.pipeline.hook import hook

if TYPE_CHECKING:
    from ccproxy.pipeline.context import Context

logger = logging.getLogger(__name__)

_GEMINI_CREDS_PATH = Path.home() / ".gemini" / "oauth_creds.json"
_BACKUP_PATH = Path.home() / ".ccproxy" / "gemini_refresh_token.bak"
_REFRESH_CMD = "gemini -m gemini-2.5-flash -p hi 2>/dev/null"
_EXPIRY_BUFFER_MS = 120_000  # Refresh when < 2 minutes remaining
_REFRESH_TIMEOUT_SEC = 30
_PROXY_ENV_VARS = frozenset(
    {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "http_proxy",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
    }
)
_BUG_SIGNATURES = ("No refresh token is set", "Failed to clear OAuth credentials")

_refresh_token_stash: str | None = None


def gemini_oauth_refresh_guard(ctx: Context) -> bool:
    """Only run for requests destined to Gemini endpoints."""
    host = ctx.get_header("host", "").lower()
    return "googleapis.com" in host


@hook(
    reads=[],
    writes=["authorization", "x-api-key"],
)
def gemini_oauth_refresh(ctx: Context, _: dict[str, Any]) -> Context:
    """Preemptively refresh Gemini OAuth token; work around #21691 refresh_token wipe."""
    creds = _read_creds()
    if creds is None:
        return ctx

    _maybe_stash_refresh_token(creds)

    remaining_ms = int(creds.get("expiry_date", 0)) - (time.time() * 1000)
    if remaining_ms > _EXPIRY_BUFFER_MS:
        return ctx

    logger.info(
        "Gemini OAuth token expires in %.0fs — running refresh command",
        max(remaining_ms, 0) / 1000,
    )

    rc, stderr = _run_refresh_cli()

    new_creds = _read_creds()
    if new_creds is not None:
        if not new_creds.get("refresh_token"):
            stashed = _refresh_token_stash or _read_disk_backup()
            if stashed:
                new_creds["refresh_token"] = stashed
                _write_creds_atomic(new_creds)
                logger.info("Restored Gemini refresh_token after CLI wiped it (#21691 workaround)")
            elif any(sig in stderr for sig in _BUG_SIGNATURES):
                logger.warning(
                    "Gemini OAuth is in an unrecoverable state (#21691). "
                    "No backup refresh_token available. "
                    "Delete ~/.gemini/oauth_creds.json and re-auth via `gemini` to recover.",
                )
        else:
            _maybe_stash_refresh_token(new_creds)

    if rc != 0:
        logger.warning("Gemini CLI refresh exited %d: %s", rc, stderr or "(no stderr)")

    try:
        from ccproxy.config import get_config

        _token, changed = get_config().refresh_oauth_token("gemini")
        if changed:
            logger.info("Gemini OAuth token refreshed in ccproxy cache")
    except Exception:
        logger.exception("Failed to refresh Gemini token in ccproxy cache")

    return ctx


def _read_creds() -> dict[str, Any] | None:
    """Read ~/.gemini/oauth_creds.json. Return None on any failure."""
    if not _GEMINI_CREDS_PATH.is_file():
        return None
    try:
        data = json.loads(_GEMINI_CREDS_PATH.read_text())
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("Cannot read Gemini creds file: %s", e)
        return None
    if not isinstance(data, dict):
        return None
    return cast(dict[str, Any], data)


def _maybe_stash_refresh_token(creds: dict[str, Any]) -> None:
    """Cache the refresh_token in memory + disk if it's new."""
    global _refresh_token_stash
    rt = creds.get("refresh_token")
    if not rt or rt == _refresh_token_stash:
        return
    _refresh_token_stash = rt
    try:
        _BACKUP_PATH.parent.mkdir(parents=True, exist_ok=True)
        _BACKUP_PATH.write_text(rt)
        _BACKUP_PATH.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError as e:
        logger.debug("Cannot write refresh_token backup: %s", e)


def _read_disk_backup() -> str | None:
    """Read the last-known-good refresh_token from disk backup."""
    try:
        if _BACKUP_PATH.is_file():
            return _BACKUP_PATH.read_text().strip() or None
    except OSError as e:
        logger.debug("Cannot read refresh_token backup: %s", e)
    return None


def _write_creds_atomic(creds: dict[str, Any]) -> None:
    """Atomically rewrite ~/.gemini/oauth_creds.json preserving 0600 perms."""
    tmp_dir = _GEMINI_CREDS_PATH.parent
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=tmp_dir,
            delete=False,
            prefix=".oauth_creds.",
            suffix=".tmp",
        ) as tf:
            json.dump(creds, tf)
            tmp_path = Path(tf.name)
        tmp_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        tmp_path.replace(_GEMINI_CREDS_PATH)
    except OSError as e:
        logger.warning("Failed to rewrite Gemini creds file: %s", e)


def _run_refresh_cli() -> tuple[int, str]:
    """Run the Gemini CLI to force an OAuth refresh. Return (returncode, stderr)."""
    env = {k: v for k, v in os.environ.items() if k not in _PROXY_ENV_VARS}
    try:
        result = subprocess.run(  # noqa: S602
            _REFRESH_CMD,
            shell=True,
            env=env,
            capture_output=True,
            timeout=_REFRESH_TIMEOUT_SEC,
            check=False,
        )
        return result.returncode, result.stderr.decode(errors="replace").strip()
    except subprocess.TimeoutExpired:
        logger.warning("Gemini CLI refresh timed out after %ds", _REFRESH_TIMEOUT_SEC)
        return -1, "timeout"
    except Exception as e:
        logger.exception("Gemini CLI refresh raised unexpected error")
        return -1, str(e)
