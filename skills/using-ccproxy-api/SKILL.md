---
name: using-ccproxy-api
description: >-
  Guides users through ccproxy as an OpenAI-compatible and Anthropic-compatible LLM API server
  with SDK integration, OAuth authentication, sentinel key substitution, model routing, and
  troubleshooting. Use when installing ccproxy, configuring SDK clients (Anthropic, OpenAI,
  LiteLLM, Agent SDK) against ccproxy, setting up per-project instances, debugging authentication
  errors, setting up OAuth token forwarding, or understanding the hook pipeline and compliance system.
---

# Using ccproxy as an LLM API Server

ccproxy exposes an OpenAI-compatible and Anthropic-compatible API via a mitmproxy-based interceptor. Any SDK or HTTP client that supports custom `base_url` can use it.

## Installation

### System-wide (Home Manager)

Add ccproxy as a flake input and enable the Home Manager module:

```nix
# flake.nix
inputs.ccproxy.url = "github:starbaser/ccproxy";

# home configuration
programs.ccproxy = {
  enable = true;
  settings = {
    # Override defaults here (port, oat_sources, transforms, etc.)
  };
};
```

This installs the `ccproxy` binary, generates `~/.ccproxy/ccproxy.yaml` from Nix, and creates a `systemd --user` service that auto-restarts on config changes.

### Standalone (any Linux)

```bash
# Clone and enter devShell
git clone https://github.com/starbaser/ccproxy
cd ccproxy
nix develop   # or: direnv allow

# Install template config
ccproxy install          # copies template to ~/.ccproxy/ccproxy.yaml
ccproxy install --force  # overwrites existing config

# Edit config
$EDITOR ~/.ccproxy/ccproxy.yaml

# Start
ccproxy start
```

### Per-project instance

Each project can run its own ccproxy with isolated config, port, and transforms via the flake's `mkConfig`. Use `ccproxy.defaultSettings.settings` (top-level, no `${system}` selector needed) as the base to inherit all defaults (hooks, compliance, oat_sources, otel).

```nix
# project flake.nix
{
  inputs.ccproxy.url = "github:starbaser/ccproxy";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
  inputs.flake-utils.url = "github:numtide/flake-utils";

  outputs = { self, nixpkgs, flake-utils, ccproxy }:
    let
      defaults = ccproxy.defaultSettings.settings;
    in
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        proxyConfig = ccproxy.lib.${system}.mkConfig {
          settings = defaults // {
            port = 4010;  # per-project: use 4010+ to avoid collisions
            inspector = defaults.inspector // {
              port = 8090;
              cert_dir = "./.ccproxy";
              transforms = [
                { match_path = "/v1/messages"; mode = "redirect";
                  dest_provider = "anthropic"; dest_host = "api.anthropic.com";
                  dest_path = "/v1/messages"; dest_api_key_ref = "anthropic"; }
              ];
            };
          };
        };
      in {
        devShells.default = pkgs.mkShell {
          packages = with pkgs; [
            ccproxy.packages.${system}.default
            just process-compose
          ];
          shellHook = proxyConfig.shellHook;
        };
      });
}
```

`mkConfig` generates a Nix store `ccproxy.yaml`, and its `shellHook` symlinks it into `.ccproxy/` and exports `CCPROXY_CONFIG_DIR`. The `.envrc` just needs `use flake`.

Add `.ccproxy/` to `.gitignore` — the directory contains a Nix-generated symlink that is machine-specific and regenerated on `nix develop`:

```
# .gitignore
.ccproxy/
```

#### Port assignment conventions

| Port | Use |
|------|-----|
| 4000 | System-wide ccproxy (Home Manager, default) |
| 4001 | ccproxy project's own devShell |
| 4010+ | Per-project instances |
| 8083 | System inspector UI (default) |
| 8084 | ccproxy dev inspector |
| 8090+ | Per-project inspector UI |

### Running the instance

```bash
# Foreground
ccproxy start

# Via process-compose (recommended for dev)
just up       # process-compose up --detached
just down     # process-compose down

# Check health
ccproxy status              # Rich panel
ccproxy status --json       # Machine-readable
ccproxy status --proxy      # Exit 0 if proxy up, 1 if down
ccproxy status --inspect    # Exit 0 if inspector up, 2 if down
```

### process-compose.yml

Use `ccproxy status --proxy` as the readiness probe so dependent processes wait for the proxy to be healthy:

```yaml
# process-compose.yml
version: "0.5"

processes:
  ccproxy:
    command: "ccproxy start"
    readiness_probe:
      exec:
        command: "ccproxy status --proxy"
      initial_delay_seconds: 5
      period_seconds: 30
      timeout_seconds: 10
      failure_threshold: 6
    availability:
      restart: on_failure
      backoff_seconds: 2
      max_restarts: 5

  myapp:
    command: "python -m myapp"
    depends_on:
      ccproxy:
        condition: process_healthy
```

### Wiring SDK clients

Point any SDK at the per-project port with a sentinel key:

```python
import anthropic

client = anthropic.Anthropic(
    api_key="sk-ant-oat-ccproxy-anthropic",
    base_url="http://localhost:4010",  # per-project port
)
```

Or via environment variables in `shellHook` / `.envrc`:

```bash
export ANTHROPIC_BASE_URL="http://localhost:4010"
export ANTHROPIC_API_KEY="sk-ant-oat-ccproxy-anthropic"
```

## Configuration

All config lives in `$CCPROXY_CONFIG_DIR/ccproxy.yaml` (default `~/.ccproxy/ccproxy.yaml`).

```yaml
ccproxy:
  host: 127.0.0.1
  port: 4000

  oat_sources:
    anthropic:
      command: "jq -r '.claudeAiOauth.accessToken' ~/.claude/.credentials.json"
      destinations: ["api.anthropic.com"]
    gemini:
      command: "jq -r '.access_token' ~/.gemini/oauth_creds.json"
      destinations: ["generativelanguage.googleapis.com", "cloudcode-pa.googleapis.com"]
      user_agent: "GeminiCLI"

  hooks:
    inbound:
      - ccproxy.hooks.forward_oauth
      - ccproxy.hooks.extract_session_id
    outbound:
      - ccproxy.hooks.inject_mcp_notifications
      - ccproxy.hooks.verbose_mode
      - ccproxy.hooks.apply_compliance

  compliance:
    enabled: true
    min_observations: 3
    seed_anthropic: true

  inspector:
    port: 8083
    cert_dir: ~/.ccproxy
    transforms:
      - match_path: /v1/messages
        mode: redirect
        dest_provider: anthropic
        dest_host: api.anthropic.com
        dest_path: /v1/messages
        dest_api_key_ref: anthropic
```

See [reference/routing-and-config.md](reference/routing-and-config.md) for transform rules, oat_sources patterns, and hook parameters.

## How authentication works

**OAuth mode** (subscription accounts -- Claude Max, Team, Enterprise):
1. Client sends sentinel key `sk-ant-oat-ccproxy-{provider}` as API key
2. `forward_oauth` hook detects sentinel prefix, looks up real token from `oat_sources`
3. `apply_compliance` hook stamps learned headers (`anthropic-beta`, `anthropic-version`), system prompt, and body envelope fields from a compliance profile
4. Request reaches provider API with valid OAuth Bearer token and full compliance contract

**API key mode** (direct API keys):
1. Client sends real API key via `x-api-key` or `Authorization` header
2. Key passes through to the provider unchanged

### Sentinel key format

```
sk-ant-oat-ccproxy-{provider}
```

Where `{provider}` matches a key in `oat_sources` config. Common values:
- `sk-ant-oat-ccproxy-anthropic` -- uses `oat_sources.anthropic` token
- `sk-ant-oat-ccproxy-gemini` -- uses `oat_sources.gemini` token

### Default hooks

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

- `forward_oauth` -- substitutes sentinel key with real token, sets `Authorization: Bearer {token}`, clears `x-api-key`
- `extract_session_id` -- parses `metadata.user_id` for MCP notification routing
- `inject_mcp_notifications` -- injects buffered MCP terminal events as tool_use/tool_result pairs
- `verbose_mode` -- strips `redact-thinking-*` from `anthropic-beta` to enable full thinking output
- `apply_compliance` -- stamps learned compliance headers, body fields, and system prompt

### Compliance-based headers and identity

Instead of explicit hooks for beta headers and identity injection, ccproxy uses a **compliance learning system**. It passively observes legitimate CLI traffic (via WireGuard) and learns the exact headers, body fields, and system prompt that constitute a compliant request. This learned profile is then stamped onto SDK requests by `apply_compliance`.

The compliance system automatically handles `anthropic-beta`, `anthropic-version`, system prompt injection, and body envelope fields. An Anthropic v0 seed profile provides baseline coverage on first startup before any real traffic is observed.

See the `using-ccproxy-inspector` skill for details on seeding and inspecting compliance profiles.

## Quick start

```python
# Anthropic SDK (OAuth via sentinel key)
import anthropic
client = anthropic.Anthropic(
    api_key="sk-ant-oat-ccproxy-anthropic",
    base_url="http://localhost:4000",
)

# OpenAI SDK
from openai import OpenAI
client = OpenAI(
    api_key="sk-ant-oat-ccproxy-anthropic",
    base_url="http://localhost:4000",
)
```

## SDK integration

### Anthropic Python SDK

```python
import anthropic

client = anthropic.Anthropic(
    api_key="sk-ant-oat-ccproxy-anthropic",
    base_url="http://localhost:4000",
)

response = client.messages.create(
    model="claude-sonnet-4-5-20250929",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Hello"}],
)
```

No extra headers needed -- the compliance system handles `anthropic-beta`, `anthropic-version`, and system prompt injection automatically.

Streaming:
```python
with client.messages.stream(
    model="claude-sonnet-4-5-20250929",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Hello"}],
) as stream:
    for text in stream.text_stream:
        print(text, end="")
```

### OpenAI Python SDK

```python
from openai import OpenAI

client = OpenAI(
    api_key="sk-ant-oat-ccproxy-anthropic",
    base_url="http://localhost:4000",
)

response = client.chat.completions.create(
    model="claude-sonnet-4-5-20250929",
    messages=[{"role": "user", "content": "Hello"}],
)
```

Requires a transform rule to rewrite from OpenAI format to the destination provider format via lightllm.

### LiteLLM SDK

```python
import asyncio, litellm

async def main():
    response = await litellm.acompletion(
        model="claude-sonnet-4-5-20250929",
        messages=[{"role": "user", "content": "Hello"}],
        api_base="http://127.0.0.1:4000",
        api_key="sk-ant-oat-ccproxy-anthropic",
    )
    print(response.choices[0].message.content)

asyncio.run(main())
```

**Note**: `litellm.anthropic.messages` bypasses proxies. Always use `litellm.acompletion()`.

### Claude Agent SDK

```python
import os
os.environ["ANTHROPIC_BASE_URL"] = "http://localhost:4000"
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-oat-ccproxy-anthropic"

from claude_agent_sdk import query, ClaudeAgentOptions

async for message in query(
    prompt="Your prompt here",
    options=ClaudeAgentOptions(
        allowed_tools=["Read", "Glob"],
        permission_mode="default",
        cwd=os.getcwd(),
    ),
):
    # Handle AssistantMessage, ResultMessage, etc.
    pass
```

### Environment variables (any SDK)

```bash
export ANTHROPIC_BASE_URL="http://localhost:4000"
export ANTHROPIC_API_KEY="sk-ant-oat-ccproxy-anthropic"
# OpenAI compat
export OPENAI_BASE_URL="http://localhost:4000"
export OPENAI_API_BASE="http://localhost:4000"
```

### curl (raw HTTP)

```bash
curl http://localhost:4000/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: sk-ant-oat-ccproxy-anthropic" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "claude-sonnet-4-5-20250929",
    "max_tokens": 100,
    "messages": [{"role": "user", "content": "Hello"}]
  }'
```

## Model routing

Model routing is configured via `inspector.transforms` in `ccproxy.yaml`. Each transform rule matches by `match_host`, `match_path`, and/or `match_model`, then rewrites to `dest_provider`/`dest_model` via the lightllm dispatch. First match wins. Unmatched reverse proxy flows get a 501 error; unmatched WireGuard flows pass through unchanged.

See [reference/routing-and-config.md](reference/routing-and-config.md) for transform configuration patterns.

## Troubleshooting

Authentication failures are the most common issue. Follow this decision tree:

```
Error message?
│
├─ "This credential is only authorized for use with Claude Code"
│  ▶ See: Missing compliance profile (system prompt not injected)
│
├─ "OAuth is not supported" / "invalid x-api-key"
│  ▶ See: Missing compliance headers (anthropic-beta not stamped)
│
├─ 401 Unauthorized / token errors
│  ▶ See: Token issues
│
├─ Connection refused / timeout
│  ▶ See: Connectivity
│
└─ Other / unclear
   ▶ See: General diagnostics
```

See [reference/troubleshooting.md](reference/troubleshooting.md) for the full diagnostic guide with resolution steps for each branch.

### Quick diagnostic commands

```bash
ccproxy status              # Verify proxy is running
ccproxy status --json       # Machine-readable status with URL
ccproxy logs -f             # Stream logs in real-time
ccproxy logs -n 50          # Last 50 lines
```

## Known limitations (upstream flake issues)

1. **`nix/defaults.nix` uses `min_observations: 1`** — permissive for dev; production configs should set `min_observations: 3`+.
2. **`compliance.seed_anthropic` not in `defaults.nix`** — must be set explicitly in consumer configs; not inherited from defaults.
3. **`devConfig` overwrites `inspector` atomically** — top-level `//` merge on `inspector` drops sub-keys not re-specified (e.g. `debug`). Deep merge each nested attrset explicitly: `defaults.inspector // { ... }`.
4. **`supportedSystems` limited** — only `x86_64-linux` and `aarch64-linux`; `aarch64-darwin` not supported.
5. ~~**`shellHook` doesn't quote `configDir`**~~ — fixed.
6. ~~**`CCPROXY_PORT` env var duplicated YAML port**~~ — fixed.
7. ~~**`defaultSettings` only accessible via per-system `lib`**~~ — fixed; now top-level at `ccproxy.defaultSettings`.

## Reference files

- [reference/troubleshooting.md](reference/troubleshooting.md) -- Full diagnostic decision tree with error-specific resolution steps
- [reference/routing-and-config.md](reference/routing-and-config.md) -- Model routing, config.yaml patterns, hook pipeline details
