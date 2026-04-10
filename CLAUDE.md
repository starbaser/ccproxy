# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

@~/.claude/standards-python-extended.md

## Project Overview

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
ccproxy logs [-f] [-n LINES]      # View logs
ccproxy dag-viz [-o ascii|mermaid|json]  # Visualize hook DAG
```

## Architecture

### Request Flow

```
ccproxy start
  -> mitmweb (reverse + WireGuard listeners)
  -> InspectorAddon -> inbound DAG -> transform (lightllm) -> outbound DAG
  -> provider API directly
```

No LiteLLM subprocess. No gateway namespace. No second WireGuard tunnel.

### Addon Chain (fixed order, registered in `inspector/process.py`)

```
ReadySignal -> InspectorAddon -> ccproxy_inbound -> ccproxy_transform -> ccproxy_outbound
               (OTel + FlowRecord)  (DAG hooks)     (lightllm dispatch)   (DAG hooks)
```

mitmweb binds two listeners: `reverse:http://localhost:1@{port}` (placeholder backend, overwritten by transform) and `wireguard:{conf}@{udp_port}`.

### Key Subsystems

**`lightllm/`** — Surgical nerve connector into LiteLLM's `BaseConfig` transformation pipeline. Two code paths in `dispatch.py`:
- Standard providers (Anthropic, OpenAI, ~90 others): `validate_environment -> get_complete_url -> transform_request -> sign_request`
- Gemini/Vertex AI: `_get_gemini_url` + `_transform_request_body` directly (VertexGeminiConfig.transform_request() raises NotImplementedError)
- `registry.py` wraps `ProviderConfigManager` — all LiteLLM providers for free
- `NoopLogging` duck-types LiteLLM's `Logging` class to bypass cost/callback machinery

**`pipeline/`** — DAG-based hook execution engine:
- `Context` wraps `HTTPFlow`. Header mutations are immediate; body mutations deferred until `commit()`.
- `@hook(reads=..., writes=...)` decorator declares data dependencies. `HookDAG` topologically sorts via Kahn's algorithm.
- `PipelineExecutor.execute(flow)` runs hooks in DAG order, calls `ctx.commit()` at the end.
- `x-ccproxy-hooks: +hook,-hook` header for per-request force-run/force-skip.

**`inspector/`** — mitmproxy addon layer:
- `addon.py` — `InspectorAddon`: OTel span lifecycle, FlowRecord creation, direction detection. All flows are `"inbound"`.
- `process.py` — In-process mitmweb via WebMaster API. Two listeners (reverse + WireGuard). Options applied via `update_defer()`.
- `pipeline.py` — `build_executor()` bridges hook registry with mitmproxy addons. `register_pipeline_routes()` wires DAG executors as xepor route handlers.
- `router.py` — Vendored xepor `InterceptedAPI` subclass with mitmproxy 12.x fixes (keyword `Server(address=...)`, `name` dedup, `host=None` wildcard).
- `routes/transform.py` — Two modes: `transform` (rewrite via lightllm dispatch, redirect to provider) and `passthrough` (forward unchanged). Unmatched reverse proxy flows get 501; unmatched WireGuard flows pass through.
- `namespace.py` — Rootless user+net namespace via `unshare` + `slirp4netns` + WireGuard. `PortForwarder` polls `/proc/{pid}/net/tcp` for dynamic port forwarding. Requires `slirp4netns`, `wg`, `unshare`, `nsenter`, `ip`.
- `flow_store.py` — TTL store keyed by `x-ccproxy-flow-id` header for cross-addon state.
- `telemetry.py` — Three-mode OTel: real OTLP export, no-op, or stub.
- `wg_keylog.py` — Writes Wireshark-compatible keylog for WireGuard tunnel decryption.

**`hooks/`** — Built-in pipeline hooks:

| Hook | Stage | Purpose |
|------|-------|---------|
| `forward_oauth` | inbound | Sentinel key (`sk-ant-oat-ccproxy-{provider}`) substitution from `oat_sources` |
| `extract_session_id` | inbound | Parses `metadata.user_id`, transparent metadata pass-through |
| `add_beta_headers` | outbound | Merges `ANTHROPIC_BETA_HEADERS` into `anthropic-beta` header |
| `inject_claude_code_identity` | outbound | Prepends system prompt prefix for OAuth requests to Anthropic |
| `inject_mcp_notifications` | outbound | Injects buffered MCP terminal events as synthetic tool_use/tool_result |
| `verbose_mode` | outbound | Strips `redact-thinking-*` from `anthropic-beta` header |

**`mcp/`** — Thread-safe notification buffer (`NotificationBuffer` singleton) + `POST /mcp/notify` FastAPI endpoint for MCP terminal event ingestion.

### Configuration

**Config discovery** (highest to lowest precedence):
1. `$CCPROXY_CONFIG_DIR/ccproxy.yaml`
2. LiteLLM proxy runtime directory (auto-detected)
3. `~/.ccproxy/ccproxy.yaml`

**Two config files**: `ccproxy.yaml` (hooks, OAuth sources, inspector, transforms) and `config.yaml` (LiteLLM model definitions — currently only used for lightllm provider imports).

**Hook config format** — two-stage dict:
```yaml
hooks:
  inbound:
    - ccproxy.hooks.forward_oauth
    - ccproxy.hooks.extract_session_id
  outbound:
    - ccproxy.hooks.add_beta_headers
    - hook: ccproxy.hooks.some_hook
      params:
        key: value
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

Matching fields: `match_host` (optional, checked against pretty_host + Host header), `match_path` (prefix), `match_model` (substring in request body).

### Singleton Patterns

`CCProxyConfig`, `NotificationBuffer`, and `FlowStore` use thread-safe singletons. Tests reset them via the `cleanup` autouse fixture (`clear_config_instance()`, `clear_buffer()`, `clear_flow_store()`).

### OAuth

- **Sentinel key**: `sk-ant-oat-ccproxy-{provider}` triggers token substitution from `oat_sources` config
- **Token sources**: `oat_sources` entries with `command` (shell) or `file` (path) to obtain tokens
- **Refresh**: TTL-based (background check every 30 min, refresh at 90% of `oauth_ttl` default 8h) + 401-triggered immediate refresh
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
- **Provider model**: Providers are generic — URL + auth method + API format. LiteLLM's `ProviderConfigManager` resolves actual hosts/paths. The lightllm dispatch module has a small set of provider name strings as dispatch keys (`_GEMINI_PROVIDERS`, `_PATH_SUFFIXES`) but URL targets themselves are resolved by LiteLLM.
- **Docker services** (`docker-compose.yaml`): `litellm-db` (postgres, port 5434) and `ccproxy-jaeger` (Jaeger, ports 4317/4318/16686) for OTel trace collection.
- **Namespace lifecycle**: `--ready-fd`/`--exit-fd` pipes for clean slirp4netns lifecycle. `PortForwarder` background thread polls `/proc/{pid}/net/tcp` every 0.5s for dynamic `add_hostfwd` port forwarding.

## Testing Patterns

- `pytest-asyncio` with `asyncio_mode = "auto"`
- Coverage threshold: 90% (`--cov-fail-under=90`)
- Mock flows use `MagicMock()` with real `ProxyMode.parse()` for mode objects
- `conftest.py` has single `cleanup` autouse fixture resetting singletons
- Each test file defines its own flow factory helpers
- e2e tests excluded by default (`-m "not e2e"`)

## Dev Instance

The Nix devShell configures a local dev instance via `mkConfig` at port 4001 (production default: 4000). Inspector UI at 8083. Entering the devShell auto-symlinks Nix-generated config files to `.ccproxy/` and sets `CCPROXY_CONFIG_DIR=$PWD/.ccproxy`, `CCPROXY_PORT=4001`. Inspector cert store at `./.ccproxy` (project-local, not `~/.mitmproxy`).

The `flake.nix` exports `lib.mkConfig` for other projects to generate ccproxy config with custom port/settings overrides, and `homeModules.ccproxy` (Home Manager module with `programs.ccproxy` options and systemd user service).

## Type Stubs (`stubs/`)

Hand-written stubs for dependencies lacking `py.typed` or with incomplete types: `mitmproxy` (full hierarchy including ProxyMode subclasses), `opentelemetry` (optional, package not installed in dev), `langfuse`, `litellm`, `psutil`, `xepor`. On `mypy_path = "stubs"`.

## Dependencies

- **litellm[proxy]** — Provider transformation pipeline (lightllm imports `BaseConfig`, `ProviderConfigManager` directly)
- **mitmproxy** — HTTP/HTTPS traffic interception
- **xepor** — Flask-style route decorators for mitmproxy (vendored subclass in `inspector/router.py`)
- **parse** — URL path template matching (NOT regex — `{param}` not `{param:.*}`)
- **pydantic/pydantic-settings** — Configuration and validation
- **tyro** + **attrs** — CLI subcommand generation
- **tiktoken** — Token counting
- **anthropic** — Anthropic API client (OAuth token refresh)

## Marketplace Plugin Sync

Plugin files (`.claude-plugin/`, `skills/`, `hooks/`, `CLAUDE.md`) are synced to `starbaser/eigenmage-marketplace`. Pushes to `starbased/dev` trigger `.github/workflows/notify-marketplace.yml`, which dispatches a `plugin-updated` event to the marketplace repo. The marketplace CI pulls the latest submodule and copies plugin-relevant files into `plugins/ccproxy/`.
