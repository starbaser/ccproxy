# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

`ccproxy` is a transparent network interceptor for LLM tooling, built on mitmproxy and WireGuard with full TLS inspection. It accepts traffic at one of two listeners (a reverse proxy on port 4000, or a rootless WireGuard namespace jail), feeds it through a DAG-driven hook pipeline, and forwards directly to the provider API. Cross-provider request/response transformation is handled by the `lightllm` subpackage — a surgical connector into LiteLLM's `BaseConfig` transformation pipeline that bypasses the LiteLLM proxy server, cost tracking, and callbacks.

The project name is `ccproxy` (lowercase). PascalCase (`CCProxyConfig`) is reserved for class names. The PyPI distribution is `claude-ccproxy`.

## Commands

```bash
just up          # Start dev services (process-compose, detached, port 4001)
just down        # Stop dev services
just test        # uv run pytest
just lint        # uv run ruff check .
just fmt         # uv run ruff format .
just typecheck   # uv run mypy src/ccproxy
just logs        # process-compose process logs ccproxy
just sync-template  # Regenerate src/ccproxy/templates/ccproxy.yaml from nix/defaults.nix
```

```bash
uv run pytest tests/test_config.py            # Single test file
uv run pytest -k "test_token_count"           # Tests matching pattern
uv run pytest -m e2e                          # E2E tests (excluded by default)
```

Coverage threshold is 90% (`--cov-fail-under=90`). E2E tests and `tests/test_shell_integration.py` are excluded by default.

The `process-compose` socket is `/tmp/process-compose-ccproxy.sock` (set via `PC_SOCKET_PATH` in the devShell). Never run `ccproxy start` with `&`/`disown` — use `just up`/`just down` so process-compose supervises it.

### Smoke Test

```bash
ccproxy run --inspect -- claude --model haiku -p "what's 2+2"
```

End-to-end check through the WireGuard namespace jail: namespace setup, TLS interception, hook pipeline, transform dispatch, upstream response, SSE streaming.

### CLI

```bash
ccproxy start                          # Start server (inspector mode, foreground)
ccproxy run [--inspect] -- <cmd>       # Run command with proxy env vars / WireGuard jail
ccproxy status [--proxy] [--inspect]   # Health check (bitmask exit codes)
ccproxy init [--force]                 # Initialize ~/.config/ccproxy/ccproxy.yaml
ccproxy logs [-f] [-n LINES]           # Tail $CCPROXY_CONFIG_DIR/ccproxy.log
ccproxy flows {list,dump,diff,compare,clear,shape}  # Flow inspection
ccproxy_mcp                            # FastMCP stdio server (separate console_script)
```

## Architecture

### Request Flow

```
ccproxy start
  → mitmweb (reverse + WireGuard listeners, in-process via WebMaster API)
  → InspectorAddon.request() → inbound DAG → transform (lightllm) → outbound DAG
  → provider API directly
```

### Response Flow

```
Provider API responds
  → InspectorAddon.responseheaders()
     ├─ SSE + cross-provider transform → flow.response.stream = SseTransformer(...), stash ref
     ├─ SSE + no transform           → flow.response.stream = True (passthrough)
     └─ not SSE                      → buffered by mitmproxy (store_streamed_bodies=True)
  → InspectorAddon.response()
     ├─ snapshot raw provider response → record.provider_response (from SseTransformer.raw_body or content)
     ├─ 401 retry / Gemini unwrap mutations
     └─ OTel span finish
  → transform RESPONSE route
     ├─ streamed → already handled chunk-by-chunk by SseTransformer
     └─ buffered + transform → transform_to_openai() overwrites flow.response.content
```

There is no LiteLLM subprocess, no gateway namespace, no second WireGuard tunnel. Two listeners are bound by mitmweb: `reverse:http://localhost:1@{port}` (placeholder backend, overwritten by transform) and `wireguard:{conf}@{udp_port}`.

### Addon Chain (fixed order, registered in `inspector/process.py:_build_addons`)

```
ReadySignal → InspectorAddon → MultiHARSaver → ShapeCapturer
            → ccproxy_inbound (DAG) → ccproxy_transform → ccproxy_outbound (DAG)
```

`InspectorAddon` owns OTel span lifecycle, FlowRecord creation, direction detection, and pre-pipeline request snapshot. `responseheaders()` enables SSE streaming (sets `flow.response.stream` to either `True` for passthrough or an `SseTransformer` for cross-provider transform). `response()` captures raw provider response into `record.provider_response` *before* 401-retry, Gemini unwrap, and transform mutations run.

### Key Subsystems (`src/ccproxy/`)

- **`lightllm/`** — Surgical nerve connector into LiteLLM's `BaseConfig` transformation pipeline. Standard providers: `validate_environment → get_complete_url → transform_request → sign_request`. Gemini/Vertex AI bypasses BaseConfig and uses `_get_gemini_url` + `_transform_request_body` directly. `SseTransformer` is the stateful `flow.response.stream` callable that parses SSE events, transforms each via per-provider `ModelResponseIterator`, and re-serializes as OpenAI-format SSE. `context_cache.py` handles Gemini/Vertex AI provider-side KV caching via Google's `cachedContents` API. `NoopLogging` duck-types LiteLLM's `Logging` to bypass cost/callback machinery.

- **`pipeline/`** — DAG-based hook execution engine.
  - `context.py` — `Context` wraps an `HTTPFlow` (or bare `http.Request` for shapes). Content fields (`messages`, `system`, `tools`) are lazy-parsed into Pydantic AI typed objects (`ModelMessage`, `SystemPromptPart`, `ToolDefinition`) and flushed back via `commit()`. Header mutations are immediate; body mutations are deferred until `commit()`.
  - `wire.py` — Bidirectional wire format ↔ Pydantic AI conversion. Handles `CachePoint` round-trip; supports both Anthropic (`{type, text}`, `input_schema`) and OpenAI (`{function: {name, parameters}}`) tool formats.
  - `hook.py` — `@hook(reads=..., writes=...)` decorator declares data dependencies as glom dot-paths (e.g. `"metadata.user_id"`, `"system.*.cache_control"`). Optional `model=` Pydantic schema for param validation.
  - `dag.py` — `HookDAG` topologically sorts hooks via Kahn's algorithm. `_root_key()` extracts the root field from glom dot-paths.
  - `executor.py` — Runs hooks in DAG order, calls `ctx.commit()` at the end.
  - `loader.py` — Resolves config hook-list entries (dotted paths or `{hook, params}` dicts) into `HookSpec` objects.
  - `render.py` — Renders the resolved pipeline as a `rich.console.Group` for `ccproxy status`.
  - `overrides.py` — `x-ccproxy-hooks: +hook,-hook` header for per-request force-run/force-skip.

- **`inspector/`** — mitmproxy addon layer.
  - `addon.py` — `InspectorAddon`. OTel + flow records + direction detection + pre-pipeline snapshot + provider response capture + 401 retry.
  - `process.py` — In-process mitmweb via `WebMaster`. Two listeners; options applied via `update_defer()`.
  - `pipeline.py` — `build_executor()` bridges hook registry with mitmproxy addons; `register_pipeline_routes()` wires DAG executors as xepor route handlers.
  - `router.py` — Vendored xepor `InterceptedAPI` subclass with mitmproxy 12.x fixes.
  - `routes/transform.py` — Three modes per match: `transform` (rewrite body + destination via lightllm), `redirect` (rewrite destination, preserve body), `passthrough` (unchanged).
  - `routes/models.py` — Synthetic `GET /v1/models`. Registered before transform routes so the specific path wins over `/{path}`.
  - `routes/health.py` — Synthetic `GET /health` and `GET /`.
  - `namespace.py` — Rootless user+net namespace via `unshare` + `slirp4netns` + WireGuard. Topology: TAP `10.0.2.100/24`, gateway `10.0.2.2`, DNS `10.0.2.3`. `route_localnet` sysctl + iptables OUTPUT DNAT redirects namespace `127.0.0.1:port` to `10.0.2.2:port` so tools with hardcoded localhost base URLs reach ccproxy. Requires `slirp4netns`, `wg`, `unshare`, `nsenter`, `ip`, `iptables`, `sysctl` on PATH.
  - `contentview.py` — Custom mitmproxy content views: `ClientRequestContentview` (pre-pipeline request) and `ProviderResponseContentview` (raw response).
  - `shape_capturer.py` — `ccproxy.shape` mitmproxy command for shape capture with flow validation.
  - `multi_har_saver.py` — `ccproxy.dump` mitmproxy command. Builds multi-page HAR 1.2 via `SaveHar.make_har()`. Layout: `entries[2i]` is `[fwdreq, provider_response]`, `entries[2i+1]` is `[clireq, client_response]`.

- **`hooks/`** — Built-in pipeline hooks. Run `ccproxy status` for the live, authoritative view of which hooks are configured, in what order, and what each reads/writes — the table below is a static reference.

  | Hook | Stage | Purpose |
  |------|-------|---------|
  | `forward_oauth` | inbound | Sentinel-key (`sk-ant-oat-ccproxy-{provider}`) substitution from `oat_sources`. Header-only. |
  | `extract_session_id` | inbound | Reads `metadata.user_id` via `glom(ctx._body, 'metadata.user_id')` → stores session_id on `flow.metadata` (NOT body metadata). |
  | `gemini_cli` | outbound | Single hook for all Gemini sentinel-key traffic: wraps standard Gemini bodies in the `v1internal` envelope, conditionally masquerades `google-genai-sdk/*` UAs as Gemini CLI (preserves urllib clients in their own rate-limit bucket), rewrites paths to `cloudcode-pa`, and unwraps the `{response: {...}}` envelope on the way back via `EnvelopeUnwrapStream`. The `cloudaicompanionProject` is resolved once at startup via `prewarm_project` in `cli.py`. |
  | `gemini_capacity_fallback` | outbound | Retries Gemini requests against a fallback model chain on 429 / 503 RESOURCE_EXHAUSTED. Sticky same-model retries honor `RetryInfo.retryDelay`, then walks the configured chain. 120s wall-clock budget. Streaming flows are supported via deferred stream setup in `responseheaders`. Default chain: `[gemini-3-flash-preview, gemini-2.5-pro, gemini-2.5-flash]`. |
  | `inject_mcp_notifications` | outbound | Injects buffered MCP terminal events as synthetic ToolCallPart/ToolReturnPart pairs (typed layer). |
  | `verbose_mode` | outbound | Strips `redact-thinking-*` from `anthropic-beta` header. Header-only. |
  | `shape` | outbound | Picks a per-provider captured shape, injects content fields from the incoming request per the provider's shaping profile, applies to the outbound flow. |
  | `commitbee_compat` | outbound | Last-mile compatibility shim for the commitbee tool. |
  | `regenerate_user_prompt_id` | shape inner-DAG | Re-rolls the shape's `user_prompt_id` per request. |
  | `regenerate_session_id` | shape inner-DAG | Re-rolls `metadata.user_id.session_id` if the shape carries an identity. |
  | `regenerate_billing_header` | shape inner-DAG | Re-signs the shape's `x-anthropic-billing-header` against the incoming first user message. Reads salt from `{config_dir}/billing_salts.json`. |
  | `caching.strip` | shape inner-DAG | Deletes values at glom dot-paths via `glom.delete()`. Accepts `StripParams(paths: list[str])`. |
  | `caching.insert` | shape inner-DAG | Sets a value at a glom dot-path via `glom.assign()`. Accepts `InsertParams(path: str, value: Any)`. Default value: `{"type": "ephemeral"}`. |

- **`shaping/`** — Request shaping framework. A *shape* is a captured `mitmproxy.http.HTTPFlow` (real Claude CLI request) persisted as a `{provider}.mflow`. At runtime, the working copy is configured via `http.Request.from_state()`, configured headers are stripped, `content_fields` from the provider's profile are injected from the incoming request, shape inner-DAG hooks run, then `apply_shape()` stamps headers + query params + body onto the outbound flow. The shape is the proven foundation — everything not in `content_fields` persists from the shape.
  - `caching/` — Composable glom-based cache control hooks for the shape inner DAG: `strip` (deletes via `glom.delete`) and `insert` (sets via `glom.assign`). Separate modules ensure DAG priority ordering.
  - `regenerate.py` — Shape inner-DAG hooks: `regenerate_user_prompt_id`, `regenerate_session_id`, `regenerate_billing_header` (re-signs the shape's `x-anthropic-billing-header` against the incoming first user message; reads salt from `{config_dir}/billing_salts.json`).
  - `gemini.py` — Gemini-specific shape hook.

- **`flows/store.py`** — TTL store keyed by `x-ccproxy-flow-id` for cross-addon state. `HttpSnapshot` is the unified HTTP message snapshot. `FlowRecord` carries `client_request`, `provider_response`, `TransformMeta`, and enrichment fields (`conversation_id` = SHA12 of first user text; `system_prompt_sha` = SHA12 of `json.dumps(system, sort_keys=True)`).

- **`oauth/`** — OAuth credential sources and provider-specific refresh.
  - `sources.py` — Discriminated `OAuthSource` union: `CommandOAuthSource`, `FileOAuthSource`, `AnthropicOAuthSource`, `GoogleOAuthSource`. `parse_oauth_source` accepts bare strings (legacy command form), explicit `type:` discriminators, or dicts inferred by their keys.
  - `anthropic.py` — POSTs `grant_type=refresh_token` form-encoded to `claude.ai/v1/oauth/token`. Atomic write-back via tmp + fsync + rename + chmod 0o600.
  - `google.py` — Mirrors the Anthropic flow but POSTs to Google's OAuth endpoint. Workaround for gemini-cli #21691: preserves on-disk `refresh_token` if Google's response omits it.

- **`specs/`** — Vendored constants, Pydantic schemas, model catalog.
  - `claude_code_constants.py` — `BASE_BETAS`, `LONG_CONTEXT_BETAS` (vendored fact lists).
  - `claude_code_request.py` — `APIRequestParams` mirroring `/v1/messages` schema (`extra="allow"`).
  - `billing_salt.py` — Reads `{config_dir}/billing_salts.json` (`{cc_version: 12-hex-salt}` map). Path is fixed (no env var); file is gitignored. mtime-cached. Anthropic's server validates the billing-header suffix against a `(salt, version)` pair embedded in each claude-code release — the committed default ships zero salts.
  - `model_catalog.py` — OpenAI-compatible `/v1/models` payload generator. `STATIC_MODEL_CATALOG` is the floor list; `build_catalog(refresh=True)` queries each provider's upstream `/v1/models` and unions deduplicated results, falling back to the static floor on per-provider failure.

- **`mcp/`** — Two surfaces.
  - `buffer.py` + `routes.py` — Thread-safe `NotificationBuffer` singleton + `POST /mcp/notify` FastAPI endpoint for MCP terminal event ingestion (consumed by the `inject_mcp_notifications` hook).
  - `server.py` — FastMCP stdio server exposing 12 tools (`list_flows`, `get_flow`, `dump_har`, `get_request_body`, `get_response_body`, `diff_flows`, `compare_flow`, `clear_flows`, `capture_shape`, `list_shapes`, `list_conversations`, `list_models`) and 2 resources (`proxy://requests`, `proxy://status`). Wraps `MitmwebClient` and `ShapeStore` so MCP-aware clients can drive ccproxy without spawning the CLI per call. Console-script entry point: `ccproxy_mcp`.

- **`flows.py` (CLI)** — `Flows*` tyro subcommands plus `MitmwebClient` for programmatic mitmweb REST access. Auth is Bearer token resolved from `inspector.mitmproxy.web_password`. All subcommands operate on a resolved flow set: `GET /flows → config default_jq_filters → CLI --jq filters → final set`. Filters are jq expressions (subprocess; not a Python dependency); each must consume and produce a JSON array. Multiple `--jq` flags chain via `|`.

### Configuration

**Discovery**: `$CCPROXY_CONFIG_DIR` (default: `$XDG_CONFIG_HOME/ccproxy/`) is the single knob. Both `ccproxy.yaml` and `billing_salts.json` are read from it. Setting `CCPROXY_CONFIG_DIR=$PWD/.ccproxy` (the dev shell does this) yields a project-local config.

**Hook config format** — each entry is either a dotted module path (bare hook) or a `{hook, params}` dict:

```yaml
hooks:
  outbound:
    - ccproxy.hooks.gemini_cli
    - hook: ccproxy.hooks.gemini_capacity_fallback
      params:
        fallback_models: [gemini-3-flash-preview, gemini-2.5-pro, gemini-2.5-flash]
    - ccproxy.hooks.shape
```

**Transform matching** — `inspector.transforms` list, first match wins. Match fields: `match_host` (checked against `pretty_host` + Host + X-Forwarded-Host), `match_path` (prefix), `match_model` (substring in body). Three modes: `redirect` (default), `transform`, `passthrough`. Vertex AI fields: `dest_vertex_project`, `dest_vertex_location`.

**Shaping config** — per-provider profiles. `content_fields` lists keys injected from the incoming request — everything else persists from the shape. `merge_strategies` overrides the default `replace`: `prepend_shape`, `append_shape`, `drop`. Append `:N` to slice the shape's array first (e.g. `prepend_shape:2`). `preserve_headers` lists target flow headers `apply_shape` must not overwrite. `strip_headers` lists shape headers to remove before stamping. `capture.path_pattern` validates flows during `ccproxy flows shape`.

### Singleton Patterns

`CCProxyConfig`, `NotificationBuffer`, `FlowStore`, `ShapeStore` are thread-safe singletons. `specs/billing_salt.py` keeps an mtime-keyed cache. The `cleanup` autouse fixture in `tests/conftest.py` resets all of them: `clear_config_instance()`, `clear_buffer()`, `clear_flow_store()`, `clear_store_instance()`, `clear_shape_hook_cache()`, `clear_salts_cache()`.

### OAuth & Sentinel Keys

The sentinel key `sk-ant-oat-ccproxy-{provider}` triggers token substitution from `oat_sources` via the `forward_oauth` hook. ALL API keys in MCP server configs and client environments must be ccproxy sentinel keys — using raw provider keys bypasses the `forward_oauth` hook and the shaping pipeline. If a provider isn't routable through a sentinel key, add an `oat_sources` entry for it.

`oat_sources` is a `dict[str, OAuthSource]` discriminated union (see `oauth/sources.py`): `command` (bare YAML strings also map here), `file`, `anthropic_oauth`, `google_oauth`. On 401, the credential source is re-resolved; if the token changed, the request is retried with the fresh token.

### Anthropic Billing Header

The `regenerate_billing_header` shape inner-DAG hook re-signs the shape's `x-anthropic-billing-header` against the incoming first user message. Anthropic's server validates the suffix against a `(salt, version)` pair embedded in each claude-code release. Salts live at `{config_dir}/billing_salts.json` — a JSON map `{cc_version: 12-hex-salt}`. The path is fixed (no config field, no env var); the file is gitignored. Users extract salts from their installed claude-code binary and write them here.

The hook parses `cc_version` from the shape's existing billing block, looks up the matching salt, and replaces only the 3-hex suffix and the 5-hex `cch` token in place. Everything else (`cc_entrypoint`, formatting, block extras like `cache_control`) survives verbatim. If no salt is configured for the shape's version, the hook no-ops with a warning and the shape's stale billing header passes through unchanged (Anthropic will then likely 400 the request — that's the correct semantics).

### Key Constants (`src/ccproxy/constants.py`)

- `OAUTH_SENTINEL_PREFIX` — `sk-ant-oat-ccproxy-`
- `SENSITIVE_PATTERNS` — regex patterns for header redaction
- `CLAUDE_CODE_SYSTEM_PREFIX` — required system prompt prefix for OAuth
- `OAuthConfigError` — fatal exception that propagates through pipeline (not swallowed)

Vendored fact lists live separately in `src/ccproxy/specs/claude_code_constants.py` (`BASE_BETAS`, `LONG_CONTEXT_BETAS`). The billing salt is NOT vendored — it lives in the user's `{config_dir}/billing_salts.json`.

### Configuration Provenance

`nix/defaults.nix` is the single source of truth for default config values. All consumers derive from it:

- `src/ccproxy/templates/ccproxy.yaml` — generated by `scripts/render_template.py`. **Do not edit directly.** Run `just sync-template` after changing `nix/defaults.nix`. A pre-commit hook auto-regenerates when `nix/defaults.nix` is staged.
- `flake.nix` exports `defaultSettings`, `lib.mkConfig` (generates a YAML config + shellHook that symlinks it and sets `CCPROXY_CONFIG_DIR`), and `homeModules.ccproxy` (Home Manager module + systemd user service).

### Dev Instance

The Nix devShell creates a dev instance by overriding `defaultSettings` with dev-specific values: port 4001, inspector UI 8084, cert store at `./.ccproxy`. Entering the devShell auto-symlinks the Nix-generated YAML to `.ccproxy/ccproxy.yaml` and sets `CCPROXY_CONFIG_DIR=$PWD/.ccproxy`. The dev instance (port 4001) and a separately-managed production instance (port 4000, Home Manager) can run simultaneously.

`.ccproxy/ccproxy.yaml` is a symlink into the Nix store (read-only). To change it: edit the `devConfig` settings override in `flake.nix`, then `direnv reload` and `just down && just up`. For one-off testing, copy the symlink target to a real file.

## Key Implementation Notes

- **TLS keylog**: `MITMPROXY_SSLKEYLOGFILE` must be set *before* any mitmproxy import (mitmproxy.net.tls evaluates it at module import). Set in `_run_inspect()` in `cli.py` before calling `run_inspector()`. Auto-exported to `{config_dir}/tls.keylog`.
- **WireGuard keylog**: Auto-exported to `{config_dir}/wg.keylog` after inspector startup for Wireshark tunnel decryption.
- **SSL CA bundle**: `_ensure_combined_ca_bundle()` combines mitmproxy CA with system CAs and injects via `SSL_CERT_FILE`, `NODE_EXTRA_CA_CERTS`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE` for `ccproxy run --inspect`.
- **Logging**: `setup_logging()` in `cli.py` installs three potential handlers on the root logger — `StreamHandler(sys.stderr)` always, `FileHandler(cfg.resolved_log_file, mode="w")` (truncated on each daemon start) when `log_file` is set, and `JournalHandler(SYSLOG_IDENTIFIER=<derived>)` when `use_journal=True`. The file is the canonical per-project log: each project's `CCPROXY_CONFIG_DIR` holds that project's `ccproxy.log`. The journal identifier defaults to a value derived from the config-dir basename (`~/.config/ccproxy/` → `ccproxy`; `~/dev/projects/foo/.ccproxy/` → `ccproxy-foo`); override with `journal_identifier:` (or `CCPROXY_JOURNAL_IDENTIFIER`). `ccproxy logs` always tails `cfg.resolved_log_file`. Use `journalctl --user -t <identifier>` for the journald-filtered view, or `process-compose process logs ccproxy` (dev shell) / `journalctl --user -u ccproxy.service` (Home Manager) for supervisor-captured stderr. All sinks carry identical content. Subprocess output routed through `ccproxy.subprocess.{slirp4netns,nsenter}` loggers. mitmproxy `TermLog` disabled (`with_termlog=False`); mitmproxy loggers route through ccproxy's handlers.
- **Hook error isolation**: Errors in one hook don't block others. `OAuthConfigError` is the exception — it propagates through the pipeline (fatal).
- **Body metadata footgun**: `ctx.metadata` uses `setdefault`, which creates an empty `metadata` key in the body on read. `commit()` strips empty metadata dicts to prevent upstream rejection (Google: "Unknown name metadata"). Hooks needing flow-level state should use `ctx.flow.metadata["ccproxy.key"]`, NOT `ctx.metadata["key"]`.
- **Three-layer access model** for hooks:
  1. Header ops — `ctx.get_header()` / `ctx.set_header()`
  2. Typed ops — `ctx.system`, `ctx.messages`, `ctx.tools` (Pydantic AI objects)
  3. Raw body ops — `from glom import glom, assign, delete` over `ctx._body`. Glom is the standard primitive for all raw body access; `reads`/`writes` declarations on `@hook` use glom dot-paths.
- **SSE streaming**: `flow.response.stream` MUST be set in `responseheaders` (before body arrives). xepor doesn't implement `responseheaders` — that lives on `InspectorAddon`. Setting `stream` in `response` is too late.
- **Provider model**: Providers are generic — URL + auth method + API format. LiteLLM's `ProviderConfigManager` resolves actual hosts/paths. The lightllm dispatch module has a small set of provider name strings as dispatch keys (`_GEMINI_PROVIDERS`, `_PATH_SUFFIXES`).
- **Docker services** (`docker-compose.yaml`): `ccproxy-jaeger` (Jaeger all-in-one, ports 4317/4318/16686) for OTel trace collection.
- **Namespace lifecycle**: `--ready-fd`/`--exit-fd` pipes for clean slirp4netns lifecycle. `PortForwarder` background thread polls `/proc/{pid}/net/tcp` every 0.5s for dynamic `add_hostfwd` port forwarding.
- **Namespace localhost routing**: Inside the WireGuard namespace, `127.0.0.1` is isolated loopback — host services are at `10.0.2.2` (slirp4netns gateway). `route_localnet` sysctl + iptables OUTPUT DNAT rules transparently redirect namespace localhost → gateway so tools with hardcoded `127.0.0.1` base URLs work. A port remap rule maps the default ccproxy port (4000) to the running instance's port when they differ.
- **Prompt caching**: Anthropic `cache_control` annotations pass through transparently via `AnthropicConfig.transform_request()`. For Gemini/Vertex AI, `cache_control` triggers the `cachedContents` API flow in `context_cache.py` (only in `transform` mode). Gemini OAuth tokens (`ya29.*`) use `Authorization: Bearer`; API keys use `?key=` in the URL. The Gemini CLI's OAuth scopes do NOT cover `cachedContents` — only API keys (`AIza*`) work for Gemini context caching.
- **Gemini through inspector**: Gemini CLI uses `cloudcode-pa.googleapis.com/v1internal:*` endpoints (matched by the `passthrough` rule). The single `gemini_cli` outbound hook wraps standard Gemini bodies in the `v1internal` envelope, conditionally masquerades the user-agent (only when it matches `google-genai-sdk/*` — preserves urllib clients in their own rate-limit bucket), rewrites the path to cloudcode-pa, and unwraps the `{response: {...}}` envelope on the way back via `EnvelopeUnwrapStream`.

## Triage Principle

ALL failures through ccproxy are OUR bug until proven otherwise. ccproxy is the intermediary — every header, token, body field, and user-agent passes through our code. When a request fails (401/403/429/5xx), triage ccproxy first: check what we're injecting, stripping, mangling, or failing to masquerade before blaming the upstream provider. For Gemini specifically: if all Gemini requests fail with 401, the in-process `GoogleOAuthSource` refresher should rotate the token automatically; if that fails, inspect `~/.gemini/oauth_creds.json` (the refresh response sometimes omits `refresh_token` per gemini-cli #21691).

## Testing

- `pytest-asyncio` with `asyncio_mode = "auto"`
- Mock flows use `MagicMock()` with real `ProxyMode.parse()` for mode objects
- Each test file defines its own flow factory helpers
- `httpx.MockTransport` is the preferred test seam for in-process HTTP (per the no-mocks-of-internals exception)
- e2e tests excluded by default (`-m "not e2e"`); `tests/test_shell_integration.py` is also excluded by default

## Type Stubs (`stubs/`)

Hand-written stubs for dependencies lacking `py.typed` or with incomplete types: `glom`, `litellm`, `opentelemetry` (optional, package not installed in dev), `xepor`. On `mypy_path = "stubs"`.

## Marketplace Plugin Sync

Plugin files (`.claude-plugin/`, `skills/`, `hooks/`, `CLAUDE.md`) are synced to `starbaser/eigenmage-marketplace`. Pushes to `starbased/dev` trigger `.github/workflows/notify-marketplace.yml`, which dispatches a `plugin-updated` event to the marketplace repo. The marketplace CI pulls the latest submodule and copies plugin-relevant files into `plugins/ccproxy/`.
