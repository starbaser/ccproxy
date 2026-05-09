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
| Capturing shapes (`ccproxy flows shape`) | `ccproxy run --inspect` (a real CLI run through the WireGuard jail produces the flow you'll capture) |
| Quick debugging of SDK integration | `ccproxy run` |
| Full traffic audit | `ccproxy run --inspect` |

## Understanding flows

### Client request vs forwarded request

Every flow has two views:

**Client request** -- what the client actually sent, captured before any hooks run. This is the ground truth of client intent: original URL, original headers (with sentinel keys, without injected OAuth), original body format.

**Forwarded request** -- what was sent to the upstream provider after the full pipeline ran. May have a different host, different headers (OAuth token injected, beta headers added, shaping headers stamped), different body format (OpenAI -> Anthropic), wrapped body envelope, and injected system prompt.

### The pipeline

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
  gemini_cli:               wrap Gemini bodies in v1internal envelope, rewrite to cloudcode-pa
  inject_mcp_notifications: buffer MCP events into messages
  verbose_mode:             strip redact-thinking from beta header
  shape:                    replay captured {provider}.mflow (identity headers, billing, system prefix)
  commitbee_compat:         last-mile compatibility shim
  │
  ▼
OAuthAddon  (response side: 401-detect -> resolve_oauth_token -> replay)
  │
  ▼
GeminiAddon (response side: capacity fallback + cloudcode-pa envelope unwrap)
  │
  ▼
Forwarded request -> Provider API
```

### Identifying flow state

| Indicator | Meaning |
|-----------|---------|
| `flow.metadata["ccproxy.oauth_injected"]` (or `x-ccproxy-oauth-injected: 1` request header) | OAuth token was injected by `forward_oauth` |
| `flow.metadata["ccproxy.oauth_provider"] == "X"` | Sentinel key resolved to provider X |
| Host changed (client vs forwarded) | Transform or redirect rewrote the destination |
| Body identity headers present on forwarded but not client | `shape` hook replayed a captured shape |
| Body wrapped in `{model, project, request}` envelope | `gemini_cli` hook wrapped the body for cloudcode-pa |
| Different body keys (messages vs contents) | Cross-provider format transformation via lightllm |
| `flow.response` replaced after a 429/503 | `GeminiAddon._try_fallback_models` succeeded |

## Inspecting flows

### CLI commands

All `ccproxy flows` subcommands operate on a resolved flow set. The `--jq` flag is repeatable; each filter consumes and produces a JSON array. Default filters from `flows.default_jq_filters` config apply first.

```bash
ccproxy flows list                        # Rich table of recent flows
ccproxy flows list --json                 # Raw JSON array
ccproxy flows list --jq 'map(select(.request.pretty_host == "api.anthropic.com"))'

# Multi-page HAR export (entries[2i] = forwarded+response, entries[2i+1] = client request+response)
ccproxy flows dump > all.har                       # Open in Chrome DevTools / Charles / Fiddler
ccproxy flows dump --jq 'map(.[-1])' > latest.har  # Just the most recent flow

# Sliding-window unified diff across consecutive request bodies in the set
ccproxy flows diff

# Per-flow client-vs-forwarded diff (URL changes + body diff)
ccproxy flows compare
ccproxy flows compare --jq 'map(.[-1])'   # Just the latest flow

# Clear (respects --jq filters; --all bypasses them)
ccproxy flows clear --jq 'map(select(.response.status_code >= 400))'
ccproxy flows clear --all

# Capture a shape from a flow (must match the provider's capture.path_pattern)
ccproxy flows shape --provider anthropic
```

### MCP server

For programmatic access from MCP-aware clients (Claude Code with the
`ccproxy_mcp` server configured), the same surface is exposed as MCP tools:
`list_flows`, `get_flow`, `dump_har`, `get_request_body`, `get_response_body`,
`diff_flows`, `compare_flow`, `clear_flows`, `capture_shape`, `list_shapes`,
`list_conversations`, `list_models`. Plus resources `proxy://requests` and
`proxy://status`. Launch via the `ccproxy_mcp` console script.

## The shape replay system

### What it does

The shape system replays a captured `mitmproxy.http.HTTPFlow` (a real, known-good request from the target SDK) onto outbound flows that lack the provider's identity envelope. It bridges the gap between a bare SDK call and what the provider API requires for identity verification.

**What gets stamped:**

- Identity headers (e.g. `anthropic-beta`, `anthropic-version`, `user-agent`, `x-stainless-*`)
- Anthropic billing header (re-signed per request via the `regenerate_billing_header` shape inner-DAG hook)
- Body envelope fields (e.g. `metadata`, `user_prompt_id`) — regenerated per request
- System prompt (per `merge_strategies.system`, e.g. `prepend_shape:2` keeps the first 2 shape blocks then appends incoming)
- Cache breakpoint normalization (caching hooks strip excess `cache_control` and re-insert one at the optimal position)

For Gemini, the cloudcode-pa body wrapping (`{model, project, request: {...}}`) is applied by the separate `gemini_cli` outbound hook, not by shape replay.

### Capturing a shape

1. Start ccproxy: `just up` (or `ccproxy start`)
2. Run the target CLI through WireGuard so a real, valid flow is captured:

   ```bash
   ccproxy run --inspect -- claude -p "shape capture"
   ```

3. Capture the most recent matching flow as the provider's shape:

   ```bash
   ccproxy flows shape --provider anthropic
   ```

4. The shape is persisted as `~/.config/ccproxy/shaping/shapes/anthropic.mflow` and immediately active for reverse proxy and OAuth-injected flows.

Re-capture whenever the target CLI version changes — Anthropic identity headers and the system prompt prefix evolve with releases.

### How it fires

The `shape` outbound hook only fires when:

1. The flow came through the **reverse proxy** OR has the `ccproxy.oauth_injected` flag (so WireGuard passthrough flows aren't reshaped)
2. The flow has a `TransformMeta` (matched a transform/redirect rule, or sentinel-key resolved to a Provider)

### Configuration

```yaml
shaping:
  enabled: true                                       # master switch
  shapes_dir: ~/.config/ccproxy/shaping/shapes        # where .mflow files live
  providers:
    anthropic:
      content_fields: [model, messages, tools, system, max_tokens, ...]
      merge_strategies:
        system: "prepend_shape:2"                     # keep first 2 shape system blocks
      shape_hooks:
        - ccproxy.shaping.regenerate                  # re-roll user_prompt_id, session_id, billing
        - hook: ccproxy.shaping.caching.strip
          params:
            paths: ["system.*.cache_control"]
        - hook: ccproxy.shaping.caching.insert
          params:
            path: "system.-1.cache_control"
            value: {type: ephemeral}
      preserve_headers: [authorization, x-api-key, x-goog-api-key, host]
      strip_headers: [authorization, x-api-key, x-goog-api-key, content-length, host, transfer-encoding, connection]
      capture:
        path_pattern: "^/v1/messages"
      billing:
        salt: "${CCPROXY_BILLING_SALT}"               # required for Anthropic
        seed: "${CCPROXY_BILLING_SEED}"
```

See [`docs/shaping.md`](../../docs/shaping.md) for the canonical reference.

## Diagnosing flow issues

```
Problem?
│
├─ Provider returns auth errors (401/403)
│  ▶ Check: ccproxy flows compare --jq 'map(.[-1])' — what auth header reached upstream?
│  ▶ Check: ccproxy.oauth_injected metadata / x-ccproxy-oauth-injected — did forward_oauth run?
│  ▶ Check: providers[name].auth — does the token source resolve manually?
│  ▶ Check: sentinel key format — sk-ant-oat-ccproxy-{provider} matches a providers entry
│  ▶ Check: ccproxy logs -f | grep -E 'OAuth|refresh' — did OAuthAddon attempt a refresh+replay?
│
├─ Request not being transformed
│  ▶ Check: ccproxy flows list — is the flow captured?
│  ▶ Check: inspector.transforms rules — does match_host/match_path/match_model match?
│  ▶ Check: ccproxy flows compare --jq 'map(.[-1])' — what URL changes were applied?
│
├─ Shape not applying (Anthropic 401/400)
│  ▶ Check: ls ~/.config/ccproxy/shaping/shapes/anthropic.mflow — does the shape file exist?
│  ▶ Check: ccproxy logs -f | grep -E 'shape|Applied' — did the shape hook fire?
│  ▶ Check: flow mode — reverse proxy or oauth-injected? (shape_guard skips raw WireGuard)
│  ▶ Check: TransformMeta — did the flow match a transform/redirect rule (or sentinel-key resolve)?
│  ▶ Check: ccproxy.yaml — is the `shape` hook in `hooks.outbound`?
│
├─ Body format wrong / API rejection
│  ▶ Run: ccproxy flows compare --jq 'map(.[-1])' — see client vs forwarded body diff
│  ▶ Check: transform mode — "transform" (full rewrite via lightllm) vs "redirect" (preserve body)
│  ▶ Check: gemini_cli hook — for cloudcode-pa flows, did the body get wrapped in {model, project, request}?
│
└─ System prompt issues
   ▶ Run: ccproxy flows compare --jq 'map(.[-1])' — was the shape's system block prepended?
   ▶ Check: merge_strategies.system in shaping config — usually `prepend_shape:N`
   ▶ Check: client system format — list of blocks vs string vs absent (affects merging)
```

## Reference files

- [reference/flow-api-reference.md](reference/flow-api-reference.md) — mitmweb REST API endpoints, flow data model, content views, authentication
- [docs/inspect.md](../../docs/inspect.md) — Inspector stack architecture
- [docs/shaping.md](../../docs/shaping.md) — Request shaping system
