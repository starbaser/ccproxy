"""Auth credential sources — discriminated union with polymorphic ``resolve``.

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
          file_path: "~/.claude/.credentials.json"
          access_path: claudeAiOauth.accessToken
          refresh_path: claudeAiOauth.refreshToken
          expiry_path: claudeAiOauth.expiresAt
          header: authorization
        host: api.anthropic.com
        path: /v1/messages
        provider: anthropic

The discriminated union dispatches via the ``type`` field. Bare command
strings and dict-without-type forms are resolved via ``parse_auth_source``.
"""

from __future__ import annotations

import copy
import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Annotated, Any, Literal

import httpx
from glom import PathAccessError, assign, glom
from pydantic import BaseModel, ConfigDict, Field, model_validator

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


class AuthFields(BaseModel):
    """Fields common to every credential source.

    Just the target header for now. Pydantic config (extra="ignore") allows
    YAML carrying obsolete keys to load without error during the rename.
    """

    model_config = ConfigDict(extra="ignore")

    header: str | None = None
    """Target header name (e.g. ``x-api-key``). When set, the resolved token
    is injected as a raw value into this header. ``None`` (default) sends
    ``Authorization: Bearer {token}``."""


class CommandAuthSource(AuthFields):
    """Token resolved by running a shell command."""

    type: Literal["command"] = "command"
    command: str

    def resolve(self, label: str = "Auth") -> str | None:
        return _run_credential_command(self.command, label)


class FileAuthSource(AuthFields):
    """Token read directly from a file (already-resolved access_token)."""

    type: Literal["file"] = "file"
    file: str

    def resolve(self, label: str = "Auth") -> str | None:
        return _read_credential_file(self.file, label)


_REFRESH_TIMEOUT_SEC = 15.0


class AuthSource(AuthFields):
    """Base for OAuth refresh sources.

    Subclasses set defaults for ``type`` (Literal discriminator), ``file_path``,
    ``endpoint``, ``client_id``, optional ``client_secret``, and may override
    the default access/refresh/expiry glom paths to match a host CLI's
    credential schema.
    """

    type: str
    """Discriminator for the union. Subclasses narrow to a Literal."""

    file_path: str
    """Path to the JSON credential file (read on every resolve, atomically
    rewritten after refresh). Subclasses set the platform-conventional default
    (``~/.claude/.credentials.json`` for Anthropic shared with Claude Code CLI,
    ``~/.gemini/oauth_creds.json`` for gemini-cli)."""

    endpoint: str
    """OAuth token endpoint URL."""

    client_id: str

    client_secret: str | None = None
    """Required by Google's OAuth flow; absent on Anthropic's installed-app flow."""

    access_path: str = "access_token"
    """glom path to the access_token in the credential JSON."""

    refresh_path: str = "refresh_token"
    """glom path to the refresh_token."""

    expiry_path: str = "expires_at"
    """glom path to the expiry timestamp (ms-since-epoch)."""

    default_expires_in_seconds: int = 3600
    """Fallback when the refresh response omits ``expires_in``. Subclasses
    override (Anthropic: 36000 = 10h; Google: 3600 = 1h)."""

    def resolve(self, label: str = "Auth") -> str | None:
        """Read cached tokens; refresh if near expiry; return access_token.

        Atomic write-back of the merged response to ``file_path``. ``None``
        on any failure (file missing, parse error, refresh HTTP error,
        response missing access_token).
        """
        path = Path(self.file_path).expanduser()
        if not path.is_file():
            logger.error("%s credential file not found: %s", label, path)
            return None

        try:
            creds: dict[str, Any] = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("%s could not read %s: %s", label, path, exc)
            return None

        access, refresh, expiry = self._read_credentials(creds)

        if not isinstance(refresh, str) or not refresh:
            logger.error(
                "%s missing refresh_token at %r in %s",
                label,
                self.refresh_path,
                path,
            )
            return None

        if isinstance(access, str) and access and isinstance(expiry, int | float) and not needs_refresh(float(expiry)):
            return access

        logger.info("%s refreshing access_token", label)
        payload = self._refresh_token(refresh)
        if payload is None:
            return None

        new_access = payload.get("access_token")
        # gemini-cli #21691 workaround: keep the on-disk refresh_token if the
        # response omits it. Applies generally — the fallback is harmless even
        # for providers that always send a fresh refresh_token.
        new_refresh = payload.get("refresh_token") or refresh
        expires_in = int(payload.get("expires_in", self.default_expires_in_seconds))
        new_expiry = int(time.time() * 1000) + expires_in * 1000

        if not isinstance(new_access, str) or not new_access:
            logger.error("%s refresh response missing access_token: %r", label, payload)
            return None

        merged = self._write_credentials(creds, new_access, new_refresh, new_expiry)
        atomic_write_back(path, merged)
        return new_access

    def _read_credentials(self, creds: dict[str, Any]) -> tuple[Any, Any, Any]:
        """Read access_token, refresh_token, expiry via this source's glom paths.

        Returns ``(None, None, None)`` on any path that doesn't resolve.
        """

        def _get(path: str) -> Any:
            try:
                return glom(creds, path)
            except PathAccessError:
                return None

        return _get(self.access_path), _get(self.refresh_path), _get(self.expiry_path)

    def _write_credentials(
        self,
        creds: dict[str, Any],
        new_access: str,
        new_refresh: str,
        new_expiry: int,
    ) -> dict[str, Any]:
        """Deep-copy ``creds`` and assign new tokens at the configured glom paths.

        ``glom.assign(..., missing=dict)`` creates intermediate dicts for
        nested paths like ``claudeAiOauth.accessToken``. Existing sibling
        fields (``scopes``, ``subscriptionType``, anything else the host CLI
        wrote) survive verbatim because we deep-copy the input first.
        """
        merged = copy.deepcopy(creds)
        assign(merged, self.access_path, new_access, missing=dict)
        assign(merged, self.refresh_path, new_refresh, missing=dict)
        assign(merged, self.expiry_path, new_expiry, missing=dict)
        return merged

    def _refresh_token(
        self,
        refresh_token: str,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> dict[str, Any] | None:
        """POST to ``endpoint`` with the body from ``_build_refresh_body``."""
        body = self._build_refresh_body(refresh_token)
        try:
            client_kwargs: dict[str, Any] = {"timeout": _REFRESH_TIMEOUT_SEC}
            if transport is not None:
                client_kwargs["transport"] = transport
            with httpx.Client(**client_kwargs) as client:
                resp = client.post(
                    self.endpoint,
                    data=body,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
        except httpx.HTTPError as exc:
            logger.error("OAuth refresh failed: %s", exc)
            return None

        if resp.status_code != 200:
            logger.error(
                "OAuth refresh returned %d: %s",
                resp.status_code,
                resp.text[:500],
            )
            return None

        try:
            payload = resp.json()
        except (json.JSONDecodeError, ValueError) as exc:
            logger.error("OAuth refresh returned non-JSON: %s", exc)
            return None

        if not isinstance(payload, dict) or "access_token" not in payload:
            logger.error("OAuth refresh response missing access_token: %r", payload)
            return None

        return payload

    def _build_refresh_body(self, refresh_token: str) -> dict[str, str]:
        """Per-provider POST body. Subclasses override."""
        raise NotImplementedError


class AnthropicAuthSource(AuthSource):
    """Refreshes Anthropic tokens in-process via claude.ai/v1/oauth/token.

    Default ``file_path`` matches ccproxy's own location; point at
    ``~/.claude/.credentials.json`` (with the ``claudeAiOauth.*`` glom paths)
    to share state with the Claude Code CLI.
    """

    type: Literal["anthropic_oauth"] = "anthropic_oauth"
    file_path: str = "~/.config/ccproxy/oauth/anthropic.json"
    endpoint: str = "https://claude.ai/v1/oauth/token"
    client_id: str = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
    default_expires_in_seconds: int = 36000  # 10 hours

    def _build_refresh_body(self, refresh_token: str) -> dict[str, str]:
        return {
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "refresh_token": refresh_token,
        }


class GoogleAuthSource(AuthSource):
    """Refreshes Google/Gemini tokens in-process via oauth2.googleapis.com.

    Defaults match gemini-cli's on-disk credential layout
    (``~/.gemini/oauth_creds.json`` with ``expiry_date`` for the expiry
    timestamp). ``client_id`` and ``client_secret`` are user-supplied —
    gemini-cli's are public installed-app credentials embedded in its
    distribution; ccproxy does NOT vendor them.
    """

    type: Literal["google_oauth"] = "google_oauth"
    file_path: str = "~/.gemini/oauth_creds.json"
    endpoint: str = "https://oauth2.googleapis.com/token"
    expiry_path: str = "expiry_date"  # gemini-cli's field name
    default_expires_in_seconds: int = 3600

    def _build_refresh_body(self, refresh_token: str) -> dict[str, str]:
        if not self.client_secret:
            raise ValueError("GoogleAuthSource requires client_secret")
        return {
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": refresh_token,
        }


AnyAuthSource = Annotated[
    CommandAuthSource | FileAuthSource | AnthropicAuthSource | GoogleAuthSource,
    Field(discriminator="type"),
]


def parse_auth_source(raw: str | dict[str, Any] | AuthFields) -> AuthFields:
    """Resolve a raw ``Provider.auth`` value into a typed AuthFields subclass.

    Accepts:
    - bare string → ``CommandAuthSource(command=raw)``
    - dict with ``type`` field → discriminated dispatch
    - dict with only ``command``/``file`` keys (no ``type``) → inferred
    - already-typed AuthFields → passthrough
    """
    if isinstance(raw, str):
        return CommandAuthSource(command=raw)
    if isinstance(raw, AuthFields):
        return raw
    if isinstance(raw, dict):
        type_ = raw.get("type")
        if type_ == "anthropic_oauth":
            return AnthropicAuthSource(**raw)
        if type_ == "google_oauth":
            return GoogleAuthSource(**raw)
        if type_ == "file" or ("file" in raw and "type" not in raw):
            return FileAuthSource(**raw)
        if type_ == "command" or ("command" in raw and "type" not in raw):
            return CommandAuthSource(**raw)
        raise ValueError(
            f"Cannot infer AuthSource type from keys {list(raw.keys())!r}; "
            f"specify 'type: command|file|anthropic_oauth|google_oauth'",
        )
    raise TypeError(f"Unsupported auth entry: {type(raw).__name__}")


def atomic_write_back(path: Path, data: dict[str, Any]) -> None:
    """Atomically rewrite a JSON credential file at ``path`` with mode 0o600.

    Writes to a tempfile in the same directory (so ``rename`` is atomic
    on the same filesystem), fsyncs, renames, then chmods.
    """
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
