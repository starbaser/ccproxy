# ccproxy Inspector, Flows & Request Shaping

## Introduction

When ccproxy transforms LLM API traffic — rerouting an OpenAI-format request to Anthropic, or channeling a Gemini SDK call through a different endpoint — the resulting outbound request is structurally correct but potentially incomplete. The `lightllm` transform produces valid API payloads, but the non-obvious compliance metadata that makes a request indistinguishable from a native SDK call can be lost: beta headers, user-agent patterns, system prompt preambles, client identity markers, and session metadata.

ccproxy solves this through a three-stage pipeline: **inspect**, **query**, and **shape**.

- **Inspect**: An in-process mitmweb instance captures every HTTP flow, snapshotting the request both before and after the hook pipeline mutates it, and the response both as the provider sent it and as the client received it. Four temporal states per flow, observable in real time.
- **Query**: A suite of CLI tools (`ccproxy flows`) lets you list, filter, diff, compare, and export flows. A jq-powered filtering pipeline narrows the working set. HAR export gives you Chrome DevTools-compatible archives with paired entries showing exactly what changed.
- **Shape**: Once you've identified a known-good request carrying the full compliance envelope, you capture it as a **shape**. From that point forward, ccproxy's outbound **shape** hook automatically inhabits every subsequent request with that shape's compliance metadata.

---

## The Inspector — Architecture & Internals

### In-Process mitmweb

ccproxy embeds mitmweb directly in-process via mitmproxy's `WebMaster` API (`inspector/process.py`). The proxy process, the interception layer, and the web UI are a single Python process sharing state.

Two listeners bind simultaneously:

```
┌─────────────────────────────────────────────────────┐
│                    ccproxy process                   │
│                                                     │
│  ┌─────────────────────┐  ┌──────────────────────┐  │
│  │   Reverse Proxy     │  │   WireGuard Tunnel   │  │
│  │   :4000 (default)   │  │   :UDP (dynamic)     │  │
│  │                     │  │                       │  │
│  │  SDK clients point  │  │  Namespace-jailed     │  │
│  │  here directly      │  │  CLI tools route      │  │
│  │                     │  │  ALL traffic here     │  │
│  └─────────┬───────────┘  └──────────┬────────────┘  │
│            │                         │               │
│            └──────────┬──────────────┘               │
│                       ▼                              │
│              ┌────────────────┐                      │
│              │  Addon Chain   │                      │
│              └────────────────┘                      │
│                       │                              │
│                       ▼                              │
│              ┌────────────────┐                      │
│              │   mitmweb UI   │                      │
│              │   :8083        │                      │
│              └────────────────┘                      │
└─────────────────────────────────────────────────────┘
```

The **reverse proxy** listener (`reverse:http://localhost:1@{port}`) serves SDK clients that connect directly — an OpenAI or Anthropic SDK configured with `base_url=http://127.0.0.1:4000`. The placeholder backend is overwritten by the transform router before the request leaves.

The **WireGuard** listener (`wireguard:{conf}@{udp_port}`) accepts traffic from CLI tools running inside a network namespace jail. In inspect mode (`ccproxy run --inspect`), a rootless user+net namespace redirects all internet traffic through a WireGuard tunnel that terminates at mitmproxy. The jailed process has no direct internet access — everything flows through ccproxy.

A `ReadySignal` addon exposes an `asyncio.Event` that fires when mitmproxy's `running()` hook completes, guaranteeing all listeners are bound before returning control to the caller.

### The Addon Chain

mitmproxy addons fire in registration order. ccproxy registers a fixed chain in `process.py:_build_addons()`:

```
ReadySignal
  │
  ▼
InspectorAddon          OTel spans, FlowRecord lifecycle, SSE streaming,
  │                     client request snapshots, provider response capture,
  │                     401 retry, Gemini response unwrap
  ▼
MultiHARSaver           Registers ccproxy.dump mitmproxy command
  │
  ▼
ShapeCapturer           Registers ccproxy.shape mitmproxy command
  │
  ▼
ccproxy_inbound         DAG-driven hooks: forward_oauth, gemini_cli_compat,
  │                     reroute_gemini, extract_session_id
  ▼
ccproxy_transform       lightllm dispatch: transform, redirect, or passthrough
  │
  ▼
ccproxy_outbound        DAG-driven hooks: inject_mcp_notifications,
                        verbose_mode, shape
```

This registration order is a load-bearing architectural constraint. The `InspectorAddon` snapshots the client request *before* the inbound hooks mutate it. The transform router rewrites the destination and body format. The outbound hooks run last, with `shape` applying the compliance envelope after the transform has already set the correct provider format.

### Flow Lifecycle & Data Model

Every HTTP flow receives a `FlowRecord` (`inspector/flow_store.py`) — a cross-phase state carrier bridging the request and response phases:

- `client_request: HttpSnapshot` — the original request frozen before hooks mutate it
- `provider_response: HttpSnapshot` — the raw response captured before response transforms
- `transform: TransformMeta` — carries provider/model/request_data/is_streaming from request to response phase
- `auth: AuthMeta` — OAuth decision record
- `otel: OtelMeta` — span lifecycle

`HttpSnapshot` is a frozen HTTP message: `headers: dict`, `body: bytes`, optional `method`/`url` (requests) or `status_code` (responses).

Records reside in a thread-safe dictionary keyed by UUID, propagated via the `x-ccproxy-flow-id` header, with a one-hour TTL and cleanup-on-insert garbage collection.

The lifecycle proceeds through six phases:

1. **`InspectorAddon.request()`** — Detects direction, creates `FlowRecord`, snapshots `client_request` as `HttpSnapshot`
2. **Inbound hooks** — OAuth injection, Gemini compat, session extraction
3. **Transform** — lightllm dispatch rewrites destination and body format
4. **Outbound hooks** — MCP notifications, verbose mode, shape
5. **`InspectorAddon.responseheaders()`** — Enables SSE streaming (`SseTransformer` for cross-provider, `True` for passthrough)
6. **`InspectorAddon.response()`** — Captures `provider_response`, handles 401 retry, unwraps Gemini envelopes

### Four HTTP Messages Per Flow

Each flow captures four distinct HTTP messages — the complete before/after picture on both sides of the proxy:

```
         SDK / CLI                    ccproxy                     Provider API
        ─────────                   ─────────                    ────────────
             │                          │                              │
             │  ① Client Request        │                              │
             │──⸺──────────────────────▶│                              │
             │  (pre-pipeline snapshot) │                              │
             │                          │                              │
             │                          │  ② Forwarded Request         │
             │                          │─────────────────────────────▶│
             │                          │  (post-pipeline, transformed)│
             │                          │                              │
             │                          │  ③ Provider Response         │
             │                          │◀─────────────────────────────│
             │                          │  (raw, pre-transform)        │
             │                          │                              │
             │  ④ Client Response       │                              │
             │◀─────────────────────────│                              │
             │  (post-transform)        │                              │
```

Messages ① and ③ are explicitly captured as `HttpSnapshot` objects on the `FlowRecord`. Messages ② and ④ are the live mitmproxy flow state. The flow CLI and HAR export expose all four.

### SSE Streaming

LLM APIs stream responses via Server-Sent Events. mitmproxy requires `flow.response.stream` to be set in `responseheaders` — before the body starts arriving. Setting it in `response` is too late; mitmproxy has already buffered.

`InspectorAddon.responseheaders()` checks for `text/event-stream` and configures streaming:

- **Cross-provider transform**: `flow.response.stream = SseTransformer(...)` — parses, transforms, and re-serializes each SSE event. Tees raw chunks via `raw_body` for provider response capture.
- **Same-provider or passthrough**: `flow.response.stream = True` — bytes pass through unchanged.

The `SseTransformer` is stashed in `flow.metadata["ccproxy.sse_transformer"]` so `response()` can later read `transformer.raw_body`.

### The mitmweb Web UI

The inspector exposes mitmweb's web interface (default port 8083), protected by a bearer token. Two custom content views are registered:

- **Client-Request**: The original request as the SDK sent it — method, URL, headers, body — before pipeline mutations
- **Provider-Response**: The raw provider response — status code, headers, body — before response transforms

Both have `render_priority: -1` (never auto-select, always available in the dropdown). The default mitmproxy view shows post-mutation state; these show pre-mutation state.

---

## The Flow CLI — Querying & Debugging

### The Set Model

Every `ccproxy flows` subcommand operates on a **resolved flow set**:

```
GET /flows (all flows from mitmweb REST API)
  │
  ▼
config.flows.default_jq_filters      Pre-filters from ccproxy.yaml
  │
  ▼
CLI --jq flags                       Per-invocation filters (repeatable)
  │
  ▼
Final set                            What the command operates on
```

Filters are jq expressions executed via the system `jq` binary. Each must consume a JSON array and produce a JSON array. Multiple filters chain with `|`. Config pre-filters run before CLI filters:

```yaml
flows:
  default_jq_filters:
    - 'map(select(.request.pretty_host | endswith("anthropic.com")))'
```

### Commands Reference

| Command | Purpose |
|---|---|
| `ccproxy flows list [--json] [--jq]` | Rich table: ID, method, status, host, path, UA, relative time |
| `ccproxy flows dump [--jq]` | Multi-page HAR 1.2 export to stdout |
| `ccproxy flows diff [--jq]` | Sliding-window unified diff across consecutive request bodies |
| `ccproxy flows compare [--jq]` | Per-flow: client-vs-forwarded request + provider-vs-client response |
| `ccproxy flows shape --provider X [--jq]` | Capture shapes for the request shaping system |
| `ccproxy flows clear [--all] [--jq]` | Delete flows (per-set or all) |

### HAR Export

The HAR export uses a two-entry-per-flow layout exposing all four HTTP messages:

```
Page: "ccproxy flow {flow_id}"
├── entries[2i]    = [forwarded request, provider response]    ← what the provider saw
└── entries[2i+1]  = [client request, client response]         ← what the SDK saw
```

`MultiHARSaver` (`inspector/multi_har_saver.py`) constructs this by cloning each flow twice — a **provider clone** (post-pipeline request + raw response) and a **client clone** (pre-pipeline request + post-transform response). Both share `pageref == flow.id`. All HAR construction delegates to mitmproxy's `SaveHar.make_har()`.

```bash
ccproxy flows dump > session.har                              # Full export
ccproxy flows dump | jq '.log.entries[0].request.url'         # Forwarded URL
ccproxy flows dump | jq '.log.pages | length'                 # Flow count
```

### Practical Examples

```bash
# Filter to Anthropic traffic
ccproxy flows list --jq 'map(select(.request.pretty_host | endswith("anthropic.com")))'

# Diff the last two requests
ccproxy flows diff --jq '[-2:]'

# See what ccproxy changed in the most recent request
ccproxy flows compare --jq '[-1:]'

# Export a single flow
ccproxy flows dump --jq 'map(select(.id | startswith("abc12")))' > flow.har
```

---

## Request Shaping — Capturing Compliance Envelopes

### What a Shape Is

When ccproxy's lightllm transform converts a request, the outbound payload is API-correct but may lack the compliance metadata a native SDK request carries:

- **Beta headers**: `anthropic-beta: prompt-caching-2024-07-31,...`
- **Client identity**: `x-stainless-arch`, `x-stainless-os`, `x-stainless-runtime`
- **User-agent**: The exact UA string the target SDK sends
- **System prompt structure**: Claude Code's compliance preamble as the first system block
- **Metadata identity**: Nested JSON in `metadata.user_id` with `device_id`, `account_uuid`, `session_id`

A **shape** is a verbatim capture of a real, known-good request carrying this complete compliance envelope — a full `mitmproxy.http.HTTPFlow` persisted in native tnetstring format.

### Shape Capture Workflow

```bash
# 1. Start ccproxy and run real traffic through the inspector
just up
ccproxy run --inspect -- claude -p "hello, this is a shape capture"

# 2. List captured flows — look for a 200 to api.anthropic.com
ccproxy flows list

# 3. Verify the flow has all expected compliance headers
ccproxy flows compare

# 4. Capture the shape
ccproxy flows shape --provider anthropic
```

A good shape has a successful (2xx) response, originates from the authentic target SDK, contains the full set of compliance headers, and has a representative system prompt structure.

### Under the Hood

`ccproxy flows shape` invokes `MitmwebClient.save_shape()` → `POST /commands/ccproxy.shape` → `ShapeCapturer.ccproxy_shape()` (`inspector/shape_capturer.py`). The capturer deep-copies the flow, strips all `ccproxy.*` runtime metadata, and appends the clean flow to the provider's shape file via `FlowWriter`.

### Shape Storage

`ShapeStore` (`shaping/store.py`) maintains one `.mflow` file per provider:

```
~/.config/ccproxy/shaping/shapes/
├── anthropic.mflow
├── gemini.mflow
└── ...
```

- **Append-only**: Each `add()` appends; previous shapes are preserved
- **Most-recent wins**: `pick()` returns the last flow in the file
- **Native format**: Inspectable via `mitmweb --rfile`
- **Thread-safe**: All operations under a threading lock

```yaml
shaping:
  enabled: true
  shapes_dir: ~/.config/ccproxy/shaping/shapes
```

---

## Request Shaping — The Shaping Pipeline

### Conceptual Model

The request shaping system works in two phases. A **shape** is the captured specimen — a real, known-good request carrying the full compliance envelope. The **prepare** phase strips the shape's original content, leaving only the structural shell: compliance headers, system preamble, metadata skeleton. The **fill** phase inhabits the empty shell with the incoming request's content. `apply_shape()` stamps the result onto the outbound flow.

```
Shape (captured flow)
  │
  ▼
Deep copy shape.request → working Shape
  │
  ▼
┌──────────────────────┐
│     PREPARE phase    │    Strip shape's original content:
│                      │    messages, model, tools, auth,
│  strip_request_content│    transport headers, system blocks
│  strip_auth_headers  │
│  strip_transport_hdrs│
│  strip_system_blocks │
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐
│      FILL phase      │    Inhabit with incoming content:
│                      │    current model, messages, tools,
│  fill_model          │    system prompt, stream flag,
│  fill_messages       │    fresh UUIDs
│  fill_tools          │
│  fill_system_append  │
│  fill_stream         │
│  regen_prompt_id     │
│  regen_session_id    │
└──────────┬───────────┘
           │
           ▼
apply_shape(shape, ctx)
  │
  ▼
Outbound flow carries shape's
compliance envelope with the
incoming request's content
```

### The Shape Hook

The `shape` hook (`hooks/shape.py`) runs last in the outbound pipeline. Its guard condition (`shape_guard`) ensures it only fires when:

- The flow entered via **reverse proxy** OR has the `ccproxy.oauth_injected` flag
- AND the `FlowRecord` has a completed `TransformMeta`

WireGuard passthrough flows (already authentic) and flows without a transform are not shaped.

When it fires:
1. `store.pick(provider)` — fetches the most recent shape
2. `http.Request.from_state(shape.request.get_state())` — deep-copies as a working `Shape`
3. Iterates configured `prepare` entries, calling each on the shape
4. Iterates configured `fill` entries, calling each with shape + pipeline `Context`
5. `apply_shape(working, ctx)` — stamps onto the outbound flow

### Prepare Functions

Each takes a `mitmproxy.http.Request` shape and mutates it in place. Body mutations use `mutate_body()` (`shaping/body.py`) — a read-modify-write helper handling JSON parse/serialize.

| Function | Strips | Why |
|---|---|---|
| `strip_request_content` | messages, model, tools, toolConfig, tool_choice, prompt, input, stream, thinking, output_config, contents, context_management | Shape's original conversation must be replaced |
| `strip_auth_headers` | authorization, x-api-key, x-goog-api-key | Auth owned by inbound pipeline |
| `strip_transport_headers` | content-length, host, transfer-encoding, connection | Would desync; mitmproxy recomputes |
| `strip_system_blocks(keep)` | system blocks per Python slice | Parameterized: `:1` keeps first, `1:` drops first, `` removes all |

The parameterized syntax works through `_resolve_entry()`: `"strip_system_blocks(:1)"` splits on `(`, imports the function, returns `functools.partial(strip_system_blocks, ":1")`.

### Fill Functions

Each takes the shape plus the pipeline `Context` and mutates the shape with incoming content.

| Function | Fills | Source |
|---|---|---|
| `fill_model` | body.model | ctx.model |
| `fill_messages` | body.messages | ctx.messages |
| `fill_tools` | body.tools, body.tool_choice | ctx._body |
| `fill_system_append` | body.system (appends) | ctx.system → appended after shape's preserved blocks |
| `fill_stream_passthrough` | body.stream | ctx._body["stream"] |
| `regenerate_user_prompt_id` | body.user_prompt_id | uuid.uuid4().hex[:13] |
| `regenerate_session_id` | body.metadata.user_id.session_id | uuid.uuid4() |

The system append pattern is key: `strip_system_blocks(:1)` keeps the shape's first block (compliance preamble), then `fill_system_append` appends the incoming system blocks after it. Result: `[shape preamble] + [incoming system prompt]`.

UUID regeneration prevents replay detection — providers that track deterministic prompt IDs or session IDs across requests won't see the same values from the shape.

### apply_shape()

`apply_shape(shape, ctx)` (`shaping/models.py`) stamps the shape onto the outbound flow with surgical header preservation:

1. Save current values of `_PRESERVE_HEADERS` from the flow: `authorization`, `x-api-key`, `x-goog-api-key`, `host`
2. Clear ALL headers on the flow
3. Copy ALL shape headers (compliance headers, user-agent, beta flags, x-stainless-*, etc.)
4. Restore the preserved headers (overwriting shape values for those keys)
5. Set `flow.request.content = shape.content`
6. Resync `ctx._body` from the shape content

Auth headers from `forward_oauth` and the `host` from the transform router survive shaping. Everything else comes from the shape's compliance envelope.

### Configuration

```yaml
hooks:
  outbound:
    - ccproxy.hooks.inject_mcp_notifications
    - ccproxy.hooks.verbose_mode
    - hook: ccproxy.hooks.shape
      params:
        prepare:
          - ccproxy.shaping.prepare.strip_request_content
          - ccproxy.shaping.prepare.strip_auth_headers
          - ccproxy.shaping.prepare.strip_transport_headers
          - "ccproxy.shaping.prepare.strip_system_blocks(:1)"
        fill:
          - ccproxy.shaping.fill.fill_model
          - ccproxy.shaping.fill.fill_messages
          - ccproxy.shaping.fill.fill_tools
          - ccproxy.shaping.fill.fill_system_append
          - ccproxy.shaping.fill.fill_stream_passthrough
          - ccproxy.shaping.fill.regenerate_user_prompt_id
          - ccproxy.shaping.fill.regenerate_session_id
```

Order matters. Prepare runs top-to-bottom, then fill top-to-bottom. `strip_system_blocks` must precede `fill_system_append`. `strip_request_content` must precede any fill that writes to the same fields.

### Writing Custom Functions

Prepare: `Callable[[http.Request], None]`
Fill: `Callable[[http.Request, Context], None]`

```python
# myproject/shaping/custom.py
from mitmproxy import http
from ccproxy.shaping.body import mutate_body
from ccproxy.pipeline.context import Context

def strip_custom_field(shape: http.Request) -> None:
    mutate_body(shape, lambda b: b.pop("custom_field", None))

def fill_custom_field(shape: http.Request, ctx: Context) -> None:
    value = ctx._body.get("custom_field")
    if value is not None:
        mutate_body(shape, lambda b: b.update(custom_field=value))
```

Reference in config: `myproject.shaping.custom.strip_custom_field`

---

## End-to-End Workflow

```bash
# Initial setup (once per provider)
just up
ccproxy run --inspect -- claude -p "shape capture"
ccproxy flows list
ccproxy flows compare
ccproxy flows shape --provider anthropic

# Verification (after capturing a shape)
# Run a request through the reverse proxy, then:
ccproxy flows compare
# The diff shows the forwarded request carrying shape compliance headers
# alongside your actual message content

# Shape maintenance
# Re-capture when the target SDK updates beta headers or system prompt structure:
ccproxy run --inspect -- claude -p "shape refresh"
ccproxy flows shape --provider anthropic
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| "No shape available for provider X" in logs | Missing shape file | Run `ccproxy flows shape --provider X` |
| Shape hook not firing (no "Applied shape" log) | Guard condition not met: flow lacks transform, or entered via WireGuard passthrough | Verify transform/redirect rule exists; check flow entered via reverse proxy or OAuth |
| System prompt wrong after shaping | Slice syntax misconfigured | Check `:1` (keep first), `1:` (drop first), `` (remove all); verify with `ccproxy flows compare` |
| 400/403 from provider after shaping | Stale shape (SDK updated headers) | Re-capture: `ccproxy run --inspect -- claude -p "refresh"` then `ccproxy flows shape --provider X` |
| Auth headers leaking from shape | `strip_auth_headers` missing from prepare list | Add `ccproxy.shaping.prepare.strip_auth_headers` to prepare config |
