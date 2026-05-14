# Perplexity Through ccproxy

Reference for routing OpenAI-format `/v1/chat/completions` requests to
Perplexity Pro's WebUI subscription endpoint via ccproxy. Covers the user
surface (SDK integration, resume modes, MCP tools, configuration) and the
internal architecture (SSE patching, thread continuation, L1 cache,
multimodal uploads, fingerprint impersonation).

The Perplexity integration is structurally *the opposite* of the other
ccproxy providers. Shaping providers (Anthropic, Gemini) accept a CLI on
the inbound side and ccproxy preserves the CLI's wire identity outbound.
Perplexity accepts an **OpenAI SDK** on the inbound side and ccproxy
**translates** OpenAI → Perplexity. There's no native Perplexity client
to mimic, no captured shape, no billing salt, no identity-preservation
layer — just clean format translation.

---

## Table of Contents

- [Quick start](#quick-start)
- [The three resume modes](#the-three-resume-modes)
- [MCP tools](#mcp-tools)
- [Configuration reference](#configuration-reference)
- [Architecture — the hot path](#architecture--the-hot-path)
- [SSE parsing — the four patch modes](#sse-parsing--the-four-patch-modes)
- [Thread continuation — internals](#thread-continuation--internals)
- [The `/search/new` preflight](#the-searchnew-preflight)
- [Multimodal file uploads](#multimodal-file-uploads)
- [Fingerprint impersonation](#fingerprint-impersonation)
- [Headers and the `x-perplexity-request-reason` family](#headers-and-the-x-perplexity-request-reason-family)
- [Code layout](#code-layout)
- [Troubleshooting](#troubleshooting)

---

## Quick start

### 1. Get a session token

Perplexity Pro authenticates via a `__Secure-next-auth.session-token` cookie.
Use the `perplexity-webui-scraper` UV tool's login command to capture one:

```bash
uv tool install perplexity-webui-scraper
uv tool run get-perplexity-session-token   # interactive OTP flow
# Saves token to ~/.config/ccproxy/perplexity-session-token (mode 0600)
```

The token is valid for ~30 days. Re-run the script when it expires.

### 2. Configure ccproxy

In your `ccproxy.yaml` (or via the Nix module):

```yaml
providers:
  perplexity_pro:
    auth:
      type: file
      file: ~/.config/ccproxy/perplexity-session-token
    host: www.perplexity.ai
    path: /rest/sse/perplexity_ask
    provider: perplexity_pro
    fingerprint_profile: chrome131         # curl-cffi TLS impersonation

pplx:
  thread:
    consistency_mode: warn                 # warn | strict | ignore
    citation_mode: markdown                # markdown | default | clean
    ttl_seconds: 1800
```

The provider key (`perplexity_pro`) determines the sentinel that clients use:
`sk-ant-oat-ccproxy-perplexity_pro`.

### 3. Point any OpenAI SDK at ccproxy

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:4000/v1",                        # or 4001 for dev
    api_key="sk-ant-oat-ccproxy-perplexity_pro",
)

resp = client.chat.completions.create(
    model="perplexity/best",
    messages=[{"role": "user", "content": "What is quantum computing?"}],
)
print(resp.choices[0].message.content)
```

Streaming works the same with `stream=True`. The OpenAI Python SDK, LiteLLM,
Aider, and any other OpenAI-compatible client work without modification —
ccproxy translates OpenAI ↔ Perplexity transparently.

### 4. Available models

22 models in the catalog (`src/ccproxy/specs/perplexity_models.json`), addressable
by their OpenAI-style ID:

| Model ID | Tier | Notes |
|---|---|---|
| `perplexity/best` | Pro | Auto-select default Pro model |
| `perplexity/deep-research` | Pro | Deep Research (multi-source reports) |
| `perplexity/sonar-2` | Pro | In-house Sonar 2 (experimental) |
| `perplexity/pro` | Pro | Default Pro model identifier |
| `perplexity/reasoning` | Pro | Reasoning-focused variant |
| `openai/gpt-5.4` / `gpt-5.4-thinking` | Pro | OpenAI GPT-5.4 |
| `openai/gpt-5.5` / `gpt-5.5-thinking` | Max | OpenAI GPT-5.5 |
| `openai/o3` / `o3-pro` | Pro / Max | OpenAI o-series |
| `anthropic/claude-sonnet-4.6` / `…-thinking` | Pro | Claude Sonnet 4.6 |
| `anthropic/claude-opus-4.7` / `…-thinking` | Max | Claude Opus 4.7 |
| `google/gemini-3.1-pro-thinking-low` / `…-high` | Pro | Gemini 3.1 Pro |
| `moonshot/kimi-k2.6-instant` / `…-thinking` | Pro | Kimi K2.6 |
| `nvidia/nemotron-3-super-thinking` | Pro | Nemotron 3 Super 120B |
| `xai/grok-4` | Pro | xAI Grok 4 |
| `deepseek/r1` | Pro | DeepSeek R1 reasoning |

---

## The three resume modes

ccproxy holds no authoritative thread state. Perplexity's server-side thread
library is the source of truth. To enable multi-turn conversations, ccproxy
implements three resolution modes — first match wins.

### Mode 1: Explicit metadata (the recommended channel)

Pass `body.metadata.ccproxy_pplx_thread = "<slug-or-uuid>"` in the OpenAI
request body. ccproxy fetches the thread via `GET /rest/thread/{slug}`,
extracts the latest entry's identifiers, and routes as a follow-up.

```python
resp = client.chat.completions.create(
    model="perplexity/best",
    messages=[{"role": "user", "content": "And how about superposition?"}],
    extra_body={"metadata": {"ccproxy_pplx_thread": "quantum-abc123"}},
)
```

This mode survives:
- ccproxy restarts (no local state required)
- machine changes (the slug is stable on perplexity.ai)
- long time gaps (no TTL — server retains threads indefinitely)
- conversation history edits (you only send the new turn)

Use this when: you have an explicit slug (from a prior response, MCP tool,
or perplexity.ai URL) and want deterministic resume.

### Mode 2: Organic L1 cache (zero-friction in-session multi-turn)

Just resend the full message history. ccproxy keys on the SHA12 hash of the
first user message — if you sent it before in this ccproxy session, the L1
cache has the thread state.

```python
messages = [{"role": "user", "content": "Name a fruit"}]

# Turn 1 — fresh thread
r1 = client.chat.completions.create(model="perplexity/best", messages=messages)
messages.append({"role": "assistant", "content": r1.choices[0].message.content})

# Turn 2 — same first user message → L1 cache hit → resumes on Perplexity
messages.append({"role": "user", "content": "And a vegetable?"})
r2 = client.chat.completions.create(model="perplexity/best", messages=messages)
```

Logs: `pplx_thread_inject: resolved_via=l1_cache backend_uuid=...`

This mode survives:
- everything inside one ccproxy session within the TTL (default 30 min)

Does NOT survive:
- ccproxy restart (L1 cache is in-memory only)
- changing the first user message (different SHA12 → different cache key)

Use this when: you have a normal OpenAI client that just sends history and
you don't want to think about thread IDs.

### Mode 3: Pass-through

No `metadata.ccproxy_pplx_thread`, no L1 cache hit → ccproxy creates a fresh
Perplexity thread for every request. Full OpenAI history is flattened into
`query_str` and sent in one shot.

Use this when: you don't care about thread continuation, or you're
single-shot querying.

### Capturing the slug from responses

Every Perplexity response echoes the thread slug back:

**Non-streaming**: top-level `pplx_thread_url_slug` field on the response:

```json
{
  "id": "chatcmpl-...",
  "choices": [{"message": {"content": "2 + 2 equals 4."}, "finish_reason": "stop"}],
  "pplx_thread_url_slug": "f8788ec5-7a79-4d12-9452-1e8cb49172b7"
}
```

Also a response header: `X-CCProxy-Perplexity-Thread-Slug: f8788ec5-...`

**Streaming**: on the final chunk (the one with `finish_reason: "stop"`):

```
data: {"choices":[{"delta":{"content":"end."},"finish_reason":"stop","index":0}],"pplx_thread_url_slug":"f8788ec5-..."}

data: [DONE]
```

Cooperating clients capture this and round-trip it via
`metadata.ccproxy_pplx_thread` on the next turn. Naive clients ignore the
non-spec field silently.

### Divergence detection

When Mode 1 resolves a slug, ccproxy compares your client-side message
history to the server-side thread:

```python
client_user_turns = sum(1 for m in messages[:-1] if m["role"] == "user")
server_entries = len(thread.entries)
```

If they don't match, your local history has diverged from Perplexity's
authoritative state. Behavior depends on `pplx.thread.consistency_mode`:

| Mode | Behavior |
|---|---|
| `warn` (default) | Continue. Response includes `X-CCProxy-Perplexity-Divergence: turn_count_mismatch: client=X server=Y`. |
| `strict` | Raise 409 Conflict with `{"error": {"type": "pplx_thread_divergence", ...}}`. |
| `ignore` | Silent. No header. |

### Slug not found

If the slug in `metadata.ccproxy_pplx_thread` doesn't exist (or was deleted
on perplexity.ai), ccproxy returns a structured 404:

```json
{
  "error": {
    "type": "pplx_thread_not_found",
    "message": "Perplexity thread 'quantum-abc123' not found or no longer accessible. Verify the slug or remove metadata.ccproxy_pplx_thread to start a new thread."
  }
}
```

This is hard-fail by design — silent degradation (falling back to a new
thread) would lose context invisibly, which is the worst failure mode.

---

## MCP tools

Ten MCP tools surface Perplexity's quota and thread API to the ccproxy
in-daemon FastMCP streamable-HTTP server. Connect from any MCP-aware client
(Claude Code, Cursor, etc.) at `http://127.0.0.1:4030/mcp` (production) or
`4031` (dev) with `Authorization: Bearer <token>`.

The FastMCP server advertises an `instructions=` block telling calling LLMs
to use the `/v1/chat/completions` endpoint for normal Perplexity queries and
reserve MCP tools for **thread library curation + quota observability**.
This is intentional — adding chat through MCP would duplicate the
chat-completions path with an extra hop and tool-call round-trip, so it's
explicitly out of scope.

### Quota observability

#### `pplx_usage(refresh=False)`

Fetches `GET /rest/rate-limit/all` and returns remaining Pro Search
(weekly), Deep Research (monthly), Labs, agentic-research, and per-source
quotas. Cached for 60 seconds — calling LLMs aggressively poll, and an
unbounded poll rate risks a shadow-ban on the session cookie.
`refresh=True` bypasses the cache.

```python
quota = pplx_usage()
# {
#   "remaining_pro": 192,
#   "remaining_research": 19,
#   "remaining_labs": 25,
#   "remaining_agentic_research": 2,
#   "model_specific_limits": {...},
#   "sources": {"source_to_limit": {"bmj": {"monthly_limit": 5, "remaining": 5}, ...}}
# }
```

Call once per session before scheduling expensive queries. Cache survives
across tool invocations within the daemon process.

### Library discovery

#### `list_pplx_threads(search_term="", limit=100, offset=0)`

Lists the user's Perplexity thread library (`POST /rest/thread/list_ask_threads`).
Returns an array of `{slug, title, context_uuid, last_query_datetime, ...}`.

```python
threads = list_pplx_threads(search_term="quantum")
for t in threads[:5]:
    print(t["title"], "→", t["slug"])
```

Pagination via `offset` + `limit`. Server caps `limit` at 100.

#### `list_pplx_recent_threads(exclude_asi=False)`

Lighter than `list_pplx_threads` — wraps `GET /rest/thread/list_recent`. No
pagination, no search, fewer fields per entry. Use for "show me my recent
threads" workflows. `exclude_asi=True` omits Deep Research / ASI threads.

#### `get_pplx_thread(slug_or_uuid)`

Fetches a single thread by slug or context UUID. Returns the full thread
dict with `entries[]` (each entry has `query_str`, `structured_answer`,
`backend_uuid`, `read_write_token`, attachments, etc.).

```python
thread = get_pplx_thread("quantum-abc123")
print(thread["thread"]["title"])
for e in thread["entries"]:
    print("Q:", e["query_str"])
```

### Resume — bring a server thread into a local conversation

#### `import_pplx_thread(slug_or_uuid, citation_mode=None, include_reasoning=False)`

The "convert Perplexity thread to OpenAI messages" tool. Returns a
request-construction kit:

```json
{
  "messages": [
    {"role": "user", "content": "What is quantum computing?"},
    {"role": "assistant", "content": "Quantum computing is... [1](https://...) ..."},
    {"role": "user", "content": "And error correction?"},
    {"role": "assistant", "content": "..."}
  ],
  "metadata": {"ccproxy_pplx_thread": "quantum-abc123"},
  "thread_info": {
    "slug": "quantum-abc123",
    "context_uuid": "...",
    "title": "What is quantum computing?",
    "entry_count": 2
  }
}
```

Assemble the next OpenAI request as:

```python
result = import_pplx_thread("quantum-abc123")
next_request = {
    "messages": result["messages"] + [{"role": "user", "content": "<your new question>"}],
    "metadata": result["metadata"],
}
```

ccproxy sees `metadata.ccproxy_pplx_thread` (Mode 1) and routes as a follow-up.

**Citation modes**: `markdown` (default) embeds URLs as `[N](url)`;
`default` preserves `[N]` markers verbatim; `clean` strips them entirely.
**Reasoning inclusion**: `include_reasoning=True` appends each turn's
`plan_block.goals[].description` strings as a footnote section.

### Library curation — slug-first mutations

All mutation tools are **slug-first**: ccproxy resolves the slug to
`context_uuid` + `read_write_token` internally via `_resolve_thread_ids`.
Callers don't need to surface those low-level IDs.

#### `set_pplx_thread_title(slug, title)`

Wraps `POST /rest/thread/set_thread_title`. Renames a thread to `title`.

#### `update_pplx_thread_access(slug, public)`

Wraps `POST /rest/thread/update_thread_access`. `public=True` sets
`updated_access=2` (shareable); `public=False` sets `1` (private). When
public, the response includes `share_url: "https://www.perplexity.ai/search/{slug}"`.

#### `delete_pplx_thread(slug)`

Wraps `DELETE /rest/thread/delete_thread_by_entry_uuid`. Deletes the entire
thread (all turns). The slug-first signature replaces the previous
`(entry_uuid, read_write_token)` pair.

#### `bulk_delete_pplx_threads(slugs)`

Wraps `DELETE /rest/thread`. Resolves each slug to its `entry_uuid`; sends
them together with a single `read_write_token` (token authority spans the
user's library). Returns `{deleted: [slug...], failed: [{slug, error}...],
response: <upstream>}` — per-slug resolution failures are collected, not
raised, so partial-success cleanup workflows behave sensibly.

#### `export_pplx_thread(slug, format="md")`

Wraps `POST /rest/entry/export`. Exports the thread's **most recent entry**
(slug-first refactor — was previously per-entry by `entry_uuid`). Format is
`"pdf"`, `"md"`, or `"docx"`. Returns `{filename, file_content_64}` —
base64-decode on the client side.

---

## Configuration reference

### Provider block (`providers.perplexity_pro`)

```yaml
providers:
  perplexity_pro:
    auth:
      type: file                           # or `command` (any shell that prints the cookie)
      file: ~/.config/ccproxy/perplexity-session-token
    host: www.perplexity.ai
    path: /rest/sse/perplexity_ask
    provider: perplexity_pro               # ccproxy-internal provider id
    fingerprint_profile: chrome131         # curl-cffi impersonation (recommended)
```

- `auth.type: file` reads the cookie value from disk on every request — no
  refresh logic, no expiry awareness. You re-seed the file with the
  perplexity-webui-scraper login command when the token expires.
- `fingerprint_profile` opts into the curl-cffi sidecar for TLS+HTTP/2
  fingerprinting. Optional but strongly recommended for production.

### Top-level `pplx` block

```yaml
pplx:
  thread:
    consistency_mode: warn        # warn | strict | ignore
    citation_mode: markdown       # markdown | default | clean
    ttl_seconds: 1800             # 30 min L1 cache TTL
```

- `consistency_mode` controls divergence handling in Mode 1.
- `citation_mode` is the default for `import_pplx_thread` (the tool's
  `citation_mode` argument overrides per-call).
- `ttl_seconds` is the L1 cache eviction threshold. Read lazily from config
  on every eviction pass — change the value in YAML and it takes effect
  on the next eviction without a restart.

### Hook registration

The pplx pipeline lives in `nix/defaults.nix`:

```yaml
hooks:
  inbound:
    - ccproxy.hooks.forward_oauth
    - ccproxy.hooks.extract_session_id
    - ccproxy.hooks.extract_pplx_files       # multimodal extraction
    - ccproxy.hooks.pplx_thread_inject       # three-mode resolution
  outbound:
    - ccproxy.hooks.gemini_cli
    - ccproxy.hooks.pplx_preflight           # /search/new warmup
    - ccproxy.hooks.inject_mcp_notifications
    - ccproxy.hooks.verbose_mode
    - ccproxy.hooks.commitbee_compat
    - ccproxy.hooks.shape
```

Order matters: `extract_pplx_files` must run before `pplx_thread_inject`
(file URLs go into `body.pplx.attachments`, which the thread inject hook
then merges with the resolved thread state).

---

## Architecture — the hot path

### Pipeline diagram

```
OpenAI client (openai-python, aider, anything)
   │  POST /v1/chat/completions
   │  Authorization: Bearer sk-ant-oat-ccproxy-perplexity_pro
   │  { model, messages, [stream], [metadata.ccproxy_pplx_thread] }
   ▼
ccproxy port 4000 / 4001 (mitmweb reverse listener)
   │
   ▼ addon chain (registered in inspector/process.py:_build_addons)
   InspectorAddon            stamps flow.metadata["ccproxy.conversation_id"] (SHA12 of first user)
                             stamps flow.metadata["ccproxy.flow_id"]
                             starts OTel span
   MultiHARSaver             HAR capture (passive)
   ShapeCapturer             shape capture (skipped for perplexity — no shaping)
   InspectorRouter (inbound) runs the inbound DAG:
     1. forward_oauth          resolves sentinel → session cookie
                               stamps flow.metadata["ccproxy.oauth_provider"] = "perplexity_pro"
     2. extract_session_id     reads metadata.user_id → flow.metadata["ccproxy.session_id"]
     3. extract_pplx_files     walks messages for image_url parts
                               uploads to S3 via batch_create_upload_urls + multipart + subscribe
                               writes S3 URLs to ctx._body["pplx"]["attachments"]
                               strips non-text parts from ctx._body["messages"]
     4. pplx_thread_inject     resolution chain:
                                 Mode 1: glom(body, "metadata.ccproxy_pplx_thread")
                                 Mode 2: PerplexityThreadStore.get(conversation_id)
                                 Mode 3: no-op
                               injects ctx._body["pplx"] = {last_backend_uuid, read_write_token, frontend_context_uuid}
   InspectorRouter (transform)  calls lightllm.transform_to_provider:
     PerplexityProConfig.validate_environment   stamps Cookie + UA + Origin + x-perplexity-request-reason + x-app-api* headers
     PerplexityProConfig.get_complete_url       returns https://www.perplexity.ai/rest/sse/perplexity_ask
     PerplexityProConfig.transform_request      calls _build_pplx_payload(
                                                  query=_flatten_messages(messages),
                                                  model_id=model,
                                                  extras=optional_params["pplx"])
                                                returns {params: {...28 fields...}, query_str: "..."}
   InspectorRouter (outbound) runs the outbound DAG:
     1. gemini_cli              skip (not Gemini)
     2. pplx_preflight          fires GET /search/new?q=<query[:2000]> as best-effort warmup
     3. inject_mcp_notifications, verbose_mode, commitbee_compat, shape  (all skip)
   TransportOverrideAddon       provider.fingerprint_profile == "chrome131"
                                rewrites flow.request to 127.0.0.1:<sidecar_port>
                                X-CCProxy-Target-Url: https://www.perplexity.ai/rest/sse/perplexity_ask
                                X-CCProxy-Impersonate: chrome131
   │
   ▼ sidecar (transport/sidecar.py)
   httpx-curl-cffi AsyncClient with impersonate=chrome131 sends real Chrome TLS+HTTP/2 to Perplexity
   │
   ▼ Perplexity (www.perplexity.ai/rest/sse/perplexity_ask)
   responds with text/event-stream (12-200 events, JSON per event)
   │
   ▼ response side
   sidecar streams bytes back through mitmproxy
   InspectorAddon.response       stashes raw upstream body to FlowRecord.provider_response.body
   InspectorRouter (transform)   non-streaming: calls handle_transform_response which calls
                                                 PerplexityProConfig.transform_response
                                                 (full SSE parse → OpenAI ChatCompletion JSON)
                                  streaming:     SseTransformer wraps each chunk through
                                                 PerplexityProIterator.chunk_parser
   InspectorRouter (outbound)   skip for response phase
   OAuthAddon.response          skip (Perplexity doesn't use OAuth Bearer; 401 path inactive)
   GeminiAddon.response         skip (not Gemini)
   PerplexityAddon.response     scans FlowRecord.provider_response.body for thread identifiers
                                saves to PerplexityThreadStore keyed by conversation_id
   │
   ▼ client receives
   stream=false → ChatCompletion JSON with pplx_thread_url_slug as non-spec top-level field
   stream=true  → SSE chunks, final chunk carries finish_reason="stop" + pplx_thread_url_slug, then [DONE]
```

### Request transformation — `_build_pplx_payload`

`src/ccproxy/lightllm/pplx.py:165-258`. The OpenAI request becomes a 28-field
Perplexity wire payload `{params: {...}, query_str: "..."}`.

**Per-request UUIDs**
```
frontend_uuid              fresh uuid4 every request (Perplexity expects rotation)
frontend_context_uuid      stable per thread — from optional_params["pplx"]["frontend_context_uuid"]
                           on followup, else fresh uuid4
```

**Production constants** (these are what real browser sessions send)
```
version: "2.18"                              x-app-apiversion header agrees
source: "default"
prompt_source: "user"
use_schematized_api: true                    enables diff_block.patches[] streaming format
send_back_text_in_streaming_api: false       legacy field — leave false
skip_search_enabled: true
should_ask_for_mcp_tool_confirmation: true
supported_features: ["browser_agent_permission_banner_v1.1"]
supported_block_use_cases: [<28 items>]      enables answer_tabs, diff_blocks, media_items, etc.
time_from_first_type: 18361 (first) | 8758 (followup)   simulated typing delay (yes, really)
```

**Routing-dependent**
```
query_source:    "home" first turn | "followup" + last_backend_uuid + read_write_token | "collection"
model_preference: PERPLEXITY_MODELS[model_id]["identifier"]   (e.g. "default", "pplx_alpha", "gpt54")
mode:             PERPLEXITY_MODELS[model_id]["mode"]         ("search" | "research" | "copilot")
search_focus:     _SEARCH_MAP[extras.search_focus]            ("internet" | "writing")
sources:          [_SOURCE_MAP[s] for s in extras.source_focus]   ("web" | "scholar" | "social" | "edgar")
search_recency_filter: _TIME_MAP[extras.time_range] or None   ("DAY"|"WEEK"|"MONTH"|"YEAR"|None)
attachments:      from extras["attachments"]                   (S3 URLs from extract_pplx_files)
is_incognito:     not extras.save_to_library                   (Spaces collection forces False)
```

The `query_str` is built by `_flatten_messages` (pplx.py:122-159) which
collapses the OpenAI message list into one string. System messages are
prefixed `[System]: ` and reordered to the front. Non-text parts (image_url,
etc.) are dropped at this stage — they've already been extracted to S3
attachments by the `extract_pplx_files` hook upstream.

### Streaming vs non-streaming

Both modes share the same parser group; they differ only in how the parsed
state is delivered to the client.

**Non-streaming** — `PerplexityProConfig.transform_response` (pplx.py:600-650):
1. Reads the full buffered SSE response via `raw_response.text.splitlines()`
2. Loops `_parse_sse_line` + `_extract_deltas` over every line
3. `state.answer_seen` and `state.reasoning_seen` accumulate
4. Emits one `Choices(message=Message(role="assistant", content=state.answer_seen))`
5. Stamps `model_response.pplx_thread_url_slug` from `state.ids["thread_url_slug"]`
6. The route layer JSON-encodes and overwrites `flow.response.content`

**Streaming** — `PerplexityProIterator.chunk_parser` (pplx.py:670-720):
1. Called once per parsed SSE chunk by `SseTransformer`
2. State persists across calls (`self._state`)
3. Each chunk → `Delta(content=answer_delta, reasoning_content=reasoning_delta)`
4. `finish_reason = "stop"` only when `state.final` is True (gated on
   `final_sse_message`, NOT on `final` which can appear multiple times)
5. After emitting the stop chunk, `self._terminated = True` and subsequent
   chunks return `None` (suppressed by `SseTransformer`'s
   `if model_chunk is None: return b""`)
6. The terminal chunk carries `response.pplx_thread_url_slug` as a non-spec
   field

---

## SSE parsing — the four patch modes

Perplexity sends the answer as a sequence of JSON patches on a virtual
`markdown_block` field. The patches are inside `event["blocks"][*].diff_block.patches[]`.
Our parser (`_extract_deltas` in pplx.py:260-440) handles four distinct
patch shapes — sometimes interleaved within a single response stream.

### Mode A — root patch with cumulative `answer` string

```json
{"path": "", "value": {"answer": "Recent developments in quantum computing include error correction", "chunks": null, "progress": "DONE"}}
```

Path is `""` (root). Value contains a cumulative `answer` string. Every new
event re-sends the full answer-so-far. We prefix-diff against
`state.answer_seen` and emit only the tail.

```python
if answer_str.startswith(state.answer_seen):
    delta = answer_str[len(state.answer_seen):]
    state.answer_seen = answer_str
```

Legacy mode. Less common today.

### Mode B — root patch with `chunks` array (the dominant mode)

```json
{"path": "", "value": {"chunks": ["2 + 2 eq"], "chunk_starting_offset": 0, "answer": null}}
```

Path is `""` but value carries a `chunks` array. `chunk_starting_offset: 0`
says "start fresh from position 0." We join the chunks; if offset is 0, we
treat it as the new full answer.

```python
new_text = "".join(c for c in chunks if isinstance(c, str))
if offset in (None, 0):
    state.answer_seen = new_text
    delta = new_text
```

### Mode C — incremental chunk append at `/chunks/N`

```json
{"path": "/chunks/1", "value": "ual"}
{"path": "/chunks/2", "value": "s 4."}
```

After Mode B sets `chunks: ["2 + 2 eq"]` at index 0, subsequent patches
append one chunk at a time. We append directly to `state.answer_seen`.

```python
if path.startswith("/chunks/") and isinstance(value, str):
    state.answer_seen += value
    answer_delta = value
```

Modes B+C together: `"2 + 2 eq" + "ual" + "s 4." = "2 + 2 equals 4."`

### Mode D — direct cumulative at `/markdown_block` or `/markdown_block/answer`

```json
{"path": "/markdown_block", "value": {"answer": "Recent developments…"}}
{"path": "/markdown_block/answer", "value": "Recent developments…"}
```

Non-root path with cumulative answer. Prefix-diff like Mode A.

### The `intended_usage` filter

Perplexity sends the answer in TWO parallel blocks: `ask_text_0_markdown`
(markdown-formatted) and `ask_text` (plain text). They carry **identical**
patches. Processing both would double every chunk. The parser skips
`ask_text`:

```python
if intended_usage == "ask_text":
    continue
```

This was the bug that produced `"2 + 2 equaluals 4.s 4."` in early testing
— each chunk was being applied to `state.answer_seen` twice.

### Reasoning extraction

Separate codepath. Blocks with `intended_usage in {"pro_search_steps", "plan", "reasoning_plan_block"}`
carry `plan_block.goals[].description` strings. Prefix-diff against
`state.reasoning_seen` produces reasoning deltas, emitted on the OpenAI
stream as `delta.reasoning_content`.

### Identifier capture

Independent of blocks. Six top-level event fields are captured into
`state.ids` whenever they appear:

```python
_PPLX_ID_FIELDS = ("backend_uuid", "read_write_token", "context_uuid",
                   "thread_url_slug", "thread_title", "display_model")

for key in _PPLX_ID_FIELDS:
    val = event.get(key)
    if isinstance(val, str) and val:
        state.ids[key] = val
```

They arrive on different events — `backend_uuid` and `context_uuid` typically
on the first event with results, `read_write_token` and `thread_url_slug`
on the final event. The cache is last-write-wins, so the final event's
values are authoritative.

### The terminal detection

```python
if event.get("final_sse_message"):
    state.final = True
```

`final_sse_message: True` is on exactly ONE event — the true terminator.
`final: True` appears on the SECOND-TO-LAST event too (which still carries
meaningful blocks like `pro_search_steps`). Gating only on
`final_sse_message` prevents emitting `finish_reason="stop"` early and
suppressing the reasoning content that arrives in that late block.

### The clarifying questions trap

Deep Research mode sometimes returns clarifying questions instead of an
answer:

```json
{"text": "[{\"step_type\": \"RESEARCH_CLARIFYING_QUESTIONS\", \"content\": {\"questions\": [\"...\"]}}]"}
```

When detected, the parser raises `_PerplexityClarifyingQuestionsError(questions)`
which surfaces as a 400 to the OpenAI client. The caller can prompt the user
for clarification then retry with a more specific query.

---

## Thread continuation — internals

### The three actors

```
                          ┌──────────────────────────┐
                          │ PerplexityThreadStore    │
                          │ (in-memory TTL, no disk) │
                          │ key: conversation_id     │
                          │ val: PerplexityThreadState│
                          │      (backend_uuid,      │
                          │       read_write_token,  │
                          │       context_uuid,      │
                          │       thread_url_slug)   │
                          └──────────┬───────────────┘
                          read       │       write
                          ▲          │          ▲
                          │          │          │
                 ┌────────┴─────┐    │   ┌──────┴──────────┐
                 │ pplx_thread_ │    │   │ PerplexityAddon │
                 │ inject hook  │    │   │ (response side) │
                 │ (inbound DAG)│    │   │                 │
                 └──────┬───────┘    │   └──────┬──────────┘
                        │            │          │
                        ▼            │          ▼
        injects into ctx._body["pplx"]  │  scans FlowRecord.provider_response.body
        as last_backend_uuid +           │  for IDs after Perplexity responds
        read_write_token +               │
        frontend_context_uuid            │
                                         │
                                  Perplexity server
                                  (canonical thread store)
```

### Resolution chain (`pplx_thread_inject`)

`src/ccproxy/hooks/pplx_thread_inject.py`. Inbound DAG hook running after
`forward_oauth` (needs `flow.metadata["ccproxy.oauth_provider"]`) and
`extract_session_id`. Stops at the first hit.

```
slug = glom(ctx._body, "metadata.ccproxy_pplx_thread", default=None)
if slug:
    # Mode 1 — Body metadata
    try:
        thread = GET /rest/thread/{slug}
    except 404:
        raise _PerplexityThreadNotFoundError
    latest = thread["entries"][-1]
    resolved = {backend_uuid, context_uuid, read_write_token}
    resolved_via = "metadata"
    divergence_check(client_user_turns, len(thread.entries))

if not resolved:
    # Mode 2 — Organic L1 cache
    conv_id = flow.metadata["ccproxy.conversation_id"]
    cached = PerplexityThreadStore.get(conv_id)
    if cached:
        resolved = {backend_uuid, context_uuid, read_write_token}
        resolved_via = "l1_cache"

if not resolved:
    # Mode 3 — Pass-through
    return ctx  # no-op

# Inject
ctx._body["pplx"] = {
    "last_backend_uuid":   resolved["backend_uuid"],
    "frontend_context_uuid": resolved["context_uuid"],
    "read_write_token":    resolved["read_write_token"],
}
flow.metadata["ccproxy.pplx.resolved_via"] = resolved_via
```

`ctx._body["pplx"]` flows through LiteLLM's `map_openai_params` into
`optional_params["pplx"]`, which `_build_pplx_payload` reads as `extras`.
The presence of `last_backend_uuid` triggers `query_source: "followup"` and
the entire continuation codepath upstream.

### Divergence math — counting user turns

```python
def _count_client_user_turns(messages):
    if len(messages) < 2:
        return 0
    history = messages[:-1]                       # exclude the new turn
    return sum(1 for m in history
               if (m.get("role") if isinstance(m, dict) else None) == "user")
```

We count user roles directly rather than `len(messages[:-1]) // 2`. The
division would be correct for strict user/assistant alternation but fails
when the client interleaves system messages or tool turns. Counting user
roles is robust to all message shapes.

Server side: `len(thread.entries)` from the GET response. Each Perplexity
entry is strictly one user_query → server_answer pair, so this is a direct
1:1 with client user turns.

### L1 cache lifecycle

`src/ccproxy/lightllm/pplx_threads.py`. The store is a thread-safe in-memory
TTL dict, no disk persistence, no cross-restart durability.

```python
@dataclass(frozen=True)
class PerplexityThreadState:
    backend_uuid: str
    read_write_token: str | None
    context_uuid: str
    thread_url_slug: str | None
    last_used: float

class PerplexityThreadStore:
    def get(self, conversation_id) -> PerplexityThreadState | None: ...
    def save(self, conversation_id, backend_uuid, read_write_token,
             context_uuid, thread_url_slug) -> None: ...
    def _evict_expired_locked(self) -> None: ...   # lazy eviction on every get/save
```

**Lazy TTL binding**: `_get_ttl_seconds()` reads
`get_config().pplx.thread.ttl_seconds` on every eviction pass. Means YAML
changes to `ttl_seconds` take effect on the next eviction without restarting
ccproxy. A constructor override (`ttl_seconds=...`) freezes the TTL for the
lifetime of the instance — used by tests for deterministic eviction.

**Singleton pattern**: `get_pplx_thread_store()` returns the process-wide
instance. `clear_pplx_threads()` is called from the autouse cleanup fixture
in `tests/conftest.py`.

### Writer: `PerplexityAddon.response`

`src/ccproxy/inspector/pplx_addon.py`. The mitmproxy addon that captures
identifiers from completed Perplexity responses.

```python
class PerplexityAddon:
    async def response(self, flow):
        if not self._is_pplx_flow(flow):
            return
        raw_body = self._extract_raw_body(flow)        # see below
        conv_id = flow.metadata.get("ccproxy.conversation_id")
        if not raw_body or not conv_id:
            return
        ids = self._scan_for_ids(raw_body)             # _parse_sse_line + _extract_deltas
        if not ids or not ids.get("backend_uuid"):
            return
        get_pplx_thread_store().save(
            conversation_id=conv_id,
            backend_uuid=ids["backend_uuid"],
            read_write_token=ids.get("read_write_token"),
            context_uuid=ids["context_uuid"],
            thread_url_slug=ids.get("thread_url_slug"),
        )
        flow.metadata["ccproxy.pplx.captured_ids"] = dict(ids)
```

**The `_extract_raw_body` trick**: by the time PerplexityAddon runs, the
route layer's `handle_transform_response` has already overwritten
`flow.response.content` with the OpenAI-format JSON. The raw Perplexity SSE
body is gone from `flow.response.content`. Solution: read from
`FlowRecord.provider_response.body`, which `InspectorAddon.response`
stashed BEFORE the rewrite.

```python
def _extract_raw_body(flow):
    # Preferred: raw upstream body stashed by InspectorAddon
    record = flow.metadata.get(InspectorMeta.RECORD)
    if record and record.provider_response:
        body = record.provider_response.body
        if isinstance(body, bytes) and body:
            return body
    # Fallback for streaming-only paths
    transformer = flow.metadata.get("ccproxy.sse_transformer")
    if transformer and transformer.raw_body:
        return transformer.raw_body
    # Last resort
    return flow.response.content or b""
```

### End-to-end multi-turn lifecycle

```
TURN 1
  Client → ccproxy   { messages: [{user, "Name a fruit"}] }
                     no metadata, conversation_id = sha12("Name a fruit") = "f6e74a48..."
  pplx_thread_inject Mode 1: miss
                     Mode 2: miss (L1 cache empty)
                     Mode 3: pass-through
  _build_pplx_payload  query_source: "home"
  → POST /rest/sse/perplexity_ask
  ← SSE → state.ids = {backend_uuid: B1, context_uuid: C1, slug: S1, rwt: T1, …}
  PerplexityAddon    Store.save("f6e74a48", B1, T1, C1, S1)
  Client ← {content: "Apple", pplx_thread_url_slug: S1}

TURN 2 (organic — client just appends to history)
  Client → ccproxy   { messages: [{user, "Name a fruit"}, {assistant, "Apple"}, {user, "Name a vegetable"}] }
                     no metadata, conversation_id = sha12("Name a fruit") = "f6e74a48..."   ← SAME
  pplx_thread_inject Mode 1: miss
                     Mode 2: HIT — cached = (B1, T1, C1, S1)
                     resolved_via = "l1_cache"
                     ctx._body["pplx"] = {last_backend_uuid: B1, frontend_context_uuid: C1, read_write_token: T1}
  _build_pplx_payload  query_source: "followup", followup_source: "link"
                       last_backend_uuid: B1, read_write_token: T1
                       query_str: "Name a vegetable"           ← only the new turn
  → POST /rest/sse/perplexity_ask
  ← SSE → new state.ids = {backend_uuid: B2, slug: S1 (same!), …}
  PerplexityAddon    Store.save("f6e74a48", B2, T1, C1, S1)   ← updates with new backend_uuid
  Client ← {content: "Carrot", pplx_thread_url_slug: S1}

TURN 3 (cross-restart resume via explicit metadata)
  ccproxy restarts — L1 cache wiped
  Client → ccproxy   { messages: [{user, "And a herb"}],
                       metadata: { ccproxy_pplx_thread: "S1" } }
                     conversation_id = sha12("And a herb") = "9a2c4811..."  ← different
  pplx_thread_inject Mode 1: HIT — slug = S1
                     GET /rest/thread/S1 → entries = [3 entries…]
                     latest entry → resolved = {backend_uuid: B3, context_uuid: C1, rwt: T1}
                     resolved_via = "metadata"
                     divergence: client_user_turns=0, server_entries=3 → "warn" mode, header stamp
                     ctx._body["pplx"] = injected
  → POST /rest/sse/perplexity_ask
  ← SSE → state.ids = {backend_uuid: B4, slug: S1, …}
  PerplexityAddon    Store.save("9a2c4811", B4, T1, C1, S1)
  Client ← {content: "Basil", pplx_thread_url_slug: S1, X-CCProxy-Perplexity-Divergence: ...}
```

---

## The `/search/new` preflight

`src/ccproxy/hooks/pplx_preflight.py`. Outbound hook that fires
`GET https://www.perplexity.ai/search/new?q=<query[:2000]>` BEFORE the main
`POST /rest/sse/perplexity_ask`.

### Why it exists

Per `core-query.md:84-87`:
> Every `perplexity_ask` call **must** be preceded by a GET to this
> endpoint. Without it, the SSE stream may return silently with no results.

Real users go through `/search/new` because the browser navigates to that
URL when they hit enter on perplexity.ai's search box. The server uses the
GET to:

1. **Initialize a search session** for the upcoming POST. Perplexity associates
   the cookie + the query with a session context.
2. **Warm CDN and rate-limit state** keyed on the query.
3. **Log search intent** for analytics.

Without the warmup, the POST sometimes succeeds with HTTP 200 and an open
SSE stream that produces a few status events then terminates with no
answer. Silent failure — the worst kind.

### Why it's a hook, not part of `transform_request`

- **Layer separation**: `transform_request` is a LiteLLM `BaseConfig`
  method whose contract is "given inputs, return the wire payload." Firing
  a side HTTP call there violates that contract.
- **Cost visibility**: as a registered hook, it shows up in
  `Pipeline execution order` logs with its own timing.
- **Symmetry**: mirrors `gemini_cli`'s `prewarm_project` hook (also fires a
  side HTTP call before the main request).

### Why it's best-effort

```python
try:
    httpx.get(PERPLEXITY_PREFLIGHT_URL, params={"q": query[:2000]}, ...)
    ctx.flow.metadata["ccproxy.pplx.preflight"] = True
except Exception:
    logger.warning("pplx_preflight: side request failed", exc_info=True)
    ctx.flow.metadata["ccproxy.pplx.preflight"] = False
return ctx
```

Failure does NOT abort the main request. If the warmup fails AND the
silent-empty-SSE thing happens, the user sees an empty response. That's
strictly better than failing the request outright when the warmup was the
only blocker.

### Truncation

The query is truncated to 2000 chars for the URL. Perplexity returns
HTTP 414 (URI Too Long) above that. The actual `query_str` in the POST
body can be much larger (system prompts + history + question) — we
truncate only for the GET, which just needs to seed the session.

---

## Multimodal file uploads

`src/ccproxy/hooks/extract_pplx_files.py`. Inbound hook that lifts
multimodal content parts from OpenAI requests into Perplexity attachments.

### What it does

OpenAI's chat-completions format allows:

```json
{"role": "user", "content": [
  {"type": "text", "text": "what is in this image?"},
  {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
]}
```

Naive `_flatten_messages` would silently drop the image_url part. This hook
upgrades the flow:

1. **Walks** `ctx._body["messages"]` for non-text parts
2. **Resolves** each part:
   - `data:image/png;base64,...` URIs decoded in-process
   - `http(s)://...` URLs fetched via `httpx.get(url, timeout=10)`
3. **Validates** per `file-uploads.md:323-329`: ≤30 files, ≤50MB each, non-empty
4. **Uploads** via the three-step S3 chain:
   - `POST /rest/uploads/batch_create_upload_urls` → presigned URLs + file_uuids
   - `POST <s3_bucket_url>` per file with `curl_cffi.CurlMime` (fields-first,
     file-last per `file-uploads.md:148-166`)
   - `POST /rest/sse/attachment_processing/subscribe` → drain SSE to completion
     (waits for Perplexity to finish parsing/OCR/thumbnail generation)
5. **Attaches** the S3 object URLs to `ctx._body["pplx"]["attachments"]`
6. **Strips** the non-text parts from `ctx._body["messages"]` so
   `_flatten_messages` builds a clean text-only `query_str`

### Constraints surfaced

```python
_MAX_FILES = 30
_MAX_FILE_SIZE = 50 * 1024 * 1024   # 50 MB
```

Exceeding either raises a structured `_PerplexityFileError` which surfaces
to the client as a 400 with the file name and reason. Never silent.

### Why curl_cffi for the S3 upload

S3 multipart needs **exact** field ordering: presigned form fields first,
the `file` part last. Standard Python multipart libraries can reorder fields,
which fails S3 validation. `curl_cffi.CurlMime` is the same library
Perplexity's own web frontend uses; the ordering matches what S3 expects.

Bonus: the upload also goes through curl-cffi impersonation, so the TLS
fingerprint matches a real browser session.

### Error handling

Failures in the file upload chain surface as 4xx/5xx structured errors:

```json
{
  "error": {
    "type": "pplx_file_too_large",
    "message": "Attachment 'screenshot.png' exceeds 50 MB limit: 73.2 MB"
  }
}
```

```json
{
  "error": {
    "type": "pplx_s3_upload_failed",
    "message": "S3 upload failed for 'image.png': status 403"
  }
}
```

The main `/rest/sse/perplexity_ask` call is NOT attempted if uploads fail
— if you asked the model to analyze an image and ccproxy couldn't upload
the image, sending the query without the attachment would yield a wrong
answer. Fail loudly.

---

## Fingerprint impersonation

### Why it exists

Perplexity sits behind Cloudflare, which uses JA3 TLS fingerprinting to
detect non-browser traffic. Naive Python HTTP libraries (urllib, requests)
have characteristic JA3 fingerprints that Cloudflare blocks. `httpx` over
stock pyOpenSSL works in dev but fails intermittently in production under
load.

The fix: route Perplexity traffic through ccproxy's in-process curl-cffi
sidecar, which uses libcurl + BoringSSL configured to emit Chrome's exact
TLS ClientHello + HTTP/2 SETTINGS frame.

### Activation

One line in `ccproxy.yaml`:

```yaml
providers:
  perplexity_pro:
    fingerprint_profile: chrome131
```

Valid values are validated against `curl_cffi.requests.impersonate.BrowserTypeLiteral`
at config-load time. Common options: `chrome131`, `chrome124`, `firefox144`,
`safari17_2_ios`, `edge101`.

### Wire path

When `fingerprint_profile` is set:

1. `TransportOverrideAddon.request` (`inspector/transport_override_addon.py:31-61`)
   intercepts the outbound flow
2. Stashes the real URL in `X-CCProxy-Target-Url`, profile in `X-CCProxy-Impersonate`
3. Rewrites `flow.request.host/port/scheme` to `127.0.0.1:<sidecar_port>`
4. mitmproxy forwards the rewritten request to the sidecar
5. `Sidecar._handle` (`transport/sidecar.py`) reads the two headers, gets a
   cached `httpx.AsyncClient` via `transport.get_client(host=..., profile=...)`,
   sends the request to the real target
6. Response streams back through the sidecar to mitmproxy to the client

The sidecar is an in-process Starlette+uvicorn HTTP server bound to
`127.0.0.1:<auto>`. Connection pool is keyed on `(host, profile)`, LRU+idle
eviction.

### What mitmweb shows

Two views via the custom contentviews:

- **Client request**: the original OpenAI request
- **Forwarded request**: the post-rewrite request as the sidecar saw it
  (real upstream URL in `X-CCProxy-Target-Url`)

The default mitmweb view shows `127.0.0.1:<sidecar_port>` as the
destination. Use `ccproxy flows compare <id>` or the "Forwarded-Request"
contentview to see the real upstream intent.

### Wireshark decryption

ccproxy writes session keys for both legs to one keylog file:

- `MITMPROXY_SSLKEYLOGFILE=$CCPROXY_CONFIG_DIR/tls.keylog` — for the
  client → mitmproxy leg
- `SSLKEYLOGFILE=$CCPROXY_CONFIG_DIR/tls.keylog` — picked up by curl-cffi
  for the sidecar → upstream leg

Wireshark with this keylog decrypts every leg including Chrome-injected
TLS extensions and the real on-the-wire HTTP/2 bytes.

---

## Headers and the `x-perplexity-request-reason` family

`PerplexityProConfig.validate_environment` (pplx.py:531-560) sets these on
every outbound request:

```http
Cookie:                       __Secure-next-auth.session-token=<token>
User-Agent:                   Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 ... Chrome/131.0.0.0 ...
Origin:                       https://www.perplexity.ai
Referer:                      https://www.perplexity.ai/
Accept:                       text/event-stream, application/json
Content-Type:                 application/json
x-perplexity-request-reason:  perplexity-query-state-provider
x-app-apiversion:             2.18
x-app-apiclient:              default
x-request-id:                 <uuid4>
sec-fetch-dest:               empty
sec-fetch-mode:               cors
sec-fetch-site:               same-origin
```

### The `x-perplexity-request-reason` family

Tells Perplexity's backend which client-side codepath originated the
request. Different actions use different values:

| Header value | Endpoint |
|---|---|
| `perplexity-query-state-provider` | `/rest/sse/perplexity_ask` (main ask) |
| `reconnect-stream` | `/rest/sse/perplexity_ask/reconnect/{uuid}` |
| `ask-input-inner-home` | `/rest/sse/attachment_processing/subscribe` |
| `threads-body` | `/rest/thread/list_ask_threads` |
| `thread-body` | `/rest/thread/{slug}` |
| `home-sidebar` | thread delete |
| `entry-export` | `/rest/entry/export` |

Server-side it affects:

1. **Rate-limit bucketing** — different actions share different pools
2. **Telemetry segmentation** — Perplexity slices analytics by request_reason
3. **Soft bot detection** — mismatched reason/endpoint pairings are a weak
   bot signal

ccproxy sends the right value for each endpoint:

- `validate_environment` (main ask) → `perplexity-query-state-provider`
- `pplx_thread_inject._fetch_thread` → `perplexity-query-state-provider`
- `extract_pplx_files._await_processing` → `ask-input-inner-home`
- MCP tools → `perplexity-query-state-provider` (observability calls)

### `x-app-apiclient` and `x-app-apiversion`

Fixed: `default` and `2.18`. The version agrees with the `version` field
inside the request body's `params` block. Mismatched versions sometimes
trigger schema-validation errors server-side.

### `sec-fetch-*`

CORS-related headers a real browser sends. Required for some Perplexity
endpoints to accept the request as a same-origin XHR rather than a
cross-origin or programmatic request.

---

## Code layout

### Files created or rewritten

```
src/ccproxy/
├── lightllm/
│   ├── pplx.py                       # renamed from perplexity.py; full rewrite
│   │   ├── _build_pplx_payload       # 28-field production payload (165-258)
│   │   ├── _flatten_messages         # OpenAI messages → query_str (122-159)
│   │   ├── _parse_sse_line           # data: <json> → dict (260-280)
│   │   ├── _extract_deltas           # the four-patch-mode parser (282-440)
│   │   ├── _StreamState              # answer_seen, reasoning_seen, ids, final
│   │   ├── _PerplexityException, _PerplexityThreadNotFoundError, _PerplexityClarifyingQuestionsError
│   │   ├── _extract_final_answer     # for thread → OpenAI conversion
│   │   ├── _format_citations         # [N] → [N](url) | strip | preserve
│   │   ├── _thread_to_openai_messages # the MCP import helper
│   │   ├── PerplexityProConfig       # LiteLLM BaseConfig subclass
│   │   └── PerplexityProIterator     # streaming chunk parser
│   └── pplx_threads.py               # NEW
│       ├── PerplexityThreadState     # frozen dataclass
│       ├── PerplexityThreadStore     # in-memory TTL store
│       ├── _get_ttl_seconds          # lazy config read
│       ├── get_pplx_thread_store     # singleton accessor
│       └── clear_pplx_threads        # test cleanup
├── hooks/
│   ├── pplx_preflight.py             # NEW: /search/new warmup
│   ├── pplx_thread_inject.py         # NEW: three-mode resolution
│   └── extract_pplx_files.py         # NEW: multimodal → S3 attachments
├── inspector/
│   └── pplx_addon.py                 # NEW: SSE state capture → L1 cache
├── specs/
│   └── perplexity_models.json        # refreshed: 15 → 22 models
└── mcp/
    └── server.py                     # added 5 pplx MCP tools

tests/
├── conftest.py                       # added clear_pplx_threads()
└── test_lightllm_pplx.py             # NEW: 19 tests

nix/
└── defaults.nix                      # added pplx block, hook registrations, fingerprint_profile

docs/
└── pplx.md                           # this document
```

### Modified files

```
src/ccproxy/lightllm/registry.py      # import from pplx (was perplexity)
src/ccproxy/lightllm/dispatch.py      # import from pplx (was perplexity)
src/ccproxy/inspector/process.py      # register PerplexityAddon in _build_addons
src/ccproxy/hooks/__init__.py         # export the three new pplx hooks
src/ccproxy/config.py                 # add PplxThreadConfig, PplxConfig classes
                                        + CCProxyConfig.pplx field
```

### Renamed

```
src/ccproxy/lightllm/perplexity.py    →    pplx.py
                                            (existing tests still load via registry)
```

### Test coverage

`tests/test_lightllm_pplx.py` has 19 test functions covering:

- Registry resolution
- Model catalog presence
- Payload construction (first turn, followup, unknown model, Spaces)
- Message flattening (drops image_url parts)
- SSE line parsing (positive and negative cases)
- Delta extraction (prefix-diffing for both answer and reasoning)
- Clarifying questions exception path
- Thread → OpenAI conversion (with citation modes)
- Thread store save/get/eviction lifecycle
- TTL eviction with explicit override
- Config defaults and Literal validation
- File-upload helpers (data URI decoding)
- User-turn counting (with system message interleaving)
- PerplexityAddon SSE ID scanning
- Iterator delta emission (content + reasoning + slug echo)

All 80 lightllm + config + pplx tests pass; the broader 957-test suite has
one pre-existing failure (`test_routing.py::test_blacklisted_domain_gets_default_response`)
unrelated to this work.

---

## Troubleshooting

### "session token cannot be empty"

The `auth.file` path is missing or empty. Re-run
`uv tool run get-perplexity-session-token` to generate one.

### Empty answer / silent SSE

The `/search/new` warmup may have failed. Check logs for
`pplx_preflight: side request failed`. The main request still went through,
but Perplexity returned empty results. Possible causes:

- Cloudflare blocked the GET (rare; impersonation should prevent this)
- Session token expired (check `~/.config/ccproxy/perplexity-session-token`)
- Network issue (warmup has 5s timeout)

### `pplx_thread_not_found`

The slug in `metadata.ccproxy_pplx_thread` doesn't exist on perplexity.ai.
Either:

- The thread was deleted via web UI or `delete_pplx_thread`
- You're using a slug from a different account (slugs are per-user)
- The slug is stale or typo'd

Action: remove `metadata.ccproxy_pplx_thread` to start fresh, or re-import
the thread via `import_pplx_thread`.

### `pplx_thread_divergence` (strict mode)

Your client-side message history has a different turn count than
Perplexity's server-side thread. Usually because you edited messages
locally. Options:

- Switch to `pplx.thread.consistency_mode: warn` to continue with the
  server state (your local edits are silently dropped, but the request
  proceeds)
- Re-import the thread via `import_pplx_thread` to sync local history with
  server state, then continue
- Remove `metadata.ccproxy_pplx_thread` to start a new thread

### Mode 2 (L1 cache) not hitting

Check `flow.metadata["ccproxy.conversation_id"]`:

```bash
ccproxy flows compare <flow_id> | grep conversation_id
```

If the SHA12 differs between Turn 1 and Turn 2, your client changed the
first user message between turns. The L1 cache keys on the first user
message — any change misses.

Also check the TTL: default 30 min. If your turns are spaced further apart,
the cache evicts. Either bump `pplx.thread.ttl_seconds` or switch to
Mode 1 (explicit metadata).

### Streaming returns one giant chunk instead of incremental tokens

Likely cause: `send_back_text_in_streaming_api: true` in the request body
(legacy mode B alternative). The current parser is tuned for
`send_back_text_in_streaming_api: false` which gives the
diff_block.patches[] schematized format. Don't override this field.

### Duplicate text in answer (`"2 + 2 equaluals 4.s 4."` pattern)

The `intended_usage == "ask_text"` filter is missing or broken. Both
`ask_text_0_markdown` and `ask_text` carry identical patches; processing
both doubles every chunk. The parser should skip `ask_text`.

### `Hook 'pplx_thread_inject' reads unavailable keys: ['metadata.ccproxy_pplx_thread']`

Benign warning. The hook declares a read of `metadata.ccproxy_pplx_thread`
but the body has no such key. Expected when the user isn't doing explicit
resume; the hook still runs (via guard) and falls through to Mode 2 or 3.
Can be silenced by removing the read declaration from the `@hook` decorator
but the warning is informative.

### Wireshark shows `127.0.0.1:<port>` instead of `www.perplexity.ai`

You're seeing the mitmproxy → sidecar leg. To see the real upstream, look
at the next outbound connection from the sidecar process to
`www.perplexity.ai:443`. With the TLS keylog file loaded, both legs
decrypt.

### `ccproxy_pplx_thread` metadata key being filtered out by client

Some OpenAI SDKs validate the `metadata` dict against a strict schema and
drop unknown keys. Use `extra_body={"metadata": {"ccproxy_pplx_thread": "..."}}`
in `openai-python` to bypass the validator. Or set the key on the request
via the SDK's raw HTTP layer.
