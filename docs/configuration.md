# Configuration

## Overview

ccproxy reads a single configuration file: `ccproxy.yaml`.

**Discovery order** (highest to lowest precedence):

1. `$CCPROXY_CONFIG_DIR/ccproxy.yaml`
2. `~/.ccproxy/ccproxy.yaml`

## Installation

Install ccproxy via uv:

```bash
uv tool install claude-ccproxy
```

Generate the template config file:

```bash
ccproxy install
```

This writes `~/.ccproxy/ccproxy.yaml` with defaults. Use `--force` to overwrite an existing file.

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

  oauth_ttl: 28800           # Token lifetime in seconds (default 8h)
  oauth_refresh_buffer: 0.1  # Refresh at (1 - buffer) × TTL; default refreshes at 7.2h

  hooks:
    inbound:
      - ccproxy.hooks.forward_oauth
      - ccproxy.hooks.extract_session_id
    outbound:
      - ccproxy.hooks.add_beta_headers
      - ccproxy.hooks.inject_claude_code_identity
      - ccproxy.hooks.inject_mcp_notifications

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
| `oauth_ttl` | int | `28800` | Token lifetime in seconds |
| `oauth_refresh_buffer` | float | `0.1` | Fraction of TTL remaining at which to refresh |
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

Tokens refresh automatically on two triggers:

1. **TTL-based**: A background task runs every 30 minutes and refreshes any token that has consumed `(1 - oauth_refresh_buffer)` of its TTL. With defaults (8h TTL, 0.1 buffer), refresh happens at ~7.2 hours.
2. **401-triggered**: An upstream 401 response causes an immediate token refresh and request retry.

```yaml
ccproxy:
  oauth_ttl: 14400           # 4-hour TTL
  oauth_refresh_buffer: 0.2  # Refresh at 80% of TTL (~3.2h)
```

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
      - ccproxy.hooks.add_beta_headers
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
| `ccproxy.hooks.forward_oauth` | inbound | Substitutes sentinel keys with OAuth tokens from `oat_sources`; injects Bearer auth |
| `ccproxy.hooks.extract_session_id` | inbound | Reads `metadata.user_id` from the request body and stores it on `flow.metadata` for downstream use |
| `ccproxy.hooks.add_beta_headers` | outbound | Merges `ANTHROPIC_BETA_HEADERS` into the `anthropic-beta` header |
| `ccproxy.hooks.inject_claude_code_identity` | outbound | Prepends the required system prompt prefix for Anthropic OAuth requests |
| `ccproxy.hooks.inject_mcp_notifications` | outbound | Injects buffered MCP terminal events as synthetic tool_use/tool_result blocks |
| `ccproxy.hooks.verbose_mode` | outbound | Strips `redact-thinking-*` flags from the `anthropic-beta` header |

## Transform Rules

`inspector.transforms` is an ordered list of `TransformRoute` entries. The first match wins.

```yaml
ccproxy:
  inspector:
    transforms:
      - mode: passthrough
        match_host: cloudcode-pa.googleapis.com

      - match_path: /v1/messages
        dest_provider: anthropic
        dest_model: claude-sonnet-4-5-20250929
        dest_api_key_ref: anthropic

      - match_path: /v1/chat/completions
        match_model: gpt-4o
        dest_provider: anthropic
        dest_model: claude-haiku-4-5-20251001
        dest_api_key_ref: anthropic
```

### TransformRoute fields

| Field | Type | Description |
|---|---|---|
| `mode` | string | `transform` (default) or `passthrough`. Passthrough forwards the request unchanged. |
| `match_host` | string | Optional. Checked against the request's `Host` header and `pretty_host`. |
| `match_path` | string | URL path prefix to match. |
| `match_model` | string | Substring match against the `model` field in the request body. |
| `dest_provider` | string | LiteLLM provider name (e.g. `anthropic`, `openai`, `gemini`). |
| `dest_model` | string | Model identifier sent to the provider. |
| `dest_api_key_ref` | string | Key name in `oat_sources` (or environment) used to authenticate with the provider. |

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

## Environment Variables

| Variable | Description |
|---|---|
| `CCPROXY_CONFIG_DIR` | Override the config directory (takes precedence over `~/.ccproxy`) |
| `CCPROXY_PORT` | Override the listen port (takes precedence over `ccproxy.port` in the config file) |
