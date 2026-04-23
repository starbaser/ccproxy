# Configuration

## Overview

ccproxy reads a single configuration file: `ccproxy.yaml`.

**Discovery order** (highest to lowest precedence):

1. `$CCPROXY_CONFIG_DIR/ccproxy.yaml`
2. `~/.config/ccproxy/ccproxy.yaml`

## Installation

Install ccproxy via uv:

```bash
uv tool install claude-ccproxy
```

Initialize the config file:

```bash
ccproxy init
```

This writes `~/.config/ccproxy/ccproxy.yaml` with defaults. Use `--force` to overwrite an existing file.

## Full Config Reference

```yaml
ccproxy:
  host: 127.0.0.1           # Listen address
  port: 4000                 # Reverse proxy listener port
  debug: false               # Debug logging

  oat_sources:               # OAuth token sources, keyed by provider name
    anthropic:
      command: "jq -r '.claudeAiOauth.accessToken' ~/.claude/.credentials.json"
      user_agent: "anthropic"
      destinations: ["api.anthropic.com"]

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
    port: 8083               # mitmweb UI port
    transforms: []           # lightllm transform rules (see Transform Rules)
    provider_map:            # Hostname → OTel gen_ai.system tag
      api.anthropic.com: anthropic
      api.openai.com: openai

  otel:
    enabled: false
    endpoint: "http://localhost:4317"
```

### Top-level fields

| Field | Type | Default | Description |
|---|---|---|---|
| `host` | string | `127.0.0.1` | Reverse proxy listen address |
| `port` | int | `4000` | Reverse proxy listen port |
| `debug` | bool | `false` | Enable debug logging |
| `oat_sources` | map | `{}` | OAuth token sources by provider name |
| `hooks` | object | — | Two-stage hook pipeline (inbound/outbound) |
| `inspector` | object | — | mitmweb and transform settings |
| `otel` | object | — | OpenTelemetry export settings |

## OAuth Configuration

### oat_sources

`oat_sources` maps provider names to token retrieval configuration. The `forward_oauth` hook uses this to inject Bearer tokens into outbound requests.

**Simple form** — shell command only:

```yaml
ccproxy:
  oat_sources:
    anthropic: "jq -r '.claudeAiOauth.accessToken' ~/.claude/.credentials.json"
```

**Extended form** — with user agent and destination filtering:

```yaml
ccproxy:
  oat_sources:
    anthropic:
      command: "jq -r '.claudeAiOauth.accessToken' ~/.claude/.credentials.json"
      user_agent: "anthropic"
      destinations: ["api.anthropic.com"]

    gemini:
      command: "~/bin/get-gemini-token.sh"
      user_agent: "MyApp/1.0"
      destinations: ["generativelanguage.googleapis.com"]
```

**oat_sources entry fields:**

| Field | Description |
|---|---|
| `command` | Shell command whose stdout is the token (mutually exclusive with `file`) |
| `file` | File path to read the token from, whitespace stripped (mutually exclusive with `command`) |
| `user_agent` | `User-Agent` header value for requests using this token |
| `destinations` | Hostname list; token only injected when the request host matches one of these |

### Sentinel Key Mechanism

SDK clients can use a sentinel API key to trigger token substitution without modifying request logic:

```python
client = Anthropic(api_key="sk-ant-oat-ccproxy-anthropic")
```

When ccproxy sees a key matching `sk-ant-oat-ccproxy-{provider}`, it substitutes the actual token from `oat_sources[provider]` and applies the provider's `user_agent` and `destinations`.

### Token Refresh

Tokens are loaded at startup and cached in memory. On a 401 response from the provider, ccproxy re-resolves the credential source (re-reads the file or re-runs the command). If the new token differs from the cached value, the request is retried with the fresh token. If the token is unchanged, the 401 is returned to the client.

## Hook Pipeline

Hooks run in two stages: `inbound` (before the request reaches the provider) and `outbound` (before the response reaches the client).

### Configuration syntax

**Simple form** — module path string:

```yaml
ccproxy:
  hooks:
    inbound:
      - ccproxy.hooks.forward_oauth
      - ccproxy.hooks.extract_session_id
    outbound:
      - ccproxy.hooks.inject_mcp_notifications
```

**Parameterized form** — dict with `hook` and `params` keys:

```yaml
ccproxy:
  hooks:
    outbound:
      - hook: ccproxy.hooks.some_hook
        params:
          key: value
```

### Built-in hooks

| Hook | Stage | Purpose |
|---|---|---|
| `ccproxy.hooks.forward_oauth` | inbound | Substitutes sentinel keys (`sk-ant-oat-ccproxy-{provider}`) with OAuth tokens from `oat_sources`; injects Bearer auth |
| `ccproxy.hooks.gemini_cli_compat` | inbound | Masquerades google-genai SDK user-agent as Gemini CLI for capacity allocation on `cloudcode-pa.googleapis.com` |
| `ccproxy.hooks.reroute_gemini` | inbound | Reroutes WireGuard flows targeting `generativelanguage.googleapis.com` to `cloudcode-pa.googleapis.com` with `v1internal` envelope wrapping |
| `ccproxy.hooks.extract_session_id` | inbound | Reads `metadata.user_id` from the request body and stores it on `flow.metadata` for downstream use |
| `ccproxy.hooks.gemini_oauth_refresh` | inbound | Preemptive Gemini OAuth token refresh with `refresh_token` backup (workaround for gemini-cli#21691). Optional — not enabled by default. |
| `ccproxy.hooks.inject_mcp_notifications` | outbound | Injects buffered MCP terminal events as synthetic tool_use/tool_result blocks |
| `ccproxy.hooks.verbose_mode` | outbound | Strips `redact-thinking-*` flags from the `anthropic-beta` header |
| `ccproxy.hooks.inject_claude_code_identity` | outbound | Prepends the required system prompt prefix for Anthropic OAuth requests. Optional — not enabled by default. |
| `ccproxy.hooks.shape` | outbound | Picks a per-provider captured shape, injects content fields from the incoming request, applies the compliance envelope to the outbound flow |

## Transform Rules

`inspector.transforms` is an ordered list of `TransformRoute` entries. The first match wins.

```yaml
ccproxy:
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

      - match_path: /v1internal
        mode: redirect
        dest_provider: gemini
        dest_host: cloudcode-pa.googleapis.com
        dest_api_key_ref: gemini

      - match_path: /v1/chat/completions
        match_model: gpt-4o
        mode: transform
        dest_provider: anthropic
        dest_model: claude-haiku-4-5-20251001
        dest_api_key_ref: anthropic
```

### TransformRoute fields

| Field | Type | Default | Description |
|---|---|---|---|
| `mode` | string | `redirect` | `redirect`: rewrite destination host, preserve request body (same-format). `transform`: rewrite both destination and body via lightllm (cross-format). `passthrough`: forward to original destination unchanged. |
| `match_host` | string | — | Optional. Checked against the request's `Host` header, `pretty_host`, and `X-Forwarded-Host`. |
| `match_path` | string | `/` | URL path prefix to match. |
| `match_model` | string | — | Substring match against the `model` field in the request body. |
| `dest_provider` | string | — | Provider name (e.g. `anthropic`, `gemini`). Used by `transform` for lightllm dispatch and `redirect` for shaping profile lookup. |
| `dest_model` | string | — | Model identifier sent to the provider. Only used in `transform` mode. |
| `dest_host` | string | — | Explicit destination host for `redirect` mode (e.g. `api.anthropic.com`). Required for `redirect` mode. |
| `dest_path` | string | — | Override the request path in `redirect` mode. If not set, the original path is preserved. |
| `dest_api_key_ref` | string | — | Provider name in `oat_sources` for credential lookup, or an environment variable name. |
| `dest_vertex_project` | string | — | GCP project ID for Vertex AI transforms. Required for context caching with `vertex_ai`/`vertex_ai_beta` providers. |
| `dest_vertex_location` | string | — | GCP region for Vertex AI transforms (e.g. `us-central1`). |

All match fields are optional and ANDed together. A rule with no match fields matches every request — use as a catch-all at the end of the list.

## Inspector Settings

```yaml
ccproxy:
  inspector:
    port: 8083
    transforms: []
    provider_map:
      api.anthropic.com: anthropic
      api.openai.com: openai
      generativelanguage.googleapis.com: google_ai_studio
```

| Field | Type | Description |
|---|---|---|
| `port` | int | mitmweb UI listen port (default `8083`) |
| `transforms` | list | Transform rules (see above) |
| `provider_map` | map | Hostname → `gen_ai.system` value for OTel span attributes |

## Shaping Configuration

Request shaping stamps captured compliance envelopes onto proxied requests. See [shaping.md](shaping.md) for the full reference.

```yaml
ccproxy:
  shaping:
    enabled: true
    shapes_dir: ~/.config/ccproxy/shaping/shapes
    providers:
      anthropic:
        content_fields:
          - model
          - messages
          - tools
          - tool_choice
          - system
          - thinking
          - context_management
          - stream
          - max_tokens
          - temperature
          - top_p
          - top_k
          - stop_sequences
        merge_strategies:
          system: "prepend_shape:2"
        shape_hooks:
          - ccproxy.shaping.callbacks.regenerate_user_prompt_id
          - ccproxy.shaping.callbacks.regenerate_session_id
        preserve_headers:
          - authorization
          - x-api-key
          - x-goog-api-key
          - host
        strip_headers:
          - authorization
          - x-api-key
          - x-goog-api-key
          - content-length
          - host
          - transfer-encoding
          - connection
        capture:
          path_pattern: "^/v1/messages"
```

| Field | Type | Description |
|---|---|---|
| `enabled` | bool | Enable/disable shaping globally (default `true`) |
| `shapes_dir` | string | Directory for `.mflow` shape files |
| `providers` | map | Per-provider shaping profiles (see [shaping.md](shaping.md)) |

## Flows Configuration

```yaml
ccproxy:
  flows:
    default_jq_filters:
      - 'map(select(.request.path | startswith("/v1/messages")))'
```

| Field | Type | Description |
|---|---|---|
| `default_jq_filters` | list | jq expressions applied before CLI `--jq` filters. Each must consume and produce a JSON array. |

## Environment Variables

| Variable | Description |
|---|---|
| `CCPROXY_CONFIG_DIR` | Override the config directory (takes precedence over `~/.config/ccproxy`) |
| `CCPROXY_PORT` | Override the listen port (takes precedence over `ccproxy.port` in the config file) |
