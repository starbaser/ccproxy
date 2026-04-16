# Provider Response Capture — Design Proposal

## Problem

ccproxy captures three states of a request lifecycle but only one state of the response:

```
Request lifecycle (captured):
  ClientRequest ──→ [inbound pipeline] ──→ [transform] ──→ [outbound pipeline] ──→ ForwardedRequest
                ↑ snapshot                                                          ↑ flow.request (mutated)

Response lifecycle (NOT captured):
  HttpSnapshot ──→ [unwrap/transform] ──→ ClientResponse
                   ↑ LOST                     ↑ flow.response (mutated in-place)
```

Three mutation points silently destroy the raw provider response:
1. `_unwrap_gemini_response` — strips v1internal `{response: {...}}` envelope
2. `handle_transform_response` — `MitmResponseShim` captures raw bytes as a local variable, `transform_to_openai()` normalizes to OpenAI format, then `flow.response.content` is overwritten. The shim goes out of scope.
3. `_retry_with_refreshed_token` — replaces the entire response on 401 retry

The HAR export duplicates the post-transform response into both entries (forwarded-request and client-request pairs), so there is no way to see what the provider actually returned vs what the client received.

## Proposed Changes

### 1. Data Model: `HttpSnapshot` and `FlowRecord`

`ClientRequest` and the provider response are both HTTP message snapshots. Instead
of a parallel `HttpSnapshot` class, unify on a single `HttpSnapshot`:

```python
# flow_store.py
@dataclass
class HttpSnapshot:
    """Frozen copy of an HTTP message (request or response)."""
    status_code: int
    headers: dict[str, str]
    body: bytes

@dataclass
class FlowRecord:
    ...
    client_request: ClientRequest | None = None        # existing (request-specific fields)
    provider_response: HttpSnapshot | None = None      # NEW
```

`ClientRequest` stays as-is — it carries request-specific fields (method, scheme,
host, port, path) that don't apply to responses. `HttpSnapshot` is the minimal
response shape: status code, headers, body. Content-type is just `headers["content-type"]`.

### 2. Capture Point: `InspectorAddon.response()` — BEFORE mutations

In `addon.py`, snapshot `flow.response` before `_retry_with_refreshed_token` and `_unwrap_gemini_response` run:

```python
async def response(self, flow):
    response = flow.response
    if not response:
        return

    # Snapshot raw provider response before any transforms
    record = flow.metadata.get(InspectorMeta.RECORD)
    if record is not None and response.content is not None:
        record.provider_response = HttpSnapshot(
            status_code=response.status_code,
            headers=dict(response.headers.items()),
            body=response.content,
        )

    # Existing mutation logic follows...
```

### 3. Capture Point: `routes/transform.py` response handler

The `handle_transform_response` runs AFTER the addon's `response()`. Currently it overwrites `flow.response.content` with `transform_to_openai()` output. The snapshot from step 2 would already have the pre-transform bytes. No additional capture needed here — the addon fires first.

**Verify ordering**: addon `response()` → xepor RESPONSE route → client. Confirm this via mitmproxy addon chain registration order in `process.py`.

### 4. Streaming: `store_streamed_bodies` + `SseTransformer` tee

#### The mitmproxy streaming gap

When `flow.response.stream` is set (to `True` or a callable like `SseTransformer`),
mitmproxy's `state_stream_response_body` forwards each chunk directly to the client
**without accumulating them**. At end-of-stream, `flow.response.content` is `None` —
the full body was never reassembled. This is controlled by the `store_streamed_bodies`
option (default `False`).

The consequence: `SaveHar.flow_entry()` sees `content = None` → HAR entries for all
SSE/streaming flows get `bodySize: 0`, `content.text: ""`. The `response` hook fires
but `flow.response.content` is `None`. Since most LLM API traffic is streamed SSE,
**the majority of response bodies are currently absent from HAR export**.

#### Mechanism

In `mitmproxy/proxy/layers/http/__init__.py`, `state_stream_response_body`:

```python
for chunk in chunks:
    if self.context.options.store_streamed_bodies:  # False by default — skipped
        self.response_body_buf += chunk
    yield SendHttp(ResponseData(self.stream_id, chunk), self.context.client)

# At ResponseEndOfMessage:
if self.context.options.store_streamed_bodies:       # False — never assigns
    self.flow.response.data.content = bytes(self.response_body_buf)
```

With `store_streamed_bodies = True`, all chunks are accumulated into `response_body_buf`
and `flow.response.data.content` is populated before the `response` hook fires. The
tradeoff is memory — all streamed bodies stay resident until the flow is dropped.

#### Implementation

**Step 1: Set `store_streamed_bodies = True` unconditionally**

In `process.py`'s `_build_opts`, hardcode `store_streamed_bodies = True` via
`opts.update_defer()`. No config exposure needed — ccproxy is an inspector,
capturing response bodies is not optional.

**Step 2: Capture the reassembled client-facing response**

With `store_streamed_bodies = True`, `flow.response.content` is populated at
end-of-stream (before the `response` hook fires). This is the **post-transform**
body (already processed by `SseTransformer` if one was set). The snapshot in
`addon.response()` (from §2 above) would capture this transformed body.

**Step 3: Tee raw provider chunks in `SseTransformer`**

To capture the **pre-transform** provider response for streaming flows, the
`SseTransformer` callable needs to buffer the raw input chunks alongside its
transformation output:

```python
class SseTransformer:
    def __init__(self, ...):
        ...
        self._raw_chunks: list[bytes] = []

    def __call__(self, chunk: bytes) -> bytes:
        self._raw_chunks.append(chunk)    # buffer raw provider bytes
        return self._transform(chunk)      # return transformed bytes

    @property
    def raw_body(self) -> bytes:
        return b"".join(self._raw_chunks)
```

At `response` hook time, if the flow has an `SseTransformer` as `flow.response.stream`,
read `transformer.raw_body` into `record.provider_response.body`. The callable
reference is still live on `flow.response.stream` at this point.

**Step 4: Passthrough streams (`flow.response.stream = True`)**

For passthrough SSE (no transform), raw = client-facing. With `store_streamed_bodies`
enabled, `flow.response.content` has the full body. `provider_response` can be set
to match, or left `None` to signal "no transform occurred."

### 5. HAR Export: Third entry per page

Update `MultiHARSaver._build_client_clone()` or add a third entry:

```
entries[3i]   → [fwdreq, fwdres]                    # forwarded request + client-facing response (current)
entries[3i+1] → [clireq, fwdres]                     # client request + client-facing response (current)
entries[3i+2] → [fwdreq, provider_response]          # forwarded request + raw provider response (NEW)
```

Alternative: keep 2 entries per page but make entries[2i] use the raw provider response and entries[2i+1] use the transformed response. Semantically cleaner:

```
entries[2i]   → [fwdreq, raw provider response]      # what was sent → what came back
entries[2i+1] → [clireq, client-facing response]     # what client sent → what client received
```

This is the more natural pairing and doesn't add a third entry.

### 6. Content View: `HttpSnapshotContentview`

Register a custom mitmproxy content view (like `ClientRequestContentview`) that renders the `HttpSnapshot` snapshot. Accessible at `GET /flows/{id}/response/content/provider-response`.

### 7. CLI: `flows compare` response diff

Extend `_do_compare` in `tools/flows.py` to also diff the response bodies:

```
--- Provider Response (raw from gemini-2.5-flash)
+++ Client Response (transformed to OpenAI format)
```

Uses `provider_response.body` vs `flow.response.content` (from HAR entry response).

## Scope

| Item | Priority | Complexity |
|------|----------|------------|
| `HttpSnapshot` dataclass + `FlowRecord.provider_response` field | P0 | Low |
| Snapshot in `addon.response()` | P0 | Low |
| Hardcode `store_streamed_bodies = True` in `_build_opts` | P0 | Trivial |
| HAR entry restructuring | P0 | Medium |
| `SseTransformer` raw chunk tee | P1 | Medium |
| `flows compare` response diff | P1 | Low |
| `HttpSnapshotContentview` | P1 | Low |

## Verification

- Run `ccproxy run --inspect -- gemini -p "hello"` (passthrough, no transform) — `provider_response` should match `flow.response`
- Run `ccproxy flows compare` on a transform flow — should show request diff AND response diff
- HAR export: open in Chrome DevTools, verify both response variants visible per page
- **Streaming**: verify `flow.response.content` is populated for SSE flows after enabling `store_streamed_bodies`
- **SSE tee**: for a cross-provider transform flow, verify `provider_response.body` contains raw provider SSE and `flow.response.content` contains transformed SSE

## Open Questions

1. **Addon ordering** — **RESOLVED**: `InspectorAddon` is registered at position 1, before
   the transform router at position 4. `InspectorAddon.response()` fires BEFORE
   `handle_transform_response`. The snapshot sees raw provider bytes. See §Reference.8.
2. **Memory**: with `store_streamed_bodies = True`, all streamed bodies stay resident
   until the flow is dropped. The flow store already has TTL support (`_STORE_TTL = 120.0`).
3. **HAR page structure**: 2-entry (reassign semantics) vs 3-entry (additive). The 2-entry
   approach is cleaner but changes the meaning of existing entries.
4. **`store_streamed_bodies` and `SseTransformer` interaction**: with
   `store_streamed_bodies = True`, `flow.response.content` gets the **post-transform**
   bytes (output of the callable). The raw provider bytes are still lost unless the
   `SseTransformer` tee (§4 Step 3) buffers them separately. These are independent —
   `store_streamed_bodies` gives us the client-facing response; the tee gives us the
   provider response.

---

## Implementation Reference

### 1. `process.py` — `_build_opts` (insertion point for `store_streamed_bodies`)

**File:** `src/ccproxy/inspector/process.py`, lines 54–88

```python
def _build_opts(
    wg_cli_conf_path: Path,
    reverse_port: int,
    wg_cli_port: int,
) -> Any:
    from mitmproxy.options import Options
    from ccproxy.config import MitmproxyOptions, get_config

    config = get_config()
    inspector = config.inspector

    opts = Options(
        mode=[
            f"reverse:http://localhost:1@{reverse_port}",
            f"wireguard:{wg_cli_conf_path}@{wg_cli_port}",
        ],
    )

    deferred: dict[str, Any] = {}
    for field_name in MitmproxyOptions.model_fields:
        if field_name == "web_password":
            continue
        value = getattr(inspector.mitmproxy, field_name)
        if value is not None:
            deferred[field_name] = value

    deferred["web_port"] = inspector.port
    # ← INSERT: deferred["store_streamed_bodies"] = True

    opts.update_defer(**deferred)
    return opts
```

### 2. `flow_store.py` — Data model (lines 17–82)

**`ClientRequest`** (lines 38–49) — request-specific snapshot (keeps method/scheme/host/port/path):
```python
@dataclass
class ClientRequest:
    method: str
    scheme: str
    host: str
    port: int
    path: str
    headers: dict[str, str]
    body: bytes
    content_type: str
```

**`HttpSnapshot`** — NEW, minimal HTTP message snapshot (for responses):
```python
@dataclass
class HttpSnapshot:
    status_code: int
    headers: dict[str, str]
    body: bytes
```

**`TransformMeta`** (lines 52–59):
```python
@dataclass
class TransformMeta:
    provider: str
    model: str
    request_data: dict[str, Any]
    is_streaming: bool
    mode: Literal["redirect", "transform"] = "redirect"
```

**`FlowRecord`** (lines 63–71) — needs new `provider_response` field:
```python
@dataclass
class FlowRecord:
    direction: Literal["inbound"]
    auth: AuthMeta | None = None
    otel: OtelMeta | None = None
    client_request: ClientRequest | None = None
    transform: TransformMeta | None = None
```

**`InspectorMeta`** constants (lines 73–77):
```python
class InspectorMeta:
    RECORD = "ccproxy.record"
    DIRECTION = "ccproxy.direction"
```

Store internals: `_STORE_TTL = 120.0`, `clear_flow_store()` resets `_flow_store: dict`.

### 3. `addon.py` — Snapshot insertion point

**`response()`** (lines 185–216) — snapshot goes before line 191:
```python
async def response(self, flow: http.HTTPFlow) -> None:
    try:
        response = flow.response
        if not response:
            return
        # ← INSERT HttpSnapshot(status_code, headers, body) HERE (before any mutations)

        if response.status_code == 401 and flow.metadata.get("ccproxy.oauth_injected"):
            retried = await self._retry_with_refreshed_token(flow)  # mutation 1
            if retried:
                response = flow.response

        if response and response.status_code < 400:
            self._unwrap_gemini_response(flow, response)            # mutation 2

        # ... OTel + logging follows
```

**`responseheaders()`** (lines 149–183) — sets `flow.response.stream`:
- Transform mode: `flow.response.stream = make_sse_transformer(provider, model, optional_params)`
- Passthrough: `flow.response.stream = True`

### 4. `routes/transform.py` — Response handler (mutation 3)

Lines 279–319. Key section:
```python
shim = MitmResponseShim(flow.response)         # line 297 — captures raw bytes
# ... transform_to_openai() consumes shim ...
flow.response.content = json.dumps(            # line 309 — overwrites with OpenAI format
    model_response.model_dump()
).encode()
# shim goes out of scope here — raw provider bytes lost
```

Streaming flows return early at line 291 (`if meta.is_streaming: return`).

### 5. `lightllm/dispatch.py` — `MitmResponseShim` (lines 204–218)

```python
class MitmResponseShim:
    def __init__(self, mitm_response: Any) -> None:
        self.status_code: int = mitm_response.status_code
        self.headers: dict[str, str] = dict(mitm_response.headers.items())
        self._content: bytes = mitm_response.content    # raw provider bytes

    @property
    def text(self) -> str:
        return self._content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self._content)
```

### 6. `lightllm/dispatch.py` — `SseTransformer` (lines 285–348)

```python
class SseTransformer:
    def __init__(self, provider: str, model: str, optional_params: dict[str, Any]) -> None:
        self._iterator = _make_response_iterator(provider, model, optional_params)
        self._buf = b""
        # ← INSERT: self._raw_chunks: list[bytes] = []

    def __call__(self, data: bytes) -> bytes | Iterable[bytes]:
        if self._iterator is None:
            return data
        if data == b"":
            return b"data: [DONE]\n\n"

        self._buf += data
        # ← INSERT: self._raw_chunks.append(data)  (tee raw bytes before transform)
        out = bytearray()

        while b"\n\n" in self._buf:
            event, self._buf = self._buf.split(b"\n\n", 1)
            out += self._process_event(event)

        return bytes(out)

    def _process_event(self, event: bytes) -> bytes:
        # ... SSE parsing, chunk_parser, OpenAI re-serialization ...
```

Tee insertion: line 303 (`self._buf += data`), add `self._raw_chunks.append(data)`.
At response time, read `transformer.raw_body` (property: `b"".join(self._raw_chunks)`).

### 7. `multi_har_saver.py` — HAR layout

**`ccproxy_dump`** (lines 38–86) — interleaves `[real, clone, real, clone, ...]`:
```python
entries[2 * i]["pageref"] = page_id          # fwdreq + fwdres
entries[2 * i + 1]["pageref"] = page_id      # clireq + fwdres (same response)
```

**`_build_client_clone`** (lines 97–125) — rebuilds request from `ClientRequest` snapshot,
copies response as-is via `flow.copy()`. No response transformation applied to clone.

### 8. Addon registration order (`process.py` lines 119–183, 263)

```
Position 0: ReadySignal
Position 1: InspectorAddon          ← response() fires HERE (sees raw provider bytes)
Position 2: MultiHARSaver
Position 3: ccproxy_inbound (xepor REQUEST routes for inbound DAG)
Position 4: ccproxy_transform       ← handle_transform_response fires HERE (overwrites body)
Position 5: ccproxy_outbound (xepor REQUEST routes for outbound DAG)
```

Confirmed: `InspectorAddon.response()` fires BEFORE `handle_transform_response`.
The snapshot in `addon.response()` captures raw provider bytes before any transform mutation.

### 9. `contentview.py` — Template for `HttpSnapshotContentview`

Full `ClientRequestContentview` (lines 1–55):
```python
class ClientRequestContentview(Contentview):
    @property
    def name(self) -> str:
        return "Client-Request"

    @property
    def syntax_highlight(self) -> SyntaxHighlight:
        return "yaml"

    def prettify(self, data: bytes, metadata: Metadata) -> str:
        flow = metadata.flow
        if flow is None:
            return "(no flow context)"
        record = flow.metadata.get(InspectorMeta.RECORD)
        if record is None or record.client_request is None:
            return "(no client request snapshot)"
        cr = record.client_request
        lines = [
            f"{cr.method} {cr.scheme}://{cr.host}:{cr.port}{cr.path}",
            "", "--- Headers ---",
        ]
        for k, v in cr.headers.items():
            lines.append(f"  {k}: {v}")
        lines.append("")
        lines.append("--- Body ---")
        if not cr.body:
            lines.append("(empty)")
        else:
            try:
                lines.append(json.dumps(json.loads(cr.body), indent=2))
            except Exception:
                lines.append(cr.body.decode("utf-8", errors="replace"))
        return "\n".join(lines)

    def render_priority(self, data: bytes, metadata: Metadata) -> float:
        return -1
```

Registered in `process.py` line 133: `contentviews.add(ClientRequestContentview())`.

### 10. `tools/flows.py` — `_do_compare` (lines 391–445)

Currently diffs only request bodies:
```python
fwd_body = _format_body(fwd_entry["request"].get("postData", {}).get("text"))
cli_body = _format_body(cli_entry["request"].get("postData", {}).get("text"))
# ... unified_diff(cli_body, fwd_body) ...
```

Response diffing would extract from HAR entries:
```python
fwd_response = _format_body(fwd_entry["response"].get("content", {}).get("text"))
cli_response = _format_body(cli_entry["response"].get("content", {}).get("text"))
```

### 11. Test infrastructure

**`conftest.py`** — autouse fixture resets 4 singletons:
`clear_config_instance()`, `clear_buffer()`, `clear_flow_store()`, `clear_store_instance()`

**Key test helpers:**
- `test_multi_har_saver.py:_make_flow_with_snapshot()` — builds `http.HTTPFlow` via
  `tflow.tflow(resp=True)` + attaches `FlowRecord` with `ClientRequest`
- `test_inspector_addon.py:_make_mock_flow(reverse=True)` — `MagicMock` with `proxy_mode`
- `test_inspector_addon.py:_make_flow_with_transform(provider, is_streaming)` — mock with
  `FlowRecord` + `TransformMeta`
- `test_inspector_addon.py:_make_flow_with_client_request(...)` — mock with `ClientRequest`
- `test_inspector_contentview.py:_make_cr(...)` — constructs `ClientRequest` directly
