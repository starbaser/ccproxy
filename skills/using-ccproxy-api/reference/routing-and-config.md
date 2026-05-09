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
  shape: Stamps captured compliance envelopes onto proxied requests.
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
  log_level: INFO

  providers:
    anthropic:
      auth:
        type: command
        command: "jq -r '.claudeAiOauth.accessToken' ~/.claude/.credentials.json"
      host: api.anthropic.com
      path: /v1/messages
      provider: anthropic

  hooks:
    inbound:
      - ccproxy.hooks.forward_oauth
      - ccproxy.hooks.extract_session_id
    outbound:
      - ccproxy.hooks.inject_mcp_notifications
      - ccproxy.hooks.verbose_mode
      - ccproxy.hooks.shape

  shaping:
    enabled: true
    shapes_dir: ~/.config/ccproxy/shaping/shapes

  inspector:
    port: 8083
    transforms:
      - match_host: cloudcode-pa.googleapis.com
        action: passthrough
      - match_path: /v1/chat/completions
        match_model: gpt-4o
        dest_provider: anthropic
        dest_model: claude-haiku-4-5-20251001
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

The default `inspector.transforms` list is empty: sentinel-keyed flows route through `providers` automatically. Override rules cover edge cases — forcing a specific provider for a path/model combo, bypassing auth for a specific host, etc. Each rule is a `TransformOverride` with these fields:

| Field | Type | Description |
|-------|------|-------------|
| `action` | `redirect` \| `transform` \| `passthrough` | Default: `redirect`. Redirect rewrites host/auth only. Transform rewrites body format via lightllm. Passthrough forwards unchanged. |
| `match_host` | `str?` | Regex matched against `pretty_host`, `Host` header, and `X-Forwarded-Host`. |
| `match_path` | `str` | Regex matched against the request path. Default: `.*`. |
| `match_model` | `str?` | Regex matched against the `model` field in the request body. |
| `dest_provider` | `str?` | ccproxy provider name — resolves to a `providers[name]` entry (host/path/auth/format). |
| `dest_model` | `str?` | Rewrites `body['model']`. |
| `dest_host` | `str?` | Raw host override. Bypasses provider lookup. |
| `dest_path` | `str?` | Raw path override. |
| `dest_vertex_project` | `str?` | GCP project ID for Vertex AI transforms. |
| `dest_vertex_location` | `str?` | GCP region for Vertex AI transforms. |

Auth is resolved via the `dest_provider` lookup: when a rule names `dest_provider: anthropic`, the auth comes from `providers.anthropic.auth` automatically — no separate auth-ref field is needed.

### Examples

```yaml
inspector:
  transforms:
    # Gemini passthrough (don't transform)
    - action: passthrough
      match_host: cloudcode-pa.googleapis.com

    # Route OpenAI requests to Anthropic
    - match_path: /v1/chat/completions
      match_model: gpt-4o
      dest_provider: anthropic
      dest_model: claude-haiku-4-5-20251001

    # Route all /v1/messages to a different Anthropic model
    - match_path: /v1/messages
      match_model: claude-sonnet
      dest_provider: anthropic
      dest_model: claude-opus-4-5-20251101
```

First regex match wins. Unmatched reverse proxy flows return a 501 error (OpenAI shape); unmatched WireGuard flows pass through unchanged.

---

## OAuth token management

### providers configuration

A `Provider` entry binds an auth source, a single destination (host + path), and a LiteLLM format identifier under a sentinel-suffix key. The sentinel key `sk-ant-oat-ccproxy-{name}` resolves to `providers[name]` for token injection and routing.

**Compact form** (bare command string auto-coerces to a `command` auth):
```yaml
providers:
  anthropic:
    auth: "jq -r '.claudeAiOauth.accessToken' ~/.claude/.credentials.json"
    host: api.anthropic.com
    path: /v1/messages
    provider: anthropic
```

**Explicit form**:
```yaml
providers:
  anthropic:
    auth:
      type: command
      command: "jq -r '.claudeAiOauth.accessToken' ~/.claude/.credentials.json"
    host: api.anthropic.com
    path: /v1/messages
    provider: anthropic

  deepseek:
    auth:
      type: command
      command: "printenv DEEPSEEK_API_KEY"
      header: x-api-key       # custom auth header — defaults to Authorization: Bearer
    host: api.deepseek.com
    path: /anthropic/v1/messages
    provider: anthropic       # destination format for lightllm dispatch
```

Provider fields:
- `auth` — discriminated union: `command`, `file`, `anthropic_oauth`, `google_oauth`. A bare string is coerced to `{type: command, command: <string>}`.
- `auth.header` — target header name; omit for the default `Authorization: Bearer {token}`.
- `host` — single destination hostname.
- `path` — destination path. Supports `{model}` and `{action}` templating substituted from glom-read body fields and URL captures.
- `provider` — LiteLLM provider identifier (`anthropic`, `gemini`, `openai`, `deepseek`, …). Drives `lightllm.transform_to_provider` when the incoming format differs from what the destination speaks.

### Token refresh

OAuth-source providers (`anthropic_oauth`, `google_oauth`) refresh in-process via `AuthSource.resolve()` whenever the cached access token is within 60s of expiry — at startup (`_load_credentials()`) and on each header injection. On a 401 from upstream, `OAuthAddon.response()` calls `config.resolve_oauth_token(provider)` to re-resolve the credential source and replays the request with whatever token the resolver returns. Static `command` / `file` loaders have no refresh capability and rely on whichever secret manager owns rotation.

### Provider resolution

Provider resolution is sentinel-driven, not destination-driven. `forward_oauth` reads the `x-api-key` / `Authorization` header, parses the `sk-ant-oat-ccproxy-{name}` suffix, and looks up `providers[name]`. When no sentinel is present, it walks `config.providers` in dict insertion order and uses the first entry with a cached token as a fallback. `Provider.host` is a single value — there is no destinations-pattern matching layer. (`inspector.provider_map` is unrelated: it's a hostname → `gen_ai.system` mapping for OTel attribution only.)
