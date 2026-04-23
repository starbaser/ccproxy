# Usage Guide

## Getting Started

Install and initialize:

```bash
uv tool install claude-ccproxy
ccproxy init
```

Start the server:

```bash
ccproxy start
```

This launches mitmweb in-process with two listeners: a reverse proxy (default port 4000) and a WireGuard server for namespace-jailed subprocesses. The inspector UI is available at `http://localhost:8083`.

---

## Routing Traffic

### Reverse Proxy (SDK clients)

Point any OpenAI-compatible or Anthropic SDK client at the reverse proxy listener using a sentinel key:

```bash
export ANTHROPIC_BASE_URL=http://localhost:4000
export ANTHROPIC_API_KEY=sk-ant-oat-ccproxy-anthropic
claude -p "hello"
```

The sentinel key `sk-ant-oat-ccproxy-{provider}` triggers automatic OAuth token substitution from `oat_sources` in your config. No raw API keys needed.

```python
from anthropic import Anthropic

client = Anthropic(
    base_url="http://localhost:4000",
    api_key="sk-ant-oat-ccproxy-anthropic",
)
message = client.messages.create(
    model="claude-sonnet-4-5-20250929",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Hello"}],
)
```

### WireGuard Namespace Jail (transparent capture)

Wrap any command in a rootless network namespace where all traffic is captured transparently — no proxy env vars, no certificate injection, no client modifications:

```bash
ccproxy run --inspect -- claude -p "hello"
```

This creates an isolated user+net namespace routed through mitmproxy's WireGuard listener. All outbound traffic from the subprocess is intercepted.

### Reverse Proxy Without Inspection

Route traffic through the reverse proxy via environment variables without WireGuard:

```bash
ccproxy run -- claude -p "hello"
```

Sets `ANTHROPIC_BASE_URL`, `OPENAI_BASE_URL`, and `OPENAI_API_BASE` in the subprocess environment.

---

## Transform Modes

Transform rules in `inspector.transforms` control how requests are routed. Three modes, first match wins:

### Redirect (default)

Rewrites the destination host while preserving the request body. Same-format routing:

```yaml
inspector:
  transforms:
    - match_path: /v1/messages
      mode: redirect
      dest_provider: anthropic
      dest_host: api.anthropic.com
      dest_path: /v1/messages
      dest_api_key_ref: anthropic
```

### Transform

Cross-format rewrite via lightllm. Converts both the destination and the request/response body:

```yaml
    - match_path: /v1/chat/completions
      match_model: gpt-4o
      mode: transform
      dest_provider: anthropic
      dest_model: claude-haiku-4-5-20251001
      dest_api_key_ref: anthropic
```

### Passthrough

Forward to the original destination unchanged:

```yaml
    - mode: passthrough
      match_host: cloudcode-pa.googleapis.com
```

---

## Inspecting Flows

All `flows` subcommands operate on a filtered set of flows. The `--jq` flag is repeatable and each filter consumes/produces a JSON array.

### List flows

```bash
ccproxy flows list
ccproxy flows list --json
ccproxy flows list --jq 'map(select(.request.path | startswith("/v1/messages")))'
```

### Compare client vs forwarded

See what the hook pipeline and transforms changed on each request:

```bash
ccproxy flows compare
```

Shows URL changes and body diffs for each flow. For transform-mode flows, also diffs provider-response vs client-response.

### Diff consecutive requests

Sliding-window diff over request bodies across the flow set (requires >= 2 flows):

```bash
ccproxy flows diff
```

### Export HAR

```bash
ccproxy flows dump > all.har
```

Multi-page HAR 1.2 — two entries per flow: `entries[2i]` = forwarded request + provider response, `entries[2i+1]` = client request + client response. Opens in Chrome DevTools, Charles, or Fiddler.

### Clear flows

```bash
ccproxy flows clear --jq 'map(select(.request.path | startswith("/v1/messages")))'
ccproxy flows clear --all
```

### Default filters

Set a baseline filter in config so all subcommands pre-filter:

```yaml
flows:
  default_jq_filters:
    - 'map(select(.request.path | startswith("/v1/messages")))'
```

---

## Request Shaping

Shaping stamps captured compliance envelopes onto proxied requests. When ccproxy transforms a request (e.g. OpenAI format → Anthropic), the outbound payload is API-correct but may lack compliance metadata: beta headers, user-agent fingerprints, system prompt preambles, client identity markers.

A **shape** is a captured real request from the target SDK carrying the full compliance envelope.

### Capture a shape

```bash
# 1. Run real traffic through the inspector
ccproxy run --inspect -- claude -p "hello, this is a shape capture"

# 2. Verify the flow
ccproxy flows list
ccproxy flows compare

# 3. Capture
ccproxy flows shape --provider anthropic
```

### How shaping works

At runtime, the `shape` hook (outbound pipeline):

1. Picks the most recent shape for the destination provider
2. Deep-copies it as a working template
3. Strips configured headers (auth, transport)
4. Injects content fields from the incoming request per merge strategy
5. Runs shape hooks (UUID re-rolls, session ID regeneration)
6. Stamps the result onto the outbound flow

The identity/content boundary is declared per-provider:

```yaml
shaping:
  enabled: true
  providers:
    anthropic:
      content_fields: [model, messages, tools, tool_choice, system, thinking,
                       context_management, stream, max_tokens, temperature,
                       top_p, top_k, stop_sequences]
      merge_strategies:
        system: "prepend_shape:2"
```

Everything NOT in `content_fields` persists from the shape — compliance headers, beta flags, client identity.

### Merge strategies

| Strategy | Behavior |
|---|---|
| `replace` (default) | Incoming value replaces shape value |
| `prepend_shape[:N]` | Shape value prepended: `[*shape, *incoming]`. `:N` slices shape to first N elements |
| `append_shape[:N]` | Incoming first: `[*incoming, *shape]` |
| `drop` | Field removed entirely |

### Shape maintenance

Re-capture when the target SDK updates beta headers or system prompt structure:

```bash
ccproxy run --inspect -- claude -p "shape refresh"
ccproxy flows shape --provider anthropic
```

See [shaping.md](shaping.md) for the full reference.

---

## Hook Pipeline

Hooks run in two stages: **inbound** (before transform) and **outbound** (after transform). Hooks are DAG-ordered by `@hook(reads=..., writes=...)` declarations.

### Default hooks

**Inbound:**
| Hook | Purpose |
|---|---|
| `forward_oauth` | Sentinel key substitution from `oat_sources` |
| `gemini_cli_compat` | Masquerades google-genai SDK as Gemini CLI |
| `reroute_gemini` | Reroutes `generativelanguage.googleapis.com` to `cloudcode-pa` with `v1internal` envelope |
| `extract_session_id` | Stores `metadata.user_id` on flow metadata |

**Outbound:**
| Hook | Purpose |
|---|---|
| `inject_mcp_notifications` | Injects buffered MCP events as tool_use/tool_result pairs |
| `verbose_mode` | Strips `redact-thinking-*` from `anthropic-beta` |
| `shape` | Applies captured compliance envelopes |

### Per-request overrides

Force-run or force-skip hooks via header:

```
x-ccproxy-hooks: +inject_mcp_notifications,-verbose_mode
```

### Custom hooks

Write a hook with the `@hook` decorator:

```python
from ccproxy.pipeline.context import Context
from ccproxy.pipeline.hook import hook

@hook(reads=["messages"], writes=["messages"])
def my_hook(ctx: Context, params: dict) -> Context:
    # Modify ctx.messages, ctx.system, ctx.headers, etc.
    return ctx
```

Register in config:

```yaml
hooks:
  outbound:
    - mypackage.my_hook
```

Parameterized hooks use a Pydantic model:

```yaml
hooks:
  outbound:
    - hook: mypackage.my_hook
      params:
        key: value
```

---

## OAuth Configuration

### Token sources

Map provider names to shell commands or file paths:

```yaml
oat_sources:
  anthropic:
    command: "jq -r '.claudeAiOauth.accessToken' ~/.claude/.credentials.json"
    destinations: ["api.anthropic.com"]
  gemini:
    command: "jq -r '.access_token' ~/.gemini/oauth_creds.json"
    destinations: ["cloudcode-pa.googleapis.com"]
    user_agent: "GeminiCLI"
```

### Token refresh

Tokens are cached in memory. On 401, ccproxy re-runs the command. If the new token differs, the request is retried automatically.

### Sentinel keys

Any SDK client can use `sk-ant-oat-ccproxy-{provider}` as an API key. The `forward_oauth` hook substitutes the real token at runtime.

---

## Smoke Test

Verify the full stack — namespace, TLS interception, hooks, transform, upstream, streaming:

```bash
ccproxy run --inspect -- claude --model haiku -p "what's 2+2"
```

Check what happened:

```bash
ccproxy flows list
ccproxy flows compare
```
