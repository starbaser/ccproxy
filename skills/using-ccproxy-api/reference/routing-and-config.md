# Model Routing & Configuration

## Contents

- [How routing works](#how-routing-works)
- [ccproxy.yaml configuration](#ccproxyyaml-configuration)
- [Transform rules](#transform-rules)
- [OAuth token management](#oauth-token-management)

---

## How routing works

Request flow through the three-stage addon chain:

```
Client request (model: "claude-sonnet-4-5-20250929")
  │
  ▼
ccproxy_inbound (DAG hooks)
  forward_oauth: Detects sentinel key, substitutes real OAuth token.
  extract_session_id: Parses session_id from metadata.user_id.
  │
  ▼
ccproxy_transform (lightllm dispatch)
  Matches request against inspector.transforms rules.
  First match wins. Rewrites host/path/body to dest_provider format.
  Unmatched flows pass through unchanged.
  │
  ▼
ccproxy_outbound (DAG hooks)
  add_beta_headers: Injects anthropic-beta headers (OAuth only).
  inject_claude_code_identity: Prepends system message prefix.
  │
  ▼
Provider API directly
```

---

## ccproxy.yaml configuration

All configuration lives in a single file: `~/.ccproxy/ccproxy.yaml` (or `$CCPROXY_CONFIG_DIR/ccproxy.yaml`).

### Full OAuth configuration

```yaml
ccproxy:
  host: 127.0.0.1
  port: 4000
  debug: true

  oat_sources:
    anthropic: "jq -r '.claudeAiOauth.accessToken' ~/.claude/.credentials.json"

  hooks:
    inbound:
      - ccproxy.hooks.forward_oauth
      - ccproxy.hooks.extract_session_id
    outbound:
      - ccproxy.hooks.add_beta_headers
      - ccproxy.hooks.inject_claude_code_identity

  inspector:
    port: 8083
    transforms:
      - match_host: cloudcode-pa.googleapis.com
        mode: passthrough
      - match_path: /v1/chat/completions
        match_model: gpt-4o
        dest_provider: anthropic
        dest_model: claude-haiku-4-5-20251001
        dest_api_key_ref: anthropic
```

### Hook parameters

Hooks accept params via dict form:

```yaml
hooks:
  inbound:
    # Simple (no params)
    - ccproxy.hooks.forward_oauth

    # With params
    - hook: ccproxy.hooks.some_hook
      params:
        key: value
```

---

## Transform rules

Transform rules are configured under `inspector.transforms`. Each rule is a `TransformRoute` with these fields:

| Field | Type | Description |
|-------|------|-------------|
| `mode` | `transform` \| `passthrough` | Default: `transform`. Passthrough forwards unchanged. |
| `match_host` | `str?` | Hostname to match (checked against `pretty_host` + `Host` header). |
| `match_path` | `str` | Path prefix to match (default: `/`). |
| `match_model` | `str?` | Model name substring to match in the request body. |
| `dest_provider` | `str` | Provider name for lightllm dispatch (e.g. `anthropic`, `gemini`). |
| `dest_model` | `str` | Model name for lightllm dispatch. |
| `dest_api_key_ref` | `str?` | Provider name in `oat_sources` for credential lookup. |

### Examples

```yaml
inspector:
  transforms:
    # Gemini passthrough (don't transform)
    - mode: passthrough
      match_host: cloudcode-pa.googleapis.com

    # Route OpenAI requests to Anthropic
    - match_path: /v1/chat/completions
      match_model: gpt-4o
      dest_provider: anthropic
      dest_model: claude-haiku-4-5-20251001
      dest_api_key_ref: anthropic

    # Route all /v1/messages to a different Anthropic model
    - match_path: /v1/messages
      match_model: claude-sonnet
      dest_provider: anthropic
      dest_model: claude-opus-4-5-20251101
      dest_api_key_ref: anthropic
```

First match wins. Unmatched flows pass through unchanged to the original destination.

---

## OAuth token management

### oat_sources configuration

**Simple form** (command string):
```yaml
oat_sources:
  anthropic: "jq -r '.claudeAiOauth.accessToken' ~/.claude/.credentials.json"
```

**Extended form** (with user_agent and destinations):
```yaml
oat_sources:
  anthropic:
    command: "jq -r '.claudeAiOauth.accessToken' ~/.claude/.credentials.json"
    user_agent: "ClaudeCode/1.0"
    destinations: ["api.anthropic.com"]

  zai:
    command: "jq -r '.accessToken' ~/.zai/credentials.json"
    user_agent: "MyApp/1.0"
    destinations: ["api.z.ai", "z.ai"]
```

Fields:
- `command` (required) — shell command that outputs the token
- `user_agent` (optional) — custom User-Agent header for this provider
- `destinations` (optional) — URL patterns for auto-matching api_base to provider

### Token refresh

Two automatic refresh triggers:
1. **TTL-based**: Background task every 30 minutes, refreshes at `oauth_ttl * (1 - oauth_refresh_buffer)`
2. **401-triggered**: Immediate refresh on authentication error, retries the failed request once

Default: 8h TTL, 10% buffer = refresh at ~7.2 hours.

### Destination matching

When `forward_oauth` and `add_beta_headers` need to determine which provider a request targets, they use this priority:

1. `destinations` patterns in `oat_sources` (checks if host contains pattern)
2. Model name fallback ("claude" -> anthropic, "gpt" -> openai, "gemini" -> gemini)
