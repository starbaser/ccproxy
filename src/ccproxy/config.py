"""Configuration management for ccproxy.

Config discovery precedence:

1. ``CCPROXY_CONFIG_DIR`` env var → ``$CCPROXY_CONFIG_DIR/ccproxy.yaml``
2. ``$XDG_CONFIG_HOME/ccproxy/ccproxy.yaml`` (defaults to ``~/.config/ccproxy/ccproxy.yaml``)

Individual fields can be overridden via ``CCPROXY_`` prefixed env vars
(e.g. ``CCPROXY_PORT=4001``).
"""

import logging
import os
import re
import threading
from pathlib import Path
from typing import Annotated, Any, Literal, cast

import yaml
from litellm.types.utils import LlmProviders
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ccproxy.oauth.sources import (
    AnyAuthSource,
    AuthFields,
    parse_auth_source,
)

logger = logging.getLogger(__name__)

__all__ = [
    "AnthropicShapingConfig",
    "AnyAuthSource",
    "BillingConfig",
    "CCProxyConfig",
    "GeminiCapacityFallbackConfig",
    "Provider",
    "ProviderShapingConfig",
    "ShapingConfig",
    "TransformOverride",
    "clear_config_instance",
    "get_config",
    "get_config_dir",
    "set_config_instance",
]


def _expand_env(value: Any) -> Any:
    """Expand ``${VAR}`` via ``os.path.expandvars``; return ``None`` if any
    reference is left unresolved so downstream "unset → no-op" gates fire
    instead of using the literal ``${VAR}`` string."""
    if not isinstance(value, str):
        return value
    expanded = os.path.expandvars(value)
    return None if "${" in expanded else expanded


EnvTemplate = Annotated[str | None, BeforeValidator(_expand_env)]
"""String field that supports ``${VAR}`` env-var references. Falls back to
``None`` when any referenced variable is unset."""


class CaptureConfig(BaseModel):
    """Validation heuristics for shape capture."""

    model_config = ConfigDict(extra="ignore")

    path_pattern: str = ""
    """Regex matched against the flow's request path. Empty means no filter."""


class BillingConfig(BaseModel):
    """Anthropic billing-header signing constants for shape replay.

     Each field accepts either a literal value or a
    ``${VAR}`` reference that's expanded against the environment at load
    time.
    When either resolves to ``None``, ``regenerate_billing_header`` no-ops.
    """

    model_config = ConfigDict(extra="ignore")

    salt: EnvTemplate = None
    """Hex salt for the SHA-256 ``cc_version`` 3-hex suffix."""

    seed: EnvTemplate = None
    """xxhash64 seed for the 5-hex ``cch`` (hex, with or without ``0x``)."""


class ProviderShapingConfig(BaseModel):
    """Per-provider shaping profile declaring the identity/content boundary."""

    model_config = ConfigDict(extra="ignore")

    content_fields: list[str] = Field(default_factory=list)
    """Body keys injected from the incoming request. Everything else persists from the shape."""

    merge_strategies: dict[str, str] = Field(default_factory=dict)
    """Per-field merge strategy overrides. Default is ``replace``.

    Supported: ``replace``, ``prepend_shape``, ``append_shape``, ``drop``.
    Append an optional ``:N`` slice to ``prepend_shape`` or ``append_shape``
    to keep only the first *N* elements of the shape's value before merging
    (e.g. ``prepend_shape:2`` keeps the first two shape blocks).
    """

    shape_hooks: list[str | dict[str, Any]] = Field(default_factory=list)
    """Dotted paths to ``@hook``-decorated functions run after content injection.

    Each hook is DAG-ordered by its ``reads``/``writes`` declarations and
    executed against the shape context. The incoming pipeline context is
    available via ``params["incoming_ctx"]``.
    """

    capture: CaptureConfig = Field(default_factory=CaptureConfig)
    """Validation heuristics applied when capturing shapes for this provider."""

    preserve_headers: list[str] = Field(
        default_factory=lambda: ["authorization", "x-api-key", "x-goog-api-key", "host"]
    )
    """Headers on the target flow that apply_shape must NOT overwrite.

    These are owned by the pipeline (auth injected by forward_oauth,
    host set by redirect handler). The shape's values for these headers
    are discarded; the target's values are restored after stamping.
    """

    strip_headers: list[str] = Field(
        default_factory=lambda: [
            "authorization",
            "x-api-key",
            "x-goog-api-key",
            "content-length",
            "host",
            "transfer-encoding",
            "connection",
        ]
    )
    """Headers stripped from the shape working copy before stamping.

    Auth headers are stripped so stale captured tokens don't leak.
    Transport headers are stripped so content-length/host don't desync.
    """


class AnthropicShapingConfig(ProviderShapingConfig):
    """Anthropic-only extension that adds billing-header signing constants.

    The base ``ProviderShapingConfig`` covers fields shared by every
    provider. Anthropic additionally requires the ``billing`` block because
    the ``regenerate_billing_header`` shape inner-DAG hook re-signs
    ``x-anthropic-billing-header`` per request. Other providers (Gemini,
    DeepSeek, …) do not have an analogue and so do not carry this field.
    """

    billing: BillingConfig = Field(default_factory=BillingConfig)
    """Billing-header signing constants — see :class:`BillingConfig`."""


_PROVIDER_SHAPING_CLASSES: dict[str, type[ProviderShapingConfig]] = {
    "anthropic": AnthropicShapingConfig,
}


class ShapingConfig(BaseModel):
    """Configuration for the request shaping system."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    """Master switch for shape storage and application."""

    shapes_dir: str | None = None
    """Directory holding per-provider ``{provider}.mflow`` shape files.

    Defaults to ``{config_dir}/shaping/shapes`` when unset.
    """

    providers: dict[str, ProviderShapingConfig] = Field(default_factory=dict)
    """Per-provider shaping profiles keyed by provider name (e.g. ``anthropic``).

    The validator below routes known provider names to their dedicated
    subclass (e.g. ``anthropic`` → :class:`AnthropicShapingConfig`) so
    provider-specific fields like ``billing`` are typed where they apply
    and absent everywhere else.
    """

    @field_validator("providers", mode="before")
    @classmethod
    def _route_provider_subclasses(cls, value: Any) -> Any:
        """Construct provider profiles using the subclass registered for each key."""
        if not isinstance(value, dict):
            return value
        result: dict[str, ProviderShapingConfig] = {}
        for name, raw in value.items():
            if isinstance(raw, ProviderShapingConfig):
                result[name] = raw
                continue
            if not isinstance(raw, dict):
                result[name] = raw  # let Pydantic raise on the wrong type
                continue
            target_cls = _PROVIDER_SHAPING_CLASSES.get(name, ProviderShapingConfig)
            result[name] = target_cls(**raw)
        return result


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


class GeminiCapacityFallbackConfig(BaseModel):
    """Sticky-retry then fallback chain for Gemini errors (capacity + backend)."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    """Master switch. When False, errors pass through unchanged."""

    retry_status_codes: list[int] = Field(default=[429, 503, 500])
    """HTTP status codes that trigger the fallback chain."""

    fallback_models: list[str] = Field(default_factory=list)
    """Models tried in order after sticky retries on the original are exhausted."""

    sticky_retry_attempts: int = Field(default=3, ge=0, le=10)
    """Same-model retries on the original before falling through."""

    sticky_retry_max_delay_seconds: float = Field(default=60.0, gt=0)
    """Per-attempt cap on retryDelay. If server asks for longer, skip remaining
    sticky attempts and move to next candidate."""

    terminal_delay_threshold_seconds: float = Field(default=300.0, gt=0)
    """Hard ceiling. retryDelay above this halts the entire chain — server
    is signaling sustained outage."""

    total_retry_budget_seconds: float = Field(default=120.0, gt=0)
    """Wall-clock budget for the entire retry chain across all candidates."""


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

    stream_large_bodies: str | None = None
    """Stream request/response bodies larger than this threshold instead of
    buffering. None (default) disables streaming — all bodies are buffered
    so the transform handler can inspect and rewrite them. Only set this if
    you need to proxy non-API traffic with very large bodies."""

    body_size_limit: str | None = None
    """Hard limit on buffered body size. Bodies exceeding this are dropped.
    None means unlimited."""

    web_host: str = "127.0.0.1"
    """mitmweb browser UI bind address."""

    web_password: AnyAuthSource | str | None = None
    """mitmweb UI password. Accepts a plain string (literal password), or a
    ``file``/``command`` source in the same format as a Provider's ``auth``
    block. None generates a random token on each startup."""

    @field_validator("web_password", mode="before")
    @classmethod
    def _coerce_web_password(cls, v: Any) -> Any:
        if v is None or isinstance(v, str | AuthFields):
            return v
        return parse_auth_source(v)

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


class Provider(BaseModel):
    """Auth + single destination + LiteLLM format identifier.

    Keyed by sentinel suffix in :class:`CCProxyConfig.providers`. When a
    request arrives with ``x-api-key: sk-ant-oat-ccproxy-{name}``, the
    matching Provider entry drives token injection and routing.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    auth: AnyAuthSource | None = None
    """Discriminated auth source (Command/File/Anthropic/Google).
    ``None`` means no managed auth — the request must already carry
    credentials."""

    host: str
    """Destination hostname (e.g. ``api.anthropic.com``)."""

    path: str = "/"
    """Destination path. Supports ``{model}`` and ``{action}`` templating
    substituted from glom-read body fields and URL captures at routing time."""

    provider: LlmProviders
    """LiteLLM provider identifier (``anthropic``, ``gemini``, ``deepseek``,
    ``openai``, …). Drives ``lightllm.transform_to_provider`` when the
    incoming format differs from what the destination speaks. When the
    incoming format matches, the routing handler just rewrites destination
    and preserves the body."""

    @field_validator("auth", mode="before")
    @classmethod
    def _parse_auth(cls, value: Any) -> Any:
        """Dispatch raw dict / bare-string YAML through ``parse_auth_source``
        so the discriminated union resolves to the right AuthSource subclass."""
        if value is None:
            return None
        return parse_auth_source(value)


class TransformOverride(BaseModel):
    """Optional regex-matched override layer over Provider auto-routing.

    The default ``inspector.transforms`` list is empty; sentinel-keyed flows
    route through :class:`CCProxyConfig.providers` automatically. Override
    rules cover edge cases — forcing a specific provider for a path/model
    combo, bypassing auth for a specific host, etc.
    """

    model_config = ConfigDict(extra="ignore")

    match_host: str | None = None
    """Regex matched against ``pretty_host``, ``Host`` header, and
    ``X-Forwarded-Host``. ``None`` matches any host."""

    match_path: str = ".*"
    """Regex matched against the request path."""

    match_model: str | None = None
    """Regex matched against ``glom(body, "model")``. ``None`` matches
    any model."""

    action: Literal["passthrough", "redirect", "transform"] = "redirect"
    """``redirect``: rewrite destination, preserve body (same-format).
    ``transform``: rewrite both destination and body via lightllm
    (cross-format). ``passthrough``: forward unchanged."""

    dest_provider: str | None = None
    """ccproxy provider name — resolves to a ``CCProxyConfig.providers``
    entry (host/path/auth/format)."""

    dest_host: str | None = None
    """Raw host override. Bypasses Provider lookup."""

    dest_path: str | None = None
    """Raw path override."""

    dest_model: str | None = None
    """Rewrites ``body['model']``."""

    dest_vertex_project: str | None = None
    """GCP project ID for Vertex AI transforms. Required for context caching
    with ``vertex_ai`` / ``vertex_ai_beta`` providers."""

    dest_vertex_location: str | None = None
    """GCP region for Vertex AI transforms (e.g. ``us-central1``)."""

    match_host_re: re.Pattern[str] | None = Field(default=None, exclude=True, repr=False)
    match_path_re: re.Pattern[str] = Field(
        default_factory=lambda: re.compile(r".*"),
        exclude=True,
        repr=False,
    )
    match_model_re: re.Pattern[str] | None = Field(default=None, exclude=True, repr=False)

    @model_validator(mode="after")
    def _compile_match_regexes(self) -> "TransformOverride":
        if self.match_host is not None:
            self.match_host_re = re.compile(self.match_host)
        self.match_path_re = re.compile(self.match_path)
        if self.match_model is not None:
            self.match_model_re = re.compile(self.match_model)
        return self


class InspectorConfig(BaseModel):
    """Configuration for the inspector (traffic capture via mitmproxy)."""

    port: int = 8083
    """mitmweb UI port. Also serves as process-alive sentinel and
    WireGuard config API endpoint."""

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

    transforms: list[TransformOverride] = Field(default_factory=list)
    """Optional regex-matched override rules layered on top of the
    sentinel-driven Provider routing. Default is empty: most routing comes
    from :class:`CCProxyConfig.providers` via ``forward_oauth``'s sentinel
    detection. Override rules force a specific destination for a
    path/model/host combination."""

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
    """Daemon log file path. Relative paths resolve against the config file's
    directory (``ccproxy_config_path.parent``); absolute paths pass through;
    ``None`` disables file logging. Only applies to ``ccproxy start`` —
    one-shot CLI commands never write here. Truncated on each daemon restart.
    Access the resolved path via ``resolved_log_file``."""

    journal_identifier: str | None = None
    """``SYSLOG_IDENTIFIER`` for the journal handler when ``use_journal=True``.
    ``None`` (default) derives from the config-dir basename:
    ``~/.config/ccproxy/`` → ``ccproxy``;
    ``~/dev/projects/foo/.ccproxy/`` → ``ccproxy-foo``;
    other names → ``ccproxy-{name}``.
    Override via this field or ``CCPROXY_JOURNAL_IDENTIFIER``."""

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

    shaping: ShapingConfig = Field(default_factory=ShapingConfig)

    flows: FlowsConfig = Field(default_factory=lambda: FlowsConfig())

    gemini_capacity: GeminiCapacityFallbackConfig = Field(default_factory=GeminiCapacityFallbackConfig)
    """Sticky-retry + fallback chain for Gemini RESOURCE_EXHAUSTED responses.
    Owned by :class:`~ccproxy.inspector.gemini_addon.GeminiAddon`."""

    providers: dict[str, Provider] = Field(default_factory=dict)
    """Provider entries keyed by sentinel suffix.

    Iteration order is load-bearing: ``forward_oauth._try_cached_token``
    walks this dict in insertion order to pick a fallback when no auth
    header is present. ``nix/defaults.nix`` and ``ccproxy.yaml`` should
    preserve the intended priority (anthropic, gemini, deepseek, …)."""

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
                "ccproxy.hooks.shape",
            ],
        },
    )

    ccproxy_config_path: Path = Field(default_factory=lambda: Path("./ccproxy.yaml"))

    @property
    def resolved_log_file(self) -> Path | None:
        """``log_file`` resolved against ``ccproxy_config_path.parent``.

        Relative paths anchor to the config file's directory; absolute
        paths pass through; ``None`` stays ``None``.
        """
        if self.log_file is None:
            return None
        if self.log_file.is_absolute():
            return self.log_file
        return self.ccproxy_config_path.parent / self.log_file

    def resolve_oauth_token(self, provider: str) -> str | None:
        """Resolve auth token for a provider via its ``Provider.auth`` source.

        Disk-as-truth: every call goes through ``Provider.auth.resolve()``,
        which reads the on-disk credential file and (for OAuth refresh
        sources) fires an HTTP refresh when the token is within the
        expiry headroom. Concurrent callers serialize on the per-provider
        lock — the first thread fires the refresh, followers read the
        now-fresh credential file from disk without re-hitting the upstream
        OAuth endpoint.
        """
        provider_entry = self.providers.get(provider)
        if provider_entry is None or provider_entry.auth is None:
            logger.warning("No auth configured for provider '%s'", provider)
            return None
        with _get_provider_lock(provider):
            return provider_entry.auth.resolve(f"OAuth/{provider}")

    def get_auth_header(self, provider: str) -> str | None:
        """Get target auth header name for a specific provider.

        Reads ``providers[name].auth.header``. Returns ``None`` when the
        provider is unknown, has no auth, or its auth source did not
        specify a header (callers default to ``Authorization: Bearer``).
        """
        provider_entry = self.providers.get(provider)
        if provider_entry is None or provider_entry.auth is None:
            return None
        return provider_entry.auth.header

    @classmethod
    def from_yaml(cls, yaml_path: Path, **kwargs: Any) -> "CCProxyConfig":
        """Load configuration from ccproxy.yaml file."""
        instance = cls(ccproxy_config_path=yaml_path, **kwargs)

        if yaml_path.exists():
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
                if "journal_identifier" in ccproxy_data:
                    instance.journal_identifier = ccproxy_data["journal_identifier"]
                if "providers" in ccproxy_data:
                    raw_providers = ccproxy_data["providers"] or {}
                    instance.providers = {
                        name: spec if isinstance(spec, Provider) else Provider(**spec)
                        for name, spec in raw_providers.items()
                    }
                inspector_data = ccproxy_data.get("inspector")
                if inspector_data:
                    instance.inspector = InspectorConfig(**cast(dict[str, Any], inspector_data))
                otel_data = ccproxy_data.get("otel")
                if otel_data:
                    instance.otel = OtelConfig(**otel_data)

                shaping_data = ccproxy_data.get("shaping")
                if shaping_data:
                    instance.shaping = ShapingConfig(**shaping_data)

                flows_data = ccproxy_data.get("flows")
                if flows_data:
                    instance.flows = FlowsConfig(**flows_data)

                hooks_data = ccproxy_data.get("hooks", [])
                if hooks_data:
                    instance.hooks = hooks_data

                gemini_capacity_data = ccproxy_data.get("gemini_capacity")
                if gemini_capacity_data:
                    instance.gemini_capacity = GeminiCapacityFallbackConfig(**gemini_capacity_data)

        return instance


_config_instance: CCProxyConfig | None = None
_config_lock = threading.Lock()

_provider_locks: dict[str, threading.Lock] = {}
_provider_locks_meta_lock = threading.Lock()


def _get_provider_lock(provider: str) -> threading.Lock:
    """Lazy per-provider lock, double-checked under a meta lock."""
    lock = _provider_locks.get(provider)
    if lock is not None:
        return lock
    with _provider_locks_meta_lock:
        if provider not in _provider_locks:
            _provider_locks[provider] = threading.Lock()
        return _provider_locks[provider]


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
                logger.info("Using config directory: %s", config_path)

                ccproxy_yaml = config_path / "ccproxy.yaml"
                if ccproxy_yaml.exists():
                    logger.info("Loading config from: %s", ccproxy_yaml)
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
