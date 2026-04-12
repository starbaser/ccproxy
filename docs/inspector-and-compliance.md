# ccproxy Inspector & Compliance System

## Part 1: The Inspector MITM System

### Overview

The inspector is ccproxy's core interception engine. It embeds mitmweb in-process, binds two listeners (a reverse proxy and a WireGuard tunnel), and feeds every HTTP flow through a three-stage addon chain: inbound hooks, lightllm transformation, and outbound hooks. The result is a transparent proxy that can observe, rewrite, and re-route LLM API traffic between any client and any provider.

### Starting the Inspector

#### `ccproxy start`

Starts the inspector in the foreground. Under the hood:

1. Loads config from `$CCPROXY_CONFIG_DIR/ccproxy.yaml` (or `~/.ccproxy/ccproxy.yaml`).
2. Runs preflight port checks on the proxy port (default 4000) and inspector UI port (default 8083).
3. Sets `MITMPROXY_SSLKEYLOGFILE` **before any mitmproxy import** (the TLS keylog path is evaluated at module import time in `mitmproxy.net.tls`).
4. Calls `run_inspector()` which creates a `WebMaster` instance with two listener modes:
   - `reverse:http://localhost:1@{port}` -- the reverse proxy entry point (the `localhost:1` backend is a placeholder; transform routes overwrite the real destination).
   - `wireguard:{conf}@{udp_port}` -- the WireGuard tunnel entry point for namespace-jailed processes.
5. Registers the addon chain (see below), starts the async event loop, and waits for SIGTERM.
6. Writes WireGuard client config to `{config_dir}/.inspector-wireguard-client.conf` and exports keylog files for Wireshark (`tls.keylog`, `wg.keylog`).

The mitmweb UI is available at `http://127.0.0.1:{inspector.port}/?token={web_token}`. The web password is auto-generated unless explicitly set in config.

#### `ccproxy run`

Runs a subprocess with proxy environment variables set:

- **Without `--inspect`**: Sets `ANTHROPIC_BASE_URL`, `OPENAI_BASE_URL`, `OPENAI_API_BASE` to `http://{host}:{port}` so SDK clients route through the reverse proxy.
- **With `--inspect`**: Creates a rootless Linux network namespace, routes all subprocess traffic through a WireGuard tunnel into mitmproxy, and injects a combined CA bundle (mitmproxy CA + system CAs) via `SSL_CERT_FILE`, `NODE_EXTRA_CA_CERTS`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`.

#### Development

```bash
just up          # process-compose, detached
just down        # clean shutdown
```

The Nix devShell configures a local instance at port 4001, inspector UI at 8083, with `CCPROXY_CONFIG_DIR=$PWD/.ccproxy`.

### Two Entry Points: Reverse Proxy vs WireGuard

Every flow enters through one of two listeners and carries its origin in `flow.client_conn.proxy_mode`:

| Entry | Mode | How traffic arrives | Use case |
|-------|------|---------------------|----------|
| **Reverse proxy** | `ReverseMode` | SDK `base_url` pointed at ccproxy | Standard SDK integration. Client sets `ANTHROPIC_BASE_URL=http://localhost:4000` or uses the sentinel API key. |
| **WireGuard** | `WireGuardMode` | All traffic from a namespace-jailed process | Full interception. `ccproxy run --inspect -- claude` captures every outbound connection. |

Both are treated as `"inbound"` flows and go through the full addon chain. The distinction matters for:

- **Compliance observation**: WireGuard flows are always observed as reference traffic; reverse proxy flows are not (they are the consumers of learned profiles).
- **Transform matching**: Unmatched reverse proxy flows get a 501 error; unmatched WireGuard flows pass through unchanged.
- **Compliance application**: The `apply_compliance` hook only fires on reverse proxy flows that have a `TransformMeta`.

### The Addon Chain

Addons are registered in a fixed order by `_build_addons()` in `inspector/process.py`:

```
┌────────────────┐
│  ReadySignal   │  Fires running() event to unblock startup
└───────┬────────┘
        │
┌───────▼────────┐
│ InspectorAddon │  Flow capture, OTel spans, compliance observation, SSE streaming, OAuth retry
└───────┬────────┘
        │
┌───────▼────────────────┐
│ ccproxy_inbound        │  DAG-driven inbound hooks (forward_oauth, extract_session_id)
│ (InspectorRouter)      │
└───────┬────────────────┘
        │
┌───────▼────────────────┐
│ ccproxy_transform      │  Route matching + lightllm dispatch (transform/redirect/passthrough)
│ (InspectorRouter)      │
└───────┬────────────────┘
        │
┌───────▼────────────────┐
│ ccproxy_outbound       │  DAG-driven outbound hooks (inject_mcp_notifications, verbose_mode,
│ (InspectorRouter)      │  apply_compliance)
└────────────────────────┘
```

Each `InspectorRouter` is a xepor `InterceptedAPI` subclass patched for mitmproxy 12.x compatibility (`Server(address=...)` keyword argument, `name` dedup, `host=None` wildcard matching).

### The Flow API

#### FlowRecord and Flow Store

Every inbound flow gets a `FlowRecord` created in `InspectorAddon.request()`. The record is a per-flow state container that travels through the entire addon chain:

```
FlowRecord
  ├── direction: str           ("inbound")
  ├── client_request: ClientRequest   (pre-pipeline snapshot)
  ├── transform: TransformMeta | None (set during transform phase)
  ├── auth: AuthMeta | None           (set by forward_oauth)
  └── otel: OtelMeta | None           (OTel span reference)
```

Records are stored in a global `FlowStore` dict (thread-safe, 120s TTL) keyed by `x-ccproxy-flow-id` -- a UUID stamped into the request headers. Any addon can look up the record via:

```python
record = flow.metadata[InspectorMeta.RECORD]
```

or by flow ID:

```python
record = get_flow_record(flow_id)
```

#### ClientRequest: The Pre-Pipeline Snapshot

Before any hook touches the flow, `InspectorAddon.request()` captures a complete `ClientRequest` snapshot:

```
ClientRequest
  ├── method: str       (GET, POST, etc.)
  ├── scheme: str       (http, https)
  ├── host: str         (original target host)
  ├── port: int         (original target port)
  ├── path: str         (original URL path)
  ├── headers: dict     (original headers, case-preserved)
  ├── body: bytes       (raw request body)
  └── content_type: str (Content-Type header value)
```

This is the ground truth of what the client actually sent, uncontaminated by pipeline mutations. It is used for:

1. **Compliance observation** -- the extractor reads from `ClientRequest`, not the mutated flow.
2. **Content view** -- the `ClientRequestContentview` shows this snapshot in the mitmweb UI under the "Client-Request" view tab.
3. **mitmproxy command** -- `ccproxy.clientrequest` returns the snapshot as JSON for programmatic access.

#### Client Request vs Forwarded Request

This is the key architectural distinction:

| | Client Request | Forwarded Request |
|---|---|---|
| **What** | What the client actually sent | What gets sent to the upstream provider |
| **When captured** | Before any hooks run | After all hooks + transform |
| **Headers** | Client's original headers | May have OAuth tokens injected, beta headers added, compliance headers stamped |
| **Body** | Client's original body | May be transformed to a different API format, wrapped in an envelope, have system prompts injected |
| **Host/URL** | Client's target (e.g. `localhost:4000/v1/messages`) | Provider's actual endpoint (e.g. `api.anthropic.com/v1/messages`) |
| **Access** | `flow.metadata[InspectorMeta.RECORD].client_request` | `flow.request` (the live mitmproxy request object) |

The forwarded request is what actually leaves ccproxy and hits the provider API. It may be radically different from the client request -- different host, different body format, different headers, different API entirely.

### Inbound Pipeline

The inbound pipeline runs DAG-sorted hooks on every `"inbound"` flow before the transform phase. Default hooks:

#### `forward_oauth`

Reads: `authorization`, `x-api-key`. Writes: `authorization`, `x-api-key`.

Three paths:

1. **Sentinel key detected** -- `x-api-key` or `x-goog-api-key` starting with `sk-ant-oat-ccproxy-{provider}`. Extracts the provider name, resolves the real token from `oat_sources` config, injects it via the configured auth header. Raises `OAuthConfigError` (fatal) if no matching source.
2. **No auth at all** -- iterates `oat_sources` for the first cached token, injects it.
3. **Real key present** -- pass-through.

Sets `x-ccproxy-oauth-injected: 1` header and `flow.metadata["ccproxy.oauth_provider"]` for downstream use (OAuth 401 retry, compliance profile selection).

#### `extract_session_id`

Reads: `metadata`. Writes: nothing (stores on flow metadata, not body).

Parses `metadata.user_id` from the request body to extract a `session_id`. Handles two formats:
- JSON: `{"session_id": "uuid", ...}`
- Legacy compound: `user_{hash}_account_{uuid}_session_{uuid}`

Stores the result in `flow.metadata["ccproxy.session_id"]` for the MCP notification injector.

### Outbound Pipeline

Runs after the transform phase, on the response path. Default hooks:

#### `inject_mcp_notifications`

Reads: `messages`. Writes: `messages`.

Drains the MCP notification buffer for the current session and injects synthetic `tool_use`/`tool_result` message pairs before the final user message. Only fires if `flow.metadata["ccproxy.session_id"]` is set and there are buffered events.

#### `verbose_mode`

Reads: `anthropic-beta`. Writes: nothing (header mutation is immediate).

Strips any `redact-thinking-*` token from the `anthropic-beta` header to enable full thinking block output.

#### `apply_compliance`

Reads: `system`, `metadata`. Writes: `system`, `metadata`.

Applies a learned compliance profile to the request. Covered in detail in Part 2.

### Per-Request Hook Overrides

Clients can control hook execution per-request via the `x-ccproxy-hooks` header:

```
x-ccproxy-hooks: +forward_oauth,-verbose_mode
```

- `+hook_name` -- force-run (skip guard, always execute)
- `-hook_name` -- force-skip (never execute)
- `hook_name` -- normal (guard decides)

### The Transformation System

The transform phase sits between the inbound and outbound pipelines. It matches the request against configured `TransformRoute` rules and rewrites the request for the target provider.

#### Transform Route Matching

Rules are defined in `inspector.transforms` and evaluated first-match-wins:

```yaml
inspector:
  transforms:
    - mode: passthrough
      match_host: cloudcode-pa.googleapis.com

    - match_path: /v1/chat/completions
      match_model: gpt-4o
      dest_provider: anthropic
      dest_model: claude-haiku-4-5-20251001
      dest_api_key_ref: anthropic

    - match_path: /v1/messages
      mode: redirect
      dest_host: api.anthropic.com
      dest_api_key_ref: anthropic
```

Matching fields:
- `match_host` -- checked against `flow.request.pretty_host`, `Host` header, `X-Forwarded-Host`
- `match_path` -- URL prefix match
- `match_model` -- substring match on the `model` field in the JSON body

#### Three Modes

**`passthrough`** -- Forward the request unchanged. No body rewriting, no host mutation. Used for flows that should be observed but not transformed (e.g. WireGuard reference traffic to cloudcode-pa).

**`redirect`** -- Rewrite the destination host/port/scheme/path and inject auth credentials, but do not transform the body format. The request body stays in whatever format the client sent it. Requires `dest_host`. Optionally overrides path with `dest_path`.

**`transform`** -- Full cross-provider transformation via lightllm. Rewrites the entire request body from one API format to another (e.g. OpenAI -> Anthropic), changes the destination URL, and handles auth. This is the heaviest mode.

#### lightllm: The Transformation Engine

lightllm is a surgical connector into LiteLLM's `BaseConfig` transformation pipeline. It imports `ProviderConfigManager` to resolve provider configs and calls the transformation methods directly, without LiteLLM's cost tracking, callbacks, or proxy server.

**Request transformation** (`transform_to_provider`):
- Standard providers: `validate_environment` -> `get_complete_url` -> `transform_request` -> `sign_request`
- Gemini/Vertex AI: `_get_gemini_url` + `_transform_request_body` (direct, bypasses `transform_request`)
- Returns `(url, headers, body_bytes)` in provider-native format

**Response transformation** (non-streaming, `transform_to_openai`):
- Calls `config.transform_response()` with a `MitmResponseShim` that duck-types `httpx.Response` for mitmproxy's `flow.response`
- Returns a LiteLLM `ModelResponse` in OpenAI format

**SSE streaming** (`SseTransformer`):
- Assigned to `flow.response.stream` in `InspectorAddon.responseheaders()` (before the body arrives)
- mitmproxy calls it with raw TCP bytes per chunk
- Buffers until `\n\n` event boundaries, parses each `data:` payload, transforms via LiteLLM's per-provider `ModelResponseIterator.chunk_parser()`, re-serializes as OpenAI-format SSE
- Provider dispatch: Anthropic -> `handler.py:ModelResponseIterator`, Gemini -> `vertex_and_google_ai_studio_gemini.py:ModelResponseIterator`, others -> `config.get_model_response_iterator()`

#### TransformMeta

When a transform or redirect route matches, a `TransformMeta` is stored on the `FlowRecord`:

```
TransformMeta
  ├── provider: str        (e.g. "anthropic", "gemini")
  ├── model: str           (e.g. "claude-sonnet-4-20250514")
  ├── request_data: dict   (LiteLLM request data, for response transform)
  └── is_streaming: bool   (True if stream=True in request body)
```

This persists across the request->response boundary. The response handler uses it to:
1. Select the correct response transformer (non-streaming)
2. Create the correct `SseTransformer` (streaming)

### The WireGuard Namespace Jail

`ccproxy run --inspect -- <command>` creates a rootless Linux user+net namespace:

```
┌─────────────────────────────────┐         ┌─────────────────────┐
│  Namespace                      │         │  Host               │
│                                 │         │                     │
│  ┌──────────┐   ┌───────────┐  │         │  ┌───────────────┐  │
│  │ command  │──▶│  wg0      │──┼── UDP ──┼──│  mitmproxy    │  │
│  └──────────┘   │10.0.0.1/32│  │         │  │  WG listener  │  │
│                 └───────────┘  │         │  └───────────────┘  │
│                                 │         │                     │
│  ┌──────────────────────────┐  │         │                     │
│  │  tap0 (slirp4netns)     │──┼── TCP ──┼── host loopback     │
│  │  10.0.2.100/24          │  │         │  (port forwarding)   │
│  └──────────────────────────┘  │         │                     │
└─────────────────────────────────┘         └─────────────────────┘
```

- All outbound traffic routes through `wg0` into mitmproxy's WireGuard listener
- `slirp4netns` provides a TAP device for the namespace's outbound connectivity to the host
- `PortForwarder` polls `/proc/{ns_pid}/net/tcp` every 0.5s and dynamically forwards new LISTEN ports via `slirp4netns` API
- OAuth callback ports are forwarded via iptables DNAT rules when available

### Configuration Reference

```yaml
host: 127.0.0.1
port: 4000

inspector:
  port: 8083                    # mitmweb UI port
  cert_dir: null                # mitmproxy CA cert store (null = default ~/.mitmproxy)
  provider_map:                 # hostname -> OTel gen_ai.system attribute
    api.anthropic.com: anthropic
    api.openai.com: openai
    generativelanguage.googleapis.com: google
    openrouter.ai: openrouter
  transforms: []                # TransformRoute list (see above)
  mitmproxy:                    # Passed through to mitmproxy Options
    ssl_insecure: true
    stream_large_bodies: "1m"
    web_host: "127.0.0.1"
    web_open_browser: false

oat_sources:                    # OAuth/API key sources per provider
  anthropic:
    command: "oauth-tool get-token anthropic"
    user_agent: "claude-code/1.0"
    destinations: ["api.anthropic.com"]
    auth_header: null            # null = Authorization: Bearer {token}
  gemini:
    file: "/path/to/api-key"
    destinations: ["generativelanguage.googleapis.com"]
    auth_header: "x-goog-api-key"

hooks:
  inbound:
    - ccproxy.hooks.forward_oauth
    - ccproxy.hooks.extract_session_id
  outbound:
    - ccproxy.hooks.inject_mcp_notifications
    - ccproxy.hooks.verbose_mode
    - ccproxy.hooks.apply_compliance
```

---

## Part 2: The Compliance System

### Overview

The compliance system passively learns the "compliance contract" -- the exact headers, body envelope fields, system prompt, and body wrapping pattern that a legitimate CLI client sends -- and then stamps that contract onto non-compliant SDK requests. It bridges the gap between what a bare SDK sends (minimal headers, no system prompt, no envelope fields) and what a provider API actually requires for full functionality.

The core insight: WireGuard-jailed CLI traffic is the reference source. It shows exactly what a compliant request looks like. Reverse proxy SDK traffic is the consumer. It gets the learned profile applied before hitting the provider.

### Architecture

```
WireGuard flow (CLI reference)                   Reverse proxy flow (SDK consumer)
        │                                                  │
        ▼                                                  ▼
 InspectorAddon.request()                         InspectorAddon.request()
        │                                                  │
        ▼                                                  │
 _observe_compliance()                                     │
        │                                                  │
        ▼                                                  │
 observe_flow()                                            │
   ├─ _should_observe() [WireGuard? or ref UA?]            │
   ├─ _resolve_provider() [oat_sources or provider_map]    │
   ├─ extract_observation() ─┐                             │
   │                         ▼                             │
   │              ObservationBundle                        │
   │                         │                             │
   │                         ▼                             │
   │              ProfileStore.submit_observation()        │
   │                ├─ accumulate values                   │
   │                └─ if count >= min_observations:       │
   │                    finalize() → ComplianceProfile     │
   │                    flush to disk                      ▼
   │                         │                     [inbound pipeline]
   │                         │                     [transform phase]
   │                         │                             │
   │                         │                             ▼
   │                         │                     [outbound pipeline]
   │                         │                     apply_compliance hook
   │                         │                             │
   │                         │                             ▼
   │                         └──── get_profile() ────▶ merge_profile()
   │                                                       │
   │                                                       ▼
   │                                               Headers stamped
   │                                               Body fields added
   │                                               System prompt injected
   │                                               Body wrapped (if needed)
   │                                               Session metadata synthesized
```

### How Observation Works

#### Triggering

Observation is triggered in `InspectorAddon.request()` after the `ClientRequest` snapshot is created. Two conditions trigger observation:

1. **WireGuard flows** -- always observed (these are the authoritative reference).
2. **Reference UA patterns** -- if the `user-agent` header matches any substring in `compliance.reference_user_agents` config.

Reverse proxy flows from SDK clients are **never** observed -- they are the consumers, not the reference.

#### Provider Resolution

The observer must map a hostname to a provider name. Two sources, checked in order:

1. `oat_sources.*.destinations` -- substring match on the hostname (e.g. `"api.anthropic.com"` matches a source with `destinations: ["api.anthropic.com"]`).
2. `inspector.provider_map` -- exact hostname key lookup.

If neither resolves, the flow is silently skipped.

#### Feature Extraction

`extract_observation()` produces an `ObservationBundle` from the raw `ClientRequest`:

**Headers**: All headers are lowercased and filtered. Excluded (never profiled):
- Auth tokens: `authorization`, `x-api-key`, `x-goog-api-key`, `cookie`
- Transport: `content-length`, `transfer-encoding`, `host`, `connection`, `accept-encoding`
- Internal: `x-ccproxy-flow-id`, `x-ccproxy-oauth-injected`, `x-ccproxy-hooks`

Everything else is a candidate -- `user-agent`, `anthropic-beta`, `anthropic-version`, `x-app`, `x-goog-api-client`, `content-type`, etc.

**Body**: Each top-level JSON key is classified:
- **Content fields** (never profiled): `messages`, `contents`, `prompt`, `tools`, `tool_choice`, `model`, `stream`, `max_tokens`, `max_completion_tokens`, `temperature`, `top_p`, `top_k`, `stop`, `n`
- **`system`**: extracted separately, stored as its own field on the bundle.
- **Wrapper detection**: if a non-content dict field contains `messages`, `contents`, or `prompt` as sub-keys, it is the `body_wrapper` (e.g. `request` in cloudcode-pa's `{model: X, request: {messages: [...]}}`). First match wins.
- **Everything else**: goes into `body_envelope` as candidate envelope fields (e.g. `metadata`, `thinking`, `user_prompt_id`).

#### Accumulation

The `ObservationAccumulator` collects values across multiple observations for the same `(provider, user_agent)` pair:

```python
header_candidates:  {"anthropic-beta": ["v1,v2", "v1,v2", "v1,v2"]}
body_candidates:    {"metadata": [{...}, {...}, {...}]}
system_observations: ["You are Claude Code...", "You are Claude Code...", ...]
body_wrapper_observations: [None, None, None]  # or ["request", "request", "request"]
```

Each `submit()` call appends values to the per-key lists.

#### Finalization

When `observation_count >= min_observations` (default 3), `finalize()` runs:

A feature is **stable** if `len(set(serialized_values)) == 1` -- identical across all observations. Variable features (per-request IDs, changing metadata) are automatically excluded.

- **Headers**: stable headers become `ProfileFeatureHeader` entries.
- **Body fields**: stable fields become `ProfileFeatureBodyField` entries. Complex values (dicts, lists) are serialized via `json.dumps(sort_keys=True)` for comparison.
- **System prompt**: if all observations have the same system prompt, it becomes a `ProfileFeatureSystem`. Strings are normalized to content-block format: `[{"type": "text", "text": "..."}]`.
- **Body wrapper**: included only if all observations agree on the same non-None wrapper field name.

The resulting `ComplianceProfile` is stored, flushed to disk, and immediately available for the `apply_compliance` hook.

### The Compliance Profile

```
ComplianceProfile
  ├── provider: str                    ("anthropic", "gemini", ...)
  ├── user_agent: str                  (full UA string of the observed client)
  ├── created_at / updated_at: str     (ISO timestamps)
  ├── observation_count: int           (how many observations produced this)
  ├── is_complete: bool                (always True after finalization)
  ├── headers: [ProfileFeatureHeader]  (name/value pairs to stamp)
  ├── body_fields: [ProfileFeatureBodyField]  (path/value pairs to add)
  ├── system: ProfileFeatureSystem | None     (content-block structure to inject)
  └── body_wrapper: str | None         (field name for body wrapping)
```

Persisted as JSON at `{config_dir}/compliance_profiles.json` with atomic write (temp + rename).

### Seeding: The Anthropic v0 Profile

On first startup (when no Anthropic profile exists), the store creates a seed profile from hardcoded constants:

```python
ComplianceProfile(
    provider="anthropic",
    user_agent="v0-seed",
    headers=[
        ProfileFeatureHeader("anthropic-beta", "oauth-2025-04-20,..."),
        ProfileFeatureHeader("anthropic-version", "2023-06-01"),
    ],
    system=ProfileFeatureSystem([
        {"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude."}
    ]),
)
```

This seed provides baseline compliance before any reference traffic is observed. It is superseded as soon as real observations finalize a new profile (the store returns the most recently `updated_at` profile for a provider, and the seed's `updated_at` is epoch zero).

Controlled by `compliance.seed_anthropic: true` (default).

### Profile Application: The `apply_compliance` Hook

The `apply_compliance` hook runs in the outbound pipeline, after transform but before the request reaches the provider.

#### Guard

Only fires when:
1. The flow came through `ReverseMode` (not WireGuard -- those are reference traffic, not consumers).
2. The flow has a `TransformMeta` on its `FlowRecord` (it was matched by a transform/redirect route).

#### Profile Selection

```python
provider = transform.provider                          # from TransformMeta
ua_hint = config.get_auth_provider_ua(provider)        # from oat_sources[provider].user_agent
profile = store.get_profile(provider, ua_hint=ua_hint)
```

The `ua_hint` bridges the observation and application sides: the `OAuthSource.user_agent` field tells ccproxy which observed profile to select. If the CLI was observed with UA `"claude-code/1.0.42"` and the oat_source has `user_agent: "claude-code"`, the substring match connects them.

When multiple profiles exist for a provider, the most recently updated one wins.

#### The Merge Operations

`merge_profile()` applies five operations, all idempotent (applying twice produces the same result):

**1. Headers** (`_merge_headers`)

For each header in the profile: add it only if the request doesn't already have it. Never overwrites.

Example: a bare SDK request missing `anthropic-beta` and `anthropic-version` gets them stamped from the profile. An SDK request that already sets these headers keeps its values.

**2. Session Metadata** (`_merge_session_metadata`)

If the profile learned a `metadata.user_id` containing `device_id` and/or `account_uuid`, the merger synthesizes a fresh session identity:

```json
{
  "device_id": "<from profile>",
  "account_uuid": "<from profile>",
  "session_id": "<freshly generated UUID>"
}
```

Stable identity fields come from the profile; `session_id` is fresh per-request. Only applies if `metadata.user_id` is absent in the request.

**3. Body Wrapping** (`_wrap_body`)

For cloudcode-pa style APIs where the body must be:
```json
{"model": "gemini-2.0-flash", "request": {"messages": [...], ...}}
```

If `profile.body_wrapper` is set (e.g. `"request"`), the merger:
1. Extracts `model` from the body, `TransformMeta`, or URL path (`/models/{model}`)
2. Moves the entire body into the wrapper field
3. Sets `model` at the top level

Idempotent: if the wrapper field already exists, no-op.

**4. Body Envelope Fields** (`_merge_body_fields`)

Adds missing envelope fields from the profile. Three categories:

- **Excluded** (`thinking`, `context_management`, `output_config`): never stamped. These are user feature choices, not compliance requirements.
- **Generated** (`user_prompt_id`): a fresh 13-character hex UUID is generated per-request if absent.
- **All others**: added with the learned value if absent; never overwritten.

**5. System Prompt** (`_merge_system`)

The most nuanced merge operation:

| Request's `system` | Profile has system | Action |
|--------------------|--------------------|--------|
| `None` (absent) | Yes | Set to profile's content blocks |
| `str` (simple) | Yes | Prepend profile blocks: `[*profile_blocks, {"type": "text", "text": current}]` |
| `list` (structured blocks) | Yes | **Skip entirely** -- client manages its own identity |
| Any | No | No-op |

The list-skip rule is critical: clients like Claude Code and the Agent SDK send structured content blocks with cache control hints. These clients already handle their own identity and compliance; stamping a profile's system prompt on top would interfere.

### With and Without Compliance

#### Without compliance (`compliance.enabled: false`)

- No observation occurs. WireGuard reference traffic passes through without being analyzed.
- No seed profile is created.
- The `apply_compliance` hook still runs (it's in the outbound pipeline) but `get_store()` returns an empty store, `get_profile()` returns `None`, and the hook returns immediately.
- SDK requests must be self-sufficient: they need their own correct headers, body fields, and system prompts.

#### With compliance, before profile finalization

- Observation accumulates but hasn't reached `min_observations` yet.
- The seed Anthropic profile (if `seed_anthropic: true`) provides baseline coverage for Anthropic targets: `anthropic-beta`, `anthropic-version`, and the Claude Code system prompt prefix.
- Other providers have no profile yet -- SDK requests go through without envelope stamping.

#### With compliance, after profile finalization

- Full learned profile is applied to every matching reverse proxy flow.
- Headers, body fields, system prompt, body wrapping, and session metadata are all stamped.
- The profile automatically evolves: new observations continue to accumulate, and re-finalization updates the profile with the latest stable features.
- Multiple profiles can coexist for different user agents (e.g. a Claude Code CLI profile and an Aider CLI profile, both for Anthropic).

### Profile Lifecycle

```
1. First startup
   └── seed Anthropic profile (if enabled)
       └── baseline headers + system prompt from constants

2. First WireGuard flow observed
   └── ObservationAccumulator created for (provider, user_agent)
       └── observation_count: 1

3. Subsequent WireGuard flows
   └── accumulator.submit() appends values
       └── observation_count: 2, 3, ...

4. min_observations reached (default: 3)
   └── accumulator.finalize()
       └── stable features extracted
       └── ComplianceProfile created, flushed to disk
       └── supersedes seed profile (newer updated_at)

5. Ongoing observations
   └── continue accumulating
       └── re-finalize on each new observation (profile evolves)
       └── flush every 10 observations (incremental persistence)
```

### Configuration Reference

```yaml
compliance:
  enabled: true                 # master switch
  min_observations: 3           # observations before first finalization
  reference_user_agents: []     # additional UA patterns for observation (substring match)
  seed_anthropic: true          # bootstrap Anthropic profile from constants

# Related: oat_sources[provider].user_agent is used as ua_hint for profile selection
oat_sources:
  anthropic:
    command: "get-token"
    user_agent: "claude-code"   # substring-matched against observed profile UAs
```

### Persistence Format

`compliance_profiles.json`:

```json
{
  "format_version": 1,
  "profiles": {
    "anthropic/claude-code/1.0.42 (Linux x86_64)": {
      "provider": "anthropic",
      "user_agent": "claude-code/1.0.42 (Linux x86_64)",
      "created_at": "2026-04-11T12:00:00+00:00",
      "updated_at": "2026-04-11T12:05:00+00:00",
      "observation_count": 5,
      "is_complete": true,
      "headers": [
        {"name": "anthropic-beta", "value": "oauth-2025-04-20,..."},
        {"name": "anthropic-version", "value": "2023-06-01"},
        {"name": "user-agent", "value": "claude-code/1.0.42 (Linux x86_64)"}
      ],
      "body_fields": [
        {"path": "metadata", "value": {"user_id": "{\"device_id\":\"abc\",\"account_uuid\":\"def\",...}"}},
        {"path": "user_prompt_id", "value": "a1b2c3d4e5f67"}
      ],
      "system": {
        "structure": [
          {"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude.", "cache_control": {"type": "ephemeral"}}
        ]
      },
      "body_wrapper": null
    }
  },
  "accumulators": {}
}
```
