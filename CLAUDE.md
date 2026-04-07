# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

@~/.claude/standards-python-extended.md

## Project Overview

**CRITICAL**: The project name is `ccproxy` (lowercase). Do NOT refer to the project as "CCProxy". The PascalCase form is used exclusively for class names (e.g., `CCProxyHandler`, `CCProxyConfig`).

`ccproxy` is a command-line tool that intercepts and routes Claude Code's requests to different LLM providers via a LiteLLM proxy server. It enables intelligent request routing based on token count, model type, tool usage, or custom rules. It also functions as a development platform for new and unexplored features or unofficial mods of Claude Code.

## Development Commands

Development uses `just` for task recipes and `process-compose` for process management.

### Just Recipes

```bash
just up          # Start dev services (process-compose, detached)
just down        # Stop dev services
just test        # Run tests (uv run pytest)
just lint        # Lint (uv run ruff check .)
just fmt         # Format (uv run ruff format .)
just typecheck   # Type check (uv run mypy src/ccproxy)
```

### Process Compose

`process-compose.yml` manages the dev ccproxy instance. Socket at `/tmp/process-compose-ccproxy.sock`.

```bash
just up                    # Start all processes
just down                  # Stop all processes
process-compose attach     # Attach to TUI
```

### Running Tests

```bash
just test                          # Run all tests
uv run pytest tests/test_config.py # Run specific test file
uv run pytest -k "test_token_count" # Run tests matching pattern
```

### CLI Commands

```bash
# Install configuration files
ccproxy install [--force]

# Start proxy server (foreground, use process-compose/systemd for supervision)
ccproxy start [--inspect/-i]

# View logs and status
ccproxy logs [-f] [-n LINES]
ccproxy status [--json]

# Run command with proxy environment
ccproxy run <command> [args...]

# Run command in WireGuard namespace jail (all traffic captured transparently)
ccproxy run --inspect -- <command> [args...]

```

**Inspect Mode**: `--inspect` enables the full inspector stack (mitmweb with WireGuard mode). `ccproxy run --inspect` confines the subprocess in a rootless network namespace routed through the WireGuard tunnel for transparent traffic capture. See `docs/inspect.md` for architecture details.

## Architecture

The codebase follows a modular architecture with clear separation of concerns:

### Request Flow (Inspect Mode)

```
┌─ cli namespace ──────────┐
│  CLI client               │
│    ↓ WG tunnel (port A)   │
└────┼──────────────────────┘
     ↓
  mitmweb (wireguard A)  ← INBOUND: OAuth injection, rewrites to LiteLLM
     ↓
┌─ litellm namespace ──────┐  ← slirp4netns port fwd for external HTTP clients
│  LiteLLM                  │
│    ↓ WG tunnel (port B)   │
└────┼──────────────────────┘
     ↓
  mitmweb (wireguard B)  ← OUTBOUND: beta header merge, forwards to provider
     ↓
  provider API

HTTP client → mitmweb (reverse :main_port) → LiteLLM  ← INBOUND (same OAuth path)
```

### Request Flow (Non-Inspect Mode)

```
Request → CCProxyHandler → Hook Pipeline → Response
                ↓
         RequestClassifier (rule evaluation)
                ↓
           ModelRouter (model lookup)
```

1. **CCProxyHandler** (`handler.py`) - LiteLLM CustomLogger that intercepts all requests
2. **RequestClassifier** (`classifier.py`) - Evaluates rules in order (first match wins)
3. **ModelRouter** (`router.py`) - Maps rule names to actual model configurations
4. **Hook Pipeline** - Sequential execution of configured hooks with error isolation

### Key Components

- **handler.py**: Main entry point as a LiteLLM CustomLogger. Orchestrates the classification and routing process via `async_pre_call_hook()`. Also patches LiteLLM's health check to inject OAuth credentials via `_inject_health_check_auth()` (module-level function).
- **classifier.py**: Rule-based classification system that evaluates rules in order to determine routing.
- **rules.py**: Defines `ClassificationRule` abstract base class and built-in rules:
  - `ThinkingRule` - Matches requests with "thinking" field
  - `MatchModelRule` - Matches by model name substring
  - `MatchToolRule` - Matches by tool name in request
  - `TokenCountRule` - Evaluates based on token count threshold
- **router.py**: Manages model configurations from LiteLLM proxy server. Lazy-loads models on first request.
- **config.py**: Configuration management using Pydantic with multi-level discovery (env var → LiteLLM runtime → ~/.ccproxy/). Contains all config models including `MitmproxyOptions` (typed facade over mitmproxy's OptManager).
- **hooks/**: Built-in pipeline hooks using `@hook` decorator with DAG-based ordering. Hooks support optional params via `hook:` + `params:` YAML format in `ccproxy.yaml`:
  - `rule_evaluator` - Evaluates rules and stores routing decision (skips classification for health checks)
  - `model_router` - Routes to appropriate model (forces passthrough for health checks)
  - `forward_oauth` - Forwards OAuth tokens to provider APIs; supports sentinel key substitution
  - `extract_session_id` - Extracts session identifiers
  - `capture_headers` - Captures HTTP headers with sensitive redaction (supports `headers` param)
  - `forward_apikey` - Forwards x-api-key header
  - `add_beta_headers` - Adds anthropic-beta headers for Claude Code OAuth
  - `verbose_mode` - Strips `redact-thinking-*` beta header to enable full thinking block output
  - `inject_claude_code_identity` - Injects required system message for OAuth
  - `inject_mcp_notifications` - Injects buffered MCP terminal events as synthetic tool_use/tool_result pairs before the final user message
- **inspector/addon.py**: Inspector addon for HTTP traffic capture with OTel span emission. Detects traffic direction per-flow via `ProxyDirection` enum (`REVERSE=0`, `FORWARD=1` (reserved), `WIREGUARD_CLI=2`, `WIREGUARD_GW=3`). Distinguishes CLI vs gateway WireGuard flows by comparing the WG listen port against the configured gateway port. Sets `flow.metadata["ccproxy.direction"]` (`"inbound"` or `"outbound"`) for downstream route handlers. Forwards `WIREGUARD_CLI` LLM API traffic to LiteLLM; explicitly skips `WIREGUARD_GW` to prevent infinite loops.
- **inspector/namespace.py**: Network namespace confinement for `ccproxy run --inspect`. Creates user+net namespace with slirp4netns bridge and WireGuard client routing through mitmweb's WireGuard server. Also provides `create_gateway_namespace()` for confining LiteLLM in its own namespace with `--port-map` for LAN accessibility. Requires `slirp4netns`, `wg`, `unshare`, `nsenter`, `ip` (all rootless on Linux 5.6+ with `unprivileged_userns_clone=1`).
- **inspector/process.py**: Process management for launching and supervising mitmproxy (mitmweb). Launches with two `--mode wireguard:` listeners (CLI port A, gateway port B) — each auto-assigns a free UDP port. Returns a 4-tuple `(proc, web_token, wg_cli_port, wg_gateway_port)`. Passes `CCPROXY_INSPECTOR_WG_CLI_PORT` and `CCPROXY_INSPECTOR_WG_GATEWAY_PORT` env vars to the addon subprocess.
- **inspector/script.py**: Mitmproxy addon script loaded via `-s` flag. Runs in the mitmproxy process. Addon chain: `InspectorScript` (OTel spans, always first) → inbound `InspectorRouter` → outbound `InspectorRouter` → optional `PcapAddon`. Loads `OtelConfig` from `ccproxy.yaml` via `CCPROXY_CONFIG_DIR`.
- **inspector/routing.py**: Vendored xepor 0.6.0 routing framework (Apache-2.0) with mitmproxy 12.x compatibility fix (`Server(address=...)` keyword arg). Provides `InterceptedAPI` with Flask-style `@router.route("/path/{param}")` decorators, `RouteType.REQUEST`/`RESPONSE`, passthrough/whitelist modes, host remapping. `InspectorRouter` subclass adds a `name` attribute to avoid mitmproxy AddonManager name collisions. Uses `parse` library for path template matching (NOT regex — `{path}` not `{path:.*}`).
- **inspector/pcap.py**: PCAP synthesizer for Wireshark integration. Constructs fake-but-valid IPv4+TCP frames from mitmproxy's HTTP-layer flow data using `struct.pack`. Based on `muzuiget/mitmpcap`. `PcapFile` writes to disk, `PcapPipe` streams to a subprocess (e.g., `wireshark -k -i -`). `PcapAddon` is a mitmproxy addon activated via `CCPROXY_PCAP_FILE` or `CCPROXY_PCAP_PIPE` env vars.
- **inspector/wg_keylog.py**: Reads mitmproxy's WireGuard keypair JSON (`wireguard.{pid}.conf`) and writes a Wireshark-compatible `wg.keylog_file` for decrypting the outer WireGuard tunnel layer in packet captures. Auto-called after inspector startup; path logged for Wireshark usage.
- **inspector/routes/**: xepor route handlers for the inspector addon chain:
  - `inbound.py` — Unified OAuth handler on ALL inbound flows (WireGuard CLI + reverse proxy HTTP). Detects sentinel keys (`sk-ant-oat-ccproxy-{provider}`), substitutes tokens from `oat_sources`, supports custom `auth_header` per provider, sets `x-ccproxy-oauth-injected: 1` header to signal LiteLLM-side hook to skip.
  - `outbound.py` — Idempotent `anthropic-beta` header merge (safety net alongside LiteLLM hook), 401/403 auth failure observation logging. Direction detected via `flow.metadata["ccproxy.direction"] == "outbound"`.
- **inspector/telemetry.py**: OpenTelemetry span emission for inspector flows. Three-mode degradation: real OTLP export, no-op tracer, or stub — depending on package availability and config. OTel config lives under top-level `ccproxy.otel`.
- **cli.py**: Tyro-based CLI interface for managing the proxy server. Foreground-only (no `--detach`/`stop`/`restart`). Status detection via TCP health probes.
- **constants.py**: Shared constants — `ANTHROPIC_BETA_HEADERS`, `OAUTH_SENTINEL_PREFIX`, `SENSITIVE_PATTERNS`, and `CLAUDE_CODE_SYSTEM_PREFIX`.
- **metadata_store.py**: Thread-safe TTL store keyed by `litellm_call_id` for bridging request metadata across LiteLLM callback boundaries.
- **mcp/buffer.py**: Thread-safe notification buffer for MCP terminal events (from mcptty). Stores per-task events with configurable TTL and max-event limits.
- **mcp/routes.py**: FastAPI routes for MCP notification ingestion (`POST /mcp/notify`). Accepts events from mcptty and writes them to the buffer.
- **preflight.py**: Pre-flight checks before proxy startup — kills orphaned ccproxy/mitmdump processes, verifies port availability, and enforces single-instance constraint.
- **utils.py**: Template discovery and debug utilities (`dt()`, `dv()`, `d()`, `p()`).
- **patches/**: Configurable monkey-patches for LiteLLM internals, loaded at startup via `load_patches()`. Each module exports `apply(handler)`. Declared in `ccproxy.yaml` under `patches:` (list of module paths). Existing hardcoded patches (`_patch_health_check`, `_patch_anthropic_oauth_headers`) remain on the handler; this system is for new patches.
  - `passthrough` - Patches `PassthroughEndpointRouter.get_credentials` to fall back to ccproxy's `oat_sources` OAuth token cache. Provider-agnostic — any provider with an `oat_sources` entry gains pass-through credential support for LiteLLM's native API pass-through routes (`/gemini/`, `/anthropic/`, etc.).
- **pipeline/**: Hook pipeline subsystem:
  - `context.py` - Typed `Context` dataclass wrapping LiteLLM's request data dict for hook access
  - `dag.py` - DAG-based dependency ordering via Kahn's algorithm; resolves hook execution order from `reads`/`writes` declarations
  - `executor.py` - Executes hooks in DAG order with override support and error isolation
  - `guards.py` - Shared guard predicates (e.g., `is_oauth_request`) used by hooks to conditionally self-skip
  - `hook.py` - `HookSpec` class and `@hook` decorator for declaring hook dependencies and metadata
  - `overrides.py` - Parses `x-ccproxy-hooks` header to force-run (`+hook`) or force-skip (`-hook`) individual hooks per request

### Rule System

Rules are evaluated in the order configured in `ccproxy.yaml`. Each rule:

- Inherits from `ClassificationRule` abstract base class
- Implements `evaluate(request: dict, config: CCProxyConfig) -> bool`
- Returns the first matching rule's name as the routing label

```yaml
# Example rule configuration in ccproxy.yaml
rules:
  - name: thinking_model
    rule: ccproxy.rules.ThinkingRule
  - name: haiku_requests
    rule: ccproxy.rules.MatchModelRule
    params:
      - model_name: "haiku"
  - name: large_context
    rule: ccproxy.rules.TokenCountRule
    params:
      - threshold: 60000
```

Custom rules can be created by implementing the ClassificationRule interface and specifying the Python import path in the configuration.

### Configuration Files

- `~/.ccproxy/config.yaml` - LiteLLM proxy configuration with model definitions
- `~/.ccproxy/ccproxy.yaml` - ccproxy-specific configuration (rules, hooks, patches, debug settings, handler path)
- `~/.ccproxy/ccproxy.py` - Auto-generated handler file (created on `ccproxy start` based on `handler` config)

**Config Discovery Precedence:**

1. `CCPROXY_CONFIG_DIR` environment variable
2. LiteLLM proxy runtime directory (auto-detected)
3. `~/.ccproxy/` (default fallback)

## Testing Patterns

The test suite uses pytest with comprehensive fixtures (24 test files, 499 tests, 90% coverage minimum):

- `mock_proxy_server` fixture for mocking LiteLLM proxy
- `cleanup` fixture (autouse) ensures singleton instances are cleared between tests (`clear_config_instance()`, `clear_router()`, `clear_buffer()`)
- Tests organized to mirror source structure (`test_<module>.py`)
- Parametrized tests for rule evaluation scenarios
- Integration tests verify end-to-end behavior
- Mock flows use real `ProxyMode.parse()` for mode objects (e.g., `ProxyMode.parse("wireguard@51820")`)
- `pytest-asyncio` for async tests (`asyncio_mode = "auto"`)
- `monkeypatch.setenv()` for env-var-dependent tests
- `tmp_path` fixture for file I/O tests (PCAP, WireGuard keylog)

**Inspector-specific test files:**
- `test_inspector_addon.py` — Direction detection (WIREGUARD_CLI vs WIREGUARD_GW), forwarding, metadata tagging
- `test_routing.py` — xepor route dispatch, passthrough, host matching, error handling, path params
- `test_pcap.py` — Frame construction, sequence tracking, file/pipe output, addr normalization
- `test_wg_keylog.py` — JSON parsing, keylog format, error cases
- `test_inbound_routes.py` — OAuth sentinel detection, token substitution, direction tagging
- `test_outbound_routes.py` — Beta header merge, dedup, auth failure observation

## Type Stubs (`stubs/`)

Several dependencies lack `py.typed` markers or have incomplete type information. Hand-written stubs in `stubs/` (on `mypy_path`) provide strict-mode coverage:

- **`mitmproxy/`** — Full stub hierarchy: `flow.Error`/`Flow`, `http.HTTPFlow`/`Request`/`Response`/`Headers` (including `Response.make()`, `HTTPFlow.server_conn`), `connection.Client` (including `ip_address`)/`Server`, `proxy/mode_specs.ProxyMode` + all concrete subclasses (`RegularMode`, `ReverseMode`, `WireGuardMode`, etc.), `addonmanager.Loader`.
- **`opentelemetry/`** — Optional OTel API/SDK stubs (package not installed in dev env): `trace`, `sdk.resources`, `sdk.trace`, `sdk.trace.export`, `exporter.otlp.proto.grpc.trace_exporter`.
- **`langfuse/__init__.pyi`** — `Langfuse` class stub (installed but re-export chain not mypy-resolvable).
- **`litellm/__init__.pyi`** — `AuthenticationError`, `_LiteLLMUtils`/`utils`, `acompletion`.
- **`psutil/`**, **`rich/`**, **`httpx/`**, **`tyro/`**, **`tiktoken.pyi`**, **`pydantic_settings.pyi`** — supplemental stubs for strict-mode gaps.

Two `setattr` calls in `handler.py` carry `# noqa: B010` to satisfy mypy (`method-assign` / `attr-defined`) while suppressing ruff B010 — direct assignment would break strict type checking.

## Important Implementation Notes

- **Singleton patterns**: `CCProxyConfig` and `ModelRouter` use thread-safe singletons. Use `clear_config_instance()` and `clear_router()` to reset state in tests.
- **Token counting**: Uses tiktoken with fallback to character-based estimation for non-OpenAI models.
- **OAuth token forwarding**: Handled specially for Claude CLI requests. Supports custom User-Agent per provider.
- **OAuth sentinel key**: SDK clients can use `sk-ant-oat-ccproxy-{provider}` as API key to trigger OAuth token substitution from `oat_sources` config. OAuth works without the inspector via pipeline hooks; the inspector provides a redundant header safety net.
- **Pass-through OAuth**: LiteLLM's native API pass-through routes (`/gemini/`, `/anthropic/`, etc.) bypass the hook pipeline entirely. The `passthrough` patch bridges `oat_sources` tokens into `PassthroughEndpointRouter.get_credentials()` as a fallback after env var lookup. Provider-agnostic.
- **OAuth token refresh**: Automatic refresh with two triggers:
  - TTL-based: Background task checks every 30 minutes, refreshes at 90% of `oauth_ttl` (default 8h)
  - 401-triggered: Immediate refresh when API returns authentication error
  - Config: `oauth_ttl` (seconds), `oauth_refresh_buffer` (ratio, default 0.1)
- **Request metadata**: Stored by `litellm_call_id` with 60-second TTL auto-cleanup (LiteLLM doesn't preserve custom metadata).
- **Health checks**: LiteLLM's `/health` endpoint performs real API calls to each provider. `_inject_health_check_auth()` patches `_update_litellm_params_for_health_check` to inject OAuth credentials (api_key, extra_headers) before `acompletion()` — required because LiteLLM validates API keys before `async_pre_call_hook` runs. The pipeline then runs with forced passthrough (rule_evaluator skips classification, model_router forces passthrough via `ccproxy_is_health_check` metadata flag) so hooks like `forward_oauth`, `add_beta_headers`, and `inject_claude_code_identity` enhance the request. Health probes use `max_tokens=1` to minimize cost.
- **Hook error isolation**: Errors in one hook don't block others from executing.
- **Lazy model loading**: Models loaded from LiteLLM proxy on first request, not at startup.
- **Inspector**: Dual-WireGuard transparent proxy architecture activated by `--inspect`. mitmweb binds two auto-assigned UDP ports for WireGuard servers — one for CLI clients (WIREGUARD_CLI), one for LiteLLM gateway (WIREGUARD_GW). Without `--inspect`, the inspector is not started. The mitmproxy-layer route handlers handle OAuth (inbound) and beta headers (outbound). The LiteLLM-side `forward_oauth` hook skips when `x-ccproxy-oauth-injected` header is present (set by the mitmproxy inbound route).
- **Inspector addon chain**: `InspectorScript` (OTel) → inbound `InspectorRouter` (OAuth) → outbound `InspectorRouter` (beta headers) → optional `PcapAddon`. Order matters: OTel spans must start before route handlers fire.
- **PCAP synthesizer**: Constructs fake-but-valid PCAP frames from mitmproxy flows for Wireshark. Activated via `CCPROXY_PCAP_FILE` or `CCPROXY_PCAP_PIPE` env vars. No kernel capture needed — pure userspace reconstruction. Wireshark gets packet timing, TCP analysis; content comes from mitmweb UI.
- **WireGuard keylog**: Auto-exported to `{config_dir}/wg.keylog` after inspector startup. Enables Wireshark to decrypt the outer WireGuard tunnel layer. Inner TLS (TLSv1.3) key export is not supported by mitmproxy (issues #3994, #4418).
- **SSL certificate handling**: `SSL_CERT_FILE` is validated on startup — if the path doesn't exist (e.g., stale venv after Python upgrade), falls back to `certifi.where()` then `/etc/ssl/certs/ca-certificates.crt`. In `--inspect` mode, the combined CA bundle (mitmproxy CA + system CAs) is built **after** mitmproxy starts to ensure the CA cert exists. All four cert env vars are set inside the gateway namespace: `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `NODE_EXTRA_CA_CERTS`.
- **Namespace confinement**: Two namespaces in `--inspect` mode:
  - **CLI namespace** (`ccproxy run --inspect`): rootless user+net namespace via `unshare`, slirp4netns bridge, WireGuard client routing to mitmweb's CLI listener. For jailed CLI clients (Claude Code, Gemini CLI).
  - **Gateway namespace** (`create_gateway_namespace()`): LiteLLM runs here. slirp4netns with `--port-map` for external HTTP client LAN access. WireGuard client routing to mitmweb's gateway listener. Eliminates `HTTPS_PROXY` env var hack.
  - Both use `--ready-fd`/`--exit-fd` pipes for clean lifecycle management. Hard-fail if prerequisites missing.
- **Docker containers**: Two containers managed via `compose.yaml`:
  - `litellm-db` (port 5434) - LiteLLM's internal database (`litellm` database)
  - `ccproxy-jaeger` (ports 4317/4318/16686) - Jaeger for OTel trace collection and visualization
- **Proxy direction tracking**: Inspector traces include `proxy_direction` field to distinguish traffic:
  - `REVERSE (0)` — External HTTP client → LiteLLM (reverse proxy listener)
  - `FORWARD (1)` — Reserved (was: LiteLLM → provider via HTTPS_PROXY, now superseded by WIREGUARD_GW)
  - `WIREGUARD_CLI (2)` — CLI client (jailed namespace) → mitmweb → LiteLLM
  - `WIREGUARD_GW (3)` — LiteLLM (gateway namespace) → mitmweb → provider API
  - Detection: `_get_wg_listen_port()` extracts the WireGuard listener port from the mode spec, compares against configured gateway port.
  - `flow.metadata["ccproxy.direction"]`: `"inbound"` for REVERSE and WIREGUARD_CLI, `"outbound"` for WIREGUARD_GW. Used by route handlers.
- **Session tracking**: Inspector addon extracts `session_id` from Claude Code's `metadata.user_id` field to link related requests across proxy layers.
- **OAuth dual-layer architecture**: OAuth handling runs at TWO layers:
  1. **mitmproxy layer** (inspector/routes/inbound.py): Sentinel key detection and token substitution on all inbound flows. Sets `x-ccproxy-oauth-injected: 1` header.
  2. **LiteLLM layer** (hooks/forward_oauth.py): Full OAuth pipeline with provider detection, model routing. Skips when `x-ccproxy-oauth-injected` header present.
  - The mitmproxy layer is the primary handler in `--inspect` mode. The LiteLLM layer is the fallback for non-inspect mode and as a safety net.
- **Provider model**: Providers are generic — URL + auth method (API key or OAuth token) + API format. No hardcoded provider names, hosts, or paths in routing logic. Provider context determined by flow properties (headers, sentinel key suffix, `oat_sources` config).

## Dev Instance

The Nix devShell configures a local dev instance via `mkConfig` with dedicated ports to avoid colliding with a production ccproxy on the default ports:

| Component | Dev Port | Production Default |
|-----------|----------|--------------------|
| LiteLLM | 4001 | 4000 |
| Inspect UI (mitmweb) | 8083 | 8083 |

Entering the devShell (`direnv` / `nix develop`) automatically:
- Creates `.ccproxy/` and symlinks Nix-generated `ccproxy.yaml` and `config.yaml`
- Sets `CCPROXY_CONFIG_DIR=$PWD/.ccproxy`
- Sets `CCPROXY_PORT=4001`
- Inspector cert store at `./.ccproxy` (project-local, not `~/.mitmproxy`)

**Dev workflow**: `just up` starts the dev ccproxy via process-compose (detached). `just down` stops it. The process-compose health probe checks `http://127.0.0.1:4001/health` every 30s with auto-restart on failure.

The `flake.nix` exports `lib.mkConfig` for other projects to generate their own ccproxy config with custom port/settings overrides.

## Dependencies

Key dependencies include:

- **litellm[proxy]** - Core proxy functionality
- **pydantic/pydantic-settings** - Configuration and validation
- **tyro** - CLI interface generation
- **tiktoken** - Token counting
- **anthropic** - Anthropic API client
- **rich** - Terminal output formatting
- **langfuse** - Observability integration
- **structlog** - Structured logging
- **mitmproxy** - HTTP/HTTPS traffic interception (inspector stack)
- **parse** - URL path template matching for xepor routing (NOT regex — uses Python format-string syntax like `{param}`)

## Development Workflow

### Local Development Setup

The Nix devShell provides all dependencies. Config files in `.ccproxy/` are auto-symlinked from the Nix store on shell entry.

```bash
# Start the dev instance
just up

# Check status
ccproxy status

# Stop
just down
```

For production/global installs, ccproxy must be installed with litellm in the same environment:

```bash
uv tool install --editable . --with 'litellm[proxy]' --force
```

### Making Changes

Source changes in the devShell are reflected immediately. Restart the proxy to pick up changes:

```bash
just down && just up

# Or manually (foreground):
ccproxy start [--inspect]

# Run tests
just test
```

### Why Bundle with LiteLLM?

LiteLLM imports `ccproxy.handler:CCProxyHandler` at runtime from the auto-generated `~/.ccproxy/ccproxy.py` file. Both must be in the same Python environment:

- `uv tool install ccproxy` → isolated env
- `uv tool install litellm` → different isolated env

Solution: Install together so they share the same environment.

The handler file is automatically regenerated on every `ccproxy start` based on the `handler` configuration in `ccproxy.yaml`.

## Marketplace Plugin Sync

This project's plugin files (`.claude-plugin/`, `skills/`, `hooks/`, `CLAUDE.md`) are synced to `starbaser/eigenmage-marketplace` via CI. Pushes to `starbased/dev` trigger `.github/workflows/notify-marketplace.yml`, which dispatches a `plugin-updated` event to the marketplace repo. The marketplace CI then pulls the latest submodule and copies plugin-relevant files into `plugins/ccproxy/`.
