"""Configuration management for ccproxy.

Config discovery precedence:

1. ``CCPROXY_CONFIG_DIR`` env var → ``$CCPROXY_CONFIG_DIR/ccproxy.yaml``
2. ``$XDG_CONFIG_HOME/ccproxy/ccproxy.yaml`` (defaults to ``~/.config/ccproxy/ccproxy.yaml``)

Individual fields can be overridden via ``CCPROXY_`` prefixed env vars
(e.g. ``CCPROXY_PORT=4001``).
"""

import logging
import os
import subprocess
import threading
from pathlib import Path
from typing import Any, Literal, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class CredentialSource(BaseModel):
    """Credential resolved from a file or shell command.

    Exactly one of ``command`` or ``file`` must be provided.
    """

    command: str | None = None
    """Shell command that outputs the credential value."""

    file: str | None = None
    """File path to read (contents stripped of whitespace)."""

    @model_validator(mode="after")
    def _validate_source(self) -> "CredentialSource":
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


def _read_credential_file(path_str: str, label: str) -> str | None:
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


class OAuthSource(CredentialSource):
    """OAuth token source with provider-specific fields."""

    user_agent: str | None = None
    """Optional custom User-Agent header to send with requests using this token"""

    destinations: list[str] = Field(default_factory=lambda: [])
    """URL patterns that should use this token (e.g., ['api.z.ai', 'anthropic.com'])"""

    auth_header: str | None = None
    """Target header name for the token (e.g., 'x-api-key').

    When set, sends raw token as this header instead of Authorization: Bearer.
    """


class ComplianceConfig(BaseModel):
    """Configuration for the compliance profile system."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    """Master switch for compliance application."""

    profile_path: str | None = None
    """Explicit path to the compliance profiles JSON file.

    When set, all instances share this file instead of each writing to
    ``{config_dir}/compliance_profiles.json``.
    """

    seed_anthropic: bool = True
    """Seed an Anthropic v0 profile from existing constants on first run."""

    additional_header_exclusions: list[str] = Field(default_factory=list)
    """Additional header names to exclude from compliance profiling."""

    additional_body_content_fields: list[str] = Field(default_factory=list)
    """Additional top-level body field names to treat as content (not envelope)."""

    merger_class: str = "ccproxy.compliance.merger.ComplianceMerger"
    """Dotted import path to a ComplianceMerger subclass for profile application."""


class FlowsConfig(BaseModel):
    """Configuration for the ``ccproxy flows`` CLI commands."""

    default_jq_filters: list[str] = Field(default_factory=list)
    """JQ filter expressions applied before any CLI ``--jq`` filters.

    Each filter must consume a JSON array and produce a JSON array, e.g.::

        map(select(.request.host | endswith("anthropic.com")))

    Filters chain in order via jq's ``|`` operator."""


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

    web_password: str | CredentialSource | dict[str, str] | None = None
    """mitmweb UI password. Accepts a plain string, or a ``file``/``command``
    credential source (same format as ``oat_sources``). None generates a
    random token on each startup."""

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

    mode: str = "redirect"
    """``redirect`` (default): rewrite destination host, preserve request body (same-format).
    ``transform``: rewrite both destination and body via lightllm (cross-format).
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
    """Destination provider name (e.g. ``anthropic``, ``gemini``).
    Used by ``transform`` for lightllm dispatch and ``redirect`` for
    compliance profile lookup. Not used in ``passthrough`` mode."""

    dest_model: str = ""
    """Destination model name for lightllm dispatch.
    Only used in ``transform`` mode."""

    dest_host: str | None = None
    """Explicit destination host for ``redirect`` mode
    (e.g. ``generativelanguage.googleapis.com``). If not set, ``redirect``
    mode is invalid."""

    dest_path: str | None = None
    """Override the request path in ``redirect`` mode. If not set, the
    original path is preserved."""

    dest_api_key_ref: str | None = None
    """Provider name in ``oat_sources`` for credential lookup, or an
    environment variable name.  ``None`` skips API key injection."""

    dest_vertex_project: str | None = None
    """GCP project ID for Vertex AI transforms. Required for context caching
    with ``vertex_ai`` / ``vertex_ai_beta`` providers."""

    dest_vertex_location: str | None = None
    """GCP region for Vertex AI transforms (e.g. ``us-central1``).
    Required for context caching with ``vertex_ai`` / ``vertex_ai_beta`` providers."""


class InspectorConfig(BaseModel):
    """Configuration for the inspector (traffic capture via mitmproxy)."""

    port: int = 8083
    """mitmweb UI port. Also serves as process-alive sentinel and
    WireGuard config API endpoint."""

    max_body_size: int = 0
    """Maximum request/response body size to capture (bytes). 0 = unlimited."""

    cert_dir: Path | None = None
    """mitmproxy CA certificate store directory. Populates mitmproxy.confdir
    via model validator when set."""

    provider_map: dict[str, str] = Field(
        default_factory=lambda: {
            "api.anthropic.com": "anthropic",
            "api.openai.com": "openai",
            "generativelanguage.googleapis.com": "google",
            "openrouter.ai": "openrouter",
        }
    )
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

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    """Root Python logger level. Applies uniformly to all loggers."""

    log_file: Path | None = Path("ccproxy.log")
    """Path to the daemon log file. Relative paths resolve against the
    config file's directory (``ccproxy_config_path.parent``); absolute
    paths pass through; ``None`` disables file logging. Only applies to
    ``ccproxy start`` — one-shot CLI commands never write here.
    Access the resolved path via ``resolved_log_file``."""

    provider_timeout: float | None = None
    """Timeout budget (seconds) for httpx-based upstream calls inside ccproxy
    (OAuth 401 retry). ``None`` (default) disables the timeout entirely,
    matching Portkey AI's upstream behavior and mitmproxy's default main-
    forward path. Set to a positive float to opt into a total request
    budget applied uniformly across connect/read/write/pool phases."""

    verify_readiness_on_startup: bool = True
    """Probe a well-known external host at startup and refuse to start if
    it is unreachable. Catches broken routes, DNS, CA bundles, or namespace
    egress problems before any real traffic is accepted."""

    use_journal: bool = False
    """Route daemon logging to the systemd journal via JournalHandler.

    Requires the ``journal`` optional extra
    (``pip install claude-ccproxy[journal]``) which pulls in
    ``systemd-python``. Only applies to ``ccproxy start`` — interactive
    commands (run, status, logs) always write to stderr.

    When enabled without ``systemd-python`` installed (or on a host without
    systemd), ccproxy falls back to stderr with a warning log."""

    readiness_probe_url: str = "https://1.1.1.1/"
    """Canary URL for the startup outbound-reachability probe. Any HTTP
    response (status code irrelevant) counts as success. Cloudflare's
    1.1.1.1 DNS server is chosen because it's reachable by direct IP
    (no DNS resolution required) and globally reliable; override if you
    need a different canary."""

    readiness_probe_timeout_seconds: float = 5.0
    """Total timeout budget for the startup readiness probe. Short by
    design — the probe is trivial and slow responses indicate a problem."""

    inspector: InspectorConfig = Field(default_factory=InspectorConfig)

    otel: OtelConfig = Field(default_factory=OtelConfig)

    compliance: ComplianceConfig = Field(default_factory=ComplianceConfig)

    flows: FlowsConfig = Field(default_factory=lambda: FlowsConfig())

    oat_sources: dict[str, str | OAuthSource | dict[str, Any]] = Field(default_factory=lambda: {})

    _oat_values: dict[str, str] = PrivateAttr(default_factory=lambda: {})

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
                "ccproxy.hooks.inject_mcp_notifications",
                "ccproxy.hooks.verbose_mode",
                "ccproxy.hooks.apply_compliance",
            ],
        },
    )

    ccproxy_config_path: Path = Field(default_factory=lambda: Path("./ccproxy.yaml"))

    @property
    def resolved_log_file(self) -> Path | None:
        """log_file resolved against ccproxy_config_path.parent.

        Relative paths anchor to the config file's directory; absolute
        paths pass through; None stays None.
        """
        if self.log_file is None:
            return None
        if self.log_file.is_absolute():
            return self.log_file
        return self.ccproxy_config_path.parent / self.log_file

    @property
    def oat_values(self) -> dict[str, str]:
        """Get the cached OAuth token values."""
        return dict(self._oat_values)

    def get_oauth_token(self, provider: str) -> str | None:
        """Get cached OAuth token for a specific provider."""
        return self._oat_values.get(provider)

    def _resolve_oauth_token(self, provider: str) -> tuple[str, str | None] | None:
        """Resolve OAuth token for a provider via its credential source."""
        source = self.oat_sources.get(provider)
        if not source:
            logger.warning("No OAuth source configured for provider '%s'", provider)
            return None

        oauth_source: OAuthSource
        if isinstance(source, str):
            oauth_source = OAuthSource(command=source)
        elif isinstance(source, OAuthSource):
            oauth_source = source
        else:
            oauth_source = OAuthSource(**source)

        token = oauth_source.resolve(f"OAuth/{provider}")
        if token is None:
            return None
        return (token, oauth_source.user_agent)

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
        """Get custom User-Agent for a specific provider."""
        return self._oat_user_agents.get(provider)

    def get_auth_header(self, provider: str) -> str | None:
        """Get target auth header name for a specific provider."""
        source = self.oat_sources.get(provider)
        if isinstance(source, OAuthSource):
            return source.auth_header
        return None

    def get_provider_for_destination(self, api_base: str | None) -> str | None:
        """Find which provider should handle requests to a given api_base."""
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
        """Execute shell commands to load OAuth tokens for all configured providers at startup."""
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
        """Load configuration from ccproxy.yaml file."""
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
                if "log_level" in ccproxy_data:
                    instance.log_level = ccproxy_data["log_level"]
                if "log_file" in ccproxy_data:
                    raw = ccproxy_data["log_file"]
                    instance.log_file = Path(raw) if raw is not None else None
                if "oat_sources" in ccproxy_data:
                    instance.oat_sources = ccproxy_data["oat_sources"]
                inspector_data = ccproxy_data.get("inspector")
                if inspector_data:
                    instance.inspector = InspectorConfig(**cast(dict[str, Any], inspector_data))
                otel_data = ccproxy_data.get("otel")
                if otel_data:
                    instance.otel = OtelConfig(**otel_data)

                compliance_data = ccproxy_data.get("compliance")
                if compliance_data:
                    instance.compliance = ComplianceConfig(**compliance_data)

                flows_data = ccproxy_data.get("flows")
                if flows_data:
                    instance.flows = FlowsConfig(**flows_data)

                hooks_data = ccproxy_data.get("hooks", [])
                if hooks_data:
                    instance.hooks = hooks_data

        instance._load_credentials()

        return instance


_config_instance: CCProxyConfig | None = None
_config_lock = threading.Lock()


def get_config_dir() -> Path:
    """Resolve the ccproxy configuration directory.

    Resolution order:

    1. ``CCPROXY_CONFIG_DIR`` env var
    2. ``$XDG_CONFIG_HOME/ccproxy`` (defaults to ``~/.config/ccproxy``)
    """
    env_dir = os.environ.get("CCPROXY_CONFIG_DIR")
    if env_dir:
        return Path(env_dir)
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg_config_home) if xdg_config_home else Path.home() / ".config"
    return base / "ccproxy"


def get_config() -> CCProxyConfig:
    global _config_instance

    if _config_instance is None:
        with _config_lock:
            if _config_instance is None:
                config_path = get_config_dir()
                logger.info(f"Using config directory: {config_path}")

                ccproxy_yaml = config_path / "ccproxy.yaml"
                if ccproxy_yaml.exists():
                    logger.info(f"Loading config from: {ccproxy_yaml}")
                    _config_instance = CCProxyConfig.from_yaml(ccproxy_yaml)
                else:
                    logger.info("No ccproxy.yaml found, using defaults")
                    _config_instance = CCProxyConfig()

    return _config_instance


def set_config_instance(config: CCProxyConfig) -> None:
    global _config_instance
    _config_instance = config


def clear_config_instance() -> None:
    global _config_instance
    _config_instance = None
