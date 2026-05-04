"""Regression: legacy auth-source YAML formats still resolve after the oauth/ split.

The split moved CredentialSource/OAuthSource out of config.py and into a
discriminated union under ccproxy.oauth.sources. parse_oauth_source must
continue to accept:

1. Bare command strings (most common form in user configs).
2. Dicts with only ``command`` or ``file`` keys (no ``type`` discriminator).
3. The new discriminated forms (``type: command|file|anthropic_oauth|google_oauth``).
"""

from __future__ import annotations

import pytest

from ccproxy.oauth.sources import (
    AnthropicOAuthSource,
    CommandOAuthSource,
    FileOAuthSource,
    GoogleOAuthSource,
    parse_oauth_source,
)


def test_bare_string_resolves_as_command_source() -> None:
    """Legacy ``providers.foo.auth: "echo bar"`` still maps to a CommandOAuthSource."""
    source = parse_oauth_source("echo bar")
    assert isinstance(source, CommandOAuthSource)
    assert source.command == "echo bar"
    assert source.type == "command"


def test_dict_with_command_only_resolves_as_command_source() -> None:
    """Legacy dict form without ``type`` key still maps to a CommandOAuthSource."""
    source = parse_oauth_source({"command": "echo tok", "user_agent": "Test/1.0"})
    assert isinstance(source, CommandOAuthSource)
    assert source.command == "echo tok"


def test_dict_with_file_only_resolves_as_file_source() -> None:
    """Legacy dict form ``{file: ...}`` (no ``type``) still maps to a FileOAuthSource."""
    source = parse_oauth_source({"file": "/etc/example/token", "destinations": ["api.test.com"]})
    assert isinstance(source, FileOAuthSource)
    assert source.file == "/etc/example/token"


def test_explicit_type_command_dispatches_correctly() -> None:
    source = parse_oauth_source({"type": "command", "command": "echo x"})
    assert isinstance(source, CommandOAuthSource)


def test_explicit_type_anthropic_oauth_dispatches_correctly() -> None:
    source = parse_oauth_source(
        {
            "type": "anthropic_oauth",
            "refresh_token_file": "~/.config/ccproxy/oauth/anthropic.json",
        }
    )
    assert isinstance(source, AnthropicOAuthSource)
    assert source.client_id == "9d1c250a-e61b-44d9-88ed-5944d1962f5e"


def test_explicit_type_google_oauth_dispatches_correctly() -> None:
    source = parse_oauth_source(
        {
            "type": "google_oauth",
            "client_id": "test.apps.googleusercontent.com",
            "client_secret": "GOCSPX-test",
        }
    )
    assert isinstance(source, GoogleOAuthSource)
    assert source.endpoint == "https://oauth2.googleapis.com/token"


def test_unknown_type_raises_value_error() -> None:
    with pytest.raises(ValueError, match="Cannot infer OAuthSource type"):
        parse_oauth_source({"unrecognized": "x"})


def test_already_typed_passthrough() -> None:
    typed = CommandOAuthSource(command="echo y")
    result = parse_oauth_source(typed)
    assert result is typed
