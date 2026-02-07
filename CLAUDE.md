# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

@~/.claude/standards-python-extended.md

## Project Overview

**CRITICAL**: The project name is `ccproxy` (lowercase). Do NOT refer to the project as "CCProxy". The PascalCase form is used exclusively for class names (e.g., `CCProxyHandler`, `CCProxyConfig`).

`ccproxy` is a command-line tool that intercepts and routes Claude Code's requests to different LLM providers via a LiteLLM proxy server. It enables intelligent request routing based on token count, model type, tool usage, or custom rules. It also functions as a development platform for new and unexplored features or unofficial mods of Claude Code.

## Development Commands

### Running Tests

```bash
# Run all tests with coverage
uv run pytest

# Run specific test file
uv run pytest tests/test_classifier.py

# Run tests matching pattern
uv run pytest -k "test_token_count"

# Run with verbose output
uv run pytest -v
```

### Linting & Formatting

```bash
# Format code with ruff
uv run ruff format .

# Check linting issues
uv run ruff check .

# Fix linting issues automatically
uv run ruff check --fix .

# Type checking with mypy
uv run mypy src/ccproxy
```

### Development Setup

```bash
# Install with dev dependencies
uv sync --dev

# Install as a tool globally
uv tool install .

# Run the module directly
uv run python -m ccproxy
```

### CLI Commands

```bash
# Install configuration files
ccproxy install [--force]

# Start/stop proxy server
ccproxy start [--detach] [--mitm]
ccproxy stop
ccproxy restart [--detach] [--mitm]

# View logs and status
ccproxy logs [-f] [-n LINES]
ccproxy status [--json]

# Run command with proxy environment
ccproxy run <command> [args...]

# Query MITM traces database
ccproxy db sql "SELECT COUNT(*) FROM \"CCProxy_HttpTraces\""
ccproxy db sql --file query.sql
ccproxy db sql "SELECT * FROM ..." --json
ccproxy db sql "SELECT * FROM ..." --csv
```

**MITM Mode**: The `--mitm` flag enables the MITM proxy layer which intercepts HTTP traffic for header/body modification. Required for OAuth sentinel key with native Anthropic SDK.

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

- **handler.py**: Main entry point as a LiteLLM CustomLogger. Orchestrates the classification and routing process via `async_pre_call_hook()`.
- **classifier.py**: Rule-based classification system that evaluates rules in order to determine routing.
- **rules.py**: Defines `ClassificationRule` abstract base class and built-in rules:
  - `ThinkingRule` - Matches requests with "thinking" field
  - `MatchModelRule` - Matches by model name substring
  - `MatchToolRule` - Matches by tool name in request
  - `TokenCountRule` - Evaluates based on token count threshold
- **router.py**: Manages model configurations from LiteLLM proxy server. Lazy-loads models on first request.
- **config.py**: Configuration management using Pydantic with multi-level discovery (env var → LiteLLM runtime → ~/.ccproxy/).
- **hooks.py**: Built-in hooks that process requests. Hooks support optional params via `hook:` + `params:` YAML format (see `HookConfig` class in config.py):
  - `rule_evaluator` - Evaluates rules and stores routing decision
  - `model_router` - Routes to appropriate model
  - `forward_oauth` - Forwards OAuth tokens to provider APIs; supports sentinel key substitution
  - `extract_session_id` - Extracts session identifiers
  - `capture_headers` - Captures HTTP headers with sensitive redaction (supports `headers` param)
  - `forward_apikey` - Forwards x-api-key header
  - `add_beta_headers` - Adds anthropic-beta headers for Claude Code OAuth
  - `inject_claude_code_identity` - Injects required system message for OAuth
- **mitm/addon.py**: MITM proxy addon for HTTP-layer modifications:
  - Removes `x-api-key` for OAuth requests
  - Adds `anthropic-beta` headers for Claude Code compliance
  - Injects "You are Claude Code" system message prefix for OAuth tokens
- **cli.py**: Tyro-based CLI interface (~900 lines) for managing the proxy server.
- **utils.py**: Template discovery and debug utilities (`dt()`, `dv()`, `d()`, `p()`).

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
- **OAuth sentinel key**: SDK clients can use `sk-ant-oat-ccproxy-{provider}` as API key to trigger OAuth token substitution from `oat_sources` config. Requires MITM mode for native Anthropic SDK (system message injection happens at HTTP layer).
- **OAuth token refresh**: Automatic refresh with two triggers:
  - TTL-based: Background task checks every 30 minutes, refreshes at 90% of `oauth_ttl` (default 8h)
  - 401-triggered: Immediate refresh when API returns authentication error
  - Config: `oauth_ttl` (seconds), `oauth_refresh_buffer` (ratio, default 0.1)
- **Request metadata**: Stored by `litellm_call_id` with 60-second TTL auto-cleanup (LiteLLM doesn't preserve custom metadata).
- **Hook error isolation**: Errors in one hook don't block others from executing.
- **Lazy model loading**: Models loaded from LiteLLM proxy on first request, not at startup.
- **MITM proxy**: Two-layer architecture - reverse proxy on port 4000 (user-facing), forward proxy on port 8081 (outbound to providers). MITM layer injects headers and modifies request bodies for OAuth compliance.
- **MITM database**: PostgreSQL for HTTP trace storage. Database URL set via `CCPROXY_DATABASE_URL` env var or in `ccproxy.yaml` under `litellm.environment`. Current setup uses `litellm-db` container with database `ccproxy_mitm` (not the `ccproxy-db` in compose.yaml).
- **Docker containers**: Two PostgreSQL containers managed via `compose.yaml`:
  - `ccproxy-db` (port 5432) - LiteLLM's internal database
  - `litellm-db` (port 5434) - MITM trace storage (`ccproxy_mitm` database)
  - When "too many database connections" errors occur, restart **both** containers: `docker restart ccproxy-db litellm-db`
- **Proxy direction tracking**: MITM traces include `proxy_direction` field (0=reverse, 1=forward) to distinguish client→LiteLLM vs LiteLLM→provider traffic.
- **Session tracking**: MITM addon extracts `session_id` from Claude Code's `metadata.user_id` field to link related requests across proxy layers.

## Dependencies

Key dependencies include:

- **litellm[proxy]** - Core proxy functionality
- **pydantic/pydantic-settings** - Configuration and validation
- **tyro** - CLI interface generation
- **tiktoken** - Token counting
- **anthropic** - Anthropic API client
- **rich** - Terminal output formatting
- **langfuse** - Observability integration
- **prisma** - Database ORM
- **structlog** - Structured logging

## Development Workflow

### Local Development Setup

ccproxy must be installed with litellm in the same environment so that LiteLLM can import the ccproxy handler:

```bash
# Install in editable mode with litellm bundled
uv tool install --editable . --with 'litellm[proxy]' --force
```

### Making Changes

With editable mode, source changes are reflected immediately. Just restart the proxy:

```bash
# Restart proxy to regenerate handler and pick up changes
ccproxy stop
ccproxy start --detach

# Verify
ccproxy status

# Run tests
uv run pytest
```

### Why Bundle with LiteLLM?

LiteLLM imports `ccproxy.handler:CCProxyHandler` at runtime from the auto-generated `~/.ccproxy/ccproxy.py` file. Both must be in the same Python environment:

- `uv tool install ccproxy` → isolated env
- `uv tool install litellm` → different isolated env

Solution: Install together so they share the same environment.

The handler file is automatically regenerated on every `ccproxy start` based on the `handler` configuration in `ccproxy.yaml`.

### Prisma Schema Changes

When modifying `prisma/schema.prisma` (e.g., adding fields to `CCProxy_HttpTraces`), you must:

```bash
# 1. Push schema changes to database
DATABASE_URL="postgresql://ccproxy:test@localhost:5432/ccproxy_mitm" uv run prisma db push

# 2. Regenerate Prisma client for the TOOL installation (not just .venv)
DATABASE_URL="postgresql://ccproxy:test@localhost:5432/ccproxy_mitm" \
  uv tool run --from claude-ccproxy prisma generate --schema prisma/schema.prisma

# 3. Restart proxy
ccproxy stop && ccproxy start --detach --mitm
```

**Why both steps?** The `uv run prisma generate` only updates `.venv/`, but ccproxy runs from the tool installation at `~/.local/share/uv/tools/claude-ccproxy/`. The tool's Prisma client must be regenerated separately.
