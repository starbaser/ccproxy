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
  inject_mcp_notifications: Injects buffered MCP events.
  verbose_mode: Strips redact-thinking from beta header.
  apply_compliance: Stamps learned headers, body fields, system prompt.
  │
  ▼
Provider API directly
```

---

## ccproxy.yaml configuration

All configuration lives in a single file: `~/.config/ccproxy/ccproxy.yaml` (or `$CCPROXY_CONFIG_DIR/ccproxy.yaml`).

### Full OAuth configuration

```yaml
ccproxy:
  host: 127.0.0.1
  port: 4000
  debug: true

  oat_sources:
    anthropic:
      command: "jq -r '.claudeAiOauth.accessToken' ~/.claude/.credentials.json"
      user_agent: "claude-code"
      destinations: ["api.anthropic.com"]

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
| `mode` | `redirect` \| `transform` \| `passthrough` | Default: `redirect`. Redirect rewrites host/auth only. Transform rewrites body format. Passthrough forwards unchanged. |
| `match_host` | `str?` | Hostname to match (checked against `pretty_host` + `Host` header). |
| `match_path` | `str` | Path prefix to match (default: `/`). |
| `match_model` | `str?` | Model name substring to match in the request body. |
| `dest_provider` | `str` | Provider name for lightllm dispatch (e.g. `anthropic`, `gemini`). |
| `dest_model` | `str` | Model name for lightllm dispatch. |
| `dest_host` | `str?` | Target hostname (redirect mode). |
| `dest_path` | `str?` | Override path (redirect mode). |
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

On HTTP 401 with `x-ccproxy-oauth-injected: 1`, the inspector addon calls `refresh_oauth_token(provider)` to re-resolve the credential source. If the token changed, the request is retried with the fresh token. If unchanged, the error propagates (credential is truly stale).

### Destination matching

When `forward_oauth` needs to determine which provider a request targets, it uses this priority:

1. `destinations` patterns in `oat_sources` (checks if host contains pattern)
2. `inspector.provider_map` (exact hostname lookup)
