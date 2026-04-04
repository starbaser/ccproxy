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

### Request Flow

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
- **config.py**: Configuration management using Pydantic with multi-level discovery (env var → LiteLLM runtime → ~/.ccproxy/).
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
- **inspector/addon.py**: Inspector addon for HTTP traffic capture with OTel span emission. Detects traffic direction per-flow and forwards WireGuard LLM API traffic to LiteLLM.
- **inspector/namespace.py**: Network namespace confinement for `ccproxy run --inspect`. Creates user+net namespace with slirp4netns bridge and WireGuard client routing through mitmweb's WireGuard server. Requires `slirp4netns`, `wg`, `unshare`, `nsenter`, `ip` (all rootless on Linux 5.6+ with `unprivileged_userns_clone=1`).
- **inspector/process.py**: Process management for launching and supervising mitmproxy (mitmweb). Auto-assigns a free UDP port for the WireGuard listener.
- **inspector/script.py**: Mitmproxy addon script loaded via `-s` flag. Runs in the mitmproxy process; delegates to `InspectorAddon` for per-flow capture and OTel span emission. Loads `OtelConfig` from `ccproxy.yaml` via `CCPROXY_CONFIG_DIR`.
- **inspector/telemetry.py**: OpenTelemetry span emission for inspector flows. Three-mode degradation: real OTLP export, no-op tracer, or stub — depending on package availability and config. OTel config lives under top-level `ccproxy.otel`.
- **cli.py**: Tyro-based CLI interface for managing the proxy server. Foreground-only (no `--detach`/`stop`/`restart`). Status detection via TCP health probes.
- **constants.py**: Shared constants — `ANTHROPIC_BETA_HEADERS`, `OAUTH_SENTINEL_PREFIX`, `SENSITIVE_PATTERNS`, and `CLAUDE_CODE_SYSTEM_PREFIX`.
- **metadata_store.py**: Thread-safe TTL store keyed by `litellm_call_id` for bridging request metadata across LiteLLM callback boundaries.
- **mcp/buffer.py**: Thread-safe notification buffer for MCP terminal events (from mcptty). Stores per-task events with configurable TTL and max-event limits.
- **mcp/routes.py**: FastAPI routes for MCP notification ingestion (`POST /mcp/notify`). Accepts events from mcptty and writes them to the buffer.
- **preflight.py**: Pre-flight checks before proxy startup — kills orphaned ccproxy/mitmdump processes, verifies port availability, and enforces single-instance constraint.
- **utils.py**: Template discovery and debug utilities (`dt()`, `dv()`, `d()`, `p()`).
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
- `~/.ccproxy/ccproxy.yaml` - ccproxy-specific configuration (rules, hooks, debug settings, handler path)
- `~/.ccproxy/ccproxy.py` - Auto-generated handler file (created on `ccproxy start` based on `handler` config)

**Config Discovery Precedence:**

1. `CCPROXY_CONFIG_DIR` environment variable
2. LiteLLM proxy runtime directory (auto-detected)
3. `~/.ccproxy/` (default fallback)

## Testing Patterns

The test suite uses pytest with comprehensive fixtures (18 test files, 90% coverage minimum):

- `mock_proxy_server` fixture for mocking LiteLLM proxy
- `cleanup` fixture ensures singleton instances are cleared between tests
- Tests organized to mirror source structure (`test_<module>.py`)
- Parametrized tests for rule evaluation scenarios
- Integration tests verify end-to-end behavior

## Important Implementation Notes

- **Singleton patterns**: `CCProxyConfig` and `ModelRouter` use thread-safe singletons. Use `clear_config_instance()` and `clear_router()` to reset state in tests.
- **Token counting**: Uses tiktoken with fallback to character-based estimation for non-OpenAI models.
- **OAuth token forwarding**: Handled specially for Claude CLI requests. Supports custom User-Agent per provider.
- **OAuth sentinel key**: SDK clients can use `sk-ant-oat-ccproxy-{provider}` as API key to trigger OAuth token substitution from `oat_sources` config. OAuth works without the inspector via pipeline hooks; the inspector provides a redundant header safety net.
- **OAuth token refresh**: Automatic refresh with two triggers:
  - TTL-based: Background task checks every 30 minutes, refreshes at 90% of `oauth_ttl` (default 8h)
  - 401-triggered: Immediate refresh when API returns authentication error
  - Config: `oauth_ttl` (seconds), `oauth_refresh_buffer` (ratio, default 0.1)
- **Request metadata**: Stored by `litellm_call_id` with 60-second TTL auto-cleanup (LiteLLM doesn't preserve custom metadata).
- **Health checks**: LiteLLM's `/health` endpoint performs real API calls to each provider. `_inject_health_check_auth()` patches `_update_litellm_params_for_health_check` to inject OAuth credentials (api_key, extra_headers) before `acompletion()` — required because LiteLLM validates API keys before `async_pre_call_hook` runs. The pipeline then runs with forced passthrough (rule_evaluator skips classification, model_router forces passthrough via `ccproxy_is_health_check` metadata flag) so hooks like `forward_oauth`, `add_beta_headers`, and `inject_claude_code_identity` enhance the request. Health probes use `max_tokens=1` to minimize cost.
- **Hook error isolation**: Errors in one hook don't block others from executing.
- **Lazy model loading**: Models loaded from LiteLLM proxy on first request, not at startup.
- **Inspector**: WireGuard transparent proxy architecture activated by `--inspect`. mitmweb binds an auto-assigned UDP port for its WireGuard server and intercepts all namespace traffic. Without `--inspect`, the inspector is not started. OAuth is handled entirely by pipeline hooks + `_patch_anthropic_oauth_headers()` monkey-patch; the inspector is not required for OAuth.
- **SSL certificate handling**: `SSL_CERT_FILE` is validated on startup — if the path doesn't exist (e.g., stale venv after Python upgrade), falls back to `certifi.where()` then `/etc/ssl/certs/ca-certificates.crt`. In `--inspect` mode, the combined CA bundle (mitmproxy CA + system CAs) is built **after** mitmproxy starts to ensure the CA cert exists. All four cert env vars are set for LiteLLM: `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `NODE_EXTRA_CA_CERTS`.
- **Namespace confinement**: `ccproxy run --inspect` creates a rootless user+net namespace via `unshare`, bridges it to the host via `slirp4netns` (gateway `10.0.2.2`, namespace IP `10.0.2.100`), and routes all traffic through a WireGuard client (`10.0.0.1/32`) pointing at mitmweb's WireGuard server. The WireGuard port is parsed from mitmweb's client config (auto-assigned at startup). Uses `--ready-fd`/`--exit-fd` pipes for clean lifecycle management. Hard-fails if prerequisites are missing (no fallback to unconfined execution). Combined CA bundle injected via all four cert env vars for transparent TLS interception.
- **Docker containers**: Two containers managed via `compose.yaml`:
  - `litellm-db` (port 5434) - LiteLLM's internal database (`litellm` database)
  - `ccproxy-jaeger` (ports 4317/4318/16686) - Jaeger for OTel trace collection and visualization
- **Proxy direction tracking**: Inspector traces include `proxy_direction` field (0=reverse, 1=forward, 2=wireguard) to distinguish client→LiteLLM, LiteLLM→provider, and namespace→tunnel traffic.
- **Session tracking**: Inspector addon extracts `session_id` from Claude Code's `metadata.user_id` field to link related requests across proxy layers.

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
