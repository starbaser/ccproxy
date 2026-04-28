# `ccproxy` — CLI Proxy [![Version](https://img.shields.io/badge/version-2.0.0-orange.svg)](https://github.com/starbaser/ccproxy)

> [Discord](https://starbased.net/discord)

ccproxy is a transparent network interceptor for LLM tooling and AI harnesses,
built on mitmproxy and WireGuard with full TLS inspection and Wireshark keylog
export. Originally purpose-built for Claude Code, ccproxy now works with any LLM
client: Aider, Cursor, OpenAI SDK, or anything else that speaks HTTP. It jails a
process inside a rootless WireGuard namespace, intercepts at the network layer,
and feeds it through a DAG-driven pipeline that can decompose, transform, and
re-route traffic between providers.
Cross-provider request and response transformation is handled by `lightllm`, a
surgical connector into LiteLLM’s `BaseConfig` completion layer — no LiteLLM
proxy subprocess, no gateway server.

**New in 2.0 beta**: DeepSeek V4 routing support — redirect Anthropic-format
requests to DeepSeek’s `/anthropic/v1/messages` endpoint with a single transform
rule. See [Configuration](#configuration) for the routing setup.

The hook pipeline is your extension point for building mods and taking control
of your LLM usage while respecting terms of service:
- **Cross-provider routing**: redirect or transform requests between Anthropic,
  Gemini, OpenAI, DeepSeek, and any LiteLLM-supported provider.
- **Compliance shaping**: capture real SDK requests via WireGuard observation
  and stamp those compliance envelopes onto proxied requests, keeping you within
  provider terms of service.
- **MCP bridging**: add unsupported MCP features to any client:
  [sampling](https://modelcontextprotocol.io/specification/2025-11-25/client/sampling)
  via sentinel key detection,
  [server notifications](https://modelcontextprotocol.io/specification/2025-11-25/basic/index#notifications)
  bridged into the LLM context via ccproxy’s `/mcp` endpoint, and experimental
  [tasks](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/tasks)
  support.

> Feedback and contributions welcome —
> [open an issue](https://github.com/starbaser/ccproxy/issues) or submit a PR.

## Installation

```bash
# Recommended: uv tool
uv tool install claude-ccproxy

# Alternative: pip
pip install claude-ccproxy
```

## Quick Start

```bash
# Initialize config template at ~/.config/ccproxy/ccproxy.yaml
ccproxy init

# Start the inspector server (foreground)
ccproxy start
```

**SDK use**: point any OpenAI-compatible client at the reverse proxy listener:

```bash
export ANTHROPIC_BASE_URL=http://localhost:4000
claude -p "hello"
```

**Transparent capture**: run a command inside the WireGuard namespace jail (all
traffic intercepted):

```bash
ccproxy run --inspect -- claude -p "hello"
```

## Architecture

Traffic enters through one of two listeners, passes through a fixed three-stage
addon chain, and exits directly to the provider API.

```mermaid
flowchart TD
    subgraph Listeners
        RP["Reverse Proxy :4000"]
        WG["WireGuard CLI"]
    end
    RP --> Chain
    WG --> Chain
    subgraph Chain["Addon Chain"]
        IN["inbound<br/>DAG hooks"] --> TX["transform<br/>lightllm"] --> OUT["outbound<br/>DAG hooks"]
    end
    Chain --> API["Provider API"]
```

**Addon chain** (fixed order):
`ReadySignal → InspectorAddon → inbound DAG → transform → outbound DAG`

**lightllm** invokes LiteLLM’s `BaseConfig` transformation pipeline directly —
URL rewriting, auth signing, request/response format conversion — without the
proxy server, cost tracking, or callback machinery.

**SSE streaming**: `SseTransformer` handles cross-provider streaming by parsing
SSE events, transforming each chunk via LiteLLM’s per-provider
`ModelResponseIterator`, and re-serializing as OpenAI-format SSE.

## Configuration

`ccproxy init` writes a template to `~/.config/ccproxy/ccproxy.yaml`. Config is
also read from `$CCPROXY_CONFIG_DIR/ccproxy.yaml`.

```yaml
ccproxy:
  port: 4000

  # OAuth token sources: map provider names to shell commands or file paths.
  # Tokens are substituted when the sentinel key sk-ant-oat-ccproxy-{provider} is used.
  oat_sources:
    anthropic:
      command: "jq -r '.claudeAiOauth.accessToken' ~/.claude/.credentials.json"

  hooks:
    inbound:
      - ccproxy.hooks.forward_oauth
      - ccproxy.hooks.gemini_cli_compat
      - ccproxy.hooks.reroute_gemini
      - ccproxy.hooks.extract_session_id
    outbound:
      - ccproxy.hooks.inject_mcp_notifications
      - ccproxy.hooks.verbose_mode
      - ccproxy.hooks.shape

  inspector:
    transforms:
      - mode: passthrough
        match_host: cloudcode-pa.googleapis.com

      - match_path: /v1/messages
        mode: redirect
        dest_provider: anthropic
        dest_host: api.anthropic.com
        dest_path: /v1/messages
        dest_api_key_ref: anthropic

      - match_path: /v1/chat/completions
        match_model: gpt-4o
        mode: transform
        dest_provider: anthropic
        dest_model: claude-haiku-4-5-20251001
        dest_api_key_ref: anthropic
```

**Transform matching**: `match_host` (optional, checked against `pretty_host` +
Host header + X-Forwarded-Host), `match_path` (prefix), `match_model` (substring
in request body). First match wins.
Three modes: `redirect` (default — rewrite destination, preserve body),
`transform` (cross-format via lightllm), `passthrough` (forward unchanged).

**Hook config**: hooks in each stage list are topologically sorted by
`@hook(reads=..., writes=...)` dependency declarations and executed in parallel
DAG order. Hooks can be parameterized:

```yaml
hooks:
  outbound:
    - hook: ccproxy.hooks.some_hook
      params:
        key: value
```

Per-request overrides via header: `x-ccproxy-hooks: +hook_name,-other_hook`.

## Hook Pipeline

| Hook | Stage | Purpose |
| --- | --- | --- |
| `forward_oauth` | inbound | Sentinel key (`sk-ant-oat-ccproxy-{provider}`) substitution from `oat_sources` |
| `gemini_cli_compat` | inbound | Masquerades google-genai SDK user-agent as Gemini CLI for capacity allocation |
| `reroute_gemini` | inbound | Reroutes WireGuard flows targeting `generativelanguage.googleapis.com` to `cloudcode-pa.googleapis.com` with `v1internal` envelope |
| `extract_session_id` | inbound | Parses `metadata.user_id` → stores session_id on `flow.metadata` |
| `inject_mcp_notifications` | outbound | Injects buffered MCP terminal events as synthetic tool_use/tool_result |
| `verbose_mode` | outbound | Strips `redact-thinking-*` from `anthropic-beta` header |
| `shape` | outbound | Stamps captured compliance envelopes onto proxied requests |

## CLI Reference

```bash
ccproxy start                          # Start server (inspector mode, foreground)
ccproxy run [--inspect] -- <command>   # Run command with proxy env vars / WireGuard namespace jail
ccproxy status [--json]                # Show running state
ccproxy init [--force]                 # Initialize config in ~/.config/ccproxy/
ccproxy logs [-f] [-n LINES]           # View logs

# Flow inspection (all commands accept repeatable --jq filters)
ccproxy flows list [--json] [--jq FILTER]...     # List flow set
ccproxy flows dump [--jq FILTER]...              # Multi-page HAR of flow set
ccproxy flows diff [--jq FILTER]...              # Sliding-window diff across set
ccproxy flows compare [--jq FILTER]...           # Per-flow client-vs-forwarded diff
ccproxy flows clear [--all] [--jq FILTER]...     # Clear flow set (--all bypasses filters)
```

`ccproxy run` (without `--inspect`) sets `ANTHROPIC_BASE_URL`,
`OPENAI_BASE_URL`, and `OPENAI_API_BASE` in the subprocess environment and
routes traffic through the reverse proxy listener.

`ccproxy run --inspect` wraps the command in a rootless WireGuard network
namespace jail — all outbound traffic is transparently intercepted regardless of
SDK configuration.

## Inspecting Flows

All `flows` subcommands operate on a resolved **set** of flows.
The set is built by a pipeline:

```
GET /flows → config default_jq_filters → CLI --jq filters → final set
```

The `--jq` flag is repeatable.
Each filter must consume a JSON array and produce a JSON array.
Multiple filters chain via jq’s `|` operator:

```bash
# Only Anthropic API calls
ccproxy flows list --jq 'map(select(.request.pretty_host == "api.anthropic.com"))'

# Only POST /v1/messages
ccproxy flows list --jq 'map(select(.request.path | startswith("/v1/messages")))'

# Chain filters: Anthropic POSTs with 200 status
ccproxy flows list \
  --jq 'map(select(.request.pretty_host == "api.anthropic.com"))' \
  --jq 'map(select(.request.method == "POST"))' \
  --jq 'map(select(.response.status_code == 200))'
```

Config-level defaults apply before CLI filters, so you can set a baseline in
`ccproxy.yaml`:

```yaml
flows:
  default_jq_filters:
    - 'map(select(.request.path | startswith("/v1/messages")))'
```

### Listing flows

```bash
# Rich table (default)
ccproxy flows list

# Raw JSON
ccproxy flows list --json

# Filtered table
ccproxy flows list --jq 'map(select(.request.path | startswith("/v1/messages")))'
```

```
┏━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━━┓
┃ ID       ┃ Method  ┃  Code ┃ Host      ┃ Path      ┃ UA       ┃ Time         ┃
┡━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━━┩
│ 3c9c224c │ POST    │   200 │ api.anth… │ /v1/mess… │ claude-… │ 42 seconds   │
│          │         │       │           │           │ (extern… │ ago          │
│ 6cc161e9 │ POST    │   200 │ api.anth… │ /v1/mess… │ claude-… │ 29 seconds   │
│          │         │       │           │           │ (extern… │ ago          │
└──────────┴─────────┴───────┴───────────┴───────────┴──────────┴──────────────┘
```

### Diffing consecutive requests

`flows diff` performs a sliding-window unified diff over request bodies.
For a set `[f0, f1, f2]`, it produces diffs `f0→f1` and `f1→f2`. Requires at
least 2 flows.

```bash
ccproxy flows diff --jq 'map(select(.request.path | startswith("/v1/messages")))'
```

```diff
--- flow:3c9c224c
+++ flow:6cc161e9
@@ -26,7 +26,7 @@
         {
           "type": "text",
-          "text": "what's 2+2",
+          "text": "what's 3+3",
           "cache_control": {
```

### Comparing client vs forwarded requests

`flows compare` diffs the pre-pipeline client request against the post-pipeline
forwarded request for each flow.
This shows what ccproxy’s hook pipeline and lightllm transform actually changed.
Supports 1+ flows.

```bash
ccproxy flows compare --jq 'map(select(.request.path | startswith("/v1/messages")))'
```

When the pipeline rewrites the request (e.g. Anthropic → Gemini transform),
you’ll see URL changes and body diffs:

```
╭──────── URL change — abc12345 ────────╮
│ - https://api.anthropic.com/v1/messages│
│ + https://generativelanguage.googleapi…│
╰───────────────────────────────────────╯
╭──────── Body diff — abc12345 ─────────╮
│ --- client:abc12345                    │
│ +++ forwarded:abc12345                 │
│ @@ -1,5 +1,5 @@                       │
│ ...                                    │
╰───────────────────────────────────────╯
```

When no transform is applied (same-provider passthrough), the output confirms
the bodies are identical:

```
3c9c224c: request bodies are identical.
6cc161e9: request bodies are identical.
```

### Dumping HAR

`flows dump` exports the flow set as a multi-page HAR 1.2 file.
Each flow becomes one page with two entries:

| Entry | Content |
| --- | --- |
| `entries[2i]` | Forwarded request + upstream response |
| `entries[2i+1]` | Client request (pre-pipeline snapshot) + upstream response |

```bash
# Dump all flows to a HAR file (open in Chrome DevTools / Charles / Fiddler)
ccproxy flows dump > all.har

# Dump only LLM requests
ccproxy flows dump --jq 'map(select(.request.path | startswith("/v1/messages")))' > llm.har

# Query HAR with jq
ccproxy flows dump | jq '.log.pages | length'           # page count
ccproxy flows dump | jq '.log.entries[0].request.url'    # first forwarded URL
```

### Clearing flows

```bash
# Clear only matching flows (respects --jq filters)
ccproxy flows clear --jq 'map(select(.request.path | startswith("/v1/messages")))'
# => Cleared 2 flow(s).

# Clear everything (bypasses all filters)
ccproxy flows clear --all
```

## Development

```bash
git clone https://github.com/starbaser/ccproxy.git
cd ccproxy
direnv allow        # activates the nix devShell

just up             # start dev services (process-compose, detached, port 4001)
just down           # stop dev services
just test           # uv run pytest
just lint           # uv run ruff check .
just fmt            # uv run ruff format .
just typecheck      # uv run mypy src/ccproxy
```

The dev instance runs on port 4001 (production default: 4000). Inspector UI at
port 8083. Config and cert store at `.ccproxy/` inside the project directory.

## Troubleshooting

### Inspector prerequisites

The WireGuard namespace jail (`ccproxy run --inspect`) requires `slirp4netns`,
`wg`, `unshare`, `nsenter`, and `ip` to be available on `PATH`. On NixOS these
are provided by the devShell; on other systems install them via your package
manager.

### OAuth token errors

OAuth tokens are loaded at startup from `oat_sources`. If a token command fails
or returns an empty string, the sentinel key substitution is skipped and the raw
sentinel key is forwarded — which will be rejected by the provider.
Verify your token command works standalone:

```bash
jq -r '.claudeAiOauth.accessToken' ~/.claude/.credentials.json
```

Tokens are refreshed automatically (TTL-based every 30 min, immediate on 401).
Set `oat_sources` correctly and restart `ccproxy start` if tokens were stale at
startup.

### TLS certificate errors in `ccproxy run`

`ccproxy run` (without `--inspect`) does not intercept TLS. It only sets env
vars pointing at the reverse proxy HTTP listener.
If the target tool performs its own TLS verification against the upstream API,
no cert installation is needed.

`ccproxy run --inspect` intercepts all traffic including TLS. The mitmproxy CA
is combined with system CAs and injected via `SSL_CERT_FILE`,
`NODE_EXTRA_CA_CERTS`, `REQUESTS_CA_BUNDLE`, and `CURL_CA_BUNDLE` into the
subprocess environment automatically.

If a tool still fails certificate verification, ensure the mitmproxy CA
(`~/.config/ccproxy/mitmproxy-ca-cert.pem`) is trusted by the tool’s runtime.
