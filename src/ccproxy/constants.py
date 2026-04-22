"""Shared constants and base exceptions for ccproxy."""


class OAuthConfigError(ValueError):
    """Raised when OAuth configuration is missing or invalid.
    """


# Sentinel API key prefix that triggers OAuth token substitution from ccproxy config.
# Format: sk-ant-oat-ccproxy-{provider} where {provider} matches a key in oat_sources.
# Example: sk-ant-oat-ccproxy-anthropic uses the token from oat_sources.anthropic
OAUTH_SENTINEL_PREFIX = "sk-ant-oat-ccproxy-"

# Regex patterns for detecting sensitive header values to redact.
# Pattern captures the prefix to preserve (e.g., "Bearer sk-ant-") while redacting middle.
# None value means fully redact the entire value.
SENSITIVE_PATTERNS: dict[str, str | None] = {
    "authorization": r"^(Bearer sk-[a-z]+-|Bearer |sk-[a-z]+-)",
    "x-api-key": r"^(sk-[a-z]+-)",
    "cookie": None,
}

# Initial value for the Anthropic shaping profile system prompt prefix.
CLAUDE_CODE_SYSTEM_PREFIX = "You are Claude Code, Anthropic's official CLI for Claude."
