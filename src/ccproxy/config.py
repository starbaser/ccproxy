"""Configuration management for ccproxy.

Config discovery precedence:

1. ``CCPROXY_CONFIG_DIR`` env var → sibling ``ccproxy.yaml`` + ``config.yaml``
2. ``$XDG_CONFIG_HOME/ccproxy/`` (defaults to ``~/.config/ccproxy/``)

``ccproxy.yaml`` owns native services. LiteLLM-compatible ``config.yaml``
model declarations compile into those existing services without importing
LiteLLM.

Individual fields can be overridden via ``CCPROXY_`` prefixed env vars
(e.g. ``CCPROXY_PORT=4001``).
"""

import logging
import os
import re
import threading
from copy import deepcopy
from pathlib import Path
from typing import Annotated, Any, Literal, cast
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ccproxy.auth.sources import (
    AnyAuthSource,
    AuthFields,
    parse_auth_source,
)

logger = logging.getLogger(__name__)

PplxSource = Literal["web", "scholar", "social", "edgar"]


def _default_pplx_sources() -> list[PplxSource]:
    return ["web"]


__all__ = [
    "AnthropicShapingConfig",
    "AnyAuthSource",
    "AuthRuntimeConfig",
    "BillingConfig",
    "CCProxyConfig",
    "GeminiCapacityFallbackConfig",
    "LightllmConfig",
    "McpBufferConfig",
    "McpConfig",
    "McpHttpConfig",
    "ModelBinding",
    "PplxConfig",
    "PplxSearchConfig",
    "PplxUploadConfig",
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

    These are owned by the pipeline (auth injected by inject_auth,
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

    Defaults to ``{config_dir}/shapes`` when unset. Provider patch queues
    live under this same directory as ``{provider}/series`` plus patch files.
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

    Each filter is a flow-set selector: it must consume a JSON array and
    produce one JSON array of flow objects, e.g.::

        map(select(.request.host | endswith("anthropic.com")))

    For arbitrary projections, use ``ccproxy flows list --json | jq ...``.
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

    enabled: bool = True
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


class AuthRuntimeConfig(BaseModel):
    """Runtime knobs for credential command execution and OAuth refreshes."""

    model_config = ConfigDict(extra="ignore")

    command_timeout_seconds: float = Field(default=5.0, gt=0)
    """Timeout for command-based credential sources."""

    refresh_timeout_seconds: float = Field(default=15.0, gt=0)
    """HTTP timeout for OAuth token refresh requests."""

    refresh_headroom_seconds: float = Field(default=60.0, ge=0)
    """Refresh cached access tokens when they expire within this many seconds."""


class PplxSearchConfig(BaseModel):
    """Perplexity query-shaping defaults and preflight behavior."""

    model_config = ConfigDict(extra="ignore")

    language: str = "en-US"
    timezone: str = "America/Los_Angeles"
    search_focus: Literal["internet", "writing"] = "internet"
    sources: list[PplxSource] = Field(default_factory=_default_pplx_sources)
    search_recency_filter: Literal["DAY", "WEEK", "MONTH", "YEAR"] | None = None
    is_incognito: bool = False
    skip_search_enabled: bool = True
    is_nav_suggestions_disabled: bool = True
    always_search_override: bool = False
    override_no_search: bool = False
    preflight_timeout_seconds: float = Field(default=5.0, gt=0)


class PplxThreadConfig(BaseModel):
    """Perplexity thread-continuation runtime knobs.

    Owned by :class:`~ccproxy.inspector.pplx_addon.PerplexityAddon` and the
    ``pplx_thread_inject`` hook. Distinct from :class:`Provider` (routing)
    and :class:`ShapingConfig` (Perplexity is the OpenAI→provider direction,
    so the identity-preserving shape replay subsystem doesn't apply).
    """

    model_config = ConfigDict(extra="ignore")

    consistency_mode: Literal["warn", "strict", "ignore"] = "warn"
    """How to react when incoming OpenAI message history diverges from
    Perplexity's authoritative thread state after explicit slug resolution.
    ``warn`` continues with server state and stamps a response header.
    ``strict`` raises a structured 409. ``ignore`` is silent."""

    citation_mode: Literal["markdown", "default", "clean"] = "markdown"
    """How the ``import_pplx_thread`` MCP tool formats ``[N]`` citation
    markers when converting a Perplexity thread to OpenAI ``messages[]``.
    ``markdown`` substitutes ``[N](url)``; ``default`` preserves verbatim;
    ``clean`` strips them entirely. Per-call argument overrides this."""

    ttl_seconds: float = Field(default=1800.0, gt=0)
    """L1 cache TTL for :class:`PerplexityThreadStore`. The store is
    organic-continuation-only; explicit resume via
    ``metadata.session_id`` bypasses TTL and hits the server."""

    fetch_page_size: int = Field(default=100, ge=1)
    """Per-request thread-detail page size; pagination continues until
    Perplexity reports no more pages."""

    fetch_timeout_seconds: float = Field(default=10.0, gt=0)
    """HTTP timeout for each Perplexity thread-detail page fetch."""


class PplxUploadConfig(BaseModel):
    """Perplexity multimodal attachment extraction/upload limits."""

    model_config = ConfigDict(extra="ignore")

    max_files: int = Field(default=30, ge=1)
    max_file_size_bytes: int = Field(default=50 * 1024 * 1024, ge=1)
    fetch_timeout_seconds: float = Field(default=10.0, gt=0)
    upload_timeout_seconds: float = Field(default=60.0, gt=0)
    subscribe_timeout_seconds: float = Field(default=120.0, gt=0)


class PplxConfig(BaseModel):
    """Perplexity-specific runtime configuration.

    Sibling of :class:`GeminiCapacityFallbackConfig` in topology and intent:
    provider-specific behavior knobs owned by the Perplexity addon/hook layer,
    separate from per-provider routing (:class:`Provider`) and from the
    request-shape replay subsystem (:class:`ShapingConfig`, which is
    structurally the wrong direction for OpenAI→Perplexity translation).
    """

    model_config = ConfigDict(extra="ignore")

    search: PplxSearchConfig = Field(default_factory=PplxSearchConfig)
    thread: PplxThreadConfig = Field(default_factory=PplxThreadConfig)
    upload: PplxUploadConfig = Field(default_factory=PplxUploadConfig)


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
    """Auth + single destination + provider format identifier.

    Native entries are keyed by sentinel suffix in
    :class:`CCProxyConfig.providers`; compiled LiteLLM deployments carry an
    effective Provider through their model binding. Both drive the same
    destination, transport, and credential services.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    auth: AnyAuthSource | None = None
    """Discriminated auth source (Command/File/Anthropic/Google).
    ``None`` means no managed auth — the request must already carry
    credentials."""

    base_url: str
    """Destination base URL, including scheme, optional port, and base path."""

    path: str = "/"
    """Destination path. Supports ``{model}`` and ``{action}`` templating
    substituted from glom-read body fields and URL captures at routing time."""

    headers: dict[str, str] = Field(default_factory=dict)
    """Static headers applied after destination selection."""

    query: dict[str, str] = Field(default_factory=dict)
    """Static query parameters applied after destination selection."""

    type: str
    """Wire-dialect identifier (``anthropic``, ``gemini``, ``deepseek``,
    ``openai``, ``perplexity_pro``, …). Drives
    ``lightllm.graph.dispatch_dump_sync`` when the incoming format differs
    from what the destination speaks."""

    fingerprint_profile: str | None = None
    """Explicit override for the transport fingerprint profile name.

    Resolution precedence in
    :class:`~ccproxy.inspector.transport_override_addon.TransportOverrideAddon`:

    1. This field set — always wins. Browser profiles (``"chrome131"``,
       ``"firefox144"``) map directly to ``curl-cffi`` impersonation;
       shape-backed names (``"anthropic"``) resolve through the named
       shape's ``.mflow`` metadata, with the bundled shape as fallback.
       Use this to force a different provider's shape or a browser-name
       profile for providers that don't have a captured shape.
    2. ``None`` and a shape for ``type`` exists with embedded
       :class:`~ccproxy.inspector.fingerprint.CapturedFingerprint` —
       sidecar engages implicitly keyed by ``type``. The fingerprint is
       treated as an inherent property of the captured shape.
    3. ``None`` and no shape fingerprint — mitmproxy's native transport.
    """

    @field_validator("type", mode="before")
    @classmethod
    def _coerce_type(cls, value: Any) -> Any:
        """Accept either a LlmProviders enum or a bare string. The lightllm
        registry validates it has a resolvable BaseConfig; routing only
        needs the string form for comparisons."""
        if hasattr(value, "value"):
            return value.value
        return value

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_host(cls, value: Any) -> Any:
        """Keep pre-``base_url`` ccproxy.yaml provider entries loadable."""
        if not isinstance(value, dict) or "base_url" in value or "host" not in value:
            return value
        migrated = dict(value)
        host = migrated.pop("host")
        if not isinstance(host, str) or not host:
            raise ValueError("provider host must be a non-empty string")
        migrated["base_url"] = f"https://{host}"
        return migrated

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
            raise ValueError("provider base_url must be an absolute http(s) URL")
        if parsed.query or parsed.fragment:
            raise ValueError("provider base_url cannot contain a query string or fragment")
        return value.rstrip("/")

    @field_validator("auth", mode="before")
    @classmethod
    def _parse_auth(cls, value: Any) -> Any:
        """Dispatch raw dict / bare-string YAML through ``parse_auth_source``
        so the discriminated union resolves to the right AuthSource subclass."""
        if value is None:
            return None
        return parse_auth_source(value)

    @property
    def host(self) -> str:
        parsed = urlsplit(self.base_url)
        assert parsed.hostname is not None
        return parsed.hostname

    @property
    def scheme(self) -> str:
        return urlsplit(self.base_url).scheme

    @property
    def port(self) -> int:
        parsed = urlsplit(self.base_url)
        return parsed.port or (443 if parsed.scheme == "https" else 80)


class ModelBinding(BaseModel):
    """Compiled LiteLLM model declaration consumed by native routing."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    model_name: str
    upstream_model: str
    owned_by: str
    provider_name: str
    provider: Provider
    request_defaults: dict[str, Any] = Field(default_factory=dict)
    model_info: dict[str, Any] = Field(default_factory=dict)
    source_index: int
    match_re: re.Pattern[str] = Field(exclude=True, repr=False)

    @classmethod
    def create(
        cls,
        *,
        model_name: str,
        upstream_model: str,
        owned_by: str,
        provider_name: str,
        provider: Provider,
        request_defaults: dict[str, Any],
        model_info: dict[str, Any],
        source_index: int,
    ) -> "ModelBinding":
        if model_name.count("*") > 1:
            raise ValueError(f"model_list[{source_index}].model_name supports at most one wildcard")
        if upstream_model.count("*") > 1:
            raise ValueError(f"model_list[{source_index}].litellm_params.model supports at most one wildcard")
        if "*" in upstream_model and "*" not in model_name:
            raise ValueError(f"model_list[{source_index}].litellm_params.model wildcard requires a wildcard model_name")
        pattern = "^" + re.escape(model_name).replace(r"\*", "(?P<wildcard>.+)") + "$"
        return cls(
            model_name=model_name,
            upstream_model=upstream_model,
            owned_by=owned_by,
            provider_name=provider_name,
            provider=provider,
            request_defaults=deepcopy(request_defaults),
            model_info=deepcopy(model_info),
            source_index=source_index,
            match_re=re.compile(pattern),
        )

    def matches(self, model: str) -> bool:
        return self.match_re.fullmatch(model) is not None

    def resolve_model(self, model: str) -> str:
        match = self.match_re.fullmatch(model)
        if match is None:
            raise ValueError(f"model {model!r} does not match binding {self.model_name!r}")
        wildcard = match.groupdict().get("wildcard", "")
        return self.upstream_model.replace("*", wildcard)

    def public_model_for_upstream(self, upstream_model: str) -> str | None:
        """Project one discovered upstream ID through paired wildcard templates."""
        if "*" not in self.model_name or "*" not in self.upstream_model:
            return None
        pattern = "^" + re.escape(self.upstream_model).replace(r"\*", "(?P<wildcard>.+)") + "$"
        match = re.fullmatch(pattern, upstream_model)
        if match is None:
            return None
        return self.model_name.replace("*", match.group("wildcard"))

    def apply_defaults(self, body: dict[str, Any]) -> dict[str, Any]:
        merged = deepcopy(body)
        for key, value in self.request_defaults.items():
            merged.setdefault(key, deepcopy(value))
        return merged


class TransformOverride(BaseModel):
    """Optional regex-matched override layer over Provider auto-routing.

    The default ``lightllm.transforms`` list is empty; sentinel-keyed flows
    route through :class:`CCProxyConfig.providers` automatically. Override
    rules cover edge cases — forcing a specific provider for a path/model
    combo, bypassing auth for a specific host, etc.
    """

    model_config = ConfigDict(extra="forbid")

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

    dest_base_url: str | None = None
    """Raw destination base URL. Bypasses Provider endpoint lookup."""

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
        if self.dest_base_url is not None:
            parsed = urlsplit(self.dest_base_url)
            if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
                raise ValueError("dest_base_url must be an absolute http(s) URL")
            if parsed.query or parsed.fragment:
                raise ValueError("dest_base_url cannot contain a query string or fragment")
            self.dest_base_url = self.dest_base_url.rstrip("/")
        if self.match_host is not None:
            self.match_host_re = re.compile(self.match_host)
        self.match_path_re = re.compile(self.match_path)
        if self.match_model is not None:
            self.match_model_re = re.compile(self.match_model)
        return self


class OpenAIConversationsConfig(BaseModel):
    """OpenAI Conversations (ChatGPT web) provider runtime knobs.

    Owned by :class:`~ccproxy.inspector.openai_conversations_addon.OpenAIConversationsAddon`
    and the ``openai_conversations_thread_inject`` hook. Lives under the
    ``lightllm`` config block (``config.lightllm.openai_conversations``),
    separate from :class:`Provider` (routing). Browser-profile constants
    (UA, sec-ch, client version/build) live in code, not config (ADR-0002).
    """

    model_config = ConfigDict(extra="ignore")

    warmup_throttle_seconds: float = Field(default=600.0, gt=0)
    """Skip cookie-jar warmup when it ran within this window and usable cookies
    already exist (~10 min per aurora ``cookie_bootstrap``; ``cf_clearance``
    expires in roughly 15-30 min)."""

    sentinel_skew_seconds: float = Field(default=60.0, ge=0)
    """Refresh the Sentinel token when within this headroom of ``expires_at``."""

    request_timeout_seconds: float = Field(default=120.0, gt=0)
    """HTTP timeout for warmup / Sentinel / conversation-prepare / final calls."""

    ttl_seconds: float = Field(default=3600.0, gt=0)
    """L1 TTL for :class:`~ccproxy.openai_conversations.conversation_store.ConversationStore`
    multi-turn continuation state."""

    default_model: str = "gpt-5-5-pro"
    """Model slug used when an incoming request omits the model (ADR-0002)."""

    image_poll_interval_seconds: float = Field(default=3.0, gt=0)
    """Seconds between conversation polls while awaiting an async image
    generation result (CHATGPT-006)."""

    image_poll_max_attempts: int = Field(default=40, gt=0)
    """Maximum conversation poll attempts before giving up on an async image
    generation (deadline is roughly ``image_poll_interval_seconds`` times
    ``image_poll_max_attempts``)."""

    turn_idle_timeout_seconds: float = Field(default=120.0, gt=0)
    """Maximum wait for the next conduit WebSocket frame before a turn gives
    up: the per-turn queue backstop in
    :mod:`ccproxy.openai_conversations.session_ws` (the persistent
    session socket) and the per-message read backstop in
    :mod:`ccproxy.openai_conversations.ws_handoff` (the per-turn fallback
    bridge) both bound the same reliability property and share this one
    magnitude rather than two independently hardcoded literals. No stronger
    rationale than "long enough to outlast normal turn latency, short enough
    to eventually notice a stuck conduit turn" — a give-up always ends the
    turn with the typed ``error`` finish reason (never a silently truncated
    stream), so raising or lowering this only trades responsiveness against
    tolerance for slow turns."""


class LightllmConfig(BaseModel):
    """Configuration for lightllm cross-format routing and transforms."""

    model_config = ConfigDict(extra="ignore")

    transforms: list[TransformOverride] = Field(default_factory=list)
    """Optional regex-matched override rules layered on top of the
    sentinel-driven Provider routing. Default is empty: most routing comes
    from :class:`CCProxyConfig.providers` via ``inject_auth``'s sentinel
    detection. Override rules force a specific destination for a
    path/model/host combination."""

    openai_conversations: OpenAIConversationsConfig = Field(default_factory=OpenAIConversationsConfig)
    """OpenAI Conversations (ChatGPT web) provider runtime knobs (ADR-0002)."""


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

    mitmproxy: MitmproxyOptions = Field(default_factory=MitmproxyOptions)
    """mitmproxy option overrides passed via --set flags."""

    @model_validator(mode="before")
    @classmethod
    def _reject_moved_transforms(cls, data: Any) -> Any:
        if isinstance(data, dict) and "transforms" in data:
            raise ValueError("inspector.transforms has moved to lightllm.transforms")
        return data

    @model_validator(mode="after")
    def _sync_cert_dir_to_confdir(self) -> "InspectorConfig":
        if self.cert_dir is not None and self.mitmproxy.confdir is None:
            self.mitmproxy.confdir = str(self.cert_dir.expanduser())
        return self


class McpHttpConfig(BaseModel):
    """Configuration for the in-daemon FastMCP streamable-HTTP server.

    The MCP server is hosted inside the running ccproxy daemon process. There
    is no stdio transport — this is the single MCP surface. Clients connect
    to ``http://<host>:<port>/mcp`` with a bearer token (when ``auth`` is set).
    """

    enabled: bool = True
    """Run the FastMCP streamable-HTTP server alongside the proxy/inspector.
    Set to ``false`` to disable the MCP surface entirely."""

    host: str = "127.0.0.1"
    """Bind address. Defaults to localhost only — do not expose to the network
    without putting it behind authenticated transport (the bearer token is the
    only credential)."""

    port: int = 4030
    """Streamable-HTTP listen port. Static so client ``.mcp.json`` entries are
    deterministic. The dev shell overrides this to ``4031`` to avoid colliding
    with a concurrently-running production daemon."""

    auth: AnyAuthSource | str | None = None
    """Bearer-token source. Accepts a plain string literal, a ``file`` source,
    or a ``command`` source — same shape as ``inspector.mitmproxy.web_password``.
    ``None`` (default) disables auth — for localhost-only daemons that's safe;
    if ``host`` is bound to a non-loopback address auth becomes mandatory."""

    @field_validator("auth", mode="before")
    @classmethod
    def _coerce_auth(cls, v: Any) -> Any:
        if v is None or isinstance(v, str | AuthFields):
            return v
        return parse_auth_source(v)


class McpBufferConfig(BaseModel):
    """Configuration for buffered MCP notification injection."""

    model_config = ConfigDict(extra="ignore")

    max_events_per_task: int = Field(default=64 * 1024, ge=1)
    ttl_seconds: int = Field(default=600, ge=1)


class McpConfig(BaseModel):
    """Top-level MCP namespace. Currently exposes only the HTTP server."""

    http: McpHttpConfig = Field(default_factory=McpHttpConfig)
    buffer: McpBufferConfig = Field(default_factory=McpBufferConfig)


def _default_hooks() -> dict[str, list[str | dict[str, Any]]]:
    return {
        "inbound": [
            "ccproxy.hooks.inject_auth",
            "ccproxy.hooks.extract_session_id",
        ],
        "outbound": [
            "ccproxy.hooks.inject_mcp_notifications",
            "ccproxy.hooks.verbose_mode",
            "ccproxy.hooks.shape",
        ],
    }


class CCProxyConfig(BaseSettings):
    """Existing ccproxy service configuration plus compiled model bindings."""

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
    (auth 401 retry). ``None`` (default) disables the timeout entirely,
    matching Portkey AI's upstream behavior and mitmproxy's default main-
    forward path. Set to a positive float to opt into a total request
    budget applied uniformly across connect/read/write/pool phases."""

    provider_max_connections: int = Field(default=256, gt=0)
    """Maximum active curl handles per cached upstream transport client."""

    verify_readiness_on_startup: bool = True
    """Probe a well-known external host at startup and refuse to start if
    it is unreachable. Catches broken routes, DNS, CA bundles, or namespace
    egress problems before any real traffic is accepted."""

    use_journal: bool = False
    """Route daemon logging to the systemd journal via JournalHandler.

    Requires the ``journal`` optional extra
    (``pip install ai-ccproxy[journal]``) which pulls in
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

    auth: AuthRuntimeConfig = Field(default_factory=AuthRuntimeConfig)

    inspector: InspectorConfig = Field(default_factory=InspectorConfig)

    lightllm: LightllmConfig = Field(default_factory=LightllmConfig)

    otel: OtelConfig = Field(default_factory=OtelConfig)

    shaping: ShapingConfig = Field(default_factory=ShapingConfig)

    flows: FlowsConfig = Field(default_factory=lambda: FlowsConfig())

    gemini_capacity: GeminiCapacityFallbackConfig = Field(default_factory=GeminiCapacityFallbackConfig)
    """Sticky-retry + fallback chain for Gemini RESOURCE_EXHAUSTED responses.
    Owned by :class:`~ccproxy.inspector.gemini_addon.GeminiAddon`."""

    pplx: PplxConfig = Field(default_factory=PplxConfig)
    """Perplexity-specific runtime knobs (thread continuation, citation mode,
    L1 cache TTL). Owned by :class:`~ccproxy.inspector.pplx_addon.PerplexityAddon`
    and the ``pplx_thread_inject`` hook."""

    mcp: McpConfig = Field(default_factory=McpConfig)
    """In-daemon FastMCP streamable-HTTP server. Hosts the tool surface
    (``mcp.streamable_http_app()``) inside ``run_inspector()`` alongside the
    transport sidecar. Stdio is intentionally absent — HTTP is the only MCP
    transport ccproxy ships."""

    providers: dict[str, Provider] = Field(default_factory=dict)
    """Provider entries keyed by sentinel suffix."""

    model_bindings: list[ModelBinding] = Field(default_factory=list, exclude=True)
    """Compiled LiteLLM model declarations in configuration order."""

    deployment_providers: dict[str, Provider] = Field(default_factory=dict, exclude=True)
    """Effective providers created for LiteLLM deployments with overrides."""

    litellm_diagnostics: list[Any] = Field(default_factory=list, exclude=True)
    """Compatibility diagnostics emitted while compiling ``config.yaml``."""

    # Hook configurations — either a flat list (all inbound) or a dict
    # with ``inbound`` and ``outbound`` keys for two-stage pipeline.
    hooks: dict[str, list[str | dict[str, Any]]] = Field(default_factory=lambda: _default_hooks())

    ccproxy_config_path: Path = Field(default_factory=lambda: Path("./ccproxy.yaml"))
    litellm_config_path: Path = Field(default_factory=lambda: Path("./config.yaml"))

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

    def resolve_auth_token(self, provider: str) -> str | None:
        """Resolve auth token for a provider via its ``Provider.auth`` source.

        Disk-as-truth: every call goes through ``Provider.auth.resolve()``,
        which reads the on-disk credential file and (for OAuth refresh
        sources) fires an HTTP refresh when the token is within the
        expiry headroom. Concurrent callers serialize on the per-provider
        lock — the first thread fires the refresh, followers read the
        now-fresh credential file from disk without re-hitting the upstream
        OAuth endpoint.
        """
        provider_entry = self.get_provider(provider)
        if provider_entry is None or provider_entry.auth is None:
            logger.warning("No auth configured for provider '%s'", provider)
            return None
        with _get_provider_lock(provider):
            return provider_entry.auth.resolve(f"Auth/{provider}")

    def get_auth_header(self, provider: str) -> str | None:
        """Get target auth header name for a specific provider.

        Reads ``providers[name].auth.header``. Returns ``None`` when the
        provider is unknown, has no auth, or its auth source did not
        specify a header (callers default to ``Authorization: Bearer``).
        """
        provider_entry = self.get_provider(provider)
        if provider_entry is None or provider_entry.auth is None:
            return None
        return provider_entry.auth.header

    def get_auth_extra_headers(self, provider: str) -> dict[str, str]:
        """Return companion auth headers for a provider, if its source exposes any."""
        provider_entry = self.get_provider(provider)
        if provider_entry is None or provider_entry.auth is None:
            return {}
        with _get_provider_lock(provider):
            return provider_entry.auth.extra_headers(f"Auth/{provider}")

    def get_provider(self, provider: str) -> Provider | None:
        """Resolve either a native provider or a compiled deployment provider."""
        return self.providers.get(provider) or self.deployment_providers.get(provider)

    def resolve_provider_auth(
        self,
        provider_name: str,
        provider: Provider,
        *,
        label: str | None = None,
    ) -> str | None:
        """Resolve credentials for an effective Provider under a stable lock."""
        if provider.auth is None:
            return None
        auth_label = label or f"Auth/{provider_name}"
        with _get_provider_lock(provider_name):
            return provider.auth.resolve(auth_label)

    @classmethod
    def from_yaml(
        cls,
        yaml_path: Path,
        *,
        litellm_path: Path | None = None,
        **kwargs: Any,
    ) -> "CCProxyConfig":
        """Load ccproxy-native YAML plus its sibling LiteLLM config frontend."""
        resolved_litellm_path = litellm_path or yaml_path.with_name("config.yaml")
        instance = cls(
            ccproxy_config_path=yaml_path,
            litellm_config_path=resolved_litellm_path,
            **kwargs,
        )

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
                lightllm_data = ccproxy_data.get("lightllm")
                if lightllm_data:
                    instance.lightllm = LightllmConfig(**cast(dict[str, Any], lightllm_data))
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

                pplx_data = ccproxy_data.get("pplx")
                if pplx_data:
                    instance.pplx = PplxConfig(**cast(dict[str, Any], pplx_data))

                auth_data = ccproxy_data.get("auth")
                if auth_data:
                    instance.auth = AuthRuntimeConfig(**cast(dict[str, Any], auth_data))

                mcp_data = ccproxy_data.get("mcp")
                if mcp_data:
                    instance.mcp = McpConfig(**cast(dict[str, Any], mcp_data))

        if resolved_litellm_path.exists():
            from ccproxy.litellm_config import load_litellm_config

            frontend = load_litellm_config(resolved_litellm_path, instance.providers)
            instance.model_bindings = frontend.bindings
            instance.deployment_providers = frontend.deployment_providers
            instance.litellm_diagnostics = frontend.diagnostics
            for diagnostic in frontend.diagnostics:
                logger.warning("LiteLLM config %s: %s", diagnostic.path, diagnostic.message)

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
                litellm_yaml = config_path / "config.yaml"
                if ccproxy_yaml.exists() or litellm_yaml.exists():
                    logger.info(
                        "Loading config from: %s",
                        ", ".join(str(path) for path in (ccproxy_yaml, litellm_yaml) if path.exists()),
                    )
                    _config_instance = CCProxyConfig.from_yaml(ccproxy_yaml, litellm_path=litellm_yaml)
                else:
                    logger.info("No ccproxy.yaml or config.yaml found, using defaults")
                    _config_instance = CCProxyConfig()

    return _config_instance


def set_config_instance(config: CCProxyConfig) -> None:
    global _config_instance
    _config_instance = config


def clear_config_instance() -> None:
    global _config_instance
    _config_instance = None
