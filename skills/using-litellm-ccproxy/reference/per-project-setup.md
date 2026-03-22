# Per-Project ccproxy Setup

Each project can run its own ccproxy instance with a dedicated config directory, port, and Langfuse keys. This isolates routing rules, model definitions, and observability per project.

## Contents

- [Config directory discovery](#config-directory-discovery)
- [Project structure](#project-structure)
- [Config files](#config-files)
- [.env file](#env-file)
- [flake.nix + direnv](#flakenix--direnv)
- [process-compose.yml](#process-composeyml)
- [justfile](#justfile)
- [Docker databases](#docker-databases)
- [Starting the instance](#starting-the-instance)
- [Langfuse integration](#langfuse-integration)
- [Observability metadata fields](#observability-metadata-fields)
- [Debugging](#debugging)

---

## Config directory discovery

ccproxy resolves its config directory with this precedence:

1. `CCPROXY_CONFIG_DIR` env var (highest)
2. LiteLLM proxy runtime directory (auto-detected)
3. `~/.ccproxy/` (default fallback)

Two ways to override:

```bash
# Via environment variable
export CCPROXY_CONFIG_DIR=./ccproxy
ccproxy start --detach

# Via CLI flag (sets CCPROXY_CONFIG_DIR for child processes)
ccproxy --config-dir ./ccproxy start --detach
```

The `--config-dir` flag defaults to `~/.ccproxy` when not provided. The `start` command propagates the resolved config dir into `CCPROXY_CONFIG_DIR` for child processes automatically.

---

## Project structure

Create a `ccproxy/` directory in the project root:

```
myproject/
├── .env                    # Langfuse keys, CCPROXY_CONFIG_DIR, DB ports
├── .envrc                  # direnv: use flake + dotenv
├── .gitignore              # .env, ccproxy/ccproxy.py
├── flake.nix               # standard devShell
├── process-compose.yml     # process management
├── justfile                # task recipes
├── compose.yaml            # Docker databases (optional, for --mitm)
└── ccproxy/
    ├── config.yaml         # LiteLLM model definitions, port, callbacks
    └── ccproxy.yaml        # hooks, rules, oat_sources, debug
```

`ccproxy/ccproxy.py` is auto-generated on `ccproxy start` — add it to `.gitignore`.

---

## Config files

### ccproxy/config.yaml

```yaml
model_list:
  - model_name: default
    litellm_params:
      model: claude-sonnet-4-6-20250514

  - model_name: claude-sonnet-4-6-20250514
    litellm_params:
      model: anthropic/claude-sonnet-4-6-20250514
      api_base: https://api.anthropic.com

litellm_settings:
  callbacks:
    - ccproxy.handler
    - langfuse
  success_callback:
    - langfuse

general_settings:
  forward_client_headers_to_llm_api: true
  # Use a different port than the global instance (default 4000)
  port: 4010
```

Pick a port that doesn't conflict with other ccproxy instances. Common convention: 4000 (global), 4010+ (per-project).

### ccproxy/ccproxy.yaml

```yaml
ccproxy:
  debug: true
  handler: "ccproxy.handler:CCProxyHandler"

  oat_sources:
    anthropic: "jq -r '.claudeAiOauth.accessToken' ~/.claude/.credentials.json"

  hooks:
    - ccproxy.hooks.rule_evaluator
    - ccproxy.hooks.model_router
    - ccproxy.hooks.extract_session_id
    - ccproxy.hooks.capture_headers
    - ccproxy.hooks.forward_oauth
    - ccproxy.hooks.add_beta_headers
    - ccproxy.hooks.inject_claude_code_identity

  default_model_passthrough: true
  rules: []
```

---

## .env file

```bash
# ccproxy per-project config
CCPROXY_CONFIG_DIR=./ccproxy

# Langfuse observability (per-project keys)
LANGFUSE_PUBLIC_KEY="pk-lf-..."
LANGFUSE_SECRET_KEY="sk-lf-..."
LANGFUSE_HOST="https://langfuse.example.com"

# Docker database ports (optional, for --mitm)
CCPROXY_DB_PORT=5435
LITELLM_DB_PORT=5436
```

Add to `.gitignore`:
```
.env
ccproxy/ccproxy.py
```

### direnv (.envrc)

```bash
use flake
dotenv_if_exists
```

Then `direnv allow`. The `dotenv_if_exists` loads `.env` automatically when entering the directory, so `CCPROXY_CONFIG_DIR` and Langfuse keys are available in the shell.

---

## flake.nix + direnv

Standard `devShells` flake (no devenv/cachix):

```nix
{
  description = "Project dev environment";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
      in
      {
        devShells.default = pkgs.mkShell {
          packages = with pkgs; [
            process-compose
            just
            jq
          ];
          shellHook = ''
            echo "ccproxy config: ''${CCPROXY_CONFIG_DIR:-~/.ccproxy}"
          '';
        };
      });
}
```

With `.envrc` containing `use flake` and `dotenv_if_exists`, entering the directory activates the devShell and loads environment variables automatically.

---

## process-compose.yml

Manages ccproxy as a background process with health checks:

```yaml
version: "0.5"

processes:
  ccproxy:
    command: ccproxy start
    is_daemon: true
    readiness_probe:
      http_get:
        host: 127.0.0.1
        port: 4010
        path: /health
      initial_delay_seconds: 5
      period_seconds: 10
      failure_threshold: 3
    namespace: infra
```

Adjust `port` to match `general_settings.port` in `ccproxy/config.yaml`.

Usage:
```bash
process-compose up -d          # start in background
process-compose status         # show process states
process-compose logs           # tail all logs
process-compose down           # stop all
process-compose attach         # interactive TUI
```

---

## justfile

Task recipes for common operations:

```makefile
# ccproxy per-project tasks

# Start ccproxy via process-compose
start:
    process-compose up -d

# Stop all processes
stop:
    process-compose down

# Tail logs
logs:
    process-compose logs

# Check ccproxy status
status:
    ccproxy --config-dir ./ccproxy status

# Start MITM database
db-up:
    docker compose --profile mitm up -d

# Stop databases
db-down:
    docker compose --profile mitm down

# Push Prisma schema to MITM database
db-push:
    DATABASE_URL="postgresql://ccproxy:test@localhost:${CCPROXY_DB_PORT:-5435}/ccproxy_mitm" \
        uv run prisma db push

# Regenerate Prisma client for tool installation
prisma-generate:
    DATABASE_URL="postgresql://ccproxy:test@localhost:${CCPROXY_DB_PORT:-5435}/ccproxy_mitm" \
        uv tool run --from claude-ccproxy prisma generate --schema \
        $(python3 -c "import ccproxy; from pathlib import Path; print(Path(ccproxy.__file__).parent.parent.parent / 'prisma' / 'schema.prisma')")
```

---

## Docker databases

Two PostgreSQL containers are available. Both are optional — include only what the project needs.

### When you need each database

| Database | When needed | Compose profile |
|---|---|---|
| `ccproxy-db` | `ccproxy start --mitm` — stores HTTP traces | `mitm` |
| `litellm-db` | `STORE_MODEL_IN_DB: "true"` — spend/cost tracking | `litellm` |

Most per-project setups only need `ccproxy-db` if using `--mitm`.

### Setup

Copy the per-project compose template from the ccproxy source repo:

```bash
cp ~/dev/projects/ccproxy/compose.per-project.yaml ./compose.yaml
```

Add database ports to `.env`:

```bash
CCPROXY_DB_PORT=5435
LITELLM_DB_PORT=5436
```

Docker Compose reads `.env` automatically, so port variables are picked up without extra configuration. Choose ports that don't conflict with other projects or the global instance (5433/5434).

### Running

Use `-p <projectname>` to scope container names and avoid collisions:

```bash
docker compose -p myproject --profile mitm up -d
```

This creates containers named `myproject-ccproxy-db-1`. Or use the justfile recipe:

```bash
just db-up
```

### Wiring DATABASE_URL

For MITM mode, ccproxy needs the database URL. Set `CCPROXY_DATABASE_URL` in `.env`:

```bash
CCPROXY_DATABASE_URL=postgresql://ccproxy:test@localhost:5435/ccproxy_mitm
```

Or set it in `ccproxy/ccproxy.yaml`:

```yaml
ccproxy:
  mitm:
    database_url: "postgresql://ccproxy:test@localhost:5435/ccproxy_mitm"
```

Resolution priority (highest first):
1. `CCPROXY_DATABASE_URL` env var
2. `DATABASE_URL` env var
3. `ccproxy.yaml` → `ccproxy.mitm.database_url`

### Prisma schema (MITM only)

After first `db-up`, push the schema:

```bash
just db-push
```

The MITM Prisma client auto-generates on first `ccproxy start --mitm` if missing. Manual regeneration after schema changes:

```bash
just prisma-generate
```

---

## Starting the instance

With process-compose (recommended):
```bash
just db-up       # if using MITM
just start       # start ccproxy
just status      # verify
just logs        # tail logs
```

Without process-compose:
```bash
ccproxy --config-dir ./ccproxy start --detach
```

Verify:
```bash
ccproxy --config-dir ./ccproxy status
ccproxy --config-dir ./ccproxy logs -f
```

SDK clients point at the project's port:
```python
import anthropic
client = anthropic.Anthropic(
    api_key="sk-ant-oat-ccproxy-anthropic",
    base_url="http://localhost:4010",  # project-specific port
)
```

---

## Langfuse integration

With `langfuse` in `callbacks` and the three env vars in `.env`, every request through the project's ccproxy instance creates a Langfuse trace automatically.

### Environment variables

| Variable | Purpose |
|----------|---------|
| `LANGFUSE_PUBLIC_KEY` | Project public key from Langfuse dashboard |
| `LANGFUSE_SECRET_KEY` | Project secret key |
| `LANGFUSE_HOST` | Langfuse endpoint URL |
| `LANGFUSE_DEBUG` | Enable debug logging (optional) |

### Verification

On startup, logs show:
```
LiteLLM Callbacks Initialized: [..., 'langfuse', ...]
```

No client-side Langfuse SDK required.

### 1Password integration

```bash
export LANGFUSE_PUBLIC_KEY="op://dev/LangFuse/public key"
export LANGFUSE_SECRET_KEY="op://dev/LangFuse/credential"
export LANGFUSE_HOST="op://dev/LangFuse/host"
```

---

## Observability metadata fields

Clients enrich traces by including `metadata` in the request body. The `extract_session_id` hook maps these to LiteLLM's Langfuse integration:

| Field | Type | Effect in Langfuse |
|-------|------|--------------------|
| `session_id` | `string` | Groups traces into a session |
| `trace_user_id` | `string` | Sets user attribution |
| `tags` | `string[]` | Filterable tags (e.g. `["myapp", "prod"]`) |
| `generation_name` | `string` | Names the generation span |

Additional keys in `metadata` are forwarded as-is to trace metadata.

### Pipeline flow

```
Client POST body.metadata
  { session_id, trace_user_id, tags, generation_name, ... }
       │
       ▼
extract_session_id hook
  Reads body.metadata fields
  Sets: metadata["session_id"], metadata["trace_metadata"]
       │
       ▼
LiteLLM Langfuse callback
  session_id ──▶ Langfuse session grouping
  trace_user_id ──▶ user attribution
  tags ──▶ trace tags
  generation_name ──▶ generation span name
       │
       ▼
Langfuse (LANGFUSE_HOST)
```

### Claude Code session ID extraction

When Claude Code is the client, session tracking is automatic. Claude Code encodes session info in `metadata.user_id`:

```
user_{hash}_account_{uuid}_session_{uuid}
```

The `extract_session_id` hook parses this and sets `metadata["session_id"]` to the trailing UUID. No explicit `session_id` needed when Claude Code is the client.

### Metadata side-channel

LiteLLM does not reliably preserve all custom metadata through its pipeline. ccproxy uses a side-channel store keyed by `litellm_call_id` (60-second TTL) to forward additional metadata (HTTP headers, custom trace attributes) that LiteLLM would otherwise drop. This is transparent to clients.

---

## Debugging

If Langfuse traces don't appear:

1. Verify env vars reached the process: `ccproxy --config-dir ./ccproxy logs -n 10`
2. Check logs: `ccproxy --config-dir ./ccproxy logs -n 50 | grep -i langfuse`
3. Set `LANGFUSE_DEBUG=true` in `.env` and restart
4. Confirm `langfuse` is in `litellm_settings.callbacks` in `./ccproxy/config.yaml`

If config directory is wrong:

```bash
# Check what ccproxy resolved
ccproxy --config-dir ./ccproxy status --json | jq .config_dir

# Verify CCPROXY_CONFIG_DIR in shell
echo $CCPROXY_CONFIG_DIR
```

If Docker databases won't start:

```bash
# Check for port conflicts
ss -tlnp | grep ${CCPROXY_DB_PORT:-5435}

# Check container logs
docker compose logs ccproxy-db
```
