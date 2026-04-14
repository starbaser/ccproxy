# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

@~/.claude/standards-python-extended.md

## Project Overview

**IMPERATIVE**: Auth failures through ccproxy are OUR bug until proven otherwise. ccproxy is the intermediary — every header, token, and credential passes through our code. When a request fails with 401/403, triage ccproxy first: check what we're injecting, stripping, or mangling before blaming the upstream provider or expired tokens. For Gemini specifically: if all Gemini requests fail with 401, run `gemini -m gemini-2.5-flash -p "hi"` directly (no ccproxy) to force an OAuth token refresh, then retry through ccproxy.

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

### CLI

```bash
ccproxy start                     # Start server (always inspector mode, foreground)
ccproxy run <command> [args...]   # Run command with proxy env vars
ccproxy run --inspect -- <cmd>    # Run command in WireGuard namespace jail
ccproxy status [--json]           # Show running state
ccproxy install [--force]         # Install template config files
ccproxy logs [-f] [-n LINES]     # View logs
ccproxy dag-viz [-o ascii|mermaid|json]  # Visualize hook DAG
ccproxy flows list [--filter PAT] [--json]  # List captured flows
ccproxy flows dump <id-prefix>    # 1-page / 2-entry HAR ([fwdreq,fwdres] + [clireq,fwdres])
ccproxy flows diff <id1> <id2>    # Unified diff of two request bodies
ccproxy flows clear               # Clear all captured flows
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
     ├─ SSE (text/event-stream) + cross-provider transform → flow.response.stream = SseTransformer(...)
     ├─ SSE + no transform → flow.response.stream = True  (passthrough)
     └─ not SSE → (buffered by mitmproxy)
  -> response phase
     ├─ streamed → already handled chunk-by-chunk above
     └─ buffered + transform → transform_to_openai() on full body (RESPONSE route)
  -> InspectorAddon.response() → OTel span finish
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
- **Response streaming** (`SseTransformer`): Stateful `flow.response.stream` callable. Parses SSE events, transforms each via LiteLLM's per-provider `ModelResponseIterator.chunk_parser()`, re-serializes as OpenAI-format SSE. Provider dispatch in `_make_response_iterator()`: Anthropic → `handler.py:ModelResponseIterator`, Gemini → `vertex_and_google_ai_studio_gemini.py:ModelResponseIterator`, others → `config.get_model_response_iterator()`.
- **Context caching** (`context_cache.py`): Gemini/Vertex AI provider-side KV caching via Google's `cachedContents` API. `resolve_cached_content()` detects `cache_control: {type: "ephemeral"}` annotations on messages (Anthropic format), separates cached messages, creates or finds existing cached content resources via paginated GET + POST to Google's API, and returns the resource name + filtered messages. The `cachedContent` name is passed through `_transform_request_body()` into the `generateContent` request body. Surgically imports LiteLLM's pure transformation functions (`separate_cached_messages`, `transform_openai_messages_to_gemini_context_caching`, `is_cached_message`). Owns the HTTP layer (plain `httpx.Client`). Cache key is SHA-256 of messages+tools+model, stored as `displayName` for deduplication. Minimum 1024 cached tokens required. Best-effort: any API failure falls through gracefully.
- `registry.py` wraps `ProviderConfigManager` — all LiteLLM providers for free
- `NoopLogging` duck-types LiteLLM's `Logging` class to bypass cost/callback machinery (includes `optional_params` for Gemini iterator)

**`pipeline/`** — DAG-based hook execution engine:
- `Context` wraps `HTTPFlow`. Header mutations are immediate; body mutations deferred until `commit()`. `commit()` strips empty `metadata` dicts injected by property access (upstream APIs reject unknown fields).
- `@hook(reads=..., writes=...)` decorator declares data dependencies. `HookDAG` topologically sorts via Kahn's algorithm.
- `PipelineExecutor.execute(flow)` runs hooks in DAG order, calls `ctx.commit()` at the end.
- `x-ccproxy-hooks: +hook,-hook` header for per-request force-run/force-skip.

**`inspector/`** — mitmproxy addon layer:
- `addon.py` — `InspectorAddon`: OTel span lifecycle, FlowRecord creation, direction detection, client request snapshot. All flows are `"inbound"`. Snapshots the full pre-pipeline request (`ClientRequest`) before any hooks mutate the flow. `responseheaders()` hook enables SSE streaming for all `text/event-stream` responses — sets `flow.response.stream` to `True` (passthrough) or `SseTransformer` (cross-provider transform). Exposes `ccproxy.clientrequest` mitmproxy command for structured JSON access to client requests.
- `process.py` — In-process mitmweb via WebMaster API. Two listeners (reverse + WireGuard). Options applied via `update_defer()`.
- `pipeline.py` — `build_executor()` bridges hook registry with mitmproxy addons. `register_pipeline_routes()` wires DAG executors as xepor route handlers.
- `router.py` — Vendored xepor `InterceptedAPI` subclass with mitmproxy 12.x fixes (keyword `Server(address=...)`, `name` dedup, `host=None` wildcard).
- `routes/transform.py` — REQUEST handler: three modes, `transform` (rewrite body + destination via lightllm dispatch), `redirect` (rewrite destination host, preserve body), and `passthrough` (forward unchanged). For Gemini transform flows, calls `resolve_cached_content()` before `transform_to_provider()` to resolve context caching. Unmatched reverse proxy flows get 501; unmatched WireGuard flows pass through. RESPONSE handler: transforms non-streaming provider responses back to OpenAI format via `transform_to_openai()`. `TransformMeta` persisted on `FlowRecord` during request phase for response handler access.
- `namespace.py` — Rootless user+net namespace via `unshare` + `slirp4netns` + WireGuard. Network topology: namespace TAP IP `10.0.2.100/24`, gateway (host) `10.0.2.2`, DNS `10.0.2.3`. Default route replaced with `wg0` so all internet traffic goes through WireGuard tunnel → mitmproxy. `route_localnet` sysctl enabled for iptables OUTPUT DNAT on loopback. Three DNAT rules: PREROUTING inbound (tap0→localhost), OUTPUT outbound (localhost→gateway), OUTPUT port remap (default port→running port). `PortForwarder` polls `/proc/{pid}/net/tcp` for dynamic `add_hostfwd` port forwarding. Requires `slirp4netns`, `wg`, `unshare`, `nsenter`, `ip`, `iptables`, `sysctl`.
- `contentview.py` — Custom mitmproxy content view "Client-Request" showing the pre-pipeline request (method, URL, headers, body). Registered via `contentviews.add()`. Accessible at `GET /flows/{id}/request/content/client-request`.
- `flow_store.py` — TTL store keyed by `x-ccproxy-flow-id` header for cross-addon state. `ClientRequest` dataclass snapshots the full client request (method, scheme, host, port, path, headers, body) before pipeline mutation. `TransformMeta` carries provider/model/request_data/is_streaming from request phase to response phase.
- `telemetry.py` — Three-mode OTel: real OTLP export, no-op, or stub.
- `wg_keylog.py` — Writes Wireshark-compatible keylog for WireGuard tunnel decryption.

**`hooks/`** — Built-in pipeline hooks:

| Hook | Stage | Purpose |
|------|-------|---------|
| `forward_oauth` | inbound | Sentinel key (`sk-ant-oat-ccproxy-{provider}`) substitution from `oat_sources` |
| `extract_session_id` | inbound | Parses `metadata.user_id` → stores session_id on `flow.metadata` (NOT body metadata) |
| `inject_mcp_notifications` | outbound | Injects buffered MCP terminal events as synthetic tool_use/tool_result |
| `verbose_mode` | outbound | Strips `redact-thinking-*` from `anthropic-beta` header |
| `apply_compliance` | outbound | Applies learned compliance profile (headers, body envelope, system prompt) to reverse proxy flows |

**`compliance/`** — Provider-agnostic compliance profile learning system:
- `models.py` — `ComplianceProfile`, `ObservationAccumulator`, feature dataclasses
- `classifier.py` — Feature classification (content vs envelope vs auth vs dynamic)
- `extractor.py` — Feature extraction from `ClientRequest` snapshots
- `store.py` — `ProfileStore` singleton with JSON persistence at `{config_dir}/compliance_profiles.json`
- `merger.py` — `ComplianceMerger` class with 5 idempotent merge operations as public methods: `merge_headers`, `merge_session_metadata`, `wrap_body`, `merge_body_fields`, `merge_system`. `merge()` calls all 5 in order. Subclass to override, skip, reorder, or extend individual operations. `resolve_merger_class()` resolves a dotted import path to a `ComplianceMerger` subclass. Config: `compliance.merger_class` (default `"ccproxy.compliance.merger.ComplianceMerger"`).
- Observation is built into `InspectorAddon.request()` pre-pipeline, triggered by WireGuard flows or configured UA patterns. Profiles keyed by `(provider, user_agent)` with stability detection across N observations.

**`mcp/`** — Thread-safe notification buffer (`NotificationBuffer` singleton) + `POST /mcp/notify` FastAPI endpoint for MCP terminal event ingestion.

**`tools/flows.py`** — `MitmwebClient` for programmatic mitmweb REST API access + `ccproxy flows` CLI tyro subcommands (`FlowsList`, `FlowsDump`, `FlowsDiff`, `FlowsClear`).
- **Auth**: Bearer token resolved from `inspector.mitmproxy.web_password` config (mitmproxy 12+ accepts `Authorization: Bearer` on the REST API directly).
- **Client methods**: `list_flows()`, `get_request_body(id)`, `resolve_id(prefix)`, `dump_har(id)` (invokes the `ccproxy.dump` mitmproxy command via `POST /commands/ccproxy.dump`), `clear()`. `_make_client()` reads auth from ccproxy config.
- **HAR output**: `ccproxy flows dump` emits HAR 1.2 JSON built server-side by `MultiHARSaver.ccproxy_dump` (see `inspector/multi_har_saver.py`). One page per flow (`pages[0].id == flow.id`), two complete HAR entries by documented index: `entries[0] = [fwdreq, fwdres]` is the real flow untouched (authoritative forwarded request + upstream response); `entries[1] = [clireq, fwdres]` is a `flow.copy()` with `.request` rebuilt from `flow.metadata[InspectorMeta.RECORD].client_request` via `http.Request.make()` — the response is duplicated so the HAR pair stays schema-complete. All HAR details (cookies, multipart bodies, binary base64, websocket messages, timings) are delegated to `mitmproxy.addons.savehar.SaveHar.make_har()`.
- **HAR consumption**: pipe to a file and open in Chrome DevTools / Charles / Fiddler (`ccproxy flows dump abc > flow.har`), or query with jq by entry index (`... | jq '.log.entries[0].request.url'` for forwarded URL, `... | jq '.log.entries[1].request.url'` for pre-pipeline URL, `... | jq '.log.entries[0].response.status'` for upstream status, `... | jq '.log.pages[0].id'` for the flow id).
- **HAR vs diff**: for quick payload comparison between two flows use `ccproxy flows diff <a> <b>` (unified diff of raw request bodies). For structural HAR comparison, save two HAR files and diff them with `jq` or a HAR viewer.

### Configuration

**Config discovery** (highest to lowest precedence):
1. `$CCPROXY_CONFIG_DIR/ccproxy.yaml`
2. `~/.ccproxy/ccproxy.yaml`

**Hook config format** — two-stage dict:
```yaml
hooks:
  inbound:
    - ccproxy.hooks.forward_oauth
    - ccproxy.hooks.extract_session_id
  outbound:
    - ccproxy.hooks.inject_mcp_notifications
    - ccproxy.hooks.verbose_mode
    - ccproxy.hooks.apply_compliance
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

**Compliance merger config** — `compliance.merger_class` dotted path to a `ComplianceMerger` subclass:
```yaml
compliance:
  merger_class: mypackage.custom_merger.MyMerger
```
Default: `ccproxy.compliance.merger.ComplianceMerger`. Subclass overrides individual methods (`merge_headers`, `merge_session_metadata`, `wrap_body`, `merge_body_fields`, `merge_system`) or `merge()` itself to reorder/skip operations.

### Singleton Patterns

`CCProxyConfig`, `NotificationBuffer`, `FlowStore`, and `ProfileStore` use thread-safe singletons. Tests reset them via the `cleanup` autouse fixture (`clear_config_instance()`, `clear_buffer()`, `clear_flow_store()`, `clear_store_instance()`).

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
- **tyro** + **attrs** — CLI subcommand generation
- **anthropic** — Anthropic API client (OAuth token refresh)
- **fastapi** — MCP notification endpoint (`POST /mcp/notify`)

## Marketplace Plugin Sync

Plugin files (`.claude-plugin/`, `skills/`, `hooks/`, `CLAUDE.md`) are synced to `starbaser/eigenmage-marketplace`. Pushes to `starbased/dev` trigger `.github/workflows/notify-marketplace.yml`, which dispatches a `plugin-updated` event to the marketplace repo. The marketplace CI pulls the latest submodule and copies plugin-relevant files into `plugins/ccproxy/`.
