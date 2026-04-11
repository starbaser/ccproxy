"""Configuration management for ccproxy.

Config discovery precedence:

1. ``CCPROXY_CONFIG_DIR`` env var → ``$CCPROXY_CONFIG_DIR/ccproxy.yaml``
2. ``~/.ccproxy/ccproxy.yaml`` (fallback)

Individual fields can be overridden via ``CCPROXY_`` prefixed env vars
(e.g. ``CCPROXY_PORT=4001``).
"""

import logging
import subprocess
import threading
from pathlib import Path
from typing import Any, cast

import yaml
from pydantic import BaseModel, Field, PrivateAttr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class OAuthSource(BaseModel):
    """OAuth token source configuration.

    Can be specified as either a simple string (shell command) or
    an object with command/file and optional user_agent.

    Exactly one of ``command`` or ``file`` must be provided.
    """

    command: str | None = None
    """Shell command to retrieve the OAuth token"""

    file: str | None = None
    """File path to read the OAuth token from (contents stripped of whitespace)"""

    user_agent: str | None = None
    """Optional custom User-Agent header to send with requests using this token"""

    destinations: list[str] = Field(default_factory=lambda: [])
    """URL patterns that should use this token (e.g., ['api.z.ai', 'anthropic.com'])"""

    auth_header: str | None = None
    """Target header name for the token (e.g., 'x-api-key').

    When set, sends raw token as this header instead of Authorization: Bearer.
    """

    @model_validator(mode="after")
    def validate_source(self) -> "OAuthSource":
        if self.command and self.file:
            raise ValueError("'command' and 'file' are mutually exclusive — specify one, not both")
        if not self.command and not self.file:
            raise ValueError("Either 'command' or 'file' must be specified")
        return self


class OtelConfig(BaseModel):
    """OpenTelemetry configuration for span export."""

    enabled: bool = False
    """Enable OpenTelemetry span emission from the inspector."""

    endpoint: str = "http://localhost:4317"
    """OTLP gRPC endpoint URL for span export (Jaeger or OTel Collector)."""

    service_name: str = "ccproxy"
    """OTel resource service.name attribute."""


class MitmproxyOptions(BaseModel):
    """Typed facade over mitmproxy's OptManager options.

    Field names match mitmproxy option names exactly. Values are serialized
    to ``--set name=value`` CLI arguments by the inspector process manager.
    """

    confdir: str | None = None
    """CA certificate store directory. None uses mitmproxy default (~/.mitmproxy).
    Typically set via InspectorConfig.cert_dir model validator."""

    ssl_insecure: bool = True
    """Skip upstream TLS certificate verification."""

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

    ignore_hosts: list[str] = Field(default_factory=lambda: [])
    """Regex patterns for hosts to bypass (no TLS interception)."""

    allow_hosts: list[str] = Field(default_factory=lambda: [])
    """Regex patterns for hosts to intercept (exclusive allowlist)."""

    termlog_verbosity: str = "warn"
    """mitmproxy terminal log level: debug, info, warn, error."""

    flow_detail: int = 0
    """Flow output verbosity: 0=none, 1=url+status, 2=headers, 3=truncated body, 4=full body."""


class TransformRoute(BaseModel):
    """A single lightllm transformation rule for the inspector."""

    mode: str = "transform"
    """``transform`` (default): rewrite request body via lightllm dispatch.
    ``passthrough``: forward to the original destination unchanged."""

    match_host: str | None = None
    """Hostname to match (e.g. ``api.openai.com``). Checked against
    ``pretty_host``, ``Host`` header, and ``X-Forwarded-Host``.
    ``None`` matches any host."""

    match_path: str = "/"
    """Path prefix to match (e.g. ``/v1/chat/completions``). Matches any
    path that starts with this prefix."""

    match_model: str | None = None
    """Model name substring to match in the request body's ``model`` field.
    ``None`` matches any model. Most useful for reverse proxy flows where
    all traffic arrives at the same host."""

    dest_provider: str = ""
    """Destination provider name for lightllm dispatch (e.g. ``anthropic``, ``gemini``).
    Not used in ``passthrough`` mode."""

    dest_model: str = ""
    """Destination model name for lightllm dispatch.
    Not used in ``passthrough`` mode."""

    dest_api_key_ref: str | None = None
    """Provider name in ``oat_sources`` for credential lookup, or an
    environment variable name.  ``None`` skips API key injection."""


class InspectorConfig(BaseModel):
    """Configuration for the inspector (traffic capture via mitmproxy)."""

    port: int = 8083
    """mitmweb UI port. Also serves as process-alive sentinel and
    WireGuard config API endpoint."""

    max_body_size: int = 0
    """Maximum request/response body size to capture (bytes). 0 = unlimited."""

    debug: bool = False
    """Enable debug logging (includes request body logging)."""

    cert_dir: Path | None = None
    """mitmproxy CA certificate store directory. Populates mitmproxy.confdir
    via model validator when set."""

    provider_map: dict[str, str] = Field(default_factory=lambda: {
        "api.anthropic.com": "anthropic",
        "api.openai.com": "openai",
        "generativelanguage.googleapis.com": "google",
        "openrouter.ai": "openrouter",
    })
    """Hostname → OTel gen_ai.system attribute mapping for provider identification."""

    transforms: list[TransformRoute] = Field(default_factory=list)
    """lightllm transformation rules. Each rule matches inbound flows by
    host+path and rewrites them to a different provider format via the
    lightllm dispatch."""

    mitmproxy: MitmproxyOptions = Field(default_factory=MitmproxyOptions)
    """mitmproxy option overrides passed via --set flags."""

    @model_validator(mode="after")
    def _sync_cert_dir_to_confdir(self) -> "InspectorConfig":
        if self.cert_dir is not None and self.mitmproxy.confdir is None:
            self.mitmproxy.confdir = str(self.cert_dir.expanduser())
        return self


class CCProxyConfig(BaseSettings):
    """Main configuration for ccproxy that reads from ccproxy.yaml."""

    model_config = SettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        env_prefix="CCPROXY_",
    )

    host: str = "127.0.0.1"
    port: int = 4000
    debug: bool = False

    inspector: InspectorConfig = Field(default_factory=InspectorConfig)

    otel: OtelConfig = Field(default_factory=OtelConfig)

    # OAuth token sources - dict mapping provider name to shell command or OAuthSource
    # Example: {"anthropic": "jq -r '.claudeAiOauth.accessToken' ~/.claude/.credentials.json"}
    # Extended: {"gemini": {"command": "jq -r '.token' ~/.gemini/creds.json", "user_agent": "MyApp/1.0"}}
    oat_sources: dict[str, str | OAuthSource | dict[str, Any]] = Field(default_factory=lambda: {})

    # Cached OAuth tokens (loaded at startup)
    _oat_values: dict[str, str] = PrivateAttr(default_factory=lambda: {})

    # Cached OAuth user agents (loaded at startup) - dict mapping provider name to user-agent
    _oat_user_agents: dict[str, str] = PrivateAttr(default_factory=lambda: {})

    # Hook configurations — either a flat list (all inbound) or a dict
    # with ``inbound`` and ``outbound`` keys for two-stage pipeline.
    hooks: dict[str, list[str | dict[str, Any]]] = Field(
        default_factory=lambda: {  # type: ignore[arg-type]
            "inbound": [
                "ccproxy.hooks.forward_oauth",
                "ccproxy.hooks.extract_session_id",
            ],
            "outbound": [
                "ccproxy.hooks.add_beta_headers",
                "ccproxy.hooks.inject_claude_code_identity",
                "ccproxy.hooks.inject_mcp_notifications",
            ],
        },
    )

    # Path to ccproxy config
    ccproxy_config_path: Path = Field(default_factory=lambda: Path("./ccproxy.yaml"))

    @property
    def oat_values(self) -> dict[str, str]:
        """Get the cached OAuth token values."""
        return dict(self._oat_values)

    def get_oauth_token(self, provider: str) -> str | None:
        """Get cached OAuth token for a specific provider."""
        return self._oat_values.get(provider)

    def _resolve_oauth_token(self, provider: str) -> tuple[str, str | None] | None:
        """Resolve OAuth token for a provider via command or file.

        Args:
            provider: Provider name to fetch token for

        Returns:
            Tuple of (token, user_agent) on success, None on failure
        """
        source = self.oat_sources.get(provider)
        if not source:
            logger.warning(f"No OAuth source configured for provider '{provider}'")
            return None

        oauth_source: OAuthSource
        if isinstance(source, str):
            oauth_source = OAuthSource(command=source)
        elif isinstance(source, OAuthSource):
            oauth_source = source
        else:
            oauth_source = OAuthSource(**source)

        if oauth_source.file:
            return self._read_oauth_file(oauth_source, provider)
        return self._run_oauth_command(oauth_source, provider)

    def _read_oauth_file(self, source: OAuthSource, provider: str) -> tuple[str, str | None] | None:
        """Read OAuth token from a file path."""
        try:
            path = Path(source.file).expanduser().resolve()  # type: ignore[arg-type]
            if not path.is_file():
                logger.error(f"OAuth file for provider '{provider}' not found: {path}")
                return None
            token = path.read_text().strip()
            if not token:
                logger.error(f"OAuth file for provider '{provider}' is empty: {path}")
                return None
            return (token, source.user_agent)
        except Exception as e:
            logger.error(f"Failed to read OAuth file for provider '{provider}': {e}")
            return None

    def _run_oauth_command(self, source: OAuthSource, provider: str) -> tuple[str, str | None] | None:
        """Execute a shell command to retrieve an OAuth token."""
        try:
            result = subprocess.run(  # noqa: S602
                source.command or "",
                shell=True,
                capture_output=True,
                text=True,
                timeout=5,
            )

            if result.returncode != 0:
                logger.error(
                    f"OAuth command for provider '{provider}' failed with exit code "
                    f"{result.returncode}: {result.stderr.strip()}"
                )
                return None

            token = result.stdout.strip()
            if not token:
                logger.error(f"OAuth command for provider '{provider}' returned empty output")
                return None

            return (token, source.user_agent)

        except subprocess.TimeoutExpired:
            logger.error(f"OAuth command for provider '{provider}' timed out after 5 seconds")
            return None
        except Exception as e:
            logger.error(f"Failed to execute OAuth command for provider '{provider}': {e}")
            return None

    def refresh_oauth_token(self, provider: str) -> tuple[str | None, bool]:
        """Re-resolve OAuth token for a provider and update cache if changed.

        Thread-safe. Returns (new_token, changed) — changed is True only when
        the freshly resolved token differs from the cached value.
        """
        with _config_lock:
            result = self._resolve_oauth_token(provider)
            if result is None:
                return None, False

            token, user_agent = result
            old_token = self._oat_values.get(provider)
            changed = token != old_token
            self._oat_values[provider] = token
            if user_agent:
                self._oat_user_agents[provider] = user_agent
            if changed:
                logger.info("OAuth token changed for provider '%s'", provider)
            return token, changed

    def get_auth_provider_ua(self, provider: str) -> str | None:
        """Get custom User-Agent for a specific provider.

        Args:
            provider: Provider name (e.g., "anthropic", "gemini")

        Returns:
            Custom User-Agent string or None if not configured for this provider
        """
        return self._oat_user_agents.get(provider)

    def get_auth_header(self, provider: str) -> str | None:
        """Get target auth header name for a specific provider.

        Args:
            provider: Provider name (e.g., "zai")

        Returns:
            Header name string (e.g., 'x-api-key') or None for default Bearer behavior
        """
        source = self.oat_sources.get(provider)
        if isinstance(source, OAuthSource):
            return source.auth_header
        return None

    def get_provider_for_destination(self, api_base: str | None) -> str | None:
        """Find which provider should handle requests to a given api_base.

        Checks configured oat_sources destinations to find a matching provider.

        Args:
            api_base: The API base URL (e.g., "https://api.z.ai/api/anthropic")

        Returns:
            Provider name if a destination pattern matches, None otherwise
        """
        if not api_base:
            return None

        api_base_lower = api_base.lower()

        for provider, source in self.oat_sources.items():
            if isinstance(source, str):
                continue  # Simple string form has no destinations
            elif isinstance(source, OAuthSource):
                oauth_source: OAuthSource = source
            else:
                oauth_source = OAuthSource(**source)

            # Check if api_base matches any destination pattern
            for dest in oauth_source.destinations:
                if dest.lower() in api_base_lower:
                    logger.debug(f"Matched api_base '{api_base}' to provider '{provider}' via destination '{dest}'")
                    return provider

        return None

    def _load_credentials(self) -> None:
        """Execute shell commands to load OAuth tokens for all configured providers at startup.

        Logs errors for providers that fail but allows the proxy to continue running.
        Requests requiring OAuth will fail at request time if tokens are unavailable.
        """
        if not self.oat_sources:
            self._oat_values = {}
            self._oat_user_agents = {}
            return

        loaded_tokens: dict[str, str] = {}
        loaded_user_agents: dict[str, str] = {}
        errors: list[str] = []

        for provider in self.oat_sources:
            result = self._resolve_oauth_token(provider)
            if result is None:
                errors.append(f"Failed to load OAuth token for provider '{provider}'")
                continue

            token, user_agent = result
            loaded_tokens[provider] = token
            logger.debug(f"Successfully loaded OAuth token for provider '{provider}'")

            if user_agent:
                loaded_user_agents[provider] = user_agent
                logger.debug(f"Loaded custom User-Agent for provider '{provider}': {user_agent}")

        self._oat_values = loaded_tokens
        self._oat_user_agents = loaded_user_agents

        if errors and loaded_tokens:
            logger.warning(
                f"Loaded OAuth tokens for {len(loaded_tokens)} provider(s), "
                f"but {len(errors)} provider(s) failed to load"
            )

        if errors and not loaded_tokens:
            logger.error(
                "Failed to load OAuth tokens for all %d provider(s). "
                "Requests requiring OAuth will fail until tokens are available:\n%s",
                len(self.oat_sources),
                "\n".join(f"  - {err}" for err in errors),
            )

    @classmethod
    def from_yaml(cls, yaml_path: Path, **kwargs: Any) -> "CCProxyConfig":
        """Load configuration from ccproxy.yaml file.

        Args:
            yaml_path: Path to the ccproxy.yaml file
            **kwargs: Additional keyword arguments

        Returns:
            CCProxyConfig instance

        Raises:
            RuntimeError: If credentials shell command fails during startup
        """
        instance = cls(ccproxy_config_path=yaml_path, **kwargs)

        if yaml_path.exists():
            import os

            with yaml_path.open() as f:
                data: dict[str, Any] = yaml.safe_load(f) or {}

                ccproxy_data: dict[str, Any] = data.get("ccproxy", {})

                # Env vars (via CCPROXY_ prefix) take precedence over YAML
                if "host" in ccproxy_data and "CCPROXY_HOST" not in os.environ:
                    instance.host = ccproxy_data["host"]
                if "port" in ccproxy_data and "CCPROXY_PORT" not in os.environ:
                    instance.port = int(ccproxy_data["port"])
                if "debug" in ccproxy_data:
                    instance.debug = ccproxy_data["debug"]
                if "oat_sources" in ccproxy_data:
                    instance.oat_sources = ccproxy_data["oat_sources"]
                inspector_data = ccproxy_data.get("inspector")
                if inspector_data:
                    inspector_dict = cast(dict[str, Any], inspector_data)
                    if "debug" not in inspector_dict and instance.debug:
                        inspector_dict = {**inspector_dict, "debug": instance.debug}
                    instance.inspector = InspectorConfig(**inspector_dict)  # pyright: ignore[reportArgumentType]
                otel_data = ccproxy_data.get("otel")
                if otel_data:
                    instance.otel = OtelConfig(**otel_data)

                hooks_data = ccproxy_data.get("hooks", [])
                if hooks_data:
                    instance.hooks = hooks_data

        instance._load_credentials()

        return instance


# Global configuration instance
_config_instance: CCProxyConfig | None = None
_config_lock = threading.Lock()


def get_config() -> CCProxyConfig:
    """Get the configuration instance."""
    global _config_instance

    if _config_instance is None:
        with _config_lock:
            if _config_instance is None:
                import os

                config_path: Path | None = None

                # Priority 1: CCPROXY_CONFIG_DIR env var
                env_config_dir = os.environ.get("CCPROXY_CONFIG_DIR")
                if env_config_dir:
                    config_path = Path(env_config_dir)
                    logger.info(f"Using config directory from environment: {config_path}")

                # Priority 2: ~/.ccproxy fallback
                if config_path is None:
                    config_path = Path.home() / ".ccproxy"

                ccproxy_yaml = config_path / "ccproxy.yaml"
                if ccproxy_yaml.exists():
                    logger.info(f"Loading config from: {ccproxy_yaml}")
                    _config_instance = CCProxyConfig.from_yaml(ccproxy_yaml)
                else:
                    logger.info("No ccproxy.yaml found, using defaults")
                    _config_instance = CCProxyConfig()

    return _config_instance


def set_config_instance(config: CCProxyConfig) -> None:
    """Set the global configuration instance (for testing)."""
    global _config_instance
    _config_instance = config


def clear_config_instance() -> None:
    """Clear the global configuration instance (for testing)."""
    global _config_instance
    _config_instance = None
