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

  providers:                 # Provider entries keyed by sentinel suffix
    anthropic:
      auth:
        type: command
        command: "jq -r '.claudeAiOauth.accessToken' ~/.claude/.credentials.json"
      host: api.anthropic.com
      path: /v1/messages
      provider: anthropic    # LiteLLM provider identifier (drives format dispatch)

  hooks:
    inbound:
      - ccproxy.hooks.forward_oauth
      - ccproxy.hooks.extract_session_id
    outbound:
      - ccproxy.hooks.gemini_cli
      - ccproxy.hooks.gemini_capacity_fallback
      - ccproxy.hooks.inject_mcp_notifications
      - ccproxy.hooks.verbose_mode
      - ccproxy.hooks.shape
      - ccproxy.hooks.commitbee_compat

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
| `providers` | map | `{}` | Provider entries keyed by sentinel suffix (auth + destination + format) |
| `hooks` | object | — | Two-stage hook pipeline (inbound/outbound) |
| `inspector` | object | — | mitmweb and transform settings |
| `otel` | object | — | OpenTelemetry export settings |

## Providers

### providers

`providers` maps a sentinel suffix to a `Provider` entry: an auth source, a single destination (`host` + `path`), and a LiteLLM `provider` identifier that names the wire format the destination speaks. When ccproxy sees a sentinel key matching `sk-ant-oat-ccproxy-{name}`, the matching `Provider` drives both token injection (`forward_oauth`) and routing (auto-redirect or cross-format `transform` via lightllm).

**Simple form** — auth dispatched as a bare shell command:

```yaml
ccproxy:
  providers:
    anthropic:
      auth: "jq -r '.claudeAiOauth.accessToken' ~/.claude/.credentials.json"
      host: api.anthropic.com
      path: /v1/messages
      provider: anthropic
```

**Full form** — explicit auth discriminator and per-provider auth header:

```yaml
ccproxy:
  providers:
    anthropic:
      auth:
        type: command
        command: "jq -r '.claudeAiOauth.accessToken' ~/.claude/.credentials.json"
      host: api.anthropic.com
      path: /v1/messages
      provider: anthropic

    gemini:
      auth:
        type: command
        command: "jq -r '.access_token' ~/.gemini/oauth_creds.json"
      host: cloudcode-pa.googleapis.com
      path: "/v1internal:{action}"
      provider: gemini

    deepseek:
      auth:
        type: command
        command: "printenv DEEPSEEK_API_KEY"
        header: x-api-key      # send token as `x-api-key: <token>` (not `Authorization: Bearer …`)
      host: api.deepseek.com
      path: /anthropic/v1/messages
      provider: anthropic      # DeepSeek's anthropic-compat endpoint speaks the anthropic format
```

**Provider entry fields:**

| Field | Description |
|---|---|
| `auth` | Discriminated auth source. Bare strings coerce to `{type: command, command: <str>}`. |
| `host` | Single destination hostname (e.g. `api.anthropic.com`). |
| `path` | Destination path. Supports `{model}` and `{action}` templating substituted from the body / URL at routing time. Defaults to `/`. |
| `provider` | LiteLLM provider identifier (`anthropic`, `gemini`, `deepseek`, `openai`, …). When the incoming format matches `provider`, the routing handler just rewrites the destination; when they differ, the body is rewritten via `lightllm.transform_to_provider`. |

**Auth source types** (the `type:` discriminator inside `auth:`):

| `type` | Required keys | Behavior |
|---|---|---|
| `command` | `command` | Shell command whose stdout is the token. Bare strings under `auth:` coerce to this. |
| `file` | `file` | File path; contents stripped of whitespace are the token. |
| `anthropic_oauth` | `refresh_token_file` (default `~/.config/ccproxy/oauth/anthropic.json`) | Refreshes Anthropic OAuth tokens in-process via `claude.ai/v1/oauth/token`. Atomically writes refreshed tokens back to disk. |
| `google_oauth` | `client_id`, `client_secret`, `refresh_token_file` (default `~/.gemini/oauth_creds.json`) | Refreshes Google/Gemini OAuth tokens in-process via `oauth2.googleapis.com`. Preserves on-disk `refresh_token` when the refresh response omits it (gemini-cli #21691). |

The `auth.header` field (inside any `auth:` block) overrides the default `Authorization: Bearer {token}` injection. Set it to a custom header name (e.g. `x-api-key`) when the destination expects the raw token in a non-Bearer header.

**Iteration order is load-bearing.** `forward_oauth` walks `providers` in insertion order to pick a fallback when no sentinel key is present on the request — the first provider with a cached token wins. Keep the highest-priority provider (typically `anthropic`) first.

### Sentinel Key Mechanism

SDK clients can use a sentinel API key to trigger token substitution without modifying request logic:

```python
client = Anthropic(api_key="sk-ant-oat-ccproxy-anthropic")
```

When ccproxy sees a key matching `sk-ant-oat-ccproxy-{name}`, it substitutes the actual token from `providers[name].auth`, sets the auth header (`Authorization: Bearer …` by default, or `providers[name].auth.header` when set), and routes the request to `providers[name].host` / `providers[name].path`. If the incoming wire format doesn't match `providers[name].provider`, lightllm rewrites the body too.

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
| `ccproxy.hooks.forward_oauth` | inbound | Substitutes sentinel keys (`sk-ant-oat-ccproxy-{name}`) with the cached auth token from `providers[name].auth`; injects `Authorization: Bearer …` (or the custom `auth.header` when set) and stamps `flow.metadata["ccproxy.oauth_provider"]` for downstream routing |
| `ccproxy.hooks.extract_session_id` | inbound | Reads `metadata.user_id` via `glom(ctx._body, 'metadata.user_id')` and stores session_id on `flow.metadata` for downstream use |
| `ccproxy.hooks.gemini_cli` | outbound | Single hook for all Gemini sentinel-key traffic. Wraps standard Gemini bodies in the `v1internal` envelope, conditionally masquerades `google-genai-sdk/*` UAs as Gemini CLI, rewrites paths to `cloudcode-pa`, and unwraps the `{response: {...}}` envelope on the way back. |
| `ccproxy.hooks.gemini_capacity_fallback` | outbound | Retries Gemini requests against a fallback model chain when cloudcode-pa returns 429 / 503 RESOURCE_EXHAUSTED. Sticky same-model retries honor `RetryInfo.retryDelay`, then walks the configured chain. |
| `ccproxy.hooks.inject_mcp_notifications` | outbound | Injects buffered MCP terminal events as synthetic tool_use/tool_result blocks |
| `ccproxy.hooks.verbose_mode` | outbound | Strips `redact-thinking-*` flags from the `anthropic-beta` header |
| `ccproxy.hooks.shape` | outbound | Picks a per-provider captured shape, injects content fields from the incoming request, applies it to the outbound flow. The shape carries the captured Claude client's identity verbatim — no separate identity-injection hook is needed. |
| `ccproxy.hooks.commitbee_compat` | outbound | Last-mile compatibility shim for the commitbee tool. |

### Writing custom hooks

Use the `@hook` decorator with `reads`/`writes` for DAG ordering. Declarations support glom dot-paths (e.g. `"metadata.user_id"`) — the DAG extracts root fields for dependency resolution:

```python
from glom import assign, glom
from ccproxy.pipeline.context import Context
from ccproxy.pipeline.hook import hook

@hook(reads=["metadata.user_id"], writes=["metadata.tracking_id"])
def my_hook(ctx: Context, params: dict) -> Context:
    # Typed layer: ctx.messages, ctx.system, ctx.tools (Pydantic AI objects)
    # Raw body layer: glom/assign/delete over ctx._body (standard primitive)
    user_id = glom(ctx._body, "metadata.user_id", default="")
    if user_id:
        assign(ctx._body, "metadata.tracking_id", f"track-{user_id}")
    return ctx
```

Register in config:

```yaml
hooks:
  outbound:
    - mypackage.my_hook
```

### Per-request overrides

Force-run or force-skip hooks via header:

```
x-ccproxy-hooks: +inject_mcp_notifications,-verbose_mode
```

## Transform Overrides

The default `inspector.transforms` list is empty: routing comes from sentinel-key resolution against the `providers` map. When a sentinel key arrives, ccproxy resolves the matching `Provider`, sets `flow.metadata["ccproxy.oauth_provider"]`, and either redirects (incoming format matches `provider`) or cross-transforms via lightllm (formats differ). Most users never need a `TransformOverride`.

`inspector.transforms` is an ordered list of `TransformOverride` entries layered on top of Provider auto-routing. The first regex match wins. Use overrides for edge cases — bypassing auth for a specific host, forcing a particular destination for a path/model combo, etc.

```yaml
ccproxy:
  inspector:
    transforms:
      # Bypass interception for a host: forward unchanged to its original destination.
      - action: passthrough
        match_host: cloudcode-pa\.googleapis\.com

      # Force a specific provider for a path. dest_provider resolves to providers["anthropic"]
      # for host/path/auth — no separate api-key reference is required.
      - match_path: ^/v1/messages$
        action: redirect
        dest_provider: anthropic

      # Cross-format transform: OpenAI-shape requests for gpt-4o get rewritten to Anthropic's
      # /v1/messages format and routed through providers["anthropic"].
      - match_path: ^/v1/chat/completions$
        match_model: ^gpt-4o
        action: transform
        dest_provider: anthropic
        dest_model: claude-haiku-4-5-20251001
```

### TransformOverride fields

| Field | Type | Default | Description |
|---|---|---|---|
| `action` | string | `redirect` | `redirect`: rewrite destination, preserve body (same-format). `transform`: rewrite both destination and body via lightllm (cross-format). `passthrough`: forward unchanged. |
| `match_host` | regex | — | Optional. Matched against `pretty_host`, the `Host` header, and `X-Forwarded-Host`. |
| `match_path` | regex | `.*` | Matched against the request path. |
| `match_model` | regex | — | Matched against `glom(body, "model")`. |
| `dest_provider` | string | — | ccproxy provider name. Resolves to a `providers` entry for host/path/auth/format. The provider's auth is applied automatically — no separate api-key field is required. |
| `dest_model` | string | — | Rewrites `body['model']`. Only used in `transform` mode. |
| `dest_host` | string | — | Raw host override. Bypasses Provider lookup. |
| `dest_path` | string | — | Raw path override. Bypasses Provider lookup. |
| `dest_vertex_project` | string | — | GCP project ID for Vertex AI transforms. Required for context caching with `vertex_ai`/`vertex_ai_beta` providers. |
| `dest_vertex_location` | string | — | GCP region for Vertex AI transforms (e.g. `us-central1`). |

`match_*` fields are full regex (compiled with `re.compile`). All match fields are optional and ANDed together. A rule with no match fields matches every request — use as a catch-all at the end of the list. Auth resolves via `dest_provider` lookup; there is no separate api-key reference field.

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
          - ccproxy.shaping.regenerate
          - hook: ccproxy.shaping.caching.strip
            params:
              paths: ["system.*.cache_control"]
          - hook: ccproxy.shaping.caching.insert
            params:
              path: "system.-1.cache_control"
              value: {type: ephemeral}
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

`shape_hooks` entries are either bare module path strings or `{hook, params}` dicts for parameterized hooks. See [shaping.md](shaping.md) for the full shape hooks reference including the cache breakpoint hooks.

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
