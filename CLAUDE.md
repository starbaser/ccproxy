# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

`ccproxy` is a transparent network interceptor for LLM tooling. It accepts traffic at one of two listeners (a reverse proxy on port 4000, or a rootless WireGuard namespace jail), feeds each request through a DAG-driven hook pipeline, and forwards directly to the provider API. Cross-provider request/response transformation is handled by the `lightllm` subpackage — a surgical connector into LiteLLM's `BaseConfig` transformation pipeline that bypasses the LiteLLM proxy server, cost tracking, and callbacks.

The package name is `ccproxy` (lowercase). The PyPI distribution is `claude-ccproxy`. Python 3.13+. Console scripts: `ccproxy` (`ccproxy.cli:entry_point`) and `ccproxy_mcp` (`ccproxy.mcp.server:main`).

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

Coverage threshold is 90% (`--cov-fail-under=90`). `-m "not e2e"` and `--ignore=tests/test_shell_integration.py` are baked into pytest's default `addopts`.

The `process-compose` socket is `/tmp/process-compose-ccproxy.sock` (set via `PC_SOCKET_PATH` in the devShell). Never run `ccproxy start` with `&`/`disown` — use `just up`/`just down` so process-compose supervises it.

`just up` is idempotent — it does NOT restart an already-running dev daemon, so source changes won't be picked up. After editing ccproxy code, run `just restart` to load the new code. Production's systemd unit reloads automatically via `X-Restart-Triggers` only when the generated YAML changes — code-only changes there require `systemctl --user restart ccproxy`.

### CLI

```bash
ccproxy start                          # Start server (inspector mode, foreground)
ccproxy run [--inspect] -- <cmd>       # Run command with proxy env vars / WireGuard jail
ccproxy status [--proxy] [--inspect]   # Health check (bitmask exit codes: 1=proxy down, 2=inspect down)
ccproxy init [--force]                 # Initialize ~/.config/ccproxy/ccproxy.yaml
ccproxy logs [-f] [-n LINES]           # Tail $CCPROXY_CONFIG_DIR/ccproxy.log
ccproxy flows {list,dump,diff,compare,clear,shape}  # Flow inspection
ccproxy_mcp                            # FastMCP stdio server (separate console_script)
```

### Smoke Test

```bash
ccproxy run --inspect -- claude --model haiku -p "what's 2+2"
```

End-to-end check through the WireGuard namespace jail: namespace setup, TLS interception, hook pipeline, transform dispatch, upstream response, SSE streaming.

## Architecture

### Request/Response Flow

```
ccproxy start
  → mitmweb (reverse + WireGuard listeners, in-process via WebMaster API)
  → InspectorAddon.request() → MultiHARSaver → ShapeCapturer
    → inbound DAG → transform router (lightllm) → outbound DAG
    → OAuthAddon → GeminiAddon
  → provider API directly
```

`InspectorAddon` owns OTel span lifecycle, FlowRecord creation, direction detection, and pre-pipeline request snapshot. `responseheaders()` sets `flow.response.stream` (either `True` for passthrough or an `SseTransformer` for cross-provider transform). `OAuthAddon` runs after the pipeline and detects 401s on flows where `forward_oauth` injected a token, refreshes, and replays. `GeminiAddon` follows it and handles cloudcode-pa response unwrapping plus capacity (429/503) sticky-retry and fallback-model walking.

There is no LiteLLM subprocess, no gateway namespace, no second WireGuard tunnel. Two listeners are bound by mitmweb: `reverse:http://localhost:1@{port}` (placeholder backend, overwritten by transform) and `wireguard:{conf}@{udp_port}`.

### Addon Chain (registered in `inspector/process.py:_build_addons`)

```
InspectorAddon → MultiHARSaver → ShapeCapturer
              → ccproxy_inbound (DAG) → ccproxy_transform → ccproxy_outbound (DAG)
              → TransportOverrideAddon → OAuthAddon → GeminiAddon
```

The pipeline routers are only added when their hook list is non-empty. `TransportOverrideAddon` runs after the outbound DAG (so it sees ccproxy-finalized requests) and before `OAuthAddon` / `GeminiAddon` — it rewrites `flow.request.host/port/scheme` to the in-process sidecar (`127.0.0.1:<sidecar_port>`) when the resolved Provider declares a `fingerprint_profile`. `OAuthAddon` and `GeminiAddon` sit after, so they see ccproxy-finalized requests/responses; `OAuthAddon.response` runs before `GeminiAddon.response`, so a 401 → refresh → replay → 429 sequence cascades into capacity fallback.

### Key Subsystems (`src/ccproxy/`)

- **`lightllm/`** — Surgical connector into LiteLLM's `BaseConfig` transformation pipeline. Standard providers: `validate_environment → get_complete_url → transform_request → sign_request`. Gemini/Vertex AI bypasses BaseConfig and uses `_get_gemini_url` + `_transform_request_body` directly. `SseTransformer` is the stateful `flow.response.stream` callable that parses SSE events, transforms each via per-provider `ModelResponseIterator`, and re-serializes as OpenAI-format SSE. `context_cache.py` handles Gemini/Vertex AI provider-side KV caching via Google's `cachedContents` API. `NoopLogging` duck-types LiteLLM's `Logging` to bypass cost/callback machinery.

- **`pipeline/`** — DAG-based hook execution engine.
  - `context.py` — `Context` wraps an `HTTPFlow` (or bare `http.Request` for shapes). Content fields (`messages`, `system`, `tools`) are lazy-parsed into Pydantic AI typed objects (`ModelMessage`, `SystemPromptPart`, `ToolDefinition`) and flushed back via `commit()`. Header mutations are immediate; body mutations are deferred until `commit()`.
  - `wire.py` — Bidirectional wire format ↔ Pydantic AI conversion. Handles `CachePoint` round-trip; supports both Anthropic (`{type, text}`, `input_schema`) and OpenAI (`{function: {name, parameters}}`) tool formats.
  - `hook.py` — `@hook(reads=..., writes=...)` decorator declares data dependencies as glom dot-paths (e.g. `"metadata.user_id"`, `"system.*.cache_control"`). Optional `model=` Pydantic schema for param validation. Convention: a sibling function named `{hook_name}_guard` becomes the hook's guard automatically.
  - `dag.py` — `HookDAG` topologically sorts hooks via Kahn's algorithm, extracting the root field from each glom dot-path for dependency resolution.
  - `executor.py` — Runs hooks in DAG order, calls `ctx.commit()` at the end. Hook errors are isolated; `OAuthConfigError` is the sole exception (fatal).
  - `loader.py` — Resolves config hook-list entries (dotted paths or `{hook, params}` dicts) into `HookSpec` objects.
  - `render.py` — Renders the resolved pipeline as a `rich.console.Group` for `ccproxy status`.
  - `overrides.py` — `x-ccproxy-hooks: +hook,-hook` header for per-request force-run/force-skip.

- **`inspector/`** — mitmproxy addon layer.
  - `addon.py` — `InspectorAddon`. OTel + flow records + direction detection + pre-pipeline snapshot + provider response capture.
  - `oauth_addon.py` — `OAuthAddon`. 401-detect → refresh → replay loop. Triggered by the `ccproxy.oauth_injected` flag set by `forward_oauth`.
  - `gemini_addon.py` — `GeminiAddon`. Capacity fallback (sticky retry + fallback chain on 429/503) plus envelope unwrap (`{response: {...}}` from cloudcode-pa). Streaming flows install `EnvelopeUnwrapStream` in `responseheaders`.
  - `process.py` — In-process mitmweb via `WebMaster`. Two listeners; options applied via `update_defer()`. WireGuard UDP port found by binding to port 0.
  - `pipeline.py` — `build_executor()` bridges hook registry with mitmproxy addons; `register_pipeline_routes()` wires DAG executors as xepor route handlers.
  - `router.py` — `InspectorRouter`, vendored xepor `InterceptedAPI` subclass with three mitmproxy 12.x fixes: addon `name` attribute, `Server(address=...)` keyword call, and wildcard host (`h is None`) match.
  - `routes/transform.py` — Three modes per match: `transform` (rewrite body + destination via lightllm), `redirect` (rewrite destination, preserve body), `passthrough` (unchanged).
  - `routes/models.py` — Synthetic `GET /v1/models`. Registered before transform routes so the specific path wins over `/{path}`.
  - `routes/health.py` — Synthetic `GET /health` and `GET /`.
  - `namespace.py` — Rootless user+net namespace via `unshare` + `slirp4netns` + WireGuard. Topology: TAP `10.0.2.100/24`, gateway `10.0.2.2`, DNS `10.0.2.3`. Requires `slirp4netns`, `wg`, `unshare`, `nsenter`, `ip`, `iptables`, `sysctl` on PATH.
  - `contentview.py` — Custom mitmproxy content views: `ClientRequestContentview` (pre-pipeline request) and `ProviderResponseContentview` (raw response).
  - `shape_capturer.py` — `ccproxy.shape` mitmproxy command for shape capture with flow validation.
  - `multi_har_saver.py` — `ccproxy.dump` mitmproxy command. Builds multi-page HAR 1.2: `entries[2i]` is `[fwdreq, provider_response]`, `entries[2i+1]` is `[clireq, client_response]`.

- **`hooks/`** — Built-in pipeline hooks. Run `ccproxy status` for the live, authoritative view of which hooks are configured, in what order, and what each reads/writes.

  | Hook | Stage | Purpose |
  |------|-------|---------|
  | `forward_oauth` | inbound | Sentinel-key (`sk-ant-oat-ccproxy-{provider}`) substitution from `providers`. Header-only. Stamps `flow.metadata["ccproxy.oauth_injected"]` and `["ccproxy.oauth_provider"]`. |
  | `extract_session_id` | inbound | Reads `metadata.user_id` via `glom(ctx._body, 'metadata.user_id')` → stores session_id on `flow.metadata` (NOT body metadata). |
  | `gemini_cli` | outbound | Single hook for all Gemini sentinel-key traffic: wraps standard Gemini bodies in the `v1internal` envelope, conditionally masquerades `google-genai-sdk/*` UAs as Gemini CLI (preserves urllib clients in their own bucket), rewrites paths to `cloudcode-pa`. Idempotent — Glass-style v1internal bodies pass through unchanged. The `cloudaicompanionProject` is resolved once at startup via `prewarm_project`. |
  | `inject_mcp_notifications` | outbound | Injects buffered MCP terminal events as synthetic `tool_use`/`tool_result` pairs, inserted BEFORE the final user message to preserve prompt cache. |
  | `verbose_mode` | outbound | Strips `redact-thinking-*` from `anthropic-beta` header. Header-only. |
  | `shape` | outbound | Picks a per-provider captured shape, injects `content_fields` from the incoming request, applies to the outbound flow. |
  | `commitbee_compat` | outbound | Last-mile compatibility shim for the commitbee tool. |

- **`shaping/`** — Request shaping framework.

  **IMPERATIVE**: Shape replay is load-bearing for Anthropic identity. The previous `inject_claude_code_identity` hook has been removed; the captured shape is now the only source of the Claude Code identity headers (user-agent, anthropic-beta, x-stainless-*, etc.) and the billing-header block. If a shape is missing or stale for the `anthropic` provider, requests will fail with 401/400 from Anthropic with no fallback. Capture a fresh shape via `ccproxy flows shape --provider anthropic` whenever the Claude CLI version changes.

  A *shape* is a captured `mitmproxy.http.HTTPFlow` (real Claude CLI request) persisted as a `{provider}.mflow`. At runtime, the working copy is configured via `http.Request.from_state()`, configured headers are stripped, `content_fields` from the provider's profile are injected from the incoming request per `merge_strategies`, shape inner-DAG hooks run, then `apply_shape()` stamps headers + query params + body onto the outbound flow.
  - `caching/` — Composable glom-based cache control hooks for the shape inner DAG: `strip` (deletes via `glom.delete`) and `insert` (sets via `glom.assign`). Used to normalize Anthropic's 4-breakpoint `cache_control` limit after `prepend_shape:N` merges.
  - `regenerate.py` — Shape inner-DAG hooks: `regenerate_user_prompt_id`, `regenerate_session_id`, `regenerate_billing_header` (re-signs `x-anthropic-billing-header`).
  - `gemini.py` — Gemini-specific shape hook.

- **`flows/store.py`** — TTL store keyed by `x-ccproxy-flow-id` for cross-addon state. `HttpSnapshot` is the unified HTTP message snapshot. `FlowRecord` carries `client_request`, `forwarded_request` (post-pipeline pre-rewrite — populated by `TransportOverrideAddon` for impersonated flows so HAR / contentviews show the real upstream intent instead of the localhost sidecar URL), `provider_response`, `TransformMeta`, `AuthMeta`, `OtelMeta`, plus enrichment fields populated in `InspectorAddon.request()`: `conversation_id` (SHA12 of first user text, or `flow:{flow.id}` fallback) and `system_prompt_sha` (SHA12 of `json.dumps(system, sort_keys=True)`). `InspectorMeta` provides string constants for `flow.metadata` keys. TTL 3600s, lazy cleanup on each `create_flow_record()`.

- **`transport/`** — Cached `httpx.AsyncClient` instances backed by `httpx-curl-cffi`'s `AsyncCurlTransport` for browser TLS+HTTP/2 fingerprint impersonation. `dispatch.py` exposes `get_client(*, host, profile) -> httpx.AsyncClient` with an LRU+idle cache keyed on `(host, profile)`; `MAX_SESSIONS=16`, 60s idle eviction, `DEFAULT_PROFILE="chrome131"`. Profile validation runs at the cache boundary against `curl_cffi.requests.impersonate.BrowserTypeLiteral` — invalid names raise `UnknownFingerprintProfileError`. `sidecar.py` runs an in-process Starlette+uvicorn HTTP server bound to `127.0.0.1:<auto>` that the `TransportOverrideAddon` redirects flows through; the two-header contract is `X-CCProxy-Target-Url` (real upstream URL) + `X-CCProxy-Impersonate` (profile). Sidecar forwards via the cached client, streams responses chunk-by-chunk via `client.send(stream=True)` + `aiter_raw()`, strips hop-by-hop both directions. `SSLKEYLOGFILE` (set in `cli.py` alongside `MITMPROXY_SSLKEYLOGFILE`) routes curl-cffi's TLS session keys into the same `tls.keylog`, so Wireshark decrypts every leg from one file. R2's OAuth and Gemini retry paths use `transport.get_client(...)` directly without going through the sidecar.

- **`oauth/sources.py`** — Class hierarchy split between static value loaders and OAuth refresh sources. `AuthFields` is the base (just optional `header` override). `CommandAuthSource` (`type: command`) and `FileAuthSource` (`type: file`) extend it as static loaders — no expiry awareness, no refresh endpoint. `AuthSource(AuthFields)` is the OAuth refresh-capable base with the `read → check expiry (60s headroom) → refresh-if-near-expiry → atomic write-back` template method, with three glom-configurable paths (`access_path`, `refresh_path`, `expiry_path`). `AnthropicAuthSource` (`type: anthropic_oauth`) and `GoogleAuthSource` (`type: google_oauth`) provide only `_build_refresh_body` plus per-provider defaults. `parse_auth_source` accepts bare strings (coerce to `command`), explicit `type:` discriminators, or dicts inferred from their `command`/`file` keys. `_write_credentials` deep-copies and uses `glom.assign(..., missing=dict)` so nested writes (e.g. `claudeAiOauth.accessToken`) preserve sibling fields (`scopes`, `subscriptionType`). Atomic write-back: tmp + fsync + rename + chmod 0o600. `gemini-cli #21691` workaround: `new_refresh = payload.get("refresh_token") or refresh` keeps the on-disk grant when Google's response omits it.

- **`specs/`** — Vendored constants, Pydantic schemas, model catalog.
  - `claude_code_constants.py` — `BASE_BETAS`, `LONG_CONTEXT_BETAS` (vendored fact lists).
  - `claude_code_request.py` — `APIRequestParams` mirroring `/v1/messages` schema (`extra="allow"`).
  - `billing_salt.py` — Returns the configured `billing_salt` from `CCProxyConfig`. The salt is NOT vendored — user supplies via `ccproxy.yaml` `shaping.providers.anthropic.billing.salt` or `CCPROXY_BILLING_SALT` env var.
  - `model_catalog.py` — OpenAI-compatible `/v1/models` payload generator. `STATIC_MODEL_CATALOG` is the floor list; `build_catalog(refresh=True)` queries each provider's upstream `/v1/models` and unions deduplicated results.

- **`mcp/`** — Two surfaces.
  - `buffer.py` + `routes.py` — Thread-safe `NotificationBuffer` singleton + `POST /mcp/notify` FastAPI endpoint for MCP terminal event ingestion (consumed by the `inject_mcp_notifications` hook). Max 50 events/task, 600s TTL, drop oldest on overflow.
  - `server.py` — FastMCP stdio server exposing tools (`list_flows`, `get_flow`, `dump_har`, `get_request_body`, `get_response_body`, `diff_flows`, `compare_flow`, `clear_flows`, `capture_shape`, `list_shapes`, `list_conversations`, `list_models`) and resources (`proxy://requests`, `proxy://status`). Wraps `MitmwebClient` and `ShapeStore`. Console-script entry point: `ccproxy_mcp`.

- **`flows.py` (CLI)** — `Flows*` tyro subcommands plus `MitmwebClient` for programmatic mitmweb REST access. Auth is Bearer token resolved from `inspector.mitmproxy.web_password`. All subcommands operate on a resolved flow set: `GET /flows → config default_jq_filters → CLI --jq filters → final set`. Filters are jq expressions (subprocess; not a Python dependency); each must consume and produce a JSON array. Multiple `--jq` flags chain via `|`.

### Configuration

**Discovery**: `$CCPROXY_CONFIG_DIR` (default: `$XDG_CONFIG_HOME/ccproxy/`) is the single knob. `ccproxy.yaml` is read from it. Setting `CCPROXY_CONFIG_DIR=$PWD/.ccproxy` (the dev shell does this) yields a project-local config.

**Provenance**: `nix/defaults.nix` is the single source of truth for default config values. `src/ccproxy/templates/ccproxy.yaml` is generated by `scripts/render_template.py`. **Do not edit the template directly.** Run `just sync-template` after changing `nix/defaults.nix`. A pre-commit hook auto-regenerates when `nix/defaults.nix` is staged. `flake.nix` exports `defaultSettings`, `lib.mkConfig` (generates a YAML config + shellHook that symlinks it and sets `CCPROXY_CONFIG_DIR`), and `homeModules.ccproxy` (Home Manager module + systemd user service in `nix/module.nix`).

**Hook config format** — each entry is either a dotted module path (bare hook) or a `{hook, params}` dict:

```yaml
hooks:
  outbound:
    - ccproxy.hooks.gemini_cli
    - hook: ccproxy.hooks.shape
    - ccproxy.hooks.verbose_mode
```

**Transform matching** — `inspector.transforms` is a list of `TransformOverride` rules layered on top of sentinel-driven Provider routing. Default is empty. Match fields are regexes: `match_host` (checked against `pretty_host` + Host + X-Forwarded-Host), `match_path`, `match_model` (matched against `glom(body, "model")`). First match wins. Three actions: `redirect` (default), `transform`, `passthrough`. Auth resolves through `dest_provider` → `config.providers[name]`; `dest_host`/`dest_path` are raw overrides that bypass the Provider lookup. Vertex AI fields: `dest_vertex_project`, `dest_vertex_location`.

**Shaping config** — per-provider profiles. `content_fields` lists keys injected from the incoming request — everything else persists from the shape. `merge_strategies` overrides the default `replace`: `prepend_shape`, `append_shape`, `drop`. Append `:N` to slice the shape's array first (e.g. `prepend_shape:2`). `preserve_headers` lists target flow headers `apply_shape` must not overwrite. `strip_headers` lists shape headers to remove before stamping. `capture.path_pattern` validates flows during `ccproxy flows shape`.

### Singleton Patterns

`CCProxyConfig`, `NotificationBuffer`, `FlowStore`, `ShapeStore` are thread-safe singletons. The `cleanup` autouse fixture in `tests/conftest.py` resets them: `clear_config_instance()`, `clear_buffer()`, `clear_flow_store()`, `clear_store_instance()`, `clear_shape_hook_cache()`.

### Providers & Sentinel Keys

The sentinel key `sk-ant-oat-ccproxy-{name}` triggers a `providers[name]` lookup via the `forward_oauth` hook: token resolution, target auth header, and routing all flow from a single `Provider` entry. ALL API keys in MCP server configs and client environments must be ccproxy sentinel keys — using raw provider keys bypasses the `forward_oauth` hook and the shaping pipeline. If a destination isn't routable through a sentinel key, add a `providers` entry for it.

`providers` is a `dict[str, Provider]`. Each `Provider` carries `auth` (an `AnyAuthSource` discriminated union — `command` / `file` / `anthropic_oauth` / `google_oauth`; bare YAML strings auto-coerce to `command`), `host` (single destination hostname), `path` (with `{model}` / `{action}` templating), `provider` (LiteLLM provider identifier OR a ccproxy-internal string registered in `lightllm/registry.py:_LOCAL_CONFIGS` like `perplexity_pro`), and an optional `fingerprint_profile` (curl-cffi impersonate name, e.g. `"chrome131"`, `"firefox144"`). `command` and `file` are static value loaders with no expiry awareness; `anthropic_oauth` and `google_oauth` extend `AuthSource` and own the in-process refresh lifecycle (60s headroom, atomic write-back to `file_path`). The optional `auth.header` field overrides the target auth header (default `authorization` with `Bearer`; set to `x-api-key` for raw injection). On 401, `OAuthAddon` re-resolves the credential source; if the token changed, the request is replayed.

When `fingerprint_profile` is set, `TransportOverrideAddon` rewrites `flow.request` to the in-process sidecar transport which forwards via `httpx-curl-cffi` — the upstream sees a real browser TLS+HTTP/2 fingerprint. Default `None` keeps mitmproxy's native transport. The field is validated against `transport.VALID_PROFILES` at config load; invalid names fail-fast. Opt in per Provider — impersonation has real costs (extra localhost hop, no HTTP/2 multiplexing across the sidecar, mitmweb's default view shows the rewritten-to-localhost request rather than the upstream URL; use the `Forwarded-Request` contentview or `ccproxy flows compare` for the real upstream intent, and Wireshark with the keylog for the on-the-wire bytes including Chrome-injected headers).

**Iteration order is load-bearing.** `providers` iteration order determines the no-sentinel fallback — the first provider with a cached token wins.

**Recommendation for Gemini**: use `type: google_oauth` (with gemini-cli's installed-app `client_id` / `client_secret`, supplied by the user — ccproxy does not vendor them) so `_load_credentials()` rotates an expired token before `prewarm_project()` POSTs to `cloudcode-pa.../v1internal:loadCodeAssist` to resolve the `cloudaicompanionProject`. With `type: command` there is no refresh — if the on-disk token is expired at startup, `prewarm_project()` silently 401s and every Gemini request lacks the `project` field.

**Perplexity Pro (`perplexity_pro`)**: ccproxy-internal provider in `lightllm/pplx.py` — a real LiteLLM `BaseConfig` subclass registered locally in `lightllm/registry.py:_LOCAL_CONFIGS`, NOT in upstream LiteLLM's `ProviderConfigManager`. Routes to `https://www.perplexity.ai/rest/sse/perplexity_ask` using a `__Secure-next-auth.session-token` cookie (Pro subscription). 22 supported models vendored in `specs/perplexity_models.json`. Token refresh via the `perplexity-webui-scraper` UV tool (`uv tool run get-perplexity-session-token`) — the previous in-tree `scripts/refresh_perplexity_token.py` is retired.

> **IMPERATIVE**: Before touching ANY code in `lightllm/pplx.py`, `lightllm/pplx_threads.py`, `hooks/pplx_*.py`, `hooks/extract_pplx_files.py`, `inspector/pplx_addon.py`, `mcp/server.py` (Perplexity tools), or anything else in the Perplexity surface — **READ `docs/pplx.md` IN ITS ENTIRETY**. The document is 1400 lines, covers the full hot path / four SSE patch modes / three resume modes / L1 cache lifecycle / multimodal upload chain / fingerprint impersonation / header semantics, and includes the troubleshooting catalogue for the specific bugs that surfaced during implementation (the `s 4.` truncation, the `equaluals 4.s 4.` doubling, the premature `finish_reason=stop`, etc.). Do NOT attempt to reconstruct mental models from this CLAUDE.md paragraph or from reading the source alone — the doc captures spec references (`~/dev/docs/man/pplx/*.md`), failure modes, and rationale that aren't in the code comments.

Routing precedence per request: (1) `inspector.transforms` regex match wins first; (2) sentinel resolution via `flow.metadata["ccproxy.oauth_provider"]` set by `forward_oauth` resolves to a `providers[name]` lookup; (3) ReverseMode flows fall through to a 501 OpenAI-shape error, WireGuard flows pass through unchanged. For sentinel-resolved Provider routing the action auto-derives: matching wire format → `redirect`, otherwise cross-format `transform` via lightllm.

### Anthropic Billing Header

The `regenerate_billing_header` shape inner-DAG hook re-signs the shape's `x-anthropic-billing-header` (`cc_version=X.Y.Z.<3hex>; cc_entrypoint=...; cch=<5hex>;`) against the incoming first user message. The salt is a single static reverse-engineered constant. It is **never committed to this repo**: users supply it via `shaping.providers.anthropic.billing.salt` in `ccproxy.yaml` or the `CCPROXY_BILLING_SALT` env var. When unset, the hook no-ops with a warning.

Two-phase signing:

1. **Typed layer (`_body`)** — read `cc_version` from the shape's existing billing block; compute the 3-hex `cc_version` suffix as `sha256(salt + sampled + version)[:3]` (where `sampled` = chars at indices 4, 7, 20 of the incoming first user text, `"0"`-padded); stamp the new text with `cch=00000;` placeholder.
2. **Wire layer (serialized bytes)** — force-commit to flush `_body`, compute `xxhash64(body_bytes, seed=billing.seed) & 0xFFFFF` formatted as 5 lowercase hex, substitute `cch=00000;` via JSON-string-scoped regex.

The version comes from the shape (not from incoming) so everything advertised upstream stays internally consistent.

### Key Constants (`src/ccproxy/constants.py`)

- `OAUTH_SENTINEL_PREFIX` — `sk-ant-oat-ccproxy-`
- `SENSITIVE_PATTERNS` — regex patterns for header redaction
- `CLAUDE_CODE_SYSTEM_PREFIX` — required system prompt prefix for OAuth
- `OAuthConfigError` — fatal exception that propagates through pipeline (not swallowed)

Vendored fact lists live separately in `src/ccproxy/specs/claude_code_constants.py`.

## Key Implementation Notes

- **TLS keylog**: `MITMPROXY_SSLKEYLOGFILE` must be set *before* any mitmproxy import (mitmproxy.net.tls evaluates it at module import). Set in `_run_inspect()` in `cli.py` before calling `run_inspector()`. Auto-exported to `{config_dir}/tls.keylog`. `SSLKEYLOGFILE` is set to the same path so curl-cffi (libcurl/BoringSSL) writes session keys for the sidecar's impersonated outbound into the same file — Wireshark decrypts client→mitmproxy and sidecar→upstream legs from one keylog.
- **WireGuard keylog**: Auto-exported to `{config_dir}/wg.keylog` after inspector startup for Wireshark tunnel decryption.
- **SSL CA bundle**: `_ensure_combined_ca_bundle()` combines mitmproxy CA with system CAs and injects via `SSL_CERT_FILE`, `NODE_EXTRA_CA_CERTS`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE` for `ccproxy run --inspect`.
- **Logging**: `setup_logging()` in `cli.py` installs three potential handlers on the root logger — `StreamHandler(sys.stderr)` always, `FileHandler(cfg.resolved_log_file, mode="w")` (truncated on each daemon start) when `log_file` is set, and `JournalHandler(SYSLOG_IDENTIFIER=<derived>)` when `use_journal=True`. The journal identifier defaults to a value derived from the config-dir basename (`~/.config/ccproxy/` → `ccproxy`; `~/dev/projects/foo/.ccproxy/` → `ccproxy-foo`). `ccproxy logs` always tails `cfg.resolved_log_file`. Subprocess output is routed through dedicated loggers (`ccproxy.subprocess.slirp4netns`, `ccproxy.subprocess.nsenter`). mitmproxy `TermLog` is disabled (`WebMaster(opts, with_termlog=False)`); mitmproxy loggers route through ccproxy's handlers.
- **Hook error isolation**: Errors in one hook don't block others. `OAuthConfigError` is the exception — it propagates through the pipeline (fatal).
- **Body metadata footgun**: `ctx.metadata` uses `setdefault`, which creates an empty `metadata` key in the body on read. `commit()` strips empty metadata dicts to prevent upstream rejection (Google: "Unknown name metadata"). Hooks needing flow-level state should use `ctx.flow.metadata["ccproxy.key"]`, NOT `ctx.metadata["key"]`.
- **Three-layer access model** for hooks:
  1. Header ops — `ctx.get_header()` / `ctx.set_header()`
  2. Typed ops — `ctx.system`, `ctx.messages`, `ctx.tools` (Pydantic AI objects)
  3. Raw body ops — `from glom import glom, assign, delete` over `ctx._body`. Glom is the standard primitive for all raw body access; `reads`/`writes` declarations on `@hook` use glom dot-paths.
- **SSE streaming**: `flow.response.stream` MUST be set in `responseheaders` (before body arrives). xepor doesn't implement `responseheaders` — that lives on `InspectorAddon` and `GeminiAddon`. Setting `stream` in `response` is too late.
- **Provider model**: Providers are generic — URL + auth method + API format. LiteLLM's `ProviderConfigManager` resolves actual hosts/paths. The lightllm dispatch module has small dispatch sets for Gemini-family providers (`_GEMINI_PROVIDERS`) and path suffixes (`_PATH_SUFFIXES`).
- **Docker services** (`docker-compose.yaml`): `ccproxy-jaeger` (Jaeger all-in-one, ports 4317/4318/16686) for OTel trace collection.
- **Namespace lifecycle**: `--ready-fd`/`--exit-fd` pipes for clean slirp4netns lifecycle. `PortForwarder` background thread polls `/proc/{pid}/net/tcp` every 0.5s for dynamic `add_hostfwd` port forwarding.
- **Namespace localhost routing**: Inside the WireGuard namespace, `127.0.0.1` is isolated loopback — host services are at `10.0.2.2` (slirp4netns gateway). `route_localnet` sysctl + iptables OUTPUT DNAT rules transparently redirect namespace localhost → gateway so tools with hardcoded `127.0.0.1` base URLs work. A port remap rule maps the default ccproxy port (4000) to the running instance's port when they differ.
- **Prompt caching**: Anthropic `cache_control` annotations pass through transparently via `AnthropicConfig.transform_request()`. For Gemini/Vertex AI, `cache_control` triggers the `cachedContents` API flow in `context_cache.py` (only in `transform` mode). Gemini OAuth tokens (`ya29.*`) use `Authorization: Bearer`; API keys use `?key=` in the URL. The Gemini CLI's OAuth scopes do NOT cover `cachedContents` — only API keys (`AIza*`) work for Gemini context caching.
- **Gemini through inspector**: Gemini CLI uses `cloudcode-pa.googleapis.com/v1internal:*` endpoints. The single `gemini_cli` outbound hook wraps standard Gemini bodies in the `v1internal` envelope, conditionally masquerades the user-agent (only when it matches `google-genai-sdk/*`), and rewrites the path to cloudcode-pa. Response unwrap is owned by `GeminiAddon`: `unwrap_buffered` in `hooks/gemini_envelope.py` for buffered (called from `GeminiAddon.response`), and `EnvelopeUnwrapStream` (also in `hooks/gemini_envelope.py`) installed by `GeminiAddon.responseheaders` for streaming.
- **Gemini capacity fallback**: Configured under `gemini_capacity` — sticky-retry attempts on the original model, then walk `fallback_models`. Honors `RetryInfo.retryDelay` capped by `sticky_retry_max_delay_seconds`; total budget bounded by `total_retry_budget_seconds`. Owned by `GeminiAddon`, NOT a hook.

## Triage Principle

ALL failures through ccproxy are OUR bug until proven otherwise. ccproxy is the intermediary — every header, token, body field, and user-agent passes through our code. When a request fails (401/403/429/5xx), triage ccproxy first: check what we're injecting, stripping, mangling, or failing to masquerade before blaming the upstream provider. For Gemini specifically: if all Gemini requests fail with 401, the in-process `GoogleAuthSource` refresher should rotate the token automatically; if that fails, inspect `~/.gemini/oauth_creds.json` (the refresh response sometimes omits `refresh_token` per gemini-cli #21691).

## Testing

- `pytest-asyncio` with `asyncio_mode = "auto"`
- Mock flows use `MagicMock()` with real `ProxyMode.parse()` for mode objects
- Each test file defines its own flow factory helpers
- `httpx.MockTransport` is the preferred test seam for in-process HTTP
- e2e tests excluded by default (`-m "not e2e"`); `tests/test_shell_integration.py` is also excluded by default
- Regression tests live under `tests/issues/regression/`

## Type Stubs (`stubs/`)

Hand-written stubs for dependencies lacking `py.typed` or with incomplete types: `glom`, `litellm`, `opentelemetry` (optional, package not installed in dev), `xepor`. On `mypy_path = "stubs"`.

## Dev Instance vs Production Instance

Two ccproxy instances can run concurrently on the same machine. They differ only in `CCPROXY_CONFIG_DIR` and the YAML beneath it; the same `nix/defaults.nix` is the floor for both.

### Dev Instance (this repo)

Defined entirely inside this repo's `flake.nix` via `devConfig = mkConfig { settings = { ... }; }`. Overrides applied to `defaultSettings`: `port = 4001`, `inspector.port = 8084`, `inspector.cert_dir = ./.ccproxy`, `inspector.mitmproxy.web_password.command = "opc secret op://dev/ccproxy/web_password"`, plus Google-OAuth `ignore_hosts`.

Lifecycle (the devShell `shellHook` does this for you):
- `mkdir -p .ccproxy`
- `ln -sfn /nix/store/<hash>-ccproxy.yaml .ccproxy/ccproxy.yaml`
- `export CCPROXY_CONFIG_DIR=$PWD/.ccproxy`

So `.ccproxy/ccproxy.yaml` is a **read-only symlink into the Nix store**. To change dev settings: edit `devConfig` in `flake.nix`, then `direnv reload` and `just down && just up`. For one-off experimental edits, replace the symlink with a real file (`cp -L .ccproxy/ccproxy.yaml /tmp/x && mv /tmp/x .ccproxy/ccproxy.yaml`); `direnv reload` will overwrite it back to a symlink.

`process-compose.yml` supervises the dev instance (`just up`/`just down`). The socket is `/tmp/process-compose-ccproxy.sock`. Logs at `.ccproxy/ccproxy.log` (truncated each start) or `process-compose process logs ccproxy`.

### Production Instance (Home Manager module)

Distributed by this repo as `homeModules.ccproxy = import ./nix/module.nix` (re-exported from `flake.nix`). Consumers add it as a flake input and import it as a Home Manager module:

```nix
# downstream flake.nix
inputs.ccproxy.url = "github:starbaser/ccproxy";  # or path:/home/.../ccproxy

# downstream home.nix
imports = [ inputs.ccproxy.homeModules.ccproxy ];
programs.ccproxy = {
  enable = true;
  settings = { providers = { ... }; otel.enabled = true; };
};
```

What the module installs:
- `cfg.package` on `home.packages` (the `ccproxy` script with `slirp4netns`/`wg`/`iproute2`/`iptables` on `PATH`).
- Generated `ccproxy.yaml` at `~/.config/ccproxy/ccproxy.yaml` (symlink into the Nix store; `home.file."${cfg.configDir}/ccproxy.yaml".source`).
- `systemd.user.services.ccproxy` running `ccproxy start` with `CCPROXY_CONFIG_DIR=%h/.config/ccproxy`. `Restart=on-failure`, `RestartSec=5s`. The unit re-runs whenever `ccproxyYaml` changes (`X-Restart-Triggers`).

Settings deep-merge over `nix/defaults.nix`. Lists (`hooks`, `transforms`, `shape_hooks`) replace wholesale; only attrset keys deep-merge. `providers` merges per-provider shallowly because each provider bundles `{auth + host + path + provider}` and `auth` is a discriminated union — partial overrides would mix exclusive auth keys.

### Defaults Flow

```
nix/defaults.nix          ← single source of truth
   │
   ├─▶ flake.nix mkConfig (dev)            ─▶ .ccproxy/ccproxy.yaml + CCPROXY_CONFIG_DIR
   ├─▶ nix/module.nix     (production HM)  ─▶ ~/.config/ccproxy/ccproxy.yaml + systemd user unit
   └─▶ scripts/render_template.py          ─▶ src/ccproxy/templates/ccproxy.yaml (used by `ccproxy init`)
```

After editing `nix/defaults.nix`, run `just sync-template` to regenerate the bundled template (a pre-commit hook does this automatically when `nix/defaults.nix` is staged).

## Marketplace Plugin Sync

Plugin files (`.claude-plugin/`, `skills/`, `hooks/`, `CLAUDE.md`) are synced to `starbaser/eigenmage-marketplace`. Pushes to `starbased/dev` trigger `.github/workflows/notify-marketplace.yml`, which dispatches a `plugin-updated` event to the marketplace repo. The marketplace CI pulls the latest submodule and copies plugin-relevant files into `plugins/ccproxy/`.
