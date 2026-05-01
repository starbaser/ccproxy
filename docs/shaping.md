# ccproxy Request Shaping

## Introduction

When ccproxy transforms LLM API traffic — rerouting an OpenAI-format request to Anthropic, or channeling a Gemini SDK call through a different endpoint — the resulting outbound request is structurally correct but potentially incomplete. The `lightllm` transform produces valid API payloads, but the non-obvious compliance metadata that makes a request indistinguishable from a native SDK call can be lost: beta headers, user-agent patterns, system prompt preambles, client identity markers, and session metadata.

ccproxy solves this through **request shaping**: capture a real, known-good request from the target SDK, persist it as a template, and at runtime inject the incoming request's content into the template's compliance envelope.

---

## Capturing Compliance Envelopes

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

`ccproxy flows shape` invokes `MitmwebClient.save_shape()` → `POST /commands/ccproxy.shape` → `ShapeCapturer.ccproxy_shape()` (`inspector/shape_capturer.py`). The capturer validates the flow (POST method, JSON content-type, `capture.path_pattern` regex), deep-copies it, strips all `ccproxy.*` runtime metadata, and appends the clean flow to the provider's shape file via `FlowWriter`.

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

## The Shaping Pipeline

### Conceptual Model

The shape IS the proven request — a captured, known-good flow carrying the full compliance envelope. At runtime, ccproxy creates a working copy, strips configured headers, injects the incoming request's content into declared fields, runs shape hooks (inner DAG) for dynamic operations, and stamps the result onto the outbound flow.

The identity/content boundary is declared per-provider in YAML config. `content_fields` lists the body keys that come from the incoming request. Everything NOT listed persists from the shape — compliance headers, beta flags, system prompt preamble, metadata skeleton, client identity markers. This inversion means the system doesn't need to enumerate what the envelope contains; it declares what it intends to inject.

```
Shape (captured flow)
  │
  ▼
Deep copy shape.request → working Shape
  │
  ▼
┌──────────────────────────┐
│     STRIP phase          │  Strip headers (auth, transport)
│                          │  per profile.strip_headers
└──────────┬───────────────┘
           │
           ▼
┌──────────────────────────┐
│   INJECT phase           │  Two-pass strip & fill of
│                          │  profile.content_fields using
│                          │  profile.merge_strategies
└──────────┬───────────────┘
           │
           ▼
┌──────────────────────────┐
│  SHAPE HOOKS phase       │  Run profile.shape_hooks via
│                          │  inner DAG (e.g., UUID re-roll)
└──────────┬───────────────┘
           │
           ▼
shape_ctx.commit()            Flush body mutations to working.content
           │
           ▼
apply_shape(working, ctx,     Stamp shape headers + query params + body
  profile.preserve_headers)   onto outbound flow, preserving auth + host
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

1. Gets the provider from `record.transform.provider`
2. Looks up `ProviderShapingConfig` from `config.shaping.providers[provider]`
3. `store.pick(provider)` — fetches the most recent shape
4. `http.Request.from_state(captured.request.get_state())` — deep-copies as a working `Shape`
5. `strip_headers(shape_ctx, profile.strip_headers)` — removes configured headers
6. `_inject_content(shape_ctx, incoming_ctx, profile)` — content injection per merge strategy
7. Runs shape hooks from `profile.shape_hooks` via inner `HookDAG`
8. `shape_ctx.commit()` — flushes body mutations to working request bytes
9. `apply_shape(working, ctx, profile.preserve_headers)` — stamps onto the outbound flow

### Content Injection

`_inject_content(shape_ctx, incoming_ctx, profile)` operates in two passes:

**Pass 1 — Strip**: For each key in `content_fields`, snapshot the shape's value (needed for non-replace strategies), then remove the key from the shape body. After this pass, the shape contains only envelope fields.

**Pass 2 — Fill**: For each key in `content_fields`, inject from the incoming request per the field's merge strategy:

| Strategy | Behavior | Use case |
|---|---|---|
| `replace` (default) | Incoming value replaces shape value. If incoming doesn't have the field, it stays absent. | model, messages, tools, stream, max_tokens |
| `prepend_shape` | Shape's original value prepended before incoming: `[*shape, *incoming]`. Strings auto-wrapped to `[{type: text, text: ...}]`. Append `:N` to keep only the first *N* shape elements (e.g. `prepend_shape:2`). | system (shape preamble + incoming prompt) |
| `append_shape` | Incoming first, shape appended: `[*incoming, *shape]`. Same string normalization. Append `:N` to keep only the first *N* shape elements. | Alternative system ordering |
| `drop` | Field removed entirely (already stripped in pass 1). | Suppress a field |

Null values from either side are coerced to empty lists for safe spreading.

### Shape Hooks (Inner DAG)

Shape hooks handle operations that can't be expressed as field injection — things that require cross-field logic, ID generation, or structural body mutations. They are standard `@hook(reads=..., writes=...)` decorated functions, DAG-ordered by their declarations and executed via `HookDAG` against the shape context.

Each hook has signature `(ctx: Context, params: dict) -> Context` where `ctx` is the shape context. The incoming pipeline context is available via `params["incoming_ctx"]`.

Shape hooks can be either bare module paths (all `@hook`-decorated functions in the module are loaded) or `{hook, params}` dicts for parameterized hooks with a `model=` Pydantic schema:

```yaml
shape_hooks:
  # Bare module path — loads all @hook functions from the module
  - ccproxy.shaping.callbacks
  # Parameterized hook — dict with hook path and params
  - hook: ccproxy.shaping.caching.strip
    params:
      paths: ["system.*.cache_control"]
```

#### Built-in Shape Hooks

| Hook | Module | Purpose |
|---|---|---|
| `regenerate_user_prompt_id` | `ccproxy.shaping.callbacks` | Re-rolls `user_prompt_id` into a new 13-character hex string if the shape carries one. |
| `regenerate_session_id` | `ccproxy.shaping.callbacks` | Parses the nested JSON in `metadata.user_id` and re-rolls `session_id` into a fresh UUID4. `device_id` and `account_uuid` persist (identity markers); only the session changes. |
| `strip` | `ccproxy.shaping.caching.strip` | Deletes values at glom dot-paths from the request body. Parameterized via `StripParams(paths: list[str])`. |
| `insert` | `ccproxy.shaping.caching.insert` | Sets a value at a glom dot-path. Parameterized via `InsertParams(path: str, value: Any)`. Default value: `{"type": "ephemeral"}`. |

### Cache Breakpoint Hooks

Anthropic limits explicit `cache_control` breakpoints to 4 per request. When `prepend_shape:2` merges the shape's system preamble (which carries its own `cache_control` annotations) with the incoming system prompt, the total breakpoint count can exceed this limit, causing API rejections.

The caching hooks in `ccproxy.shaping.caching` solve this by normalizing breakpoints after content injection: strip all existing breakpoints, then insert exactly one at the optimal position for prefix caching.

#### strip

Deletes values at one or more glom dot-paths using `glom.delete()` with `ignore_missing=True`. Non-existent paths are silently skipped.

```yaml
- hook: ccproxy.shaping.caching.strip
  params:
    paths: ["system.*.cache_control"]
```

**`StripParams` fields:**

| Field | Type | Description |
|---|---|---|
| `paths` | `list[str]` | Glom dot-paths to delete. Supports wildcards. |

#### insert

Sets a value at a single glom dot-path using `glom.assign()`. If the target path doesn't exist (e.g., empty list), the operation is silently skipped.

```yaml
- hook: ccproxy.shaping.caching.insert
  params:
    path: "system.-1.cache_control"
    value: {type: ephemeral}
```

**`InsertParams` fields:**

| Field | Type | Default | Description |
|---|---|---|---|
| `path` | `str` | — | Glom dot-path target. |
| `value` | `Any` | `{"type": "ephemeral"}` | Value to set at the path. |

#### Default Anthropic Configuration

The default config strips all `cache_control` from system blocks, then inserts one on the last block (optimal for prefix caching — the longest shared prefix gets cached):

```yaml
shape_hooks:
  - ccproxy.shaping.callbacks
  - hook: ccproxy.shaping.caching.strip
    params:
      paths: ["system.*.cache_control"]
  - hook: ccproxy.shaping.caching.insert
    params:
      path: "system.-1.cache_control"
      value: {type: ephemeral}
```

**Before** (after `prepend_shape:2` merges system blocks):
```
system[0]: shape preamble    → cache_control: {type: ephemeral}  ← from shape
system[1]: shape preamble    → cache_control: {type: ephemeral}  ← from shape
system[2]: app system block  → (none)
system[3]: app system block  → cache_control: {type: ephemeral}  ← from client
system[4]: app system block  → cache_control: {type: ephemeral}  ← from client
```
Total: 4 breakpoints. Any additional client breakpoint exceeds the limit.

**After** (strip + insert):
```
system[0]: shape preamble    → (stripped)
system[1]: shape preamble    → (stripped)
system[2]: app system block  → (stripped)
system[3]: app system block  → (stripped)
system[4]: app system block  → cache_control: {type: ephemeral}  ← inserted
```
Total: 1 breakpoint. The last block is the optimal position because prefix caching benefits from caching the longest shared prefix.

#### Glom Dot-Path Syntax

The caching hooks use [glom](https://glom.readthedocs.io/) for path-based access into nested data structures. Paths are dot-separated, with special syntax for list access:

| Pattern | Meaning | Example |
|---|---|---|
| `field.*.key` | Wildcard — iterates all items in the list | `system.*.cache_control` strips `cache_control` from every system block |
| `field.0.key` | Specific index | `system.0.cache_control` targets the first system block |
| `field.-1.key` | Negative index (last item) | `system.-1.cache_control` targets the last system block |
| `a.b.c` | Nested dict traversal | `metadata.user_id` reaches into nested dicts |

Numeric path segments auto-coerce to list indices. Non-numeric segments are dict key lookups.

### apply_shape()

`apply_shape(shape, ctx, preserve_headers)` (`shaping/models.py`) stamps the shape onto the outbound flow:

1. Snapshot `preserve_headers` values from the target flow (auth headers from `forward_oauth`, host from redirect handler)
2. Clear ALL headers on the target flow
3. Copy ALL shape headers (compliance headers, user-agent, beta flags, x-stainless-*, etc.)
4. Restore the preserved headers (overwriting any shape values for those keys)
5. Merge query parameters from the shape (e.g. `?beta=true`)
6. Set `flow.request.content = shape.content`
7. Resync `ctx._body` from the shape content

Auth headers from `forward_oauth` and the `host` from the transform router survive shaping. Everything else comes from the shape's compliance envelope. The `preserve_headers` list is configurable per-provider.

### Configuration

The shape hook reads its behavior entirely from the per-provider shaping profile in `config.shaping.providers`. The hook is a bare module path — no `{hook, params}` wrapper needed:

```yaml
hooks:
  outbound:
    - ccproxy.hooks.inject_mcp_notifications
    - ccproxy.hooks.verbose_mode
    - ccproxy.hooks.shape

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
        - ccproxy.shaping.callbacks
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

**Field reference (`ProviderShapingConfig`):**

| Field | Type | Default | Purpose |
|---|---|---|---|
| `content_fields` | `list[str]` | `[]` | Body keys injected from incoming request |
| `merge_strategies` | `dict[str, str]` | `{}` | Per-field override: replace, prepend_shape[:N], append_shape[:N], drop |
| `shape_hooks` | `list[str \| dict]` | `[]` | Dotted module paths or `{hook, params}` dicts containing `@hook`-decorated functions, DAG-ordered |
| `preserve_headers` | `list[str]` | auth + host | Target headers apply_shape must NOT overwrite |
| `strip_headers` | `list[str]` | auth + transport | Shape headers to remove before stamping |
| `capture.path_pattern` | `str` | `""` | Regex for flow validation during `ccproxy flows shape` |

### Writing Custom Shape Hooks

Shape hooks use the standard `@hook` decorator with `reads`/`writes` for DAG ordering.

**Simple hook** (no parameters — registered as a bare module path):

```python
# myproject/shaping/custom.py
from typing import Any
from ccproxy.pipeline.context import Context
from ccproxy.pipeline.hook import hook

@hook(reads=["metadata"], writes=["metadata"])
def inject_custom_metadata(ctx: Context, params: dict[str, Any]) -> Context:
    """Add a custom tracking field from the incoming request into the shape."""
    incoming_ctx = params.get("incoming_ctx")
    if incoming_ctx is not None:
        value = incoming_ctx._body.get("custom_tracking_id")
        if value is not None:
            ctx._body["custom_tracking_id"] = value
    return ctx
```

```yaml
shape_hooks:
  - myproject.shaping.custom
```

**Parameterized hook** (accepts config-driven parameters via a Pydantic model):

```python
# myproject/shaping/tag.py
from typing import Any
from pydantic import BaseModel
from ccproxy.pipeline.context import Context
from ccproxy.pipeline.hook import hook

class TagParams(BaseModel):
    key: str
    value: str

@hook(reads=["metadata"], writes=["metadata"], model=TagParams)
def add_tag(ctx: Context, params: dict[str, Any]) -> Context:
    """Set a metadata tag from config params."""
    ctx._body.setdefault("metadata", {})[params["key"]] = params["value"]
    return ctx
```

```yaml
shape_hooks:
  - hook: myproject.shaping.tag
    params:
      key: "environment"
      value: "production"
```

The `model=` kwarg on `@hook` declares a Pydantic model for parameter validation. When `load_hooks()` processes a `{hook, params}` entry, it validates `params` against the model and rejects invalid configurations at load time.

To add a new provider, add an entry under `shaping.providers` with the appropriate `content_fields` for that provider's API schema. No Python code changes required.

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
# Run a request through the reverse proxy with the sentinel key, then:
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
| "No shaping profile for provider X" in logs | Missing provider config | Add `shaping.providers.X` to ccproxy.yaml |
| Shape hook not firing (no "Applied shape" log) | Guard condition not met: flow lacks transform, or entered via WireGuard passthrough | Verify transform/redirect rule exists; check flow entered via reverse proxy or OAuth |
| System prompt missing shape's preamble | `merge_strategies` misconfigured | Ensure `system: prepend_shape` is set in the provider's `merge_strategies` config |
| 400 "too many cache_control breakpoints" | Shape system blocks carry `cache_control` that survives `prepend_shape` merge | Add the `strip` and `insert` caching hooks to `shape_hooks` (see Cache Breakpoint Hooks) |
| 400/403 from provider after shaping | Stale shape (SDK updated headers) | Re-capture: `ccproxy run --inspect -- claude -p "refresh"` then `ccproxy flows shape --provider X` |
| Auth headers leaking from shape | `strip_headers` misconfigured | Ensure `authorization` and `x-api-key` are in the provider's `strip_headers` list |
