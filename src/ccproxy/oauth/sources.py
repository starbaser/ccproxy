"""OAuth credential sources — discriminated union with polymorphic ``resolve``.

Configuration shape in ``ccproxy.yaml``, nested under each Provider's ``auth``::

    providers:
      anthropic:
        auth:
          type: command
          command: "jq -r '.access_token' ~/.claude/.credentials.json"
          header: authorization
        host: api.anthropic.com
        path: /v1/messages
        provider: anthropic
      claude_oauth:
        auth:
          type: anthropic_oauth
          refresh_token_file: "~/.config/ccproxy/oauth/anthropic.json"
          header: authorization
        host: api.anthropic.com
        path: /v1/messages
        provider: anthropic

The discriminated union dispatches via the ``type`` field. Bare command
strings and dict-without-type forms are resolved via ``parse_oauth_source``.
"""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

logger = logging.getLogger(__name__)


def _read_credential_file(path_str: str, label: str) -> str | None:
    """Read a credential value from a file. Returns None on failure."""
    try:
        path = Path(path_str).expanduser().resolve()
        if not path.is_file():
            logger.error("%s file not found: %s", label, path)
            return None
        value = path.read_text().strip()
        if not value:
            logger.error("%s file is empty: %s", label, path)
            return None
        return value
    except Exception as e:
        logger.error("Failed to read %s file: %s", label, e)
        return None


def _run_credential_command(cmd: str, label: str) -> str | None:
    """Run a shell command and return its stdout. Returns None on failure."""
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=5)  # noqa: S602
        if result.returncode != 0:
            logger.error("%s command failed (exit %d): %s", label, result.returncode, result.stderr.strip())
            return None
        value = result.stdout.strip()
        if not value:
            logger.error("%s command returned empty output", label)
            return None
        return value
    except subprocess.TimeoutExpired:
        logger.error("%s command timed out after 5 seconds", label)
        return None
    except Exception as e:
        logger.error("Failed to execute %s command: %s", label, e)
        return None


class CredentialSource(BaseModel):
    """Generic credential source for non-OAuth use cases (mitmweb password, etc.).

    Exactly one of ``command`` or ``file`` must be provided.
    """

    command: str | None = None
    """Shell command that outputs the credential value."""

    file: str | None = None
    """File path to read (contents stripped of whitespace)."""

    @model_validator(mode="after")
    def _validate_source(self) -> CredentialSource:
        if self.command and self.file:
            raise ValueError("Specify either 'command' or 'file', not both")
        if not self.command and not self.file:
            raise ValueError("Must specify either 'command' or 'file'")
        return self

    def resolve(self, label: str = "credential") -> str | None:
        """Resolve the credential value. Returns None on failure."""
        if self.file:
            return _read_credential_file(self.file, label)
        if self.command:
            return _run_credential_command(self.command, label)
        return None


class _OAuthFields(BaseModel):
    """Fields common to all OAuthSource subclasses."""

    model_config = ConfigDict(extra="ignore")

    header: str | None = None
    """Target header name (e.g. ``x-api-key``). When set, the resolved token
    is injected as a raw value into this header. ``None`` (default) sends
    ``Authorization: Bearer {token}``."""


class CommandOAuthSource(_OAuthFields):
    """OAuth token resolved by running a shell command."""

    type: Literal["command"] = "command"
    command: str

    def resolve(self, label: str = "OAuth") -> str | None:
        return _run_credential_command(self.command, label)


class FileOAuthSource(_OAuthFields):
    """OAuth token read directly from a file (already-resolved access_token)."""

    type: Literal["file"] = "file"
    file: str

    def resolve(self, label: str = "OAuth") -> str | None:
        return _read_credential_file(self.file, label)


class AnthropicOAuthSource(_OAuthFields):
    """OAuth source that refreshes Anthropic tokens in-process via claude.ai/v1/oauth/token.

    Reads ``refresh_token_file`` (JSON containing ``refresh_token`` +
    ``access_token`` + ``expires_at``). When the cached access_token is
    within 60s of expiry, POSTs ``grant_type=refresh_token`` to ``endpoint``,
    atomically writes the new tokens back, and returns the new access_token.
    """

    type: Literal["anthropic_oauth"]
    refresh_token_file: str = "~/.config/ccproxy/oauth/anthropic.json"  # noqa: S105 (filename, not a secret)
    client_id: str = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
    endpoint: str = "https://claude.ai/v1/oauth/token"

    def resolve(self, label: str = "AnthropicOAuth") -> str | None:
        from ccproxy.oauth.anthropic import resolve_anthropic_token
        return resolve_anthropic_token(self, label=label)


class GoogleOAuthSource(_OAuthFields):
    """OAuth source that refreshes Google/Gemini tokens in-process via oauth2.googleapis.com.

    Reads ``refresh_token_file`` (JSON written by gemini-cli into
    ``~/.gemini/oauth_creds.json``). When the cached access_token is within
    60s of expiry (per ``expiry_field``, expressed in milliseconds), POSTs
    ``grant_type=refresh_token`` to ``endpoint``. The refresh response may
    omit ``refresh_token`` (gemini-cli #21691 upstream bug); this resolver
    preserves the existing on-disk ``refresh_token`` in that case so the
    next refresh still succeeds.
    """

    type: Literal["google_oauth"]
    refresh_token_file: str = "~/.gemini/oauth_creds.json"  # noqa: S105 (filename, not a secret)
    client_id: str
    client_secret: str
    endpoint: str = "https://oauth2.googleapis.com/token"
    expiry_field: str = "expiry_date"
    """Name of the expiry field in the refresh-token JSON. gemini-cli writes ``expiry_date`` (ms-since-epoch)."""

    def resolve(self, label: str = "GoogleOAuth") -> str | None:
        from ccproxy.oauth.google import resolve_google_token
        return resolve_google_token(self, label=label)


OAuthSource = CommandOAuthSource | FileOAuthSource | AnthropicOAuthSource | GoogleOAuthSource


def parse_oauth_source(raw: str | dict[str, Any] | OAuthSource) -> OAuthSource:
    """Resolve a raw ``Provider.auth`` value into a typed OAuthSource subclass.

    Accepts:
    - bare string → ``CommandOAuthSource(command=raw)``
    - dict with ``type`` field → discriminated dispatch
    - dict with only ``command``/``file`` keys (no ``type``) → inferred
    - already-typed OAuthSource → passthrough
    """
    if isinstance(raw, str):
        return CommandOAuthSource(command=raw)
    if isinstance(raw, _OAuthFields):
        return raw  # already typed
    if isinstance(raw, dict):
        type_ = raw.get("type")
        if type_ == "anthropic_oauth":
            return AnthropicOAuthSource(**raw)
        if type_ == "google_oauth":
            return GoogleOAuthSource(**raw)
        if type_ == "file" or ("file" in raw and "type" not in raw):
            return FileOAuthSource(**raw)
        if type_ == "command" or ("command" in raw and "type" not in raw):
            return CommandOAuthSource(**raw)
        raise ValueError(
            f"Cannot infer OAuthSource type from keys {list(raw.keys())!r}; "
            f"specify 'type: command|file|anthropic_oauth|google_oauth'",
        )
    raise TypeError(f"Unsupported auth entry: {type(raw).__name__}")


def atomic_write_back(path: Path, data: dict[str, Any]) -> None:
    """Atomically rewrite a JSON credential file at ``path`` with mode 0o600.

    Writes to a tempfile in the same directory (so ``rename`` is atomic
    on the same filesystem), fsyncs, renames, then chmods.
    """
    import json
    import os
    import stat
    import tempfile

    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd: int | None = None
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=path.parent,
            delete=False,
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as tf:
            json.dump(data, tf)
            tf.flush()
            os.fsync(tf.fileno())
            tmp_path = Path(tf.name)
        tmp_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        tmp_path.replace(path)
        tmp_path = None
    finally:
        if tmp_fd is not None:
            os.close(tmp_fd)
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


_REFRESH_HEADROOM_MS = 60_000
"""Refresh access_token when it expires in under 60 seconds."""


def needs_refresh(expiry_ms: float, now_ms: float | None = None) -> bool:
    """True when the cached access_token is within ``_REFRESH_HEADROOM_MS`` of expiry."""
    if now_ms is None:
        now_ms = time.time() * 1000
    return (expiry_ms - now_ms) <= _REFRESH_HEADROOM_MS
