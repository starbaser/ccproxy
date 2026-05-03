"""OAuth credential sources and provider-specific refresh logic."""

from ccproxy.oauth.anthropic import refresh_anthropic_token, resolve_anthropic_token
from ccproxy.oauth.google import refresh_google_token, resolve_google_token
from ccproxy.oauth.sources import (
    AnthropicOAuthSource,
    CommandOAuthSource,
    CredentialSource,
    FileOAuthSource,
    GoogleOAuthSource,
    OAuthSource,
    atomic_write_back,
    needs_refresh,
    parse_oauth_source,
)

__all__ = [
    "AnthropicOAuthSource",
    "CommandOAuthSource",
    "CredentialSource",
    "FileOAuthSource",
    "GoogleOAuthSource",
    "OAuthSource",
    "atomic_write_back",
    "needs_refresh",
    "parse_oauth_source",
    "refresh_anthropic_token",
    "refresh_google_token",
    "resolve_anthropic_token",
    "resolve_google_token",
]
