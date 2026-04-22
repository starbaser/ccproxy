# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**IMPERATIVE**: ALL failures through ccproxy are OUR bug until proven otherwise. ccproxy is the intermediary — every header, token, body field, and user-agent passes through our code. When a request fails with any error (401/403/429/5xx), triage ccproxy first: check what we're injecting, stripping, mangling, or failing to masquerade before blaming the upstream provider. For Gemini specifically: if all Gemini requests fail with 401, run `gemini -m gemini-2.5-flash -p "hi"` directly (no ccproxy) to force an OAuth token refresh, then retry through ccproxy.

**IMPERATIVE**: All API keys in MCP server configs and client environments MUST be ccproxy sentinel keys (`sk-ant-oat-ccproxy-{provider}`). Using raw provider keys (OpenRouter, direct API keys, etc.) bypasses the `forward_oauth` hook and the shaping pipeline — traffic escapes ccproxy's control. If a provider isn't routable through a sentinel key, add an `oat_sources` entry for it.

**CRITICAL**: The project name is `ccproxy` (lowercase). The PascalCase form is used exclusively for class names (e.g., `CCProxyConfig`).

ccproxy is a mitmproxy-based transparent LLM API interceptor that routes Claude Code's requests to different providers. It runs mitmweb in-process with a DAG-driven hook pipeline and uses the `lightllm` subpackage to invoke LiteLLM's provider transformation code surgically (without cost tracking, callbacks, or the proxy server). Traffic enters via either a reverse proxy listener or a WireGuard network namespace jail, passes through a three-stage addon chain, gets transformed by lightllm, and forwards directly to the provider API.

## Development Commands

```bash
just up          # Start dev services (process-compose, detached)
just down        # Stop dev services
just test        # Run tests (uv run pytest)
just lint        # Lint (uv run ruff check .)
just fmt         # Format (uv run ruff format .)
just typecheck   # Type check (uv run mypy src/ccproxy)
```

```bash
uv run pytest tests/test_config.py           # Single test file
uv run pytest -k "test_token_count"          # Tests matching pattern
uv run pytest -m e2e                         # E2E tests (excluded by default)
```

**IMPORTANT**: Always use `just up` / `just down` for the dev instance. Never run `ccproxy start` with `&`/`disown`.

### Smoke Test

```bash
ccproxy run --inspect -- claude --model haiku -p "what's 2+2"
```

Sends a real request through the WireGuard namespace jail. Verifies: namespace setup, TLS interception, hook pipeline, transform dispatch, upstream response, SSE streaming.

### CLI

```bash
ccproxy start                     # Start server (always inspector mode, foreground)
ccproxy run <command> [args...]   # Run command with proxy env vars
ccproxy run --inspect -- <cmd>    # Run command in WireGuard namespace jail
ccproxy status [--json]           # Show running state
ccproxy init [--force]            # Initialize config files
ccproxy logs [-f] [-n LINES]     # View logs
ccproxy flows list [--json] [--jq FILTER]...     # List flow set
ccproxy flows dump [--jq FILTER]...              # Multi-page HAR of flow set
ccproxy flows diff [--jq FILTER]...              # Sliding-window diff across set
ccproxy flows compare [--jq FILTER]...           # Per-flow client-vs-forwarded diff
ccproxy flows clear [--all] [--jq FILTER]...     # Clear flow set (--all bypasses filters)
ccproxy flows shape --provider X                 # Capture a shape for a provider
```

## Architecture

### Request Flow

```
ccproxy start
  -> mitmweb (reverse + WireGuard listeners)
  -> InspectorAddon.request() -> inbound DAG -> transform (lightllm) -> outbound DAG
  -> provider API directly
```

### Response Flow

```
Provider API responds
  -> InspectorAddon.responseheaders()
     ├─ SSE + cross-provider transform → flow.response.stream = SseTransformer(...), stash ref
     ├─ SSE + no transform → flow.response.stream = True  (passthrough)
     └─ not SSE → (buffered by mitmproxy, store_streamed_bodies=True)
  -> InspectorAddon.response()
     ├─ snapshot raw provider response → record.provider_response (from SseTransformer.raw_body or content)
     ├─ 401 retry / Gemini unwrap mutations
     └─ OTel span finish
  -> transform RESPONSE route
     ├─ streamed → already handled chunk-by-chunk by SseTransformer
     └─ buffered + transform → transform_to_openai() overwrites flow.response.content
```

No LiteLLM subprocess. No gateway namespace. No second WireGuard tunnel.

### Addon Chain (fixed order, registered in `inspector/process.py`)

```
ReadySignal -> InspectorAddon -> ccproxy_inbound -> ccproxy_transform -> ccproxy_outbound
               (OTel + FlowRecord)  (DAG hooks)     (lightllm dispatch)   (DAG hooks)
```

mitmweb binds two listeners: `reverse:http://localhost:1@{port}` (placeholder backend, overwritten by transform) and `wireguard:{conf}@{udp_port}`.

### Key Subsystems

**`lightllm/`** — Surgical nerve connector into LiteLLM's `BaseConfig` transformation pipeline.
- **Request** (`transform_to_provider`): Standard providers: `validate_environment -> get_complete_url -> transform_request -> sign_request`. Gemini/Vertex AI: `_get_gemini_url` + `_transform_request_body` directly. For Gemini with API key auth, the `Authorization` header from `validate_environment()` is stripped — Google rejects API keys as Bearer tokens; auth is via `?key=` in the URL only.
- **Response non-streaming** (`transform_to_openai`): `BaseConfig.transform_response()` via `MitmResponseShim` (duck-types `httpx.Response` for mitmproxy's `flow.response`).
- **Response streaming** (`SseTransformer`): Stateful `flow.response.stream` callable. Parses SSE events, transforms each via LiteLLM's per-provider `ModelResponseIterator.chunk_parser()`, re-serializes as OpenAI-format SSE. Tees raw input chunks via `_raw_chunks` / `raw_body` property for pre-transform capture. Provider dispatch in `_make_response_iterator()`: Anthropic → `handler.py:ModelResponseIterator`, Gemini → `vertex_and_google_ai_studio_gemini.py:ModelResponseIterator`, others → `config.get_model_response_iterator()`.
- **Context caching** (`context_cache.py`): Gemini/Vertex AI provider-side KV caching via Google's `cachedContents` API. `resolve_cached_content()` detects `cache_control: {type: "ephemeral"}` annotations on messages (Anthropic format), separates cached messages, creates or finds existing cached content resources via paginated GET + POST to Google's API, and returns the resource name + filtered messages. The `cachedContent` name is passed through `_transform_request_body()` into the `generateContent` request body. Surgically imports LiteLLM's pure transformation functions (`separate_cached_messages`, `transform_openai_messages_to_gemini_context_caching`, `is_cached_message`). Owns the HTTP layer (plain `httpx.Client`). Cache key is SHA-256 of messages+tools+model, stored as `displayName` for deduplication. Minimum 1024 cached tokens required. Best-effort: any API failure falls through gracefully.
- `registry.py` wraps `ProviderConfigManager` — all LiteLLM providers for free
- `NoopLogging` duck-types LiteLLM's `Logging` class to bypass cost/callback machinery (includes `optional_params` for Gemini iterator)

**`pipeline/`** — DAG-based hook execution engine:
- `context.py` — `Context` wraps an `HTTPFlow` or bare `http.Request` (for shapes). Content fields (`messages`, `system`, `tools`) are lazy-parsed into Pydantic AI typed objects (`ModelMessage`, `SystemPromptPart`, `ToolDefinition`) and flushed back via `commit()`. `flow` is `HTTPFlow | None` — shape contexts use `from_request()` factory with `_request` stash. `_resolve_request()` returns the underlying `http.Request` from either source. Header mutations are immediate; body mutations deferred until `commit()`. `commit()` strips empty `metadata` dicts injected by property access (upstream APIs reject unknown fields).
- `wire.py` — Bidirectional wire format ↔ Pydantic AI type conversion. Pure functions: `parse_messages`/`serialize_messages`, `parse_system`/`serialize_system`, `parse_tools`/`serialize_tools`. Handles `CachePoint` round-trip (wire `cache_control` → inline `CachePoint` in `UserPromptPart.content` → `cache_control` on preceding block). Both Anthropic (`{type, text}` blocks, `input_schema`) and OpenAI (`{function: {name, parameters}}`) tool formats supported. Format-neutral: parses whatever arrives, serializes back in the same structure.
- `types.py` — Extension types for cache_control on request-side Pydantic AI types that lack it: `CachedSystemPromptPart(SystemPromptPart)` with `cache_control: dict[str, str] | None`, `CachedToolDefinition(ToolDefinition)` with `cache_control: dict[str, Any] | None`. User content uses `CachePoint` directly (already in Pydantic AI).
- `hook.py` — `@hook(reads=..., writes=...)` decorator declares data dependencies. Global `HookSpec` registry.
- `dag.py` — `HookDAG` topologically sorts hooks via Kahn's algorithm.
- `executor.py` — `PipelineExecutor.execute(flow)` runs hooks in DAG order, calls `ctx.commit()` at the end.
- `loader.py` — `load_hooks()` resolves config hook-list entries (dotted module paths or `{hook, params}` dicts) into `HookSpec` objects. Validates YAML-supplied params against each hook's declared Pydantic model.
- `render.py` — `render_pipeline()` builds a `rich.console.Group` representing the full DAG: inbound stage → lightllm transform bridge → outbound stage → provider sink. Each hook is a `rich.panel.Panel` with reads/writes. Parallel groups use `rich.columns.Columns`.
- `overrides.py` — `x-ccproxy-hooks: +hook,-hook` header for per-request force-run/force-skip.

**`inspector/`** — mitmproxy addon layer:
- `addon.py` — `InspectorAddon`: OTel span lifecycle, FlowRecord creation, direction detection, client request snapshot, provider response capture. All flows are `"inbound"`. Snapshots the pre-pipeline request as `HttpSnapshot` before hooks mutate the flow. `responseheaders()` enables SSE streaming — sets `flow.response.stream` to `True` (passthrough) or `SseTransformer` (cross-provider transform); stashes the `SseTransformer` ref in `flow.metadata["ccproxy.sse_transformer"]`. `response()` captures raw provider response into `record.provider_response` before 401 retry, Gemini unwrap, and transform mutations — reads `SseTransformer.raw_body` for streaming transform flows. Exposes `ccproxy.clientrequest` mitmproxy command for structured JSON access to client requests.
- `process.py` — In-process mitmweb via WebMaster API. Two listeners (reverse + WireGuard). Options applied via `update_defer()`.
- `pipeline.py` — `build_executor()` bridges hook registry with mitmproxy addons. `register_pipeline_routes()` wires DAG executors as xepor route handlers.
- `router.py` — Vendored xepor `InterceptedAPI` subclass with mitmproxy 12.x fixes (keyword `Server(address=...)`, `name` dedup, `host=None` wildcard).
- `routes/transform.py` — REQUEST handler: three modes, `transform` (rewrite body + destination via lightllm dispatch), `redirect` (rewrite destination host, preserve body), and `passthrough` (forward unchanged). For Gemini transform flows, calls `resolve_cached_content()` before `transform_to_provider()` to resolve context caching. Unmatched reverse proxy flows get 501; unmatched WireGuard flows pass through. RESPONSE handler: transforms non-streaming provider responses back to OpenAI format via `transform_to_openai()`. `TransformMeta` persisted on `FlowRecord` during request phase for response handler access.
- `namespace.py` — Rootless user+net namespace via `unshare` + `slirp4netns` + WireGuard. Network topology: namespace TAP IP `10.0.2.100/24`, gateway (host) `10.0.2.2`, DNS `10.0.2.3`. Default route replaced with `wg0` so all internet traffic goes through WireGuard tunnel → mitmproxy. `route_localnet` sysctl enabled for iptables OUTPUT DNAT on loopback. Three DNAT rules: PREROUTING inbound (tap0→localhost), OUTPUT outbound (localhost→gateway), OUTPUT port remap (default port→running port). `PortForwarder` polls `/proc/{pid}/net/tcp` for dynamic `add_hostfwd` port forwarding. Requires `slirp4netns`, `wg`, `unshare`, `nsenter`, `ip`, `iptables`, `sysctl`.
- `contentview.py` — Custom mitmproxy content views. `ClientRequestContentview` shows the pre-pipeline request (method, URL, headers, body). `ProviderResponseContentview` shows the raw provider response before transforms. Both registered via `contentviews.add()`.
- `flow_store.py` — TTL store keyed by `x-ccproxy-flow-id` header for cross-addon state. `HttpSnapshot` dataclass is the unified HTTP message snapshot (headers, body, optional method/url for requests, optional status_code for responses). `FlowRecord` carries `client_request: HttpSnapshot` (pre-pipeline request), `provider_response: HttpSnapshot` (raw provider response before mutations), and `TransformMeta` (provider/model/request_data/is_streaming from request phase to response phase). `ClientRequest` is an alias for `HttpSnapshot`.
- `multi_har_saver.py` — `MultiHARSaver` addon registering the `ccproxy.dump` mitmproxy command. Accepts comma-separated flow IDs, builds a multi-page HAR 1.2 via `SaveHar.make_har()`. Layout: `entries[2i] = [fwdreq, provider_response]` (forwarded request + raw provider response), `entries[2i+1] = [clireq, client_response]` (client request + post-transform response). `_build_provider_clone()` replaces response with raw snapshot; `_build_client_clone()` replaces request with client snapshot. Falls back when snapshots are absent. One page per flow, `pageref == flow.id`. Registered in `process.py` addon chain.
- `telemetry.py` — Three-mode OTel: real OTLP export, no-op, or stub.
- `wg_keylog.py` — Writes Wireshark-compatible keylog for WireGuard tunnel decryption.

**`hooks/`** — Built-in pipeline hooks:

| Hook | Stage | Purpose |
|------|-------|---------|
| `forward_oauth` | inbound | Sentinel key (`sk-ant-oat-ccproxy-{provider}`) substitution from `oat_sources` |
| `gemini_cli_compat` | inbound | Masquerades google-genai SDK user-agent as Gemini CLI for capacity allocation |
| `extract_session_id` | inbound | Parses `metadata.user_id` → stores session_id on `flow.metadata` (NOT body metadata) |
| `inject_mcp_notifications` | outbound | Injects buffered MCP terminal events as synthetic ToolCallPart/ToolReturnPart pairs |
| `verbose_mode` | outbound | Strips `redact-thinking-*` from `anthropic-beta` header |
| `shape` | outbound | Picks a per-provider captured shape, strips its original content via `prepare` fns, inhabits it with the incoming request via `fill` fns, applies to the outbound flow |

**`shaping/`** — Request shaping framework:
- **Shape**: a user-curated ``mitmproxy.http.HTTPFlow`` persisted verbatim on disk. One ``{provider}.mflow`` file per provider under ``shapes_dir``, appended to on each capture. Captured via ``ccproxy flows shape --provider X`` (invokes the ``ccproxy.shape`` mitmproxy command). At runtime, a working copy of ``shape.request`` — alias ``Shape = mitmproxy.http.Request`` — is created per outbound request via ``http.Request.from_state(shape.request.get_state())``, wrapped in ``Context.from_request(working)`` for typed access. Prepare fns strip shape content; fill fns inhabit with incoming content; ``shape_ctx.commit()`` flushes typed changes back; ``apply_shape()`` field-copies the working request onto ``ctx.flow.request`` and syncs ``ctx._body``.
- `models.py` — ``Shape`` type alias + ``apply_shape(shape, ctx)`` free function.
- `body.py` — JSON body helpers (``get_body``, ``set_body``, ``mutate_body``) for low-level access outside the typed layer.
- `store.py` — ``ShapeStore`` singleton wrapping a directory of ``.mflow`` files. Uses ``mitmproxy.io.FlowWriter``/``FlowReader``. ``pick()`` returns the most recently appended flow for a provider.
- `prepare.py` — default prepare fns (``strip_request_content``, ``strip_auth_headers``, ``strip_transport_headers``, ``strip_system_blocks``). Signature: ``Callable[[Context], None]``.
- `fill.py` — default fill fns (``fill_model``, ``fill_messages``, ``fill_tools``, ``fill_system_append``, ``fill_stream_passthrough``, ``regenerate_user_prompt_id``, ``regenerate_session_id``). Signature: ``Callable[[Context, Context], None]`` (shape_ctx, incoming_ctx).
- The ``shape`` hook composes prepare/fill via dotted-path lists (``ShapeParams``), letting users override, extend, or replace the default pipeline without subclassing.

**`mcp/`** — Thread-safe notification buffer (`NotificationBuffer` singleton) + `POST /mcp/notify` FastAPI endpoint for MCP terminal event ingestion.

**`tools/flows.py`** — `MitmwebClient` for programmatic mitmweb REST API access + `ccproxy flows` CLI tyro subcommands (`FlowsList`, `FlowsDump`, `FlowsDiff`, `FlowsCompare`, `FlowsClear`). All subcommands inherit `_FlowsBase` which provides a repeatable `--jq FILTER` arg.
- **Auth**: Bearer token resolved from `inspector.mitmproxy.web_password` config (mitmproxy 12+ accepts `Authorization: Bearer` on the REST API directly).
- **Set model**: all subcommands operate on a resolved flow set: `GET /flows` → config `flows.default_jq_filters` → CLI `--jq` filters → final set. Filters are jq expressions that consume and produce JSON arrays (e.g. `map(select(.request.host | endswith("anthropic.com")))`). Multiple `--jq` flags chain via `|`. The `jq` binary (subprocess) is used — no pypi dependency.
- **Client methods**: `list_flows()`, `get_request_body(id)`, `dump_har(ids: list[str])` (invokes the `ccproxy.dump` mitmproxy command via `POST /commands/ccproxy.dump` with comma-joined ids), `delete_flow(id)`, `clear()`. `_make_client()` reads auth from ccproxy config.
- **HAR output**: `ccproxy flows dump` emits multi-page HAR 1.2 JSON built server-side by `MultiHARSaver.ccproxy_dump` (see `inspector/multi_har_saver.py`). One page per flow, two complete HAR entries per page: `entries[2i] = [fwdreq, provider_response]` (raw), `entries[2i+1] = [clireq, client_response]` (post-transform). All HAR details delegated to `mitmproxy.addons.savehar.SaveHar.make_har()`.
- **HAR consumption**: `ccproxy flows dump > all.har` (opens in Chrome DevTools / Charles / Fiddler). Query with jq: `... | jq '.log.entries[0].request.url'` for forwarded URL, `... | jq '.log.pages | length'` for page count.
- **diff vs compare**: `diff` does a sliding-window diff of request bodies across consecutive flows in the set (requires >= 2). `compare` diffs client-request vs forwarded-request within each flow (1+ flows), plus provider-response vs client-response body diff for transform flows.

### Configuration

**Config discovery** (highest to lowest precedence):
1. `$CCPROXY_CONFIG_DIR/ccproxy.yaml`
2. `~/.config/ccproxy/ccproxy.yaml`

**Hook config format** — two-stage dict. Each entry is either a dotted module path (bare hook with empty params) or a ``{hook, params}`` dict for hooks with a ``model=`` Pydantic schema:
```yaml
hooks:
  inbound:
    - ccproxy.hooks.forward_oauth
    - ccproxy.hooks.extract_session_id
  outbound:
    - ccproxy.hooks.inject_mcp_notifications
    - ccproxy.hooks.verbose_mode
    - hook: ccproxy.hooks.shape
      params:
        prepare:
          - ccproxy.shaping.prepare.strip_request_content
          - ccproxy.shaping.prepare.strip_auth_headers
        fill:
          - ccproxy.shaping.fill.fill_model
          - ccproxy.shaping.fill.fill_messages
```

**Transform config** — `inspector.transforms` list, first match wins:
```yaml
inspector:
  transforms:
    - mode: passthrough
      match_host: cloudcode-pa.googleapis.com
    - match_path: /v1/chat/completions
      match_model: gpt-4o
      dest_provider: anthropic
      dest_model: claude-haiku-4-5-20251001
      dest_api_key_ref: anthropic
```

Matching fields: `match_host` (optional, checked against pretty_host + Host header), `match_path` (prefix), `match_model` (substring in request body). Vertex AI fields: `dest_vertex_project` and `dest_vertex_location` (required for Gemini context caching with `vertex_ai`/`vertex_ai_beta` providers).

**Shaping config** — shapes directory and the shape hook's prepare/fill lists:
```yaml
shaping:
  enabled: true
  shapes_dir: ~/.config/ccproxy/shaping/shapes  # optional; defaults to {config_dir}/shaping/shapes
```
Customization is done at the hook-params level (``ccproxy.hooks.shape.params.prepare``/``fill`` lists of dotted paths), not by subclassing. Prepare fns have signature ``Callable[[Context], None]``; fill fns have signature ``Callable[[Context, Context], None]`` (shape_ctx, incoming_ctx).

**Flows config** — `flows.default_jq_filters` list of jq expressions applied before CLI `--jq` filters:
```yaml
flows:
  default_jq_filters:
    - 'map(select(.request.host | endswith("anthropic.com")))'
```
Each filter must consume a JSON array and produce a JSON array. Filters chain in order via jq's `|` operator. An empty list (default) means no pre-filtering.

### Singleton Patterns

`CCProxyConfig`, `NotificationBuffer`, `FlowStore`, and `ShapeStore` use thread-safe singletons. Tests reset them via the `cleanup` autouse fixture (`clear_config_instance()`, `clear_buffer()`, `clear_flow_store()`, `clear_store_instance()`).

### OAuth

- **Sentinel key**: `sk-ant-oat-ccproxy-{provider}` triggers token substitution from `oat_sources` config
- **Token sources**: `oat_sources` entries with `command` (shell) or `file` (path) to obtain tokens
- **Refresh**: On 401, re-resolves the credential source. If the token changed, retries the request with the fresh token. If unchanged, fails (credential is truly stale).
- `forward_oauth` hook sets `x-ccproxy-oauth-injected: 1` to signal downstream

### Key Constants (`constants.py`)

- `ANTHROPIC_BETA_HEADERS` — required beta headers for Claude Code OAuth
- `OAUTH_SENTINEL_PREFIX` — `sk-ant-oat-ccproxy-`
- `SENSITIVE_PATTERNS` — regex patterns for header redaction
- `CLAUDE_CODE_SYSTEM_PREFIX` — required system prompt prefix for OAuth
- `OAuthConfigError` — fatal exception that propagates through pipeline (not swallowed)

## Implementation Notes

- **TLS keylog**: `MITMPROXY_SSLKEYLOGFILE` must be set before any mitmproxy import (evaluated at module import time in `mitmproxy.net.tls`). Set in `_run_inspect()` before `run_inspector()`. Auto-exported to `{config_dir}/tls.keylog`.
- **WireGuard keylog**: Auto-exported to `{config_dir}/wg.keylog` after inspector startup for Wireshark tunnel decryption.
- **SSL certificate handling**: `_ensure_combined_ca_bundle()` in cli.py combines mitmproxy CA with system CAs for `ccproxy run --inspect`. Sets `SSL_CERT_FILE`, `NODE_EXTRA_CA_CERTS`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE` in the subprocess environment. Falls back to `/etc/ssl/certs/ca-certificates.crt`.
- **Logging**: `setup_logging()` in cli.py. Two modes: journal-only under systemd (`INVOCATION_ID` detected), stderr + file (`{config_dir}/ccproxy.log`, truncated on restart) otherwise. Subprocess output routed through `ccproxy.subprocess.{slirp4netns,nsenter}` loggers. mitmproxy TermLog disabled (`with_termlog=False`); mitmproxy loggers route through ccproxy's handlers.
- **Hook error isolation**: Errors in one hook don't block others. `OAuthConfigError` is the exception — it propagates through the pipeline (fatal).
- **Body metadata footgun**: `ctx.metadata` uses `setdefault` — reading it creates an empty `metadata` key in the body. `commit()` strips empty metadata dicts to prevent upstream API rejections (Google: "Unknown name metadata"). Hooks that need flow-level state should use `ctx.flow.metadata["ccproxy.key"]`, NOT `ctx.metadata["key"]` which writes into the request body.
- **SSE streaming**: `flow.response.stream` must be set in `responseheaders` (before body arrives). xepor does not implement `responseheaders` — it lives on `InspectorAddon`. Setting `stream` in `response` is too late, mitmproxy has already buffered.
- **Provider model**: Providers are generic — URL + auth method + API format. LiteLLM's `ProviderConfigManager` resolves actual hosts/paths. The lightllm dispatch module has a small set of provider name strings as dispatch keys (`_GEMINI_PROVIDERS`, `_PATH_SUFFIXES`) but URL targets themselves are resolved by LiteLLM.
- **Docker services** (`docker-compose.yaml`): `ccproxy-jaeger` (Jaeger, ports 4317/4318/16686) for OTel trace collection.
- **Namespace lifecycle**: `--ready-fd`/`--exit-fd` pipes for clean slirp4netns lifecycle. `PortForwarder` background thread polls `/proc/{pid}/net/tcp` every 0.5s for dynamic `add_hostfwd` port forwarding.
- **Namespace localhost routing**: Inside the WireGuard namespace, `127.0.0.1` is isolated loopback — host services are at `10.0.2.2` (slirp4netns gateway). `route_localnet` sysctl + iptables OUTPUT DNAT rules transparently redirect namespace localhost→gateway so tools with hardcoded `127.0.0.1` base URLs work. A port remap rule maps the default ccproxy port (4000) to the running instance's port when they differ.
- **Prompt caching**: Anthropic `cache_control` annotations pass through transparently via `AnthropicConfig.transform_request()`. For Gemini/Vertex AI, `cache_control` triggers the `cachedContents` API flow in `context_cache.py` (only in `transform` mode — `redirect` and `passthrough` modes don't invoke lightllm transforms). Gemini OAuth tokens (`ya29.*`) use `Authorization: Bearer`; API keys use `?key=` in the URL. The Gemini CLI's OAuth scopes do NOT cover the `cachedContents` endpoint — only API keys (`AIza*`) work for Gemini context caching through Google AI Studio.
- **Gemini through inspector**: Gemini CLI uses `cloudcode-pa.googleapis.com/v1internal:*` endpoints. These match the `passthrough` transform rule (`match_host: cloudcode-pa.googleapis.com`). PAL MCP server uses the google-genai Python SDK which connects to `generativelanguage.googleapis.com`, but its MCP config sets `GEMINI_BASE_URL=http://127.0.0.1:4000/gemini` with sentinel key `sk-ant-oat-ccproxy-gemini`. In inspect mode, the DNAT rules redirect this through the running ccproxy instance where `forward_oauth` resolves the sentinel to a real OAuth token. The Gemini `redirect` transform rules (`match_path: /v1internal`, `/gemini/`) rewrite paths to cloudcode-pa endpoints via `_rewrite_path()`.

## Testing Patterns

- `pytest-asyncio` with `asyncio_mode = "auto"`
- Coverage threshold: 90% (`--cov-fail-under=90`)
- Mock flows use `MagicMock()` with real `ProxyMode.parse()` for mode objects
- `conftest.py` has single `cleanup` autouse fixture resetting singletons
- Each test file defines its own flow factory helpers
- e2e tests excluded by default (`-m "not e2e"`)

## Dev Instance

The Nix devShell configures a local dev instance via `mkConfig` at port 4001 (production default: 4000). Inspector UI at 8084. Entering the devShell auto-symlinks Nix-generated config files to `.ccproxy/` and sets `CCPROXY_CONFIG_DIR=$PWD/.ccproxy`. Port is configured exclusively via the YAML config generated by `devConfig`. Inspector cert store at `./.ccproxy` (project-local, not `~/.mitmproxy`).

Production instance runs at port 4000 via systemd. Both instances can run simultaneously — dev on 4001, production on 4000.

The `flake.nix` exports `lib.mkConfig` for other projects to generate ccproxy config with custom port/settings overrides, `defaultSettings` (system-agnostic, top-level) for consumers to merge with, and `homeModules.ccproxy` (Home Manager module with `programs.ccproxy` options and systemd user service).

## Type Stubs (`stubs/`)

Hand-written stubs for dependencies lacking `py.typed` or with incomplete types: `litellm`, `opentelemetry` (optional, package not installed in dev), `xepor`. On `mypy_path = "stubs"`.

## Dependencies

- **litellm** — Provider transformation pipeline (lightllm imports `BaseConfig`, `ProviderConfigManager` directly)
- **mitmproxy** — HTTP/HTTPS traffic interception
- **xepor** — Flask-style route decorators for mitmproxy (vendored subclass in `inspector/router.py`)
- **parse** — URL path template matching (NOT regex — `{param}` not `{param:.*}`)
- **pydantic/pydantic-settings** — Configuration and validation
- **pydantic-ai-slim** — Typed message/tool objects (`ModelMessage`, `SystemPromptPart`, `ToolDefinition`, `CachePoint`) for the pipeline's typed content layer
- **tyro** + **attrs** — CLI subcommand generation
- **anthropic** — Anthropic API client (OAuth token refresh)
- **fastapi** — MCP notification endpoint (`POST /mcp/notify`)

## Marketplace Plugin Sync

Plugin files (`.claude-plugin/`, `skills/`, `hooks/`, `CLAUDE.md`) are synced to `starbaser/eigenmage-marketplace`. Pushes to `starbased/dev` trigger `.github/workflows/notify-marketplace.yml`, which dispatches a `plugin-updated` event to the marketplace repo. The marketplace CI pulls the latest submodule and copies plugin-relevant files into `plugins/ccproxy/`.
