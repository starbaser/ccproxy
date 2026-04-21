---
name: using-ccproxy-inspector
description: >-
  Operates the ccproxy inspector MITM system for intercepting, inspecting, and
  transforming LLM API traffic. Covers running CLI tools through the inspector
  (Claude Code, Aider, any LLM harness), inspecting flows with client-vs-forwarded
  request comparison, understanding the inbound/transform/outbound pipeline,
  capturing and checking shaping profiles, and diagnosing flow issues. Use when
  running CLI applications through ccproxy, inspecting intercepted flows, comparing
  client request vs forwarded request, checking shaping profile status, using
  WireGuard namespace jail, or debugging the hook pipeline.
---

# Using the ccproxy Inspector

The inspector intercepts LLM API traffic via mitmproxy, routing it through a three-stage hook pipeline (inbound -> transform -> outbound) before forwarding to the provider. It captures pre-pipeline snapshots, enabling comparison of what the client sent vs what the provider received.

**Prerequisite**: ccproxy must be configured and running. See the `using-ccproxy-api` skill for authentication, sentinel keys, and `ccproxy.yaml` setup.

## Verify ccproxy is running

```bash
ccproxy status              # Human-readable panel
ccproxy status --json       # Machine-readable (includes URLs, ports)
ccproxy status --proxy      # Exit 0 if proxy is up, 1 if down
ccproxy status --inspect    # Exit 0 if inspector UI is up, 2 if down
```

## Running CLI tools through the inspector

### Mode 1: Reverse proxy (`ccproxy run`)

Sets SDK environment variables to route traffic through ccproxy's reverse proxy listener.

```bash
ccproxy run -- claude              # Claude Code
ccproxy run -- aider               # Aider
ccproxy run -- python my_agent.py  # Any Python script using Anthropic/OpenAI SDK
ccproxy run -- curl http://localhost:4000/v1/messages ...
```

Sets `ANTHROPIC_BASE_URL`, `OPENAI_BASE_URL`, `OPENAI_API_BASE` to `http://{host}:{port}`. The CLI tool must respect these environment variables.

**Use when**: the tool uses an SDK with configurable `base_url` and you want lightweight interception.

### Mode 2: WireGuard namespace jail (`ccproxy run --inspect`)

Creates a rootless Linux network namespace where ALL outbound traffic routes through a WireGuard tunnel into mitmproxy. No `base_url` configuration needed -- every HTTP/HTTPS connection is intercepted.

```bash
ccproxy run --inspect -- claude
ccproxy run --inspect -- aider --model claude-sonnet-4-5-20250929
ccproxy run --inspect -- python my_agent.py
```

Injects a combined CA bundle (mitmproxy CA + system CAs) via `SSL_CERT_FILE`, `NODE_EXTRA_CA_CERTS`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`.

**Use when**: the tool doesn't support `base_url`, you need full traffic capture, or you want to observe reference traffic for shape learning.

### When to use which

| Scenario | Mode |
|----------|------|
| SDK client with configurable base_url | `ccproxy run` |
| Tool that hardcodes API endpoints | `ccproxy run --inspect` |
| Capturing shaping profiles | `ccproxy run --inspect` (WireGuard flows are always observed) |
| Quick debugging of SDK integration | `ccproxy run` |
| Full traffic audit | `ccproxy run --inspect` |

## Understanding flows

### Client request vs forwarded request

Every flow has two views:

**Client request** -- what the client actually sent, captured before any hooks run. This is the ground truth of client intent: original URL, original headers (with sentinel keys, without injected OAuth), original body format.

**Forwarded request** -- what was sent to the upstream provider after the full pipeline ran. May have a different host, different headers (OAuth token injected, beta headers added, shaping headers stamped), different body format (OpenAI -> Anthropic), wrapped body envelope, and injected system prompt.

### The three-stage pipeline

```
Client request (captured as ClientRequest snapshot)
  │
  ▼
Inbound hooks (DAG order)
  forward_oauth:      sentinel key -> real OAuth token
  extract_session_id: metadata.user_id -> flow.metadata
  │
  ▼
Transform (first matching rule wins)
  passthrough: forward unchanged
  redirect:    rewrite host/path/auth, keep body format
  transform:   full cross-provider body rewrite via lightllm
  │
  ▼
Outbound hooks (DAG order)
  inject_mcp_notifications: buffer MCP events into messages
  verbose_mode:             strip redact-thinking from beta header
  apply_shaping:            stamp learned headers/body/system
  │
  ▼
Forwarded request -> Provider API
```

### Identifying flow state

| Indicator | Meaning |
|-----------|---------|
| `x-ccproxy-oauth-injected: 1` header | OAuth token was injected by forward_oauth |
| Host changed (client vs forwarded) | Transform or redirect rewrote the destination |
| Body has `system` field not in client request | Shaping injected system prompt |
| Body wrapped in `request` field | Shaping applied body_wrapper (cloudcode-pa) |
| Different body keys (messages vs contents) | Cross-provider format transformation |

## Inspecting flows

### CLI commands

```bash
ccproxy flows list                        # Table of all flows
ccproxy flows list --filter "anthropic"   # Filter by host+path regex
ccproxy flows list --json                 # Raw JSON array

# `dump` emits a 1-page / 2-entry HAR 1.2 file for a single flow:
#   entries[0] = [fwdreq, fwdres]  real flow (forwarded request + upstream response)
#   entries[1] = [clireq, fwdres]  clone with .request from ClientRequest snapshot
ccproxy flows dump a1b2c3d4                                 # Write HAR to stdout
ccproxy flows dump a1b2c3d4 | jq '.log.entries[0].request.url'   # Forwarded URL
ccproxy flows dump a1b2c3d4 | jq '.log.entries[1].request.url'   # Pre-pipeline URL
ccproxy flows dump a1b2c3d4 | jq '.log.entries[0].response.status'
ccproxy flows dump a1b2c3d4 > /tmp/flow.har                 # Open in Chrome DevTools

ccproxy flows diff a1b2c3d4 e5f6a7b8     # Unified diff of two request bodies

ccproxy flows clear                       # Clear all captured flows
```

### Helper scripts

The `scripts/` directory contains Python scripts that import ccproxy's `MitmwebClient` directly for richer, machine-readable output.

**List flows with filtering:**
```bash
uv run python scripts/list_flows.py                          # JSON output (default)
uv run python scripts/list_flows.py --table                  # Rich table
uv run python scripts/list_flows.py --provider anthropic     # Filter by provider
uv run python scripts/list_flows.py --model claude --latest 5  # Filter by model
uv run python scripts/list_flows.py --status 401             # Find auth failures
```

**Inspect a single flow (client vs forwarded diff):**
```bash
uv run python scripts/inspect_flow.py a1b2c3d4               # Rich panels + change summary
uv run python scripts/inspect_flow.py a1b2c3d4 --json        # Structured JSON with diff
uv run python scripts/inspect_flow.py a1b2c3d4 --with-response  # Include response body
```

The `inspect_flow.py` output includes a change summary: URL rewrites, headers added/removed, body format transforms, system prompt injection, OAuth injection, body wrapping.

**Check shaping status:**
```bash
uv run python scripts/shaping_status.py                   # Profile + accumulator tables
uv run python scripts/shaping_status.py --provider anthropic  # Detailed profile contents
uv run python scripts/shaping_status.py --shape-status    # Is the v0 shape active?
uv run python scripts/shaping_status.py --json            # Structured JSON
```

All scripts run from the ccproxy project root using `uv run python scripts/...` and resolve the mitmweb auth token from config automatically. They exit with actionable error messages when ccproxy is not running.

## The shaping system

### What it does

The shaping system passively learns the "shaping contract" from legitimate CLI traffic (WireGuard-observed) and stamps it onto non-compliant SDK requests (reverse proxy). It bridges the gap between a bare SDK call and what the provider API requires.

**What gets stamped:**
- Missing headers (e.g. `anthropic-beta`, `anthropic-version`, `user-agent`)
- Body envelope fields (e.g. `metadata`, `user_prompt_id`)
- System prompt (prepended as content blocks, only if absent or a plain string)
- Body wrapping (e.g. cloudcode-pa's `{model: X, request: {<body>}}` pattern)
- Session metadata (synthesized `device_id` + `account_uuid` + fresh `session_id`)

### Capturing a shaping profile

1. Start ccproxy: `just up` (or `ccproxy start`)
2. Run a CLI tool through WireGuard:
   ```bash
   ccproxy run --inspect -- claude
   ```
3. Make at least 3 requests (configurable via `shaping.min_observations`)
4. Check progress:
   ```bash
   uv run python scripts/shaping_status.py --shape-status
   ```
5. Once finalized, the profile is persisted to `{config_dir}/shaping_profiles.json` and immediately active for reverse proxy flows

### How it fires

The `apply_shaping` outbound hook only fires when:
1. The flow came through the **reverse proxy** (not WireGuard)
2. The flow has a `TransformMeta` (matched a transform/redirect rule)

WireGuard flows are reference traffic (observed, not modified). Reverse proxy flows are consumers (modified, not observed).

### Anthropic v0 shape

On first startup, an initial shape is created from hardcoded constants (`anthropic-beta` headers, system prompt prefix). It provides baseline shaping before any real observations. It is superseded once a learned profile finalizes (the store returns the most recently updated profile).

Check shape status: `uv run python scripts/shaping_status.py --shape-status`

### Configuration

```yaml
shaping:
  enabled: true           # master switch
  min_observations: 3     # observations before finalization
  reference_user_agents: []  # extra UA patterns for observation
  seed_anthropic: true    # bootstrap Anthropic v0 shape
```

## Diagnosing flow issues

```
Problem?
│
├─ Provider returns auth errors (401/403)
│  ▶ Check: ccproxy flows dump <id> | jq '.log.entries[0].request.headers' — is Authorization header present?
│  ▶ Check: x-ccproxy-oauth-injected header — did forward_oauth run?
│  ▶ Check: oat_sources config — is the token source valid?
│  ▶ Check: sentinel key format — sk-ant-oat-ccproxy-{provider}
│
├─ Request not being transformed
│  ▶ Check: ccproxy flows list — is the flow captured?
│  ▶ Check: transform rules — does match_host/match_path/match_model match?
│  ▶ Check: ccproxy flows dump <id> | jq '.log.entries[1].request.url' — what did the client send (pre-pipeline)?
│
├─ Shaping not applying
│  ▶ Check: shaping_status.py — is a profile finalized?
│  ▶ Check: flow mode — is it a reverse proxy flow? (not WireGuard)
│  ▶ Check: TransformMeta — did the flow match a transform rule?
│  ▶ Check: ua_hint — does oat_sources[provider].user_agent match the profile?
│
├─ Body format wrong / API rejection
│  ▶ Run: inspect_flow.py <id> --json — compare client vs forwarded body
│  ▶ Check: transform mode — is it "transform" (full rewrite) or "redirect" (passthrough body)?
│  ▶ Check: body_wrapper — is shaping wrapping when it shouldn't (or not wrapping when it should)?
│
└─ System prompt issues
   ▶ Check: inspect_flow.py <id> — was system prompt injected?
   ▶ Check: client system format — list (skip) vs string (prepend) vs absent (set)
   ▶ Check: shaping_status.py --provider X — what system prompt is in the profile?
```

## Reference files

- [reference/flow-api-reference.md](reference/flow-api-reference.md) — mitmweb REST API endpoints, flow data model, content views, authentication
- [docs/inspector-and-shaping.md](../../docs/inspector-and-shaping.md) — Full architectural documentation of the inspector and shaping systems
