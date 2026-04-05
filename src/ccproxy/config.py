"""Configuration management for ccproxy.

Configuration Discovery Precedence (Highest to Lowest Priority):
===============================================================

1. **CCPROXY_CONFIG_DIR Environment Variable** (Highest Priority)
   - Set by CLI or manually: `export CCPROXY_CONFIG_DIR=/path/to/config`
   - Looks for: `${CCPROXY_CONFIG_DIR}/ccproxy.yaml`
   - Use case: Development, testing, custom deployments

2. **LiteLLM Proxy Server Runtime Directory**
   - Automatically detected from proxy_server.config_path
   - Looks for: `{proxy_runtime_dir}/ccproxy.yaml`
   - Use case: Production deployments with LiteLLM proxy

3. **~/.ccproxy Directory** (Fallback)
   - User's home directory default location
   - Looks for: `~/.ccproxy/ccproxy.yaml`
   - Use case: Default user installations

The first existing `ccproxy.yaml` found in this order is used.
If no `ccproxy.yaml` is found, default configuration is applied.

Examples:
--------
# Override with environment variable (highest priority)
export CCPROXY_CONFIG_DIR=/custom/path
litellm --config /custom/path/config.yaml

# Use proxy runtime directory (automatic detection)
litellm --config /etc/litellm/config.yaml
# Will look for /etc/litellm/ccproxy.yaml

# Fallback to user directory
# Will look for ~/.ccproxy/ccproxy.yaml
"""

import importlib
import logging
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

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

    destinations: list[str] = Field(default_factory=list)
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


class InspectorConfig(BaseModel):
    """Configuration for the inspector (traffic capture via mitmproxy)."""

    port: int = 8083
    """mitmweb UI port. Also serves as process-alive sentinel and
    WireGuard config API endpoint."""

    max_body_size: int = 0
    """Maximum request/response body size to capture (bytes). 0 = unlimited."""

    capture_bodies: bool = True
    """Whether to capture request/response bodies."""

    excluded_hosts: list[str] = Field(default_factory=list)
    """Hosts to exclude from trace capture (checked by inspector addon)."""

    forward_domains: list[str] = Field(default_factory=lambda: [
        "api.anthropic.com",
        "api.openai.com",
        "generativelanguage.googleapis.com",
        "openrouter.ai",
        "api.z.ai",
    ])
    """LLM API domains to forward from WireGuard to LiteLLM."""

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

    mitmproxy: MitmproxyOptions = Field(default_factory=MitmproxyOptions)
    """mitmproxy option overrides passed via --set flags."""

    @model_validator(mode="after")
    def _sync_cert_dir_to_confdir(self) -> "InspectorConfig":
        if self.cert_dir is not None and self.mitmproxy.confdir is None:
            self.mitmproxy.confdir = str(self.cert_dir.expanduser())
        return self


class RuleConfig:
    """Configuration for a single classification rule."""

    def __init__(self, name: str, rule_path: str, params: list[Any] | None = None) -> None:
        self.model_name = name
        self.rule_path = rule_path
        self.params = params or []

    def create_instance(self) -> Any:
        """Create an instance of the rule class.

        Returns:
            An instance of the ClassificationRule

        Raises:
            ImportError: If the rule class cannot be imported
            TypeError: If the rule class cannot be instantiated with provided params
        """
        # Import the rule class
        module_path, class_name = self.rule_path.rsplit(".", 1)
        module = importlib.import_module(module_path)
        rule_class = getattr(module, class_name)

        if not self.params:
            return rule_class()

        if all(isinstance(p, dict) for p in self.params):
            kwargs = {}
            for p in self.params:
                kwargs.update(p)
            return rule_class(**kwargs)
        return rule_class(*self.params)


class CCProxyConfig(BaseSettings):
    """Main configuration for ccproxy that reads from ccproxy.yaml."""

    model_config = SettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
    )

    debug: bool = False
    default_model_passthrough: bool = True

    # Handler import path (e.g., "ccproxy.handler:CCProxyHandler")
    handler: str = "ccproxy.handler:CCProxyHandler"

    inspector: InspectorConfig = Field(default_factory=InspectorConfig)

    otel: OtelConfig = Field(default_factory=OtelConfig)

    # OAuth token sources - dict mapping provider name to shell command or OAuthSource
    # Example: {"anthropic": "jq -r '.claudeAiOauth.accessToken' ~/.claude/.credentials.json"}
    # Extended: {"gemini": {"command": "jq -r '.token' ~/.gemini/creds.json", "user_agent": "MyApp/1.0"}}
    oat_sources: dict[str, str | OAuthSource] = Field(default_factory=dict)

    # OAuth TTL in seconds (default 8 hours)
    oauth_ttl: int = 28800

    # OAuth refresh buffer (refresh at 90% of TTL by default)
    oauth_refresh_buffer: float = 0.1

    # Cached OAuth tokens (loaded at startup) - dict mapping provider name to (token, timestamp)
    _oat_values: dict[str, tuple[str, float]] = PrivateAttr(default_factory=dict)

    # Cached OAuth user agents (loaded at startup) - dict mapping provider name to user-agent
    _oat_user_agents: dict[str, str] = PrivateAttr(default_factory=dict)

    # Hook configurations (function import paths or dict with params)
    hooks: list[str | dict[str, Any]] = Field(default_factory=list)

    # Rule configurations
    rules: list[RuleConfig] = Field(default_factory=list)

    # Path to ccproxy config
    ccproxy_config_path: Path = Field(default_factory=lambda: Path("./ccproxy.yaml"))

    # Path to LiteLLM config (for model lookups)
    litellm_config_path: Path = Field(default_factory=lambda: Path("./config.yaml"))

    @property
    def oat_values(self) -> dict[str, str]:
        """Get the cached OAuth token values.

        Returns:
            Dict mapping provider name to OAuth token
        """
        return {provider: token for provider, (token, _) in self._oat_values.items()}

    def get_oauth_token(self, provider: str) -> str | None:
        """Get OAuth token for a specific provider.

        Args:
            provider: Provider name (e.g., "anthropic", "gemini")

        Returns:
            OAuth token string or None if not configured for this provider
        """
        entry = self._oat_values.get(provider)
        return entry[0] if entry else None

    def is_token_expired(self, provider: str) -> bool:
        """Check if OAuth token for provider needs refresh using TTL buffer rule.

        Args:
            provider: Provider name (e.g., "anthropic", "gemini")

        Returns:
            True if token is missing or has exceeded TTL buffer threshold
        """
        entry = self._oat_values.get(provider)
        if not entry:
            return True
        _, loaded_at = entry
        # Refresh at (1 - buffer) of TTL (e.g., 90% through TTL with 0.1 buffer)
        refresh_threshold = self.oauth_ttl * (1 - self.oauth_refresh_buffer)
        return time.time() - loaded_at >= refresh_threshold

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

        if isinstance(source, str):
            oauth_source = OAuthSource(command=source)
        elif isinstance(source, OAuthSource):
            oauth_source = source
        elif isinstance(source, dict):
            oauth_source = OAuthSource(**source)
        else:
            logger.error(f"Invalid OAuth source type for provider '{provider}': {type(source)}")
            return None

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

    def refresh_oauth_token(self, provider: str) -> str | None:
        """Refresh OAuth token for a specific provider by re-resolving its source.

        Thread-safe method that updates the cached token with new value and timestamp.

        Args:
            provider: Provider name (e.g., "anthropic", "gemini")

        Returns:
            New token string on success, None on failure
        """
        with _config_lock:
            result = self._resolve_oauth_token(provider)
            if result is None:
                return None

            token, user_agent = result
            self._oat_values[provider] = (token, time.time())
            if user_agent:
                self._oat_user_agents[provider] = user_agent
            logger.debug(f"Refreshed OAuth token for provider '{provider}'")
            return token

    def get_oauth_user_agent(self, provider: str) -> str | None:
        """Get custom User-Agent for a specific provider.

        Args:
            provider: Provider name (e.g., "anthropic", "gemini")

        Returns:
            Custom User-Agent string or None if not configured for this provider
        """
        return self._oat_user_agents.get(provider)

    def get_oauth_auth_header(self, provider: str) -> str | None:
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
                oauth_source = source
            elif isinstance(source, dict):
                oauth_source = OAuthSource(**source)
            else:
                continue

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

        loaded_tokens: dict[str, tuple[str, float]] = {}
        loaded_user_agents: dict[str, str] = {}
        errors: list[str] = []
        current_time = time.time()

        for provider in self.oat_sources:
            result = self._resolve_oauth_token(provider)
            if result is None:
                errors.append(f"Failed to load OAuth token for provider '{provider}'")
                continue

            token, user_agent = result
            loaded_tokens[provider] = (token, current_time)
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
    def from_proxy_runtime(cls, **kwargs: Any) -> "CCProxyConfig":
        """Load configuration from ccproxy.yaml file in the same directory as config.yaml.

        This method looks for ccproxy.yaml in the same directory as the LiteLLM config.
        """
        instance = cls(**kwargs)

        # Try to find ccproxy.yaml in the same directory as config.yaml
        config_dir = instance.litellm_config_path.parent
        ccproxy_yaml_path = config_dir / "ccproxy.yaml"

        if ccproxy_yaml_path.exists():
            instance = cls.from_yaml(ccproxy_yaml_path, **kwargs)

        return instance

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
            with yaml_path.open() as f:
                data: dict[str, Any] = yaml.safe_load(f) or {}

                ccproxy_data: dict[str, Any] = data.get("ccproxy", {})

                if "debug" in ccproxy_data:
                    instance.debug = ccproxy_data["debug"]
                if "default_model_passthrough" in ccproxy_data:
                    instance.default_model_passthrough = ccproxy_data["default_model_passthrough"]
                if "oat_sources" in ccproxy_data:
                    instance.oat_sources = ccproxy_data["oat_sources"]
                if "oauth_ttl" in ccproxy_data:
                    instance.oauth_ttl = ccproxy_data["oauth_ttl"]
                if "oauth_refresh_buffer" in ccproxy_data:
                    instance.oauth_refresh_buffer = ccproxy_data["oauth_refresh_buffer"]
                inspector_data = ccproxy_data.get("inspector")
                if inspector_data:
                    if "debug" not in inspector_data and instance.debug:
                        inspector_data = {**inspector_data, "debug": instance.debug}
                    instance.inspector = InspectorConfig(**inspector_data)
                # Migrate OTel fields from legacy inspector section
                otel_data = ccproxy_data.get("otel")
                if otel_data:
                    instance.otel = OtelConfig(**otel_data)

                # Backwards compatibility: migrate deprecated 'credentials' field
                if "credentials" in ccproxy_data:
                    logger.error(
                        "DEPRECATED: The 'credentials' field is deprecated and will be removed in a future version. "
                        "Please migrate to 'oat_sources' in your ccproxy.yaml configuration. "
                        "Example:\n"
                        "  oat_sources:\n"
                        "    anthropic: \"jq -r '.claudeAiOauth.accessToken' ~/.claude/.credentials.json\"\n"
                        "The deprecated 'credentials' field has been automatically migrated to "
                        "oat_sources['anthropic'] for this session."
                    )
                    # Migrate credentials to oat_sources for anthropic provider
                    if "anthropic" not in instance.oat_sources:
                        instance.oat_sources["anthropic"] = ccproxy_data["credentials"]
                    else:
                        logger.warning(
                            "Both 'credentials' and 'oat_sources[\"anthropic\"]' are configured. "
                            "Using 'oat_sources[\"anthropic\"]' and ignoring deprecated 'credentials' field."
                        )

                hooks_data = ccproxy_data.get("hooks", [])
                if hooks_data:
                    instance.hooks = hooks_data

                rules_data = ccproxy_data.get("rules", [])
                instance.rules = []
                for rule_data in rules_data:
                    if isinstance(rule_data, dict):
                        name = rule_data.get("name", "")
                        rule_path = rule_data.get("rule", "")
                        params = rule_data.get("params", [])
                        if name and rule_path:
                            rule_config = RuleConfig(name, rule_path, params)
                            instance.rules.append(rule_config)

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

                config_path = None
                config_source = None

                # Priority 1: Environment variable
                env_config_dir = os.environ.get("CCPROXY_CONFIG_DIR")
                if env_config_dir:
                    config_path = Path(env_config_dir)
                    config_source = f"ENV:CCPROXY_CONFIG_DIR={env_config_dir}"
                    logger.info(f"Using config directory from environment: {config_path}")
                else:
                    # Priority 2: LiteLLM proxy server runtime directory
                    try:
                        from litellm.proxy import proxy_server

                        if proxy_server and hasattr(proxy_server, "config_path") and proxy_server.config_path:
                            config_path = Path(proxy_server.config_path).parent
                            config_source = f"PROXY_RUNTIME:{config_path}"
                            logger.info(f"Using config directory from proxy runtime: {config_path}")
                    except ImportError:
                        logger.debug("LiteLLM proxy server not available for config discovery")

                if config_path:
                    # Try to load ccproxy.yaml from discovered path
                    ccproxy_yaml_path = config_path / "ccproxy.yaml"
                    if ccproxy_yaml_path.exists():
                        logger.info(f"Loading ccproxy config from: {ccproxy_yaml_path} (source: {config_source})")
                        _config_instance = CCProxyConfig.from_yaml(ccproxy_yaml_path)
                        _config_instance.litellm_config_path = config_path / "config.yaml"
                    else:
                        logger.info(
                            f"ccproxy.yaml not found at {ccproxy_yaml_path}, using default config "
                            f"(source: {config_source})"
                        )
                        # Create default config with proper paths
                        _config_instance = CCProxyConfig(
                            litellm_config_path=config_path / "config.yaml", ccproxy_config_path=ccproxy_yaml_path
                        )
                else:
                    # Priority 3: Fallback to ~/.ccproxy directory
                    fallback_config_dir = Path.home() / ".ccproxy"
                    ccproxy_path = fallback_config_dir / "ccproxy.yaml"
                    if ccproxy_path.exists():
                        logger.info(f"Using fallback config directory: {fallback_config_dir}")
                        _config_instance = CCProxyConfig.from_yaml(ccproxy_path)
                        _config_instance.litellm_config_path = fallback_config_dir / "config.yaml"
                    else:
                        logger.info("No ccproxy.yaml found in any location, using proxy runtime defaults")
                        # Use from_proxy_runtime which will look for ccproxy.yaml
                        # in the same directory as config.yaml
                        _config_instance = CCProxyConfig.from_proxy_runtime()

    return _config_instance


def set_config_instance(config: CCProxyConfig) -> None:
    """Set the global configuration instance (for testing)."""
    global _config_instance
    _config_instance = config


def clear_config_instance() -> None:
    """Clear the global configuration instance (for testing)."""
    global _config_instance
    _config_instance = None
