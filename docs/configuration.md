# Configuration

## Overview

ccproxy reads two sibling configuration files from the same directory:

- `ccproxy.yaml` configures ccproxy-native services: providers, authentication,
  hooks, inspection, shaping, and transport behavior.
- `config.yaml` is a LiteLLM-compatible frontend for model aliases,
  destinations, credentials, and per-model request defaults.

`config.yaml` is compiled into ccproxy's existing provider, routing,
authentication, transform, and model-discovery services. LiteLLM is not
installed or executed, and the retired LiteLLM runtime router is not restored.

**Discovery order** (highest to lowest precedence):

1. `$CCPROXY_CONFIG_DIR/{ccproxy.yaml,config.yaml}`
2. `~/.config/ccproxy/{ccproxy.yaml,config.yaml}`

Either file may be absent. Without `ccproxy.yaml`, native settings use their
defaults. Without `config.yaml`, no model bindings are generated.

## Installation

Install ccproxy via uv:

```bash
uv tool install ai-ccproxy
```

Initialize both configuration files:

```bash
ccproxy init
```

This writes `~/.config/ccproxy/ccproxy.yaml` and the sibling
`~/.config/ccproxy/config.yaml` with defaults. Use `--force` to overwrite
existing files.

## LiteLLM-compatible model configuration

Existing LiteLLM model declarations can be placed in the sibling
`config.yaml`. For example, Requesty needs only its normal OpenAI-compatible
endpoint and bearer key:

```yaml
model_list:
  - model_name: requesty/*
    litellm_params:
      model: requesty/*
      api_base: https://router.requesty.ai/v1
      api_key: os.environ/REQUESTY_API_KEY
      temperature: 0.2
```

Clients can then request `requesty/openai/gpt-4o-mini` (or another model
reported by Requesty). The public wildcard is matched to the upstream
wildcard, the destination is injected as an existing ccproxy `Provider`, and
an explicitly supplied client parameter overrides the configured default.

The supported compatibility surface includes:

- `model_list`, `model_name`, `litellm_params`, and opaque `model_info`;
- exact aliases, one-wildcard aliases, and deterministic alias chains;
- `model`, `custom_llm_provider`, `api_base`, and its `base_url` alias;
- literal API keys and runtime `os.environ/NAME` API-key sources;
- `extra_headers`, OpenAI `organization`, and per-model request defaults;
- relative `include` files, with recursive include and cycle handling;
- native-provider inheritance when no endpoint is supplied, allowing a model
  declaration to reuse ccproxy OAuth, shaping, fingerprint, and transport
  configuration.

For safety, an explicit `api_base`/`base_url` does not inherit credentials,
headers, query values, or fingerprint data from a native provider. Supply the
deployment's `api_key` explicitly or let the client credential pass through.

The compiler deliberately rejects active behavior that would require
LiteLLM's runtime router, including duplicate deployments for the same
`model_name`, load-balancing weights/order, RPM/TPM limits, parallel-request
limits, retries, fallbacks, tag routing, budgets, and active
`router_settings`. LiteLLM proxy-management and callback settings have no
ccproxy runtime effect and produce compatibility diagnostics.

Pricing fields that LiteLLM stores with deployment metadata remain catalog
metadata; they are not inserted into provider request bodies. Arbitrary model
capability databases are not reproduced inside ccproxy. Opaque `model_info`
values are preserved without expanding `os.environ/NAME`, preventing an
environment value from being exposed through `/v1/models`. LiteLLM behavior
controls such as `drop_params` and `merge_reasoning_content_in_choices` are
unsupported compatibility semantics and are never forwarded as inference
parameters.

## Full Config Reference

```yaml
ccproxy:
  host: 127.0.0.1           # Listen address
  port: 4000                 # Reverse proxy listener port
  log_level: INFO            # Root logger level: DEBUG, INFO, WARNING, ERROR, CRITICAL

  # Daemon log file path. Relative to config dir, or absolute.
  # Set to null to disable file logging. Only `ccproxy start` writes here.
  # log_file: ccproxy.log

  providers:                 # Provider entries keyed by sentinel suffix
    anthropic:
      auth:
        type: command
        command: "jq -r '.claudeAiOauth.accessToken' ~/.claude/.credentials.json"
      base_url: https://api.anthropic.com
      path: /v1/messages
      type: anthropic        # adapter-family name (drives wire-format dispatch)

  hooks:
    inbound:
      - ccproxy.hooks.inject_auth
      - ccproxy.hooks.extract_session_id
    outbound:
      - ccproxy.hooks.gemini_cli
      - ccproxy.hooks.pplx_stamp_headers
      - ccproxy.hooks.inject_mcp_notifications
      - ccproxy.hooks.verbose_mode
      - ccproxy.hooks.commitbee_compat
      - ccproxy.hooks.shape

  gemini_capacity:
    enabled: true
    fallback_models:
      - gemini-3-flash-preview
      - gemini-2.5-pro
      - gemini-2.5-flash

  lightllm:
    transforms: []           # Transform override rules (see Transform Rules)

  inspector:
    port: 8083               # mitmweb UI port
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
| `log_level` | string | `INFO` | Root logger level: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `log_file` | path | `ccproxy.log` | Daemon log file path. Relative to config dir, or absolute. `null` disables. |
| `use_journal` | bool | `false` | Route daemon logging to systemd journal (requires `journal` extra) |
| `journal_identifier` | string | — | `SYSLOG_IDENTIFIER` for journal handler. Derived from config-dir basename when unset. |
| `provider_timeout` | float | — | Timeout budget (seconds) for upstream httpx calls. `null` disables the timeout. |
| `providers` | map | `{}` | Provider entries keyed by sentinel suffix (auth + destination + format) |
| `hooks` | object | — | Two-stage hook pipeline (inbound/outbound) |
| `gemini_capacity` | object | — | Sticky-retry + fallback chain for Gemini RESOURCE_EXHAUSTED (see below) |
| `lightllm` | object | — | Cross-format routing and transform overrides |
| `inspector` | object | — | mitmweb listener, UI, and capture settings |
| `otel` | object | — | OpenTelemetry export settings |
| `shaping` | object | — | Request shaping configuration (see [shaping.md](shaping.md)) |
| `flows` | object | — | Flow CLI defaults (see below) |

## Logging

ccproxy writes to three potential sinks simultaneously: **stderr** (always), a **log file** (daemon mode), and the **systemd journal** (optional).

```yaml
ccproxy:
  log_level: INFO
  log_file: ccproxy.log
  use_journal: false
  journal_identifier: null
```

### `log_level`

Root Python logger level, applied uniformly to all loggers (ccproxy, mitmproxy, httpx, httpcore). One of `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. `DEBUG` emits library internals — noisy but useful for tracing request/response cycles through the pipeline.

### `log_file`

Daemon log file path. Relative paths resolve against the config file's directory (`ccproxy.yaml`'s parent); absolute paths pass through. Set to `null` to disable file logging entirely. Only `ccproxy start` writes here — one-shot CLI commands (`run`, `status`, `flows`) always write to stderr. The file is **truncated on each daemon restart**. Access the resolved path via `ccproxy logs`.

### `use_journal` and `journal_identifier`

When `use_journal: true`, ccproxy attaches a `systemd.journal.JournalHandler` to the root logger so daemon output is routed to the systemd journal. Requires the `journal` optional extra (`pip install ai-ccproxy[journal]`). Falls back to stderr with a warning when `systemd-python` is unavailable or the host lacks systemd. Only applies to `ccproxy start`.

`journal_identifier` sets the `SYSLOG_IDENTIFIER` field in journal entries. When unset (default), it derives from the config-dir basename:

| Config dir | Derived identifier |
|---|---|
| `~/.config/ccproxy/` | `ccproxy` |
| `~/dev/projects/foo/.ccproxy/` | `ccproxy-foo` |
| `~/.config/myapp/` | `ccproxy-myapp` |

Override via this field or the `CCPROXY_JOURNAL_IDENTIFIER` env var. View journal output with:

```bash
journalctl --user -t ccproxy           # default
journalctl --user -t ccproxy-myproject # custom identifier
```

## Upstream Timeout

```yaml
ccproxy:
  provider_timeout: null
```

`provider_timeout` sets a timeout budget (seconds) for httpx-based upstream HTTP calls inside ccproxy — specifically auth token refresh and the 401-retry path. It applies uniformly across connect, read, write, and pool phases.

When `null` (default), there is **no enforced timeout**. This matches mitmproxy's default main-forward path and Portkey AI's upstream behavior — requests can take as long as the upstream needs (important for long-running streaming inference). Set to a positive float to opt into a bounded timeout for internal calls.

This does NOT affect the main request/response forwarding path (mitmproxy handles that independently). It only gates ccproxy's own outbound HTTP calls.

## Providers

### providers

`providers` maps a sentinel suffix to a `Provider` entry: an auth source, a single destination (`base_url` + `path`), and an adapter-family `type` identifier that names the wire format the destination speaks (one of `anthropic`, `openai`, `google` / `gemini` / `vertex_ai` / `vertex_ai_beta`, `perplexity_pro`). Compatible providers use the closest wire-format family, such as `type: anthropic`. When ccproxy sees a sentinel key matching `sk-ant-oat-ccproxy-{name}`, the matching `Provider` drives both auth injection (`inject_auth`) and routing (auto-redirect or cross-format `transform` via lightllm).

**Simple form** — auth dispatched as a bare shell command:

```yaml
ccproxy:
  providers:
    anthropic:
      auth: "jq -r '.claudeAiOauth.accessToken' ~/.claude/.credentials.json"
      base_url: https://api.anthropic.com
      path: /v1/messages
      type: anthropic
```

**Full form** — explicit auth discriminator and per-provider auth header:

```yaml
ccproxy:
  providers:
    anthropic:
      auth:
        type: command
        command: "jq -r '.claudeAiOauth.accessToken' ~/.claude/.credentials.json"
      base_url: https://api.anthropic.com
      path: /v1/messages
      type: anthropic

    gemini:
      auth:
        type: command
        command: "jq -r '.access_token' ~/.gemini/oauth_creds.json"
      base_url: https://cloudcode-pa.googleapis.com
      path: "/v1internal:{action}"
      type: gemini

    deepseek:
      auth:
        type: command
        command: "printenv DEEPSEEK_API_KEY"
        header: x-api-key      # send token as `x-api-key: <token>` (not `Authorization: Bearer …`)
      base_url: https://api.deepseek.com
      path: /anthropic/v1/messages
      type: anthropic          # DeepSeek's anthropic-compat endpoint speaks the anthropic format

    minimax:
      auth:
        type: command
        command: "printenv MINIMAX_API_KEY"
        header: x-api-key      # send token as `x-api-key: <token>` (not `Authorization: Bearer ...`)
      base_url: https://api.minimax.io/anthropic
      path: /v1/messages
      type: anthropic          # MiniMax-M3 and MiniMax-M2.7 via the compatible endpoint
```

The packaged template selects the global `anthropic` endpoint. A provider has one destination, so use the
corresponding values below when selecting another MiniMax endpoint:

| `type` | Region | `base_url` | `path` | `auth.header` |
|---|---|---|---|---|
| `anthropic` | Global | `https://api.minimax.io/anthropic` | `/v1/messages` | `x-api-key` |
| `anthropic` | China | `https://api.minimaxi.com/anthropic` | `/v1/messages` | `x-api-key` |
| `openai` | Global | `https://api.minimax.io/v1` | `/chat/completions` | Omit to use the default Bearer header |
| `openai` | China | `https://api.minimaxi.com/v1` | `/chat/completions` | Omit to use the default Bearer header |

MiniMax-M3 supports adaptive and disabled thinking. The Anthropic-compatible API defaults thinking off,
while the OpenAI-compatible API defaults adaptive thinking on; set the mode explicitly for portable behavior.
MiniMax-M2.7 always uses interleaved thinking.

**Provider entry fields:**

| Field | Description |
|---|---|
| `auth` | Discriminated auth source. Bare strings coerce to `{type: command, command: <str>}`. |
| `base_url` | Absolute destination URL, including an optional base path (e.g. `https://api.minimax.io/anthropic`). |
| `path` | Destination path. Supports `{model}` and `{action}` templating substituted from the body / URL at routing time. Defaults to `/`. |
| `type` | Wire-format identifier (`anthropic`, `gemini`, `openai`, `openai_responses`, `perplexity_pro`, …). When the incoming format matches `type`, the routing handler just rewrites the destination; when they differ, the body is rewritten via `lightllm`. |

**Auth source types** (the `type:` discriminator inside `auth:`):

| `type` | Required keys | Behavior |
|---|---|---|
| `command` | `command` | Shell command whose stdout is the token. Bare strings under `auth:` coerce to this. |
| `file` | `file` | File path; contents stripped of whitespace are the token. |
| `anthropic_oauth` | `file_path` (default `~/.config/ccproxy/oauth/anthropic.json`) | Refreshes Anthropic OAuth tokens in-process via `claude.ai/v1/oauth/token`. Atomically writes refreshed tokens back to `file_path`. |
| `google_oauth` | `client_id`, `client_secret`, `file_path` (default `~/.gemini/oauth_creds.json`) | Refreshes Google/Gemini OAuth tokens in-process via `oauth2.googleapis.com`. Preserves on-disk `refresh_token` when the refresh response omits it (gemini-cli #21691). |
| `codex_oauth` | `file_path` (default `~/.codex/auth.json`) | Refreshes Codex ChatGPT OAuth tokens in-process via `auth.openai.com`, atomically writes the updated auth envelope, and stamps account-routing companion headers. |

The `auth.header` field (inside any `auth:` block) overrides the default `Authorization: Bearer {token}` injection. Set it to a custom header name (e.g. `x-api-key`) when the destination expects the raw token in a non-Bearer header.

The packaged `codex` provider uses `type: openai_responses` and routes
same-format Responses requests to ChatGPT's Codex backend
(`/backend-api/codex/responses`). That backend follows Codex CLI semantics, not
the full public OpenAI Responses API surface: use streaming requests, leave
public-only fields such as `max_output_tokens` unset, and let the
`ccproxy.shaping.codex` shape hook normalize string `input` and enforce
`store: false`.

#### Auth source class hierarchy

Configuration values dispatch through a small Pydantic class hierarchy:

```
AuthFields                                  # base — only `header`
├── CommandAuthSource    type: command          → run a shell command, return stdout
├── FileAuthSource       type: file             → read a file, return contents
├── CodexAuthSource      type: codex_oauth      → refresh Codex ChatGPT JWTs + account headers
└── AuthSource                              # OAuth refresh-capable base
    ├── AnthropicAuthSource   type: anthropic_oauth
    └── GoogleAuthSource      type: google_oauth
```

`AuthFields` carries only the optional target-header override. `CommandAuthSource` and `FileAuthSource` extend it as static credential value loaders — they have no expiry awareness and never POST to a refresh endpoint. They suit any long-lived API key (DeepSeek, Z.AI, OpenRouter) wired through opnix/SOPS, `printenv`, or a managed secret file; rotation happens out-of-band through whichever secret manager produced the value.

`AuthSource` is the OAuth refresh-capable base. It owns the `read → check expiry → refresh-if-near-expiry → atomic write-back` template method. Subclasses provide only:

- defaults for `type` (the `Literal` discriminator), `file_path`, `endpoint`, `client_id`, optional `client_secret`, and `default_expires_in_seconds`;
- a `_build_refresh_body(refresh_token) -> dict[str, str]` that returns the per-provider POST body (Anthropic uses `grant_type=refresh_token` + `client_id`; Google adds `client_secret`).

`CodexAuthSource` is separate because Codex's auth file stores JWTs and
ChatGPT account metadata under a different envelope, and the refresh endpoint
expects JSON rather than the form body used by Anthropic and Google. The
discriminator literal mirrors the distinction in YAML: bare `command` / `file`
for the static loaders, `*_oauth` for the refresh sources. Pick the right one
for the credential's lifecycle, not for the brand of the destination —
pointing a Gemini destination at `type: command` is legal, but ccproxy will
not refresh anything in that case (see "Why Gemini wants `google_oauth`"
below).

**Iteration order is load-bearing.** `inject_auth` walks `providers` in insertion order to pick a fallback when no sentinel key is present on the request — the first provider with a cached token wins. Keep the highest-priority provider (typically `anthropic`) first.

### Sentinel Key Mechanism

SDK clients can use a sentinel API key to trigger token substitution without modifying request logic:

```python
client = Anthropic(api_key="sk-ant-oat-ccproxy-anthropic")
```

When ccproxy sees a key matching `sk-ant-oat-ccproxy-{name}`, it substitutes the actual token from `providers[name].auth`, sets the auth header (`Authorization: Bearer …` by default, or `providers[name].auth.header` when set), and routes the request to `providers[name].host` / `providers[name].path`. If the incoming wire format doesn't match `providers[name].type`, lightllm rewrites the body too.

### Token Refresh

Tokens are loaded at startup via `_load_credentials()` and cached in memory.
For refresh-capable providers (`anthropic_oauth`, `google_oauth`,
`codex_oauth`), `resolve()` rotates the cached access token in-process
whenever it is near expiry and atomically writes the updated credentials back
to `file_path`.

On a 401 response from upstream, `AuthAddon.response()` calls `config.resolve_auth_token(provider)` to re-resolve the credential source — for OAuth sources this triggers another refresh attempt; for static `command` / `file` loaders it just re-reads. The request is then replayed with whatever token the resolver returns; if the resolver returns nothing (empty token, refresh failed), the 401 propagates to the client.

### OAuth refresh lifecycle

`AuthSource.resolve()` implements the in-process refresh template method shared by `anthropic_oauth` and `google_oauth`:

1. **Read.** Open `file_path`, parse JSON, pull `(access_token, refresh_token, expiry)` via the configured glom paths (`access_path`, `refresh_path`, `expiry_path`).
2. **Check expiry.** A 60-second headroom (`_REFRESH_HEADROOM_MS = 60_000`) — if the cached access token is more than 60 seconds away from expiry, return it unchanged.
3. **Refresh.** Otherwise POST `_build_refresh_body(refresh_token)` to `endpoint` (form-encoded). On HTTP error or non-JSON response, give up and return `None`.
4. **Merge.** `copy.deepcopy(creds)` so the original dict is untouched, then `glom.assign(merged, access_path, new_access, missing=dict)` for each of the three paths. `missing=dict` creates intermediate dicts when the credential file uses a nested envelope like `claudeAiOauth.accessToken`. Sibling fields the host CLI maintains — `scopes`, `subscriptionType`, anything else under that envelope or at the top level — survive verbatim.
5. **Write back atomically.** `atomic_write_back(path, merged)`: `tempfile.NamedTemporaryFile` in the same directory, `tf.flush()`, `os.fsync(tf.fileno())`, `tmp.chmod(0o600)`, `tmp.replace(path)`. The rename is atomic on the same filesystem, so a concurrent reader (the host CLI, another ccproxy instance) sees either the old file or the new file, never a partial write.

The `gemini-cli #21691` workaround lives at the merge step: `new_refresh = payload.get("refresh_token") or refresh`. Google's OAuth response sometimes omits `refresh_token`; the fallback keeps the on-disk value so the next refresh still has a valid grant.

#### Startup sequence

`from_yaml()` calls `_load_credentials()` before the inspector listeners come up. `_load_credentials()` iterates every `providers[name]` whose `auth` is set and calls `auth.resolve(label=name)`, populating `_cached_auth_tokens[name]`. For `anthropic_oauth`, `google_oauth`, and `codex_oauth` entries, that single call performs the full read → expiry-check → refresh → write-back dance, so the cached token is fresh by the time mitmweb starts accepting traffic.

This ordering matters most for Gemini. The `prewarm_project()` hook in `ccproxy.hooks.gemini_cli` runs once after readiness, POSTs to `https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist` with the cached `gemini` token, and stashes the resulting `cloudaicompanionProject` for the process lifetime:

```
from_yaml()
 └── _load_credentials()                        # iterates providers, calls auth.resolve() for each
      └── GoogleAuthSource.resolve()            # refresh-if-near-expiry, atomic write-back
           └── _cached_auth_tokens["gemini"] = <fresh token>

[mitmweb starts, addons register, ready signal]

prewarm_project()
 └── token = config.resolve_auth_token("gemini") # reads or refreshes the configured token
 └── POST cloudcode-pa.../v1internal:loadCodeAssist with Bearer <fresh>
 └── _cached_project = response["cloudaicompanionProject"]
```

#### Why Gemini wants `google_oauth`

`prewarm_project()` requires a valid bearer token. With `type: google_oauth`, `_load_credentials()` rotates an expired Gemini token before `prewarm_project()` runs, so the `loadCodeAssist` POST succeeds and the `cloudaicompanionProject` is cached for every subsequent Gemini request.

With `type: command` (e.g. `jq -r '.access_token' ~/.gemini/oauth_creds.json`), `CommandAuthSource.resolve()` just runs `jq` and returns whatever's in the file — no refresh. If the file holds an expired token at startup, `prewarm_project()` silently fails (`loadCodeAssist returned 401; project field will be omitted`) and every subsequent Gemini request lacks the `project` field.

For Gemini the recommended setup is therefore `type: google_oauth` with `file_path: ~/.gemini/oauth_creds.json` and gemini-cli's installed-app credentials. The `client_id` and `client_secret` are public installed-app values embedded in the gemini-cli npm distribution — ccproxy does not vendor them; supply them in your config:

```yaml
ccproxy:
  providers:
    gemini:
      auth:
        type: google_oauth
        file_path: ~/.gemini/oauth_creds.json
        client_id: <gemini-cli installed-app client_id>
        client_secret: <gemini-cli installed-app client_secret>
        header: authorization
      base_url: https://cloudcode-pa.googleapis.com
      path: "/v1internal:{action}"
      type: gemini
```

### Sharing the Claude Code CLI credential file

When you run both ccproxy and the Claude Code CLI on the same machine, the recommended setup is to point the `anthropic` provider at the CLI's own credential file (`~/.claude/.credentials.json`). Both tools then read *and* write the same JSON, so a refresh performed by either side is visible to the other on the next read — eliminating token desync.

```yaml
ccproxy:
  providers:
    anthropic:
      auth:
        type: anthropic_oauth
        file_path: ~/.claude/.credentials.json
        access_path: claudeAiOauth.accessToken
        refresh_path: claudeAiOauth.refreshToken
        expiry_path: claudeAiOauth.expiresAt
        header: authorization
      base_url: https://api.anthropic.com
      path: /v1/messages
      type: anthropic
```

The Claude Code CLI stores its OAuth state under a `claudeAiOauth` envelope:

```json
{
  "claudeAiOauth": {
    "accessToken": "...",
    "refreshToken": "...",
    "expiresAt": 1735689600000,
    "scopes": ["org:create_api_key", "user:profile"],
    "subscriptionType": "max"
  }
}
```

The four glom path fields declare where each credential lives inside that file:

| Field | Purpose | Example |
|---|---|---|
| `file_path` | Path to the credential file on disk. `~` is expanded. | `~/.claude/.credentials.json` |
| `access_path` | Glom dot-path to the access token (read on every request, written after refresh). | `claudeAiOauth.accessToken` |
| `refresh_path` | Glom dot-path to the refresh token (used to mint a new access token). | `claudeAiOauth.refreshToken` |
| `expiry_path` | Glom dot-path to the expiry timestamp (millis since epoch; ccproxy refreshes a few minutes before expiry). | `claudeAiOauth.expiresAt` |

Write-back is atomic — tmpfile → fsync → rename → chmod 0600 — and only the three values addressed by the glom paths are mutated. Sibling fields the CLI maintains (`scopes`, `subscriptionType`, anything else under `claudeAiOauth` or at the top level) are preserved verbatim, so the CLI keeps working without re-authentication after ccproxy refreshes the token.

## Hook Pipeline

Hooks run in two stages: `inbound` (before the request reaches the provider) and `outbound` (before the response reaches the client).

### Configuration syntax

**Simple form** — module path string:

```yaml
ccproxy:
  hooks:
    inbound:
      - ccproxy.hooks.inject_auth
      - ccproxy.hooks.extract_session_id
    outbound:
      - ccproxy.hooks.gemini_cli
      - ccproxy.hooks.pplx_stamp_headers
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
| `ccproxy.hooks.inject_auth` | inbound | Substitutes sentinel keys (`sk-ant-oat-ccproxy-{name}`) with the cached auth token from `providers[name].auth`; injects `Authorization: Bearer …` (or the custom `auth.header` when set) and stamps `ctx.metadata.auth_provider` / `ctx.metadata.auth_injected` for downstream routing and retry logic |
| `ccproxy.hooks.extract_session_id` | inbound | Reads `metadata.user_id` via `glom(ctx._body, 'metadata.user_id')` and stores session_id on `ctx.metadata.session_id` for downstream use |
| `ccproxy.hooks.gemini_cli` | outbound | Single hook for all Gemini sentinel-key traffic. Wraps standard Gemini bodies in the `v1internal` envelope, conditionally masquerades `google-genai-sdk/*` UAs as Gemini CLI, rewrites paths to `cloudcode-pa`, and unwraps the `{response: {...}}` envelope on the way back. |
| `ccproxy.hooks.pplx_stamp_headers` | outbound | Converts Perplexity Pro's injected bearer placeholder into the cookie-auth browser header bundle expected by the WebUI endpoint. |
| `ccproxy.hooks.inject_mcp_notifications` | outbound | Injects buffered MCP terminal events as synthetic tool_use/tool_result blocks |
| `ccproxy.hooks.verbose_mode` | outbound | Strips `redact-thinking-*` flags from the `anthropic-beta` header |
| `ccproxy.hooks.shape` | outbound | Picks a per-provider packaged or local shape, injects content fields from the incoming request, applies it to the outbound flow. The shape carries the native client identity envelope — no separate identity-injection hook is needed. |
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

## Gemini Capacity Fallback

The `gemini_capacity` block configures sticky-retry + fallback chain behavior for Gemini `RESOURCE_EXHAUSTED` (429/503) responses. This is managed by `GeminiAddon` internally — there is no separate hook to configure.

```yaml
ccproxy:
  gemini_capacity:
    enabled: true
    fallback_models:
      - gemini-3-flash-preview
      - gemini-2.5-pro
      - gemini-2.5-flash
    sticky_retry_attempts: 3
    sticky_retry_max_delay_seconds: 60
    terminal_delay_threshold_seconds: 300
    total_retry_budget_seconds: 120
```

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Master switch. When false, capacity errors pass through unchanged. |
| `fallback_models` | list | `[]` | Models tried in order after sticky retries on the original are exhausted. |
| `sticky_retry_attempts` | int | `3` | Same-model retries on the original model before falling through. Range 0–10. |
| `sticky_retry_max_delay_seconds` | float | `60.0` | Per-attempt cap on `retryDelay`. If the server asks for longer, skip remaining sticky attempts and move to next candidate. |
| `terminal_delay_threshold_seconds` | float | `300.0` | Hard ceiling. `retryDelay` above this halts the entire chain — the server is signaling sustained outage. |
| `total_retry_budget_seconds` | float | `120.0` | Wall-clock budget for the entire retry chain across all candidates. |

### Retry behavior

1. **Sticky phase**: On 429/503, retry the same model up to `sticky_retry_attempts` times, honoring `RetryInfo.retryDelay` (capped by `sticky_retry_max_delay_seconds`).
2. **Fallback phase**: If sticky retries are exhausted, walk `fallback_models` in order, trying each once.
3. **Terminal**: If any `retryDelay` exceeds `terminal_delay_threshold_seconds`, or the wall clock exceeds `total_retry_budget_seconds`, stop and return the error to the client.

## Transform Overrides

The default `lightllm.transforms` list is empty: routing comes from sentinel-key resolution against the `providers` map. When a sentinel key arrives, ccproxy resolves the matching `Provider`, sets `ctx.metadata.auth_provider`, and either redirects (incoming format matches the provider `type`) or cross-transforms via lightllm (formats differ). Most users never need a `TransformOverride`.

`lightllm.transforms` is an ordered list of `TransformOverride` entries layered on top of Provider auto-routing. The first regex match wins. Use overrides for edge cases — bypassing auth for a specific host, forcing a particular destination for a path/model combo, etc.

```yaml
ccproxy:
  lightllm:
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
| `dest_base_url` | string | — | Absolute destination base URL override. Bypasses Provider lookup. |
| `dest_path` | string | — | Raw path override. Bypasses Provider lookup. |
| `dest_vertex_project` | string | — | GCP project ID for Vertex AI transforms. Required for context caching with `vertex_ai`/`vertex_ai_beta` providers. |
| `dest_vertex_location` | string | — | GCP region for Vertex AI transforms (e.g. `us-central1`). |

`match_*` fields are full regex (compiled with `re.compile`). All match fields are optional and ANDed together. A rule with no match fields matches every request — use as a catch-all at the end of the list. Auth resolves via `dest_provider` lookup; there is no separate api-key reference field.

## Inspector Settings

```yaml
ccproxy:
  inspector:
    port: 8083
    cert_dir: ~/.config/ccproxy
    provider_map:
      api.anthropic.com: anthropic
      api.openai.com: openai
      generativelanguage.googleapis.com: google
      openrouter.ai: openrouter
    mitmproxy:
      ssl_insecure: true
      web_host: 127.0.0.1
      web_password: null
      web_open_browser: false
      ignore_hosts: []
      allow_hosts: []
      stream_large_bodies: null
      body_size_limit: null
      termlog_verbosity: warn
      flow_detail: 0
```

| Field | Type | Default | Description |
|---|---|---|---|
| `port` | int | `8083` | mitmweb UI listen port |
| `cert_dir` | path | — | mitmproxy CA certificate store directory. Populates `mitmproxy.confdir`. |
| `provider_map` | map | — | Hostname → `gen_ai.system` value for OTel span attributes |

### mitmproxy Options

The `inspector.mitmproxy` block passes options directly to mitmproxy's `OptManager` via `--set` flags:

| Field | Type | Default | Description |
|---|---|---|---|
| `ssl_insecure` | bool | `true` | Skip upstream TLS certificate verification |
| `web_host` | string | `127.0.0.1` | mitmweb browser UI bind address |
| `web_password` | string | — | mitmweb UI password. Plain string, or a `file`/`command` credential source dict. `null` generates a random token on each startup. |
| `web_open_browser` | bool | `false` | Auto-open browser when mitmweb starts |
| `ignore_hosts` | list | `[]` | Regex patterns for hosts to bypass (no TLS interception) |
| `allow_hosts` | list | `[]` | Regex patterns for hosts to intercept (exclusive allowlist) |
| `stream_large_bodies` | string | — | Stream bodies larger than this threshold. `null` disables streaming so the transform handler can inspect and rewrite all bodies. |
| `body_size_limit` | string | — | Hard limit on buffered body size. Bodies exceeding this are dropped. `null` means unlimited. |
| `termlog_verbosity` | string | `warn` | mitmproxy terminal log level: `debug`, `info`, `warn`, `error` |
| `flow_detail` | int | `0` | Flow output verbosity: 0=none, 1=url+status, 2=headers, 3=truncated body, 4=full body |

### Startup Readiness Probe

Before ccproxy accepts traffic, it verifies it can reach the open internet. This catches broken routes, DNS failures, missing CA bundles, or namespace egress problems at startup — before any real requests are accepted. The probe is configured by three top-level fields (siblings of `host`/`port`, not under `inspector`):

```yaml
ccproxy:
  verify_readiness_on_startup: true   # false to skip (e.g. air-gapped environments)
  readiness_probe_url: "https://1.1.1.1/"
  readiness_probe_timeout_seconds: 5.0
```

At startup, ccproxy issues `HEAD <url>` via httpx. Any HTTP response (200, 301, 404) proves the full network stack works. Any exception is a **hard failure**: ccproxy refuses to start.

| Field | Type | Default | Description |
|---|---|---|---|
| `verify_readiness_on_startup` | bool | `true` | Master switch. `false` skips the probe entirely. |
| `readiness_probe_url` | string | `https://1.1.1.1/` | Canary URL. Defaults to Cloudflare's 1.1.1.1 DNS (direct IP, no DNS resolution required, globally reliable). |
| `readiness_probe_timeout_seconds` | float | `5.0` | Total timeout budget. Short by design — the probe is trivial. |

## MCP Server

The daemon hosts a FastMCP streamable-HTTP server for flow inspection, shape capture, and Perplexity library tools. It binds its own internal port, and the proxy listener also forwards `/mcp` to it — MCP clients can use either `http://{mcp.http.host}:{mcp.http.port}/mcp` directly or `http://{host}:{port}/mcp` on the proxy socket. `POST /mcp/notify` on the proxy socket ingests fire-and-forget MCP terminal events into the notification buffer (drained by the `inject_mcp_notifications` hook; see `docs/mcp.md`).

```yaml
ccproxy:
  mcp:
    http:
      enabled: true
      host: 127.0.0.1
      port: 4030
      auth: null
    buffer:
      max_events_per_task: 65536
      ttl_seconds: 600
```

| Field | Type | Default | Description |
|---|---|---|---|
| `http.enabled` | bool | `true` | Host the MCP server (and the `/mcp` forward on the proxy listener). |
| `http.host` | string | `127.0.0.1` | Internal bind address for the MCP uvicorn server. |
| `http.port` | int | `4030` | Internal bind port. |
| `http.auth` | auth source | `null` | Bearer token for MCP clients — plain string, or a `command`/`file` credential source. `null` disables auth. |
| `buffer.max_events_per_task` | int | `65536` | Notification buffer capacity per task; oldest events drop first on overflow. |
| `buffer.ttl_seconds` | int | `600` | Buffered events expire after this many seconds. |

`POST /mcp/notify` requires no auth (fire-and-forget contract): it always answers `200`, with `{"status": "ok"}` on ingestion or `{"status": "error"}` for malformed payloads.

## Shaping Configuration

Request shaping stamps packaged or local compliance envelopes onto proxied requests. See [shaping.md](shaping.md) for the full reference.

```yaml
ccproxy:
  shaping:
    enabled: true
    shapes_dir: ~/.config/ccproxy/shapes
    providers:
      anthropic:
        billing:
          salt: "${CCPROXY_BILLING_SALT}"
          seed: "${CCPROXY_BILLING_SEED}"
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

### Anthropic Billing Header

The Anthropic shaping profile includes a `billing` sub-block for the `regenerate_billing_header` shape hook. Both fields accept either literal values or `${VAR}` environment variable references. When either resolves to `None`, the billing header regeneration silently no-ops.

```yaml
shaping:
  providers:
    anthropic:
      billing:
        salt: "${CCPROXY_BILLING_SALT}"    # Hex salt for SHA-256 cc_version suffix
        seed: "${CCPROXY_BILLING_SEED}"    # xxhash64 seed for the 5-hex cch field
```

| Field | Type | Description |
|---|---|---|
| `billing.salt` | string | Hex salt for the SHA-256 `cc_version` 3-hex suffix. Supports `${VAR}` expansion. |
| `billing.seed` | string | xxhash64 seed for the 5-hex `cch` field (hex, with or without `0x` prefix). Supports `${VAR}` expansion. |

The salt is a static reverse-engineered constant (it does not rotate per release). It is **never committed** — supply via `ccproxy.yaml` or the `CCPROXY_BILLING_SALT` / `CCPROXY_BILLING_SEED` environment variables.

| Field | Type | Description |
|---|---|---|
| `enabled` | bool | Enable/disable shaping globally (default `true`) |
| `shapes_dir` | string | Directory for `.mflow` overrides and provider patch queues |
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
| `default_jq_filters` | list | jq flow-set selectors applied before CLI `--jq` filters. Each must consume a JSON array and produce one JSON array of flow objects. |

## Environment Variables

All `CCPROXY_` prefixed environment variables override their corresponding YAML field. For example, `CCPROXY_PORT=4001` overrides `ccproxy.port`.

| Variable | Description |
|---|---|
| `CCPROXY_CONFIG_DIR` | Override the config directory (takes precedence over `~/.config/ccproxy`) |
| `CCPROXY_HOST` | Override the listen address |
| `CCPROXY_PORT` | Override the listen port |
| `CCPROXY_LOG_LEVEL` | Override `log_level` |
| `CCPROXY_LOG_FILE` | Override `log_file` |
| `CCPROXY_JOURNAL_IDENTIFIER` | Override `journal_identifier` |
| `CCPROXY_BILLING_SALT` | Hex salt for Anthropic billing header `cc_version` suffix |
| `CCPROXY_BILLING_SEED` | xxhash64 seed for Anthropic billing header `cch` field |
| `MITMPROXY_SSLKEYLOGFILE` | Path for TLS keylog (auto-exported by `ccproxy start` to `{config_dir}/tls.keylog`) |
