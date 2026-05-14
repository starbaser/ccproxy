#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = ["httpx>=0.27"]
# ///
"""Refresh the Perplexity session token via Gmail OTP.

Replays the same email-OTP flow as ``perplexity-webui-scraper get-session-token``
but reads the OTP code straight from the configured Gmail mailbox via IMAP, so
the refresh runs without human interaction.

Reads Gmail credentials and target output path from
``$CCPROXY_CONFIG_DIR/perplexity-gmail.json`` (or
``~/.config/ccproxy/perplexity-gmail.json``):

    {
      "email": "you@example.com",
      "app_password": "abcdabcdabcdabcd",
      "imap_host": "imap.gmail.com",
      "imap_port": 993,
      "from_filter": "no-reply@perplexity.ai",
      "subject_filter": "your code is",
      "max_age_seconds": 300
    }

The new token is written atomically (mode 0600) to the file at
``--output`` (default ``$CCPROXY_CONFIG_DIR/perplexity-session-token``).

Usage:
    refresh_perplexity_token.py [--output PATH] [--config PATH] [--debug]
"""

from __future__ import annotations

import argparse
import email
import imaplib
import json
import logging
import os
import re
import stat
import sys
import tempfile
import time
from email.message import Message
from pathlib import Path

import httpx


PERPLEXITY_BASE = "https://www.perplexity.ai"
SESSION_COOKIE = "__Secure-next-auth.session-token"
CHROME_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
OTP_REGEX = re.compile(r"\b(\d{6})\b")

logger = logging.getLogger("refresh_perplexity_token")


def _config_dir() -> Path:
    env = os.environ.get("CCPROXY_CONFIG_DIR")
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "ccproxy"


def _load_gmail_config(path: Path) -> dict[str, object]:
    if not path.is_file():
        raise SystemExit(f"Gmail config not found at {path}. Create it with email + app_password.")
    cfg = json.loads(path.read_text())
    if not cfg.get("email") or not cfg.get("app_password"):
        raise SystemExit(f"{path} missing 'email' or 'app_password'.")
    return cfg


def _atomic_write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(value)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp, path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def _request_otp(client: httpx.Client, email_addr: str) -> None:
    """Hit /api/auth/csrf then /api/auth/signin/email to send the OTP message."""
    client.get(PERPLEXITY_BASE).raise_for_status()
    csrf = client.get(f"{PERPLEXITY_BASE}/api/auth/csrf").json().get("csrfToken", "")
    if not csrf:
        raise RuntimeError("Failed to obtain CSRF token")

    r = client.post(
        f"{PERPLEXITY_BASE}/api/auth/signin/email",
        params={"version": "2.18", "source": "default"},
        json={
            "email": email_addr,
            "csrfToken": csrf,
            "useNumericOtp": "true",
            "json": "true",
            "callbackUrl": f"{PERPLEXITY_BASE}/?login-source=floatingSignup",
        },
    )
    r.raise_for_status()
    logger.info("OTP request sent for %s", email_addr)


def _poll_otp_email(
    *,
    imap_host: str,
    imap_port: int,
    email_addr: str,
    app_password: str,
    from_filter: str,
    subject_filter: str,
    max_age_seconds: int,
    request_started_at: float,
    poll_interval: float = 3.0,
    poll_timeout: float = 90.0,
) -> str:
    """Poll Gmail for the OTP code emitted at or after ``request_started_at``."""
    deadline = time.time() + poll_timeout
    last_uid: bytes | None = None

    with imaplib.IMAP4_SSL(imap_host, imap_port) as imap:
        imap.login(email_addr, app_password)
        imap.select("INBOX")

        while time.time() < deadline:
            search_args = ["UNSEEN", f'FROM "{from_filter}"']
            typ, data = imap.search(None, *search_args)
            if typ != "OK" or not data or not data[0]:
                time.sleep(poll_interval)
                continue

            uids = data[0].split()
            for uid in reversed(uids):
                if uid == last_uid:
                    continue
                typ, msg_data = imap.fetch(uid, "(RFC822)")
                if typ != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                    continue
                raw = msg_data[0][1]
                if not isinstance(raw, (bytes, bytearray)):
                    continue
                msg: Message = email.message_from_bytes(bytes(raw))

                date_hdr = msg.get("Date") or ""
                try:
                    msg_ts = email.utils.parsedate_to_datetime(date_hdr).timestamp()
                except (TypeError, ValueError):
                    msg_ts = 0.0
                if msg_ts and msg_ts < request_started_at - 30:
                    last_uid = uid
                    continue

                subject = (msg.get("Subject") or "").lower()
                if subject_filter and subject_filter.lower() not in subject:
                    last_uid = uid
                    continue

                body = _extract_body(msg)
                age = time.time() - (msg_ts or time.time())
                if age > max_age_seconds:
                    last_uid = uid
                    continue

                match = OTP_REGEX.search(body) or OTP_REGEX.search(subject)
                if match:
                    code = match.group(1)
                    imap.store(uid, "+FLAGS", "\\Seen")
                    logger.info("Captured OTP code from message uid=%s", uid.decode())
                    return code
                last_uid = uid

            time.sleep(poll_interval)

    raise RuntimeError(f"Timed out waiting for OTP email after {poll_timeout:.0f}s")


def _extract_body(msg: Message) -> str:
    """Return text body from a multipart-or-flat message."""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype in ("text/plain", "text/html"):
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    return payload.decode("utf-8", errors="replace")
        return ""
    payload = msg.get_payload(decode=True)
    return payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else str(payload)


def _redeem_otp(client: httpx.Client, email_addr: str, otp: str) -> str:
    """POST the OTP, follow the redirect, return the session token cookie."""
    r = client.post(
        f"{PERPLEXITY_BASE}/api/auth/otp-redirect-link",
        json={
            "email": email_addr,
            "otp": otp,
            "redirectUrl": f"{PERPLEXITY_BASE}/?login-source=floatingSignup",
            "emailLoginMethod": "web-otp",
        },
    )
    r.raise_for_status()
    redirect_path = r.json().get("redirect", "")
    if not redirect_path:
        raise RuntimeError("No redirect URL received from OTP exchange")

    redirect_url = f"{PERPLEXITY_BASE}{redirect_path}" if redirect_path.startswith("/") else redirect_path
    client.get(redirect_url).raise_for_status()

    token = client.cookies.get(SESSION_COOKIE)
    if not token:
        raise RuntimeError(f"Auth flow completed but {SESSION_COOKIE} cookie not set")
    return token


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--config",
        type=Path,
        default=_config_dir() / "perplexity-gmail.json",
        help="Path to gmail config JSON (default: $CCPROXY_CONFIG_DIR/perplexity-gmail.json).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=_config_dir() / "perplexity-session-token",
        help="Path to write the new session token (default: $CCPROXY_CONFIG_DIR/perplexity-session-token).",
    )
    parser.add_argument("--debug", action="store_true", help="Verbose logging.")
    args = parser.parse_args(argv)

    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(message)s",
        level=logging.DEBUG if args.debug else logging.INFO,
        stream=sys.stderr,
    )

    cfg = _load_gmail_config(args.config)
    app_password = str(cfg["app_password"]).replace(" ", "")

    started = time.time()
    headers = {
        "User-Agent": CHROME_UA,
        "Origin": PERPLEXITY_BASE,
        "Referer": f"{PERPLEXITY_BASE}/",
        "Accept": "application/json, text/plain, */*",
    }
    with httpx.Client(headers=headers, follow_redirects=True, timeout=30.0) as client:
        _request_otp(client, str(cfg["email"]))

        otp = _poll_otp_email(
            imap_host=str(cfg.get("imap_host", "imap.gmail.com")),
            imap_port=int(cfg.get("imap_port", 993)),
            email_addr=str(cfg["email"]),
            app_password=app_password,
            from_filter=str(cfg.get("from_filter", "no-reply@perplexity.ai")),
            subject_filter=str(cfg.get("subject_filter", "")),
            max_age_seconds=int(cfg.get("max_age_seconds", 300)),
            request_started_at=started,
        )

        token = _redeem_otp(client, str(cfg["email"]), otp)

    _atomic_write(args.output, token)
    logger.info("Wrote new session token (%d bytes) to %s", len(token), args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
