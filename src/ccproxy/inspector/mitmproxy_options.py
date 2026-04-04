"""Typed pydantic stub for mitmproxy's OptManager options.

mitmproxy registers options at runtime via OptManager.add_option() with no
static typed config class. This module provides a pydantic BaseModel facade
so ccproxy validates mitmproxy options at config load time. Field names match
mitmproxy's option names exactly for direct --set passthrough.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class MitmproxyOptions(BaseModel):
    """Typed facade over mitmproxy's OptManager options.

    Field names match mitmproxy option names exactly. Values are serialized
    to ``--set name=value`` CLI arguments by the inspector process manager.
    """

    confdir: str | None = None
    """CA certificate store directory. None uses mitmproxy default (~/.mitmproxy).
    Typically set via InspectorConfig.cert_dir model validator."""

    ssl_insecure: bool = True
    """Skip upstream TLS certificate verification. Required when mitmproxy
    reverse-proxies to localhost LiteLLM."""

    stream_large_bodies: str = "1m"
    """Stream bodies larger than this threshold instead of buffering.
    Accepts mitmproxy size notation: '512k', '1m', '10m'."""

    body_size_limit: str | None = None
    """Hard limit on buffered body size. Bodies exceeding this are dropped.
    None means unlimited."""

    web_host: str = "127.0.0.1"
    """mitmweb browser UI bind address."""

    web_password: str | None = None
    """mitmweb UI password. None means no authentication (open UI)."""

    web_open_browser: bool = False
    """Auto-open browser when mitmweb starts."""

    ignore_hosts: list[str] = Field(default_factory=list)
    """Regex patterns for hosts to bypass (no TLS interception)."""

    allow_hosts: list[str] = Field(default_factory=list)
    """Regex patterns for hosts to intercept (exclusive allowlist)."""

    termlog_verbosity: str = "warn"
    """mitmproxy terminal log level: debug, info, warn, error."""

    flow_detail: int = 0
    """Flow output verbosity: 0=none, 1=url+status, 2=headers, 3=truncated body, 4=full body."""
