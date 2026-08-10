# lightllm — wire translation layer

`ccproxy.lightllm` is the IR ↔ wire translation layer. It turns an incoming
request body (Anthropic Messages, OpenAI Chat Completions) into an
intermediate representation that ccproxy's hook pipeline can manipulate, and
back into a request body for whatever upstream provider the router resolves
to (Anthropic, OpenAI, Google Gemini, Perplexity Pro, plus the
Anthropic-compatible forks DeepSeek and ZAI). On the response side the same
package turns upstream SSE bytes (or buffered JSON) back into IR events and
re-renders to the listener's wire format.

The response side uses an FSM idiom built on `pydantic_graph.GraphBuilder`
(pinned at >=1.99.0, importing from canonical paths — no longer `.beta`):
`*_intake.py` / `*_render.py` modules per provider/listener-format handle
streaming SSE transformations. Request-side wire ↔ IR translation lives in
`src/ccproxy/lightllm/adapters/` as `UIAdapter` subclasses, one per wire
format. There is no runtime LiteLLM dependency; remaining source mentions are
historical notes about the pre-adapter implementation.

---

## Architecture

### The system at a glance

```
Client                              ccproxy                                Provider
  │                                    │                                      │
  │── REQUEST (listener wire) ────────▶│                                      │
  │                                    │  ┌─────────────────────────────┐     │
  │                                    │  │ Context.from_flow(flow)     │     │
  │                                    │  │   ↓                         │     │
  │                                    │  │ Context.parse_sync()        │     │
  │                                    │  │   → populates ctx fields:   │     │
  │                                    │  │     _cached_messages        │     │
  │                                    │  │     _cached_settings        │     │
  │                                    │  │     _cached_request_params  │     │
  │                                    │  │     _cached_raw_extras      │     │
  │                                    │  └──────────┬──────────────────┘     │
  │                                    │             ↓                        │
  │                                    │  ┌──────────────────────┐            │
  │                                    │  │ Pipeline hooks (DAG) │            │
  │                                    │  └──────────┬───────────┘            │
  │                                    │             ↓                        │
  │                                    │  ┌──────────────────────────────┐    │
  │                                    │  │ ctx.commit() calls           │    │
  │                                    │  │ dispatch_dump_sync(ctx, ...)  │    │
  │                                    │  │   → provider wire bytes ────────▶│
  │                                    │  └──────────────────────────────┘    │
  │                                    │                                      │
  │                                    │◀── provider wire (buffered or SSE) ──│
  │                                    │  ┌──────────────────────────────┐    │
  │                                    │  │ SSE: SSEPipeline (sync       │    │
  │                                    │  │   mitmproxy stream callable) │    │
  │                                    │  │   → persistent asyncio loop  │    │
  │                                    │  │     ↓                        │    │
  │                                    │  │   dispatch_intake(provider=) │    │
  │                                    │  │     → ModelResponseStream    │    │
  │                                    │  │       Event (IR)             │    │
  │                                    │  │     ↓                        │    │
  │                                    │  │   dispatch_render(listener=) │    │
  │                                    │  │     ↓                        │    │
  │                                    │  │ Buffered: transform_buffered │    │
  │                                    │  │   _response_sync(...) drives │    │
  │                                    │  │   intake once + emits        │    │
  │                                    │  │   listener-shape JSON        │    │
  │                                    │  └──────────────────────────────┘    │
  │◀── RESPONSE (listener wire) ───────│                                      │
  │                                    │                                      │
```

The thick line through the middle is `pydantic_ai.messages` — `ModelMessage`
+ `ModelResponseStreamEvent` are the canonical IR types the pipeline hooks
operate on.

### Module layout

```
src/ccproxy/lightllm/
├── parsed.py             ParsedRequest (reduced role), InboundFormat
├── registry.py           Local Perplexity Pro registration (no LiteLLM fallback)
├── pplx.py               Perplexity Pro config + exceptions (no LiteLLM bases)
├── pplx_steps.py         Perplexity step trail renderer
├── pplx_threads.py       Perplexity thread continuation helpers
│
├── adapters/             ← UIAdapter subclasses (request-side wire ↔ IR)
│   ├── __init__.py       LLMRenderInput Protocol + adapter exports
│   ├── anthropic.py      AnthropicAdapter
│   ├── openai_chat.py    OpenAIChatAdapter
│   ├── google.py         GoogleAdapter (outbound-only)
│   ├── perplexity.py     PerplexityAdapter (outbound-only)
│   ├── _envelope.py      parse_request_into_fields, parse_request, render_request
│   ├── _anthropic_envelope.py  Anthropic wire helpers
│   ├── _openai_envelope.py     OpenAI wire helpers
│   └── _tool_kinds.py    wire-type → ToolPartKind mapping for typed promotion
│
└── graph/                ← FSM modules for streaming responses
    ├── __init__.py       dispatch_dump_sync, dispatch_intake, dispatch_render
    │
    ├── _base.py          IntakeState / ResponseIntakeFSM + RenderState /
    │                      ResponseRenderFSM — shared state slots (funnel,
    │                      telemetry), feed()/render() templates, SSE framing
    │
    ├── _usage.py         usage_from_* capture + to_* projection helpers
│
├── _finish_reason.py from_* capture + to_* projection helpers for the
│                      per-listener finish-reason / stop-reason vocabulary
    │
    ├── _subgraph_patch.py Monkey-patch installing GraphBuilder.add_subgraph
    │                      (temporary until pydantic_graph ships it natively)
    │
    ├── anthropic_intake.py Anthropic SSE → IR events
    ├── anthropic_render.py IR events → Anthropic SSE
    │
    ├── openai_intake.py  OpenAI SSE → IR events
    ├── openai_render.py  IR events → OpenAI SSE
    │
    ├── google_intake.py  Google streamGenerateContent SSE → IR events
    │                      (cloudcode-pa envelope unwrap folded in;
    │                       two-level FSM with per-chunk subgraph)
    │
    ├── perplexity_intake.py Perplexity Pro SSE → IR events
    │                      (two-level FSM with per-event subgraph)
    │
    ├── sse_pipeline.py   SSEPipeline — persistent asyncio loop per stream
    └── buffered.py       transform_buffered_response_sync — non-streaming
                          cross-format transform via FSM
```

There is no `*_load.py` / `*_dump.py` anymore (moved to `adapters/`), no
`response/` subpackage (deleted), no `dispatch.py` (deleted), no
`context_cache.py` (deleted — Gemini cachedContents is unsupported via the
OAuth path the production deployment uses).

---

## The IR

### `LLMRenderInput` Protocol — the request envelope

The canonical IR is now a Protocol defined in
`src/ccproxy/lightllm/adapters/__init__.py`:

```python
@runtime_checkable
class LLMRenderInput(Protocol):
    @property
    def model(self) -> str: ...
    @property
    def messages(self) -> list[ModelMessage]: ...
    @property
    def request_parameters(self) -> ModelRequestParameters: ...
    @property
    def settings(self) -> ModelSettings: ...
    @property
    def stream(self) -> bool: ...
    @property
    def raw_extras(self) -> dict[str, Any]: ...
```

Any object exposing these six properties satisfies the protocol.
`Context` (in `src/ccproxy/pipeline/context.py`) is the production
implementation; it owns `_cached_messages`, `_cached_request_parameters`,
`_cached_settings`, `_cached_raw_extras` fields populated by `parse_sync()`.

### `ParsedRequest` — reduced role

`ParsedRequest` (in `src/ccproxy/lightllm/parsed.py`) still exists as a
frozen dataclass implementing `LLMRenderInput`, but its role is now limited:

```python
@dataclass(frozen=True)
class ParsedRequest:
    model: str
    messages: list[ModelMessage]
    request_parameters: ModelRequestParameters
    settings: ModelSettings
    stream: bool = False
    raw_extras: dict[str, Any] = field(default_factory=dict)
```

It's a **test-only helper today**. The convenience wrappers
`_envelope.parse_request()` and `_envelope.render_request()` build it for
roundtrip tests; production code (including the inspector) uses `Context`
directly via `Context.parse_sync()`, which calls
`parse_request_into_fields()` to populate Context's lazy-parse slots
in-place without an intermediate bundle.

### `ModelMessage` and `ModelResponseStreamEvent` — the conversation IR

From `pydantic_ai.messages`.

* **`ModelRequest(parts=[...])`** — user/system turn. Parts:
  `SystemPromptPart`, `UserPromptPart(content=str | list[UserContent])`
  where `UserContent` is one of `str`, `BinaryContent`, `ImageUrl`,
  `DocumentUrl`, `AudioUrl`, `UploadedFile`, `CachePoint`; plus
  `ToolReturnPart`, `RetryPromptPart`.

* **`ModelResponse(parts=[...])`** — assistant turn. Parts: `TextPart`,
  `ToolCallPart`, `ThinkingPart` (including `id="redacted_thinking"` for
  opaque ciphertext).

Streaming uses `ModelResponseStreamEvent` — a union of `PartStartEvent`,
`PartDeltaEvent`, `PartEndEvent`, `FinalResultEvent`. The intake FSM drives
pydantic-ai's `ModelResponsePartsManager` and yields these events; the
render FSM consumes them.

### `InboundFormat` — what the client sent (inbound wire format)

`src/ccproxy/lightllm/parsed.py`:

```python
class InboundFormat(StrEnum):  # StrEnum native in pydantic_graph >=1.99.0
    UNKNOWN = "unknown"
    ANTHROPIC_MESSAGES = "anthropic_messages"   # /v1/messages
    OPENAI_CHAT = "openai_chat"                 # /v1/chat/completions
    OPENAI_RESPONSES = "openai_responses"       # /v1/responses (Codex CLI)
```

Pinned at `Context` construction from path + headers. Drives the choice of
inbound parser (adapter's `load_messages`) AND the choice of response
renderer (`dispatch_render`). The **upstream provider** the request routes
to is a separate decision (made by the transform router via sentinel-key or
`TransformOverride` rule).

---

## The FSM pattern (response side only)

The six `lightllm/graph/*_intake.py` modules and three `*_render.py` modules
share a single shape, anchored by the base classes in `graph/_base.py`
(`IntakeState` / `ResponseIntakeFSM` for intakes, `RenderState` /
`ResponseRenderFSM` for renders). These handle **streaming SSE**
transformations and are the only place ccproxy still uses pydantic-graph at
runtime — the request side is procedural adapter classmethods, not graphs.
Reading `anthropic_intake.py` end-to-end is the fastest way to understand the
idiom; the other modules echo it.

```python
from pydantic_graph import GraphBuilder, StepContext  # canonical, not .beta

from ccproxy.lightllm.graph._base import IntakeState, ResponseIntakeFSM

# 1. State — a mutable dataclass inheriting the shared slots from
#    IntakeState[EventT] (parts_manager, events_queue, out_events, the funnel
#    slots usage/raw_extras/finish_reason/provider_response_id, and the
#    telemetry counters). The subclass carries only provider-specific fields.
@dataclass
class _AnthropicIntakeState(IntakeState[BetaRawMessageStreamEvent]):
    provider_name: str
    current_block: BetaContentBlock | None = None
    # ... per-FSM extra fields

# 2. Marker classes — sentinel values the decision routes on.
class _FeedDone: ...        # queue exhausted; route to terminal step
class _IgnoredEvent: ...    # event has no IR equivalent; loop back to router

# 3. GraphBuilder — type parameters: [state, deps, inputs, output].
_g: GraphBuilder[
    _AnthropicIntakeState, None, None, list[ModelResponseStreamEvent]
] = GraphBuilder(
    state_type=_AnthropicIntakeState,
    output_type=list[ModelResponseStreamEvent],
)

# 4. Router step — pops the next typed event OR signals done.
@_g.step
async def frame_next_event(ctx: StepContext[_AnthropicIntakeState, None, None]) -> Any:
    state = ctx.state
    while state.events_queue:
        event = state.events_queue.popleft()
        if isinstance(event, (BetaRawMessageStartEvent, BetaRawMessageDeltaEvent)):
            return _IgnoredEvent()
        if isinstance(event, BetaRawMessageStopEvent):
            state.current_block = None
            return _IgnoredEvent()
        return event
    return _FeedDone()

# 5. Per-variant handler steps — one per concrete BetaRaw*Event subclass.
@_g.step
async def handle_content_block_start(
    ctx: StepContext[_AnthropicIntakeState, None, BetaRawContentBlockStartEvent],
) -> None:
    # ... drive ctx.state.parts_manager and append to ctx.state.out_events
    ...

# (handle_content_block_delta, handle_content_block_stop, skip_ignored_event,
#  emit_done all follow the same shape)

# 6. Terminal step — pulls the accumulated output out of state.
@_g.step
async def emit_done(
    ctx: StepContext[_AnthropicIntakeState, None, _FeedDone],
) -> list[ModelResponseStreamEvent]:
    return ctx.state.out_events

# 7. Wire the topology — declarative edges with a single decision fan-out.
_g.add(
    _g.edge_from(_g.start_node).to(frame_next_event),
    _g.edge_from(frame_next_event).to(
        _g.decision()
        .branch(_g.match(_FeedDone).to(emit_done))
        .branch(_g.match(_IgnoredEvent).to(skip_ignored_event))
        .branch(_g.match(BetaRawContentBlockStartEvent).to(handle_content_block_start))
        .branch(_g.match(BetaRawContentBlockDeltaEvent).to(handle_content_block_delta))
        .branch(_g.match(BetaRawContentBlockStopEvent).to(handle_content_block_stop))
    ),
    # Loop-back: every handler step feeds back into the router.
    _g.edge_from(
        handle_content_block_start,
        handle_content_block_delta,
        handle_content_block_stop,
        skip_ignored_event,
    ).to(frame_next_event),
    _g.edge_from(emit_done).to(_g.end_node),
)

# 8. Build once at import time.
_intake_graph = _g.build()

# 9. Public FSM wrapper — ResponseIntakeFSM owns the SSE buffering, the
#    byte tee, feed() (frame → enqueue → run graph), the funnel properties
#    (usage / raw_extras / finish_reason / provider_response_id), and the
#    shared _split_sse_frames() framer. The subclass provides the graph, the
#    state, and the provider-specific payload parsing.
class AnthropicResponseIntakeFSM(ResponseIntakeFSM[_AnthropicIntakeState]):
    name = "anthropic"
    _graph = _intake_graph

    def _initial_state(self, *, model, request_params):
        return _AnthropicIntakeState(
            parts_manager=ModelResponsePartsManager(model_request_parameters=request_params),
            provider_name="anthropic",
        )

    def _drain_events(self):
        # per-provider ``data:`` payload semantics — validate each complete
        # frame into a typed event (or dispatch envelope)
        for frame in self._split_sse_frames():
            ...
```

The render side (`anthropic_render.py`, `openai_render.py`,
`openai_responses_render.py`) is symmetric: state inherits `RenderState`
(`pending_events` deque, `out: bytearray`, telemetry counters) and the wrapper
inherits `ResponseRenderFSM` (shared `render()` = push event → run graph, an
`_initial_state` hook for the per-listener id generation, and the
`_log_silent_close()` telemetry helper); the outer router dispatches each IR
event kind, with the `part` / `delta` type-switches handled by inner no-loop
subgraphs; the terminal step returns `bytes(state.out)`. Each `close()` stays
wire-specific but shares the `close(*, usage=, raw_extras=, finish_reason=)`
keyword signature declared abstract on the base.

### Why this shape

| Concern | Solution |
|---|---|
| **Polymorphic walk** over heterogeneous typed events | One router step (`frame_next_event`) + a decision with a branch per concrete event class. |
| **End-of-graph from a router** | A marker class (`_FeedDone`) routed via `g.match(_FeedDone).to(emit_done)`. The terminal step returns the accumulated state — that value becomes the graph's output. |
| **Events with no IR output** (e.g. `message_start`, `message_delta`) | A `_IgnoredEvent` marker matched to a `skip_ignored_event` step that loops back to the router. |
| **Per-chunk drive** | `feed(data)` parses SSE frames out of an internal buffer into typed events, clears `state.out_events`, runs the graph once, returns the accumulated IR events. State persists across chunks (current block, parts_manager, etc.). |
| **Mermaid visualization** | Free via `graph.render(title=..., direction='LR')`. See the Visualization section below. |

### Subgraph composition

Every intake and render FSM graph-ifies its inner per-event dispatch through a
named subgraph; no imperative `isinstance` ladder remains inside a handler
step. The **form** follows the wire shape:

- **List traversal → looping subgraph.** When an event/chunk carries a *list*
  to iterate (Google `chunk.candidates[0].content.parts`, Perplexity
  `event["blocks"]`, OpenAI chunk `delta.tool_calls`), the subgraph drains a
  `state.<x>_queue` deque with a `pop_next_*` router + loop-back — the
  sequential, ordered analogue of `.map()`, which must NOT be used here because
  parallel forks would race the shared `parts_manager` and reorder SSE output.
- **Single-object type-switch → no-loop subgraph.** When an event carries one
  object to type-switch on (Anthropic `event.content_block` / `event.delta`,
  OpenAI-Responses `item`, the render-side IR `part` / `delta`), the subgraph is
  a flat `open_* → g.decision() → handler` with no loop. The decision matches
  the concrete SDK/IR classes directly (no wrapper dataclasses when the variants
  are distinct classes) and ends with a `g.match(TypeExpression[object])`
  catch-all that logs unhandled variants instead of silently dropping them.

To collapse those ladders back into the declarative graph idiom, the
graph layer ships a temporary monkey-patch at
`src/ccproxy/lightllm/graph/_subgraph_patch.py` that installs a
`GraphBuilder.add_subgraph` method. The patch tracks the upstream TODO at
`pydantic_graph/graph_builder.py:1469`:

```
# TODO(DavidM): Support adding subgraphs; I think this behaves like a step
# with the same inputs/outputs but gets rendered as a subgraph in mermaid
```

The patch follows that contract literally: `add_subgraph(subgraph, *,
node_id=None, label=None)` wraps a built `Graph` in a synthetic `Step`
whose body awaits `subgraph.run(state=ctx.state, deps=ctx.deps,
inputs=ctx.inputs)`. The returned `Step` is usable in `edge_from(...).to(...)`
like any other step. Shared `StateT` flows through unchanged — the inner
graph sees and mutates the same state instance as the parent, which is how
cross-block invariants (e.g. Perplexity's `state.answer_seen` prefix
accumulation) survive the decomposition.

Every intake/render module that composes a subgraph imports the patch module
at top-level to install the method before they use it:

```python
import ccproxy.lightllm.graph._subgraph_patch  # noqa: F401  — installs add_subgraph
```

Mermaid renders the composed step as a single labelled node:

```
subgraph_pplx_event_dispatch: dispatch_event
```

Each inner subgraph is exposed at module scope (e.g.
`_block_start_graph` / `_block_delta_graph` in anthropic_intake,
`_chunk_dispatch_graph` in google_intake, `_event_dispatch_graph` in
perplexity_intake) so it can be rendered standalone for the visualization
sanity check (see the Visualization section). The patch deliberately does NOT
integrate with mermaid's `subgraph` cluster syntax — that needs upstream
cooperation.

Removal trigger: delete `_subgraph_patch.py` and remove its
`# noqa: F401` import the day `pydantic_graph.GraphBuilder` exposes a
native `add_subgraph` (or equivalent). The call sites should work
unchanged unless upstream picks a different method name, in which case
one rename pass at the import sites suffices.

### What each file does

**Request-side (adapters/):**

| File | What it does |
|---|---|
| `anthropic.py` | `AnthropicAdapter` — bidirectional wire ↔ IR for Anthropic Messages |
| `openai_chat.py` | `OpenAIChatAdapter` — bidirectional wire ↔ IR for OpenAI Chat Completions |
| `google.py` | `GoogleAdapter` — outbound-only IR → Google Gemini `generateContent` wire bytes. Direct dict construction with camelCase keys, base64-inline binary data, `generationConfig` hoist for sampling params. Does NOT wrap pydantic-ai's `GoogleModel` — too many ccproxy-specific tweaks (cloudcode-pa envelope, raw_extras passthrough). |
| `perplexity.py` | `PerplexityAdapter` — outbound-only IR → Perplexity Pro wire bytes. Projects IR back to OpenAI-format dicts, then invokes `pplx.py:_build_pplx_payload` (the 28-field Perplexity payload builder) with `raw_extras["pplx"]` as the params block. |
| `promptast.py` | `PromptAstAdapter` — **library-only, inbound-only** Alloy PromptAst JSON → IR. Not a listener wire format: no `InboundFormat`, no `Context`, no dispatch wiring. `parsed_request_from_alloy(prompt_ast, client_view)` builds a `ParsedRequest` a consumer hands to `dispatch_dump_sync`. See the Alloy PromptAst bridge section. |
| `_envelope.py` | `parse_request_into_fields`, `parse_request`, `render_request` — test/inspector helpers |
| `_anthropic_envelope.py` | Anthropic wire helpers |
| `_openai_envelope.py` | OpenAI wire helpers |

**Response-side (graph/):**

| File | What its FSM does | Key marker classes |
|---|---|---|
| `_base.py` | Shared base classes. `IntakeState[EventT]` / `RenderState` carry the common state slots (queues, funnel slots `usage`/`raw_extras`/`finish_reason`/`provider_response_id`, telemetry counters); `ResponseIntakeFSM[StateT]` owns the byte tee, SSE buffering, the `feed()` template, `_split_sse_frames()`, the funnel properties, and the silent-empty telemetry helpers; `ResponseRenderFSM[StateT]` owns `render()` and the abstract `close(*, usage=, raw_extras=, finish_reason=)` signature. Subclass hooks: `_initial_state`, `_drain_events`, `_graph`. | — |
| `_subgraph_patch.py` | Installs `GraphBuilder.add_subgraph` via monkey-patch (tracks upstream TODO at `pydantic_graph/graph_builder.py:1469`). Registers a built `Graph` as a synthetic `Step` whose body awaits `subgraph.run(state=ctx.state, deps=ctx.deps, inputs=ctx.inputs)`. Shared `StateT` flows through unchanged; inner subgraph mutates the same state instance as the parent. Mermaid renders the subgraph as a single labelled node. Removable when upstream ships native subgraph composition. | — |
| `anthropic_intake.py` | Anthropic SSE → IR `ModelResponseStreamEvent` (typed dispatch on `BetaRawMessageStreamEvent` union) | `_FeedDone`, `_IgnoredEvent` |
| `anthropic_render.py` | IR `ModelResponseStreamEvent` → Anthropic SSE wire bytes | `_RenderDone` |
| `openai_intake.py` | OpenAI Chat Completions SSE → IR (per-chunk envelope dispatch on content/tool_call/refusal shapes) | `_FeedDone`, `_RefusalChunk`, `_StandardChunk`, `_EmptyChoicesChunk` |
| `openai_render.py` | IR → OpenAI Chat Completions SSE | `_RenderDone` |
| `google_intake.py` | Google `streamGenerateContent` chunks → IR. Two-level FSM: outer pops chunks from the events queue; the inner `_chunk_dispatch_graph` (composed via `add_subgraph`) pops one `Part` at a time and routes it through a typed-marker decision to the matching arm (`_TextPart` → text delta, `_FunctionCallPart` → tool-call delta, `_InlineDataPart` → `FilePart`, `_FunctionResponsePart` → log + drop, `_UnknownPart` → no-op). Envelope unwrap of `{response: {...}}` from cloudcode-pa folded in at the SSE-frame parser. | `_FeedDone`, `_GenerateChunk`, `_PartDispatch`, `_ChunkDone`, `_TextPart`, `_FunctionCallPart`, `_InlineDataPart`, `_FunctionResponsePart`, `_UnknownPart` |
| `perplexity_intake.py` | Perplexity Pro SSE → IR. Two-level FSM: outer pops events from the queue; the inner `_event_dispatch_graph` (composed via `add_subgraph`) runs `absorb_event → apply_text_mirror → pop_next_block → {plan_arm → bare_markdown_arm → diff_block_arm | flush}` per event. Cross-block invariants (`has_plan_block` precondition, batched `pending_*_delta` accumulation, single end-of-event flush) preserved via per-event scratch fields on `_PerplexityIntakeState` that `flush_event_deltas` resets. The four documented diff-block patch modes (Mode A root cumulative, Mode B chunks-array, Mode C `/chunks/N` append, Mode D `/markdown_block`) are still handled by `_apply_markdown_patch`. | `_FeedDone`, `_PerplexityEventEnvelope`, `_BlockDispatch`, `_EventDone` |
| `sse_pipeline.py` | Sync mitmproxy stream callable backed by a persistent asyncio loop + daemon thread; drives an intake + render FSM pair per stream | — |
| `buffered.py` | Non-streaming buffered-body cross-format transform; synthesizes streaming events from buffered JSON per provider, drives the intake FSM, emits listener-shape JSON | — |

---

## Public API

### Request side

```python
from ccproxy.lightllm.graph import dispatch_dump_sync
from ccproxy.lightllm.adapters import LLMRenderInput

# Inbound: wire → IR (production path via Context)
ctx = Context.from_flow(flow)
ctx.parse_sync()  # returns None; populates ctx._cached_* fields
# ctx's typed fields are now populated:
messages = ctx.messages
settings = ctx.settings
request_params = ctx.request_parameters

# Outbound (sync — from inside mitmproxy hooks or pipeline executors)
# ctx satisfies LLMRenderInput Protocol
wire_bytes: bytes = dispatch_dump_sync(ctx, provider_type="anthropic")
```

`dispatch_dump_sync` routes by upstream provider:
* `anthropic` / `deepseek` / `zai` → `AnthropicAdapter.render(req)`
* `minimax` -> `AnthropicAdapter.render(req)`
* `openai` → `OpenAIChatAdapter.render(req)`
* `google` / `gemini` / `vertex_ai` / `vertex_ai_beta` → `GoogleAdapter.render(req)`
* `perplexity_pro` → `PerplexityAdapter.render(req)`
* anything else → `UnsupportedUpstreamError`

The Anthropic-compatible forks (`deepseek`, `zai`) deliberately share the
Anthropic adapter — their wire format is identical, only the upstream URL
and auth differ (and those are handled by the `Provider` config).

MiniMax uses the Anthropic adapter for its compatible endpoint, with the
destination URL and authentication supplied by the `Provider` config.

### Response side

```python
from ccproxy.lightllm.graph import dispatch_intake, dispatch_render
from ccproxy.lightllm.graph.sse_pipeline import SSEPipeline
from ccproxy.lightllm.graph.buffered import transform_buffered_response_sync

# Streaming (mitmproxy installs this on flow.response.stream)
intake = dispatch_intake(
    provider_type="anthropic", model="claude-...", request_params=...,
)
render = dispatch_render(inbound_format=InboundFormat.OPENAI_CHAT, model="claude-...")
pipeline = SSEPipeline(intake=intake, render=render)
flow.response.stream = pipeline

# Buffered (one-shot from inspector route handler)
listener_body: bytes = transform_buffered_response_sync(
    raw_bytes=flow.response.content,
    provider_type="anthropic",
    inbound_format=InboundFormat.OPENAI_CHAT,
    model="claude-...",
    request_params=...,
)
```

`dispatch_intake` and `dispatch_render` return async FSM instances. The
`SSEPipeline` adapts them to mitmproxy's sync stream callable contract.

### `ParsedRequest` — direct construction (tests only)

Production code uses `Context`. For tests and tooling, `ParsedRequest` can
be built directly as a test stub:

```python
from ccproxy.lightllm.parsed import ParsedRequest
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models import ModelRequestParameters

req = ParsedRequest(
    model="claude-3-5-haiku-20241022",
    messages=[ModelRequest(parts=[UserPromptPart(content="hello")])],
    request_parameters=ModelRequestParameters(),
    settings={"max_tokens": 1024},
)

# req satisfies LLMRenderInput Protocol
wire_bytes = dispatch_dump_sync(req, provider_type="anthropic")
```

---

## The sync/async bridges

### Request-side is now pure sync

The adapters in `src/ccproxy/lightllm/adapters/` are pure Python (no async):
`json.loads` + procedural dispatch over pydantic-ai objects. No asyncio
bridge is needed. `Context.parse_sync()` calls
`parse_request_into_fields()` which populates Context fields in-place
synchronously. `dispatch_dump_sync` calls the adapter's `.render(req)`
classmethod directly — also synchronous.

The old worker-thread pattern (`_run_coro_sync`) was deleted along with the
async load/dump FSMs. Request-side translation is fast enough (~10-100µs per
request) to run inline.

### Response-side persistent loop (`SSEPipeline`)

The per-invocation worker-thread pattern would be pathological for
streaming responses — mitmproxy delivers SSE in many small chunks per
stream, and spawning one thread + fresh loop per chunk would mean ~200
fresh loops in a 5-second stream.

`SSEPipeline` (`lightllm/graph/sse_pipeline.py`) instead owns one
persistent `asyncio.AbstractEventLoop` running in a daemon thread per
instance. Each chunk is submitted to that loop via
`asyncio.run_coroutine_threadsafe` and the result awaited synchronously:

```python
class SSEPipeline:
    def __init__(self, *, intake, render):
        self._intake = intake
        self._render = render
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, daemon=True, name="ccproxy-sse-loop",
        )
        self._thread.start()

    def __call__(self, data: bytes) -> bytes | list[bytes]:
        if data == b"":
            return self._flush_and_close()
        future = asyncio.run_coroutine_threadsafe(self._process_chunk(data), self._loop)
        return future.result() or []

    async def _process_chunk(self, data: bytes) -> bytes:
        out = bytearray()
        for event in await self._intake.feed(data):
            out.extend(await self._render.render(event))
        return bytes(out)
```

Per-chunk overhead is ~10-50 µs of cross-thread hop, negligible against
the ~10-100 ms-per-chunk network-I/O floor.

Lifecycle: the daemon thread dies with the process, so a missed `close()`
won't leak — but `InspectorAddon.response` calls `pipeline.close()`
explicitly on flow finalization for tidiness. `close()` is idempotent.

### Buffered transforms use a simpler per-call loop

`transform_buffered_response_sync` in `lightllm/graph/buffered.py` is
one-shot per response (no streaming) so it just uses the per-call
asyncio-loop pattern. No persistent thread, no overhead.

---

## Context.extras — typed glom accessor

Hooks reach raw body fields via `ctx.extras`, a typed wrapper around
`glom` calls on `ctx._body`:

```python
session_id = ctx.extras.get("metadata.user_id", default=None)
ctx.extras.set("pplx.attachments", [...])
ctx.extras.delete("tool_choice")
exists = ctx.extras.has("metadata.user_id")  # bool
```

Path strings are standard glom dot-paths. The accessor reads/writes
`ctx._body` directly — no parse cache interaction, no commit needed for
the mutation to be visible to later hooks. Existing
`glom(ctx._body, ...)` / `assign(...)` / `delete(...)` call sites stay
valid; migration is opportunistic.

This is layer 3 of the three-layer access model:
1. Header ops (`ctx.get_header()` / `ctx.set_header()`)
2. Typed ops (`ctx.system`, `ctx.messages`, `ctx.tools`)
3. Raw body ops (`ctx.extras.*`)

## raw_extras contract

`raw_extras` is the lossless-passthrough mechanism. Anything the IR
doesn't natively model gets stashed here under a conventional key, and the
outbound renderer (or response render) stitches it back onto the wire body.

### Request-side conventions

**Anthropic adapter** (`adapters/anthropic.py`):

| Key | What | Why |
|---|---|---|
| `cc:msg:{i}:block:{j}` | Original `cache_control` dict from a content block | TTL wasn't `5m` or `1h` (the only values pydantic-ai's `CachePoint` accepts) — preserved so dump can re-apply verbatim |
| `unknown_block:msg:{i}:idx:{j}` | Original wire-block dict | Block had a `type` we don't recognize — preserved so dump can emit it back |
| `system` | The original `system` list from the body | Non-uniform `cache_control` across system blocks — can't be expressed via `settings['anthropic_cache_instructions']` (which is uniform-only) |
| `tools` | The original `tools` list from the body | Two independent triggers. **Typed tools**: any entry whose `type` is present and not `"custom"` (server/builtin tools — versioned `type`, side fields like `max_uses` — the IR can't model them; `ToolDefinition`s are still built for typed promotion, but the wire rides here verbatim). **Non-canonical cache markers**: the lift to `settings['anthropic_cache_tool_definitions']` fires only for exactly one marker, on the **last non-deferred** tool, with a supported TTL; every other pattern (all-stamped, mixed TTLs, marker on a deferred or non-boundary tool) rides here verbatim, and when the typed trigger fires the lift is skipped entirely (the marker already reaches the wire; the knob would double-count in the cache engine's census). `defer_loading` round-trips on `ToolDefinition.defer_loading` and is re-emitted only when `True` |
| `metadata` | The body's `metadata` dict | Anthropic-specific; no IR slot |
| Other unmodeled top-level keys | Copied verbatim under their wire name | E.g. `service_tier` |

**OpenAI adapter** (`adapters/openai_chat.py`):

| Key | What | Why |
|---|---|---|
| `image_detail:msg:{i}:block:{j}` | The `image_url.detail` string | Not currently part of the `ImageUrl` IR |
| `file:msg:{i}:block:{j}` | Original `file` content block | Preserved verbatim |
| `unknown_block:msg:{i}:block:{j}` | Unknown content block | Same as Anthropic |
| `refusal:msg:{i}` | Refusal text | Assistant refusal isn't in the IR |
| `function_call:msg:{i}` | Legacy `function_call` field | Pre-`tool_calls` OpenAI format |
| `tool_choice` | The body's `tool_choice` | IR has no slot |
| `response_format` | The body's `response_format` | IR has no slot |

**OpenAI Responses adapter** (`adapters/openai_responses.py`):

The `input[]` discriminated union has 27 `type` values. Four conventional
buckets cover them all plus forward-compat:

| Key | What | Why |
|---|---|---|
| `openai_responses:reasoning:{i}` | Full ``reasoning`` item dict at index `i` | pydantic-ai's `ThinkingPart` only carries a content string; structured `summary[]` + `content[]` + `encrypted_content` cannot be modelled |
| `openai_responses:server_tool:{i}` | One of 17 server-side tool kinds (`web_search_call`, `code_interpreter_call`, `mcp_call`, `file_search_call`, `computer_call`/`_output`, `apply_patch_call`/`_output`, `local_shell_call`/`_output`, `shell_call`/`_output`, `image_generation_call`, `custom_tool_call`/`_output`, `mcp_list_tools`, `mcp_approval_request`/`_response`, `tool_search_call`/`_output`, `compaction`, `item_reference`) | No IR equivalent; preserved for lossless round-trip when re-rendering the request |
| `openai_responses:item_id:{i}` | Item `id` field | Used by ``previous_response_id`` chaining (Codex CLI resume) |
| `openai_responses:unknown_item:{i}` | Item with unrecognized `type` | Forward-compat: future SDK additions degrade safely instead of crashing |
| `openai_responses:refusal:{i}:{j}` | Assistant `refusal` content part | No IR slot |
| `tool_choice` | The body's `tool_choice` | IR has no slot |
| Other unmodeled top-level keys | Copied verbatim under their wire name | E.g. `previous_response_id`, `prompt_cache_key`, `prompt_cache_retention`, `reasoning`, `parallel_tool_calls` |

**Bare-string input normalization**: ``ResponseCreateParams.input`` is
``Union[str, list[ResponseInputItem]]``. The Responses parser
(`adapters/_envelope.py:_parse_openai_responses`) wraps a bare string
into a single ``{"type": "message", "role": "user", "content": "..."}``
item before invoking ``OpenAIResponsesAdapter.load_messages``. The
adapter's render path always emits the verbose-message form (never bare
string) — round-tripping a bare-string request through IR produces a
verbose-form wire body, which is semantically identical for upstreams.

**Buffered output arm**: ``InboundFormat.OPENAI_RESPONSES`` is wired
into ``buffered.py:transform_buffered_response_sync`` via the
``_parts_to_openai_responses`` helper. Any upstream provider
(Anthropic, OpenAI Chat, OpenAI Responses, Google, Perplexity) can
satisfy a ``/v1/responses`` request — the buffered transform synthesizes
the upstream's SSE shape, drains the existing intake FSM, then renders
``parts_manager.get_parts()`` into the ``Response`` envelope JSON
returned to the listener.

**Streaming render**: ``InboundFormat.OPENAI_RESPONSES`` is wired into
``dispatch_render`` via ``OpenAIResponsesRenderFSM``, so a Responses-shaped
listener can receive rendered Responses SSE when the upstream intake produces
response IR. ``dispatch_intake`` now also accepts
``provider_type="openai_responses"`` and routes Responses SSE through
``OpenAIResponsesIntakeFSM``.

**Same-format Codex passthrough (the canonical path)**: When a
listener `/v1/responses` request resolves (via sentinel) to a Provider
whose `type` is also ``openai_responses``, the transform router
auto-derives action=``redirect``. This bypasses cross-format transform
entirely — no `dispatch_dump_sync`, no buffered intake, no SSE
transform. ccproxy stamps the auth header, rewrites
host/path to the upstream (typically
`chatgpt.com/backend-api/codex/responses`), and streams the upstream
response straight back to the client. The buffered output arm above is
ONLY used when a `/v1/responses` request cross-format-transforms to a
non-Responses upstream (e.g., Anthropic for testing). The default `codex`
provider entry uses `type: openai_responses`, so normal Codex sentinel traffic
is same-format redirect plus shape replay, not a buffered cross-format
transform.

The default Codex route targets ChatGPT's Codex backend, which is stricter than
the public `/v1/responses` API. Keep default `codex` traffic streaming, leave
public-only fields such as `max_output_tokens` unset, and rely on the
`ccproxy.shaping.codex` inner shape hook for the backend-required verbose
`input` form and `store: false`.

`_FORMAT_PATTERNS` in `inspector/routes/transform.py` and
`_select_inbound_format` in `pipeline/context.py` both recognize
the canonical Codex CLI path `/backend-api/codex/responses` (the
`CHATGPT_CODEX_BASE_URL` base + `/responses` endpoint) in addition to
the public-API `/v1/responses` form.

### Response-side conventions — the usage/metadata funnel

Streaming intakes drive `ModelResponsePartsManager` directly, which — like
pydantic-ai's own parts manager — carries no token usage (in pydantic-ai
proper, usage rides `StreamedResponse._usage` as side-channel state, never as a
stream event). ccproxy replicates that accumulator: the shared state base
(`graph/_base.py:IntakeState`) carries `usage: RequestUsage`,
`raw_extras: dict`, `finish_reason`, and `provider_response_id` slots, exposed
via same-named properties on `ResponseIntakeFSM` — so the funnel interface is
guaranteed by type, and `buffered.py` / `sse_pipeline.py` /
`inspector/addon.py` read the attributes directly (no `getattr` plumbing).
Capture happens off the
already-parsed wire events the IR has no slot for — Anthropic
`message_start` / `message_delta`, the OpenAI terminal `include_usage` chunk,
the OpenAI Responses `response.completed` envelope, Gemini `usageMetadata` —
which the router would otherwise discard as ignored/skipped events. The mapping
helpers live in `graph/_usage.py` (`usage_from_*` capture into the canonical
`RequestUsage` where `input_tokens` excludes cache; `to_*` project back into
each listener's native usage block).

Usage is then re-stamped at every render seam so a cross-format transform never
drops it: the buffered assemblers emit the listener's usage block (or omit it
when the upstream reported none — never a fabricated zero), the OpenAI Chat
stream appends a terminal `{choices: [], usage: {...}}` chunk before `[DONE]`,
the OpenAI Responses stream fills `response.completed.usage`, and the Anthropic
stream fills `message_delta.usage`. `raw_extras` additionally carries unmodeled
per-message metadata (e.g. the upstream response id) rather than dropping it
silently. All six intakes inherit the slots from the base, so the funnel
interface is homogeneous; slots the wire never populates stay inert
(`usage` empty, `finish_reason` / `provider_response_id` `None`) — Perplexity
Pro and `openai_conversations` capture no token usage because their wire
protocols report none (Perplexity's only usage is subscription quota, via the
`pplx_usage` MCP tool), and only the OpenAI-family intakes populate
`provider_response_id` (Anthropic carries its message id through
`raw_extras["response_id"]`). If an upstream later exposes any of these,
capture drops into the same seam.

#### The finish reason travels the same funnel

`finish_reason` is the other side-channel pydantic-ai keeps off the parts
manager (it rides `ModelResponse.finish_reason`), and it answers a question no
part can: whether the turn *completed*. Its mapping helpers live in
`graph/_finish_reason.py`, same two directions as `_usage`:

| Direction | Helpers |
|---|---|
| Capture | `from_anthropic` (`message_delta.delta.stop_reason`), `from_openai_chat` (`choice.finish_reason`), `from_openai_responses` (`incomplete_details.reason`, else envelope `status`), `from_google` (`candidate.finishReason`) |
| Projection | `to_openai_chat` (`choice.finish_reason`), `to_anthropic` (`stop_reason`), `to_openai_responses_status` + `to_openai_responses_incomplete_reason` |

Every terminator seam projects it, so the two transport paths agree for the same
intake state: `SSEPipeline._drain_and_terminate` hands `finish_reason` to all
three streaming `close()` implementations, and `render_parts_to_listener` hands
it to all three buffered assemblers.

* **OpenAI Chat** — the terminal `finish_reason` chunk before `[DONE]`.
* **Anthropic** — `message_delta.delta.stop_reason`.
* **OpenAI Responses** — the terminal envelope event *and* its `status`:
  `response.completed`, `response.incomplete` (with `incomplete_details.reason`),
  or `response.failed`.

Two rules the projection encodes. A **rendered tool call supplies a reason the
upstream did not give** — Gemini answers `STOP` even for a function call, so a
tool-calling turn ends as `tool_calls` / `tool_use` rather than a bare
completion the client would not act on; a substantive reason such as `length`
still outranks it. And **`"error"` reaches the wire as `"error"`** on OpenAI
Chat and Anthropic, whose closed enums have no member for it: both SDKs parse
response enums permissively for forward compatibility, and an invented
completion (`stop` / `end_turn`) would be the exact lie this funnel exists to
remove. Anthropic reasons the IR cannot express (`pause_turn`, `compaction`)
stay unmapped and ride `raw_extras["stop_reason"]`.

### Round-trip contract

Both request-side dumps strip IR-internal markers (anything starting with
`cc:`, `unknown_block:`, `refusal:`, `file:`, `image_detail:`,
`function_call:`) when stitching `raw_extras` back onto the body. Override
keys (`system`, `tools`, `tool_choice`, `response_format`) win over
whatever the FSM produced. Everything else is `setdefault`'d onto the
body.

### What this guarantees

If a client sends a request to ccproxy, the inbound parser produces an IR,
the outbound renderer produces a wire body — the round-trip should be
**semantically equivalent** to the original. The `tests/test_lightllm_graph_*`
tests assert this via canonicalization helpers
(`assert_anthropic_bodies_equivalent`) for every shape in the test corpus.

The lossiness invariants specifically called out:
* `ToolReturnPart.tool_name` populated via the adapter's two-pass lookup
  (scan assistant turns to build `{tool_use_id: tool_name}`, then attach
  during user-turn `tool_result` parsing).
* Image `media_type` preserved on `BinaryContent` (no default-fallback).
* `cache_control` TTLs pydantic-ai's `CachePoint` can't represent (anything
  other than `5m` / `1h`) stashed in `raw_extras["cc:msg:N:block:M"]` and
  re-applied verbatim by the adapter's `render()` path.
* Unknown content blocks (anything with an unrecognized `type`) preserved
  in `raw_extras["unknown_block:msg:N:idx:M"]` and re-emitted on dump.

---

## Typed-part promotion (`tool_kind`)

`pydantic_ai.messages.ModelResponsePartsManager` (pinned 1.99+) auto-promotes
a base `ToolCallPart` to its typed subclass (e.g. `ToolSearchCallPart`) when
the matching `ToolDefinition` in the request's `ModelRequestParameters.
function_tools` carries a `tool_kind` discriminator. The promotion happens
inside `handle_tool_call_delta` and `handle_tool_call_part` via
`ToolCallPart.narrow_type(part, tool_kind=kind)` — no extra call needed
from intake code.

`ToolPartKind` is a `Literal['tool-search']` today (extensible — new kinds
appear in `pydantic_ai/messages.py`'s `ToolPartKind` alias). The native
server-side path narrows to `NativeToolSearchCallPart`; the local-fallback
path narrows to `ToolSearchCallPart`.

The listener-side gap was the wire `type` → `ToolPartKind` mapping. The
adapter's `_parse_tools` functions now consult
`src/ccproxy/lightllm/adapters/_tool_kinds.py`:

```python
# Anthropic — versioned wire-type discriminators
ANTHROPIC_TYPED_TOOLS: dict[str, ToolPartKind] = {
    "web_search_20250305": "tool-search",
    "web_search_20260209": "tool-search",
    "tool_search_tool_bm25_20251119": "tool-search",
    "tool_search_tool_regex_20251119": "tool-search",
}

# OpenAI — built-in server tools (Chat Completions sees these rarely)
OPENAI_TYPED_TOOLS: dict[str, ToolPartKind] = {}
```

**Promotion, not preservation.** This map only drives response-part
promotion. Wire fidelity for typed tools is the `raw_extras["tools"]`
verbatim override in `_parse_tools` (any `type` other than `"custom"`
triggers it). The bm25/regex entries are mostly redundant for *native*
traffic — native tool-search calls arrive as `server_tool_use` blocks
already typed by `_map_server_tool_use_block` — but matter for the
local/client-flavored path where a plain `tool_use` needs name-keyed
promotion.

`_anthropic_envelope._parse_tools` reads `tool["type"]` and looks up the
kind; `_openai_envelope._parse_tools` does the same with its own table.
Tools without a recognized `type` (most user-defined tools) keep
`tool_kind=None` and pass through as base `ToolCallPart` instances.

The threading from listener → FSM is straight-through:

```
incoming wire body
  → _parse_tools           sets ToolDefinition.tool_kind
  → ModelRequestParameters carries function_tools (with kind)
  → TransformMeta          carries request_parameters from ctx.metadata
  → dispatch_intake        passes request_params into FSM constructor
  → ModelResponsePartsManager.__init__
                           builds _tool_kind_by_name from function_tools
  → handle_tool_call_delta auto-promotes ToolCallPart via _typed_call_part
```

Add a new entry to `_tool_kinds.py` when a new typed server-side tool
ships upstream (e.g. a new Anthropic dated web-search variant). Tests
asserting typed parts go alongside the existing intake tests; see
`tests/test_lightllm_graph_intake_anthropic.py::test_typed_search_tool_promotes_tool_call_part`
for the canonical pattern.

### Tool-search invariants

The Anthropic intake FSM handles both sides of a native tool-search
interaction: `server_tool_use` calls named `tool_search_tool_bm25` /
`tool_search_tool_regex` map to `NativeToolSearchCallPart` (streamed args
finalize to the canonical `{"queries": [...]}` shape at `content_block_stop`),
and `tool_search_tool_result` blocks map to `NativeToolSearchReturnPart`
(success → `discovered_tools`; error variant → `provider_details`).

Constraints anything that touches these conversations must honor:

- **Never orphan a call/result pair.** Anthropic 400s a replayed history
  containing an unpaired `tool_search_tool_*` block. Hooks that reorder or
  inject messages must not split or drop one side. Known hazard:
  `inject_mcp_notifications` inserts its synthetic pair immediately before
  the final message, which can split a trailing assistant-call/user-result
  pair — harmless for plain tool_use (Anthropic tolerates the detached
  result) but not for `tool_search_tool_*` blocks.
- **Never strand a `tool_reference`.** `tool_reference` blocks inside a
  `tool_result` may only name tools present in the same request's `tools`
  array; hooks that filter or rewrite `tools` must keep referenced tools.
- **Known dump-side limitation.** `dump_messages` does not re-emit
  `server_tool_use` / `tool_search_tool_result` blocks from IR history:
  native parts (`NativeToolSearchCallPart` / `NativeToolSearchReturnPart`)
  are dropped, and client-flavored search parts degrade to plain
  `tool_use` / `tool_result`. The same applies to the Anthropic render FSM
  (IR → SSE), whose `NativeToolCallPart` branch emits plain `tool_use`.
  Both must be revisited *together with* OpenAI Responses server-execution
  intake support — they only matter as a pair, and doing render alone
  invites the pairing/`tool_reference` 400s above.

---

## `HookResult` and the pipeline executor

Hook execution results are tracked via a discriminated union in
`src/ccproxy/pipeline/results.py`:

```python
@dataclass(frozen=True)
class _HookSuccess:
    kind: Literal["success"] = "success"

@dataclass(frozen=True)
class _HookSkipped:
    kind: Literal["skipped"] = "skipped"
    reason: str

@dataclass(frozen=True)
class _HookError:
    kind: Literal["error"] = "error"
    error: str

@dataclass(frozen=True)
class _HookDeferred:
    kind: Literal["deferred"] = "deferred"

HookResult = _HookSuccess | _HookSkipped | _HookError | _HookDeferred
```

The executor in `src/ccproxy/pipeline/executor.py` wraps each hook
invocation and stores the resulting `HookResult` on
`ctx.metadata.hook_results`. Hook
implementations don't construct these directly — the executor emits the
appropriate variant based on execution outcome, guard evaluation, and
override headers.

Stored results are consumed by `ccproxy status` for per-hook execution
reporting and by inspector routes for flow debugging.

---

## How Context wires the request side

`src/ccproxy/pipeline/context.py:Context` is the per-request envelope
hooks and inspector routes operate on. The lightllm integration is three
calls:

### Inbound — parsing

```python
ctx = Context.from_flow(flow)        # builds Context with _inbound_format
ctx.parse_sync()                     # returns None; populates ctx._cached_* fields
# ctx's typed fields are now populated
messages = ctx.messages
settings = ctx.settings
request_params = ctx.request_parameters
```

The typed property accessors (`ctx.messages`, `ctx.system`, `ctx.tools`)
all funnel through `ctx.parse_sync()` on first access. They return mutable
IR objects; hooks can edit them in place.

### Outbound — committing

```python
ctx.messages = new_messages          # mutate via setter (rebuilds IR)
ctx.system = new_system_parts
ctx.tools = new_tool_definitions
ctx.commit()                         # → _flush_parsed_to_body()
                                     #   → <Adapter>.render(ctx)
                                     # body is re-rendered, written back to flow.request
```

`commit()` is what hook executors call after the DAG runs. It calls
`_flush_parsed_to_body()` which routes through the listener format's
adapter (e.g., `AnthropicAdapter.render(ctx)` for
`ANTHROPIC_MESSAGES`), then writes the resulting bytes back to
`flow.request.content`.

The provider name passed to the adapter's render method is the **listener
format**, not the upstream provider — the transform router decides the
upstream separately. Listener `anthropic_messages` → `AnthropicAdapter`;
listener `openai_chat` → `OpenAIChatAdapter`. Cross-format transformation
happens upstream of `commit()` — by then, the IR is in the target format
already.

---

## How the inspector wires the response side

`src/ccproxy/inspector/addon.py:InspectorAddon` installs the streaming
pipeline in `responseheaders`:

```python
def _install_streaming_transformer(self, flow, transform):
    inbound_format = InboundFormat(transform.inbound_format)
    intake = dispatch_intake(
        provider_type=transform.provider_type,
        model=transform.model,
        request_params=transform.request_parameters,
    )
    render = dispatch_render(inbound_format=inbound_format, model=transform.model)
    pipeline = SSEPipeline(intake=intake, render=render)
    flow.response.stream = pipeline
    metadata_from_flow(flow).sse_transformer = pipeline
```

`InspectorAddon.response` calls `pipeline.close()` on flow finalization to
tear down the daemon thread promptly.

For non-streaming flows, `inspector/routes/transform.py:handle_transform_response`
calls `transform_buffered_response_sync` instead — same `dispatch_intake`
under the hood, plus per-provider buffered-body-to-streaming-events
synthesis where the upstream's buffered shape differs from its streaming
shape (Anthropic, OpenAI, Google) or direct feed where it doesn't
(Perplexity Pro always streams, so its buffered body IS concatenated SSE).

`GeminiAddon.responseheaders` backs off from installing its
`EnvelopeUnwrapStream` when `flow.response.stream` is already a callable
(i.e., when `InspectorAddon` installed an `SSEPipeline`). The unwrap is
folded into `google_intake.py` for that path; the addon-installed
`EnvelopeUnwrapStream` still handles passthrough Gemini flows.

---

## Alloy PromptAst bridge (library lane)

`adapters/promptast.py` is a **library-only** cross-IR transform, not a proxy
listener format. Alloy renders a typed prompt to its provider-specialized
prompt AST (`alloy/baml/render_prompt {projection: "ast"}` →
`prompt_ast_to_json`), and a Python consumer (first: talkstream) converts that
AST **directly** to the pydantic-ai `list[ModelMessage]` IR — never
PromptAst → wire bytes → `load_messages`, which would lower and re-raise
through a wire format both ends already structure.

```
Alloy BAML source ──render_prompt{ast}──▶ PromptAst JSON
                                              │
                       PromptAstAdapter.load_messages   (AST → IR, directly)
                                              ▼
                                     list[ModelMessage]
                                              │
                        parsed_request_from_alloy(+ client_view)
                                              ▼
                                       ParsedRequest ──▶ dispatch_dump_sync ──▶ upstream
```

The AST arrives already provider-specialized (roles wrapped/merged/validated,
text runs coalesced, `ctx.output_format` schema prose burned into message
text — the SAP owns typing on the reply side, so `request_parameters` stays
empty). Node mapping: `message` role `system`/`user` → `SystemPromptPart` /
`UserPromptPart` accumulating into one `ModelRequest`; `assistant`/`model`
closes the request and appends a `ModelResponse[TextPart]`; any other role
fails loudly. Media maps to `ImageUrl`/`Audio`/`Video`/`DocumentUrl` (url) or
`BinaryContent` (base64, no `media_type` fallback); a local `file` source or
media inside a system/assistant message fails loudly. Message `metadata`
follows the Alloy VM-003 forward contract: `cache_control` → `CachePoint`
(supported TTL) or `raw_extras["cc:promptast:msg:{i}"]` (other TTL); any other
metadata key stashes verbatim under `raw_extras["promptast_meta:msg:{i}"]`.

**Boundary.** This module has no `InboundFormat` entry, no `Context`
integration, and no `dispatch_dump_sync`/`dispatch_intake`/`dispatch_render`
branch — the proxy pipeline is untouched. It is a first-party consumer entry
point that produces an `LLMRenderInput` (`ParsedRequest`), reusing the same
outbound adapters the proxy uses.

## Cache-breakpoint policy engine (`cache_policy.py`)

`ccproxy.lightllm.cache_policy` is a pure placement engine for Anthropic
prompt-cache breakpoints over the pydantic-ai IR. Division of labor:
BAML/Alloy owns *content* — statically authored markers arrive in-body
(VM-003 → `cache_control` → `CachePoint`s / `cc:` raw_extras via the inbound
adapters); cache *placement* is transport policy, and this engine computes
the dynamic placements an author can't know at blueprint time.

```python
from ccproxy.lightllm import CachePolicy, apply_cache_policy

policy = CachePolicy(tools="1h", system="1h", user_tail=2, user_tail_ttl="5m")
new_messages, new_settings, report = apply_cache_policy(
    messages, settings, policy, raw_extras=raw_extras
)
```

Pure function: inputs are never mutated (settings copied, touched message
parts rebuilt). Placements:

- `tools` → sets `anthropic_cache_tool_definitions` on the returned settings
  (there is no IR slot for tool markers; the dump side stamps the wire —
  exactly one marker, on the last non-deferred tool, matching pydantic-ai's
  contract; Anthropic rejects `cache_control` on `defer_loading` tools. An
  all-deferred tool set makes the placement a wire no-op — the engine can't
  see tools, so it still counts 1 breakpoint, a safe overestimate).
- `system` → appends the sentinel `UserPromptPart([CachePoint(ttl=…)])`
  after the last `SystemPromptPart` (the `dump_system` convention). No
  system parts → skipped, reported.
- `user_tail=N` → `CachePoint` appended to the final `UserPromptPart` of the
  last N user-content-bearing `ModelRequest`s, newest first (`str` content
  promoted to `[str, CachePoint]`). Shortfall → placed where possible,
  reported.

**Budget arbitration** — Anthropic allows at most 4 breakpoints per request;
the engine owns that invariant. It first censuses every marker that will
reach the wire, then admits policy placements in order `tools` → `system` →
`user_tail` newest-first; the first placement that would exceed 4 is dropped
along with everything after it.

| Marker source | Census | Removable by policy |
|---|---|---|
| Authored `CachePoint`s in messages (VM-003 / prior run) | 1 each | never |
| Sentinel system markers | 1 each | never |
| `cc:`-family `raw_extras` (`cc:msg:*`, `cc:promptast:msg:*`) | 1 each | never |
| `cache_control` on `raw_extras["tools"]` entries (verbatim override) | 1 each | never |
| `anthropic_cache_tool_definitions` / `_instructions` / `_messages` | 1 each | never |
| `anthropic_cache` shorthand | 1 | never |
| Policy placements | admitted while budget holds | yielded first |

When `raw_extras` contains a `"tools"` override, the `tools` placement is
always skipped (marker or not): the verbatim array overwrites the dump
side's formatted tools at stitch time, so a knob placement would burn
budget for a wire no-op.

Authored always wins: the engine never removes or re-TTLs an existing
marker. Idempotent: re-running a policy over its own output places nothing —
a target position that already carries a marker (any TTL) is skipped, never
stacked. `CacheBudgetReport` (`existing`, `placed`, `dropped`, `skipped` —
labels `"tools"`, `"system"`, `"user[-1]"`, …) accounts for every decision;
nothing is dropped silently.

Scope: Anthropic semantics only; callers gate on provider (the
`cache_breakpoints` hook guards on the anthropic-compatible family; library
callers — e.g. talkstream via `parsed_request_from_alloy(...)` — know their
upstream).

Wire-side, the `ccproxy.hooks.cache_breakpoints` outbound hook exposes the
engine per-request via the `x-ccproxy-cache-policy` header or per-instance
via hook `params`. Header DSL: comma-separated `key[=value]` pairs —
`tools[=ttl]`, `system[=ttl]`, `user_tail=N[:ttl]`; bare `tools`/`system`
default `5m`; e.g. `x-ccproxy-cache-policy: tools=1h,system=1h,user_tail=2:5m`.
Header beats config params; parse errors surface as an error `HookResult`
naming the bad token; the control header is stripped before egress.

## Adding a new provider

Suppose you're adding a new upstream provider — say "MyVendor" — that
accepts an Anthropic-compatible wire format. Walkthrough:

### 1. Configure the provider

In `ccproxy.yaml`:

```yaml
providers:
  myvendor:
    auth:
      type: file
      file: ~/.myvendor/token
    base_url: https://api.myvendor.com
    path: /v1/messages
    type: anthropic        # ← wire format = anthropic-compatible
```

Done. Sentinel key `sk-ant-oat-ccproxy-myvendor` now routes to
`api.myvendor.com` with the Anthropic adapter + intake + render, because
`type: anthropic` and `_ANTHROPIC_COMPATIBLE` includes it.

If the wire is OpenAI-compatible, use `type: openai`. If it's
Google-compatible, `type: google`.

### 2. If the wire format is genuinely new

Then you need a new adapter (request-side) and intake/render FSMs
(response-side). Files to add:

**Request side:**
* `src/ccproxy/lightllm/adapters/myvendor.py` — `MyVendorAdapter`
  subclass extending `pydantic_ai.ui.UIAdapter`. Implement
  `load_messages` (wire → IR) and either `dump_messages` (IR → wire for
  symmetric formats) or a `render(req)` classmethod (for outbound-only).
  Pattern from `adapters/anthropic.py` or `adapters/google.py`.
* Update `src/ccproxy/lightllm/adapters/__init__.py` to export the new
  adapter in `__all__`.

**Response side:**
* `src/ccproxy/lightllm/graph/myvendor_intake.py` — wire SSE → IR events.
  Pattern from `anthropic_intake.py`.
* `src/ccproxy/lightllm/graph/myvendor_render.py` (only if listener
  format is also new — i.e. ccproxy needs to ACCEPT requests AND render
  responses in MyVendor's wire format. Most new providers are
  upstream-only and only need intake.)
* Update `src/ccproxy/lightllm/graph/__init__.py`:
  * Add `myvendor` to the dispatch branches in `dispatch_dump_sync`,
    `dispatch_intake`, and `dispatch_render` (the last only if the
    listener format is also new).
  * Add `MyVendorResponseIntakeFSM` to the `AnyAsyncIntakeFSM` union and
    (if applicable) `MyVendorResponseRenderFSM` to `AnyAsyncRenderFSM`.

If the new provider just needs buffered response support, add a synthesis
branch to `buffered.py:_synthesize_chunks_for` covering its buffered-body
shape.

### 3. Write the tests

Copy `tests/test_lightllm_graph_<vendor>_load.py` and
`tests/test_lightllm_graph_<vendor>_dump.py` for the adapter, plus
`tests/test_lightllm_graph_intake_<vendor>.py` (and a corresponding
render file when the vendor is a listener format) for the FSMs:
* Roundtrip cases — at minimum: simple_text, multi_turn_with_tool_use,
  system_as_string, image_with_media_type, sampling_settings.
* Lossiness regressions: `test_metadata_preserved_via_raw_extras`,
  `test_render_returns_bytes`, `test_render_compact_json`.
* Run `uv run pytest tests/test_lightllm_graph_myvendor_*.py -q --no-cov`.

---

## Testing

### Roundtrip semantic equivalence (request side)

`tests/test_lightllm_graph_anthropic_dump.py` and
`tests/test_lightllm_graph_anthropic_load.py` together assert the
roundtrip. (The historical ``_dump`` / ``_load`` names predate the
adapter consolidation — the tests exercise `AnthropicAdapter` through
the `parse_request` / `render_request` fixtures in
``adapters/_envelope.py``.) The pattern is: load body → IR via the
adapter, wrap in a `ParsedRequest` (or `Context`) test fixture, render
back to wire bytes via the adapter, then compare against the input:

```python
# Load wire → IR. raw_extras and settings come from envelope helpers;
# adapter.load_messages only returns the message stream.
raw_extras: dict[str, Any] = {}
messages = AnthropicAdapter.load_messages(
    case.body["messages"], system=case.body.get("system"), raw_extras=raw_extras,
)
# In the test bench, build a ParsedRequest fixture with the full IR shape:
req = ParsedRequest(
    model=case.body["model"],
    messages=messages,
    request_parameters=ModelRequestParameters(function_tools=...),
    settings=settings,
    raw_extras=raw_extras,
)
rendered = AnthropicAdapter.render(req)
rebuilt = json.loads(rendered)
assert_anthropic_bodies_equivalent(case.body, rebuilt)
```

The `assert_anthropic_bodies_equivalent` helper tolerates field ordering,
`null` vs missing, `content` string ↔ single-block-list normalization,
`system` string ↔ block-list normalization, uniform-cache block
concatenation, default `tool_choice = auto`, and redundant
`is_error: False` defaults on tool_result blocks. Asserts equality on
`model`, `max_tokens`, `tools`, `messages`, `system`, and the sampling
settings.

### Roundtrip event-sequence equivalence (response side)

`tests/test_lightllm_graph_render_anthropic.py` feeds a
canonical SSE byte stream through the intake FSM, captures the resulting
IR event sequence, drives it back through the render FSM, parses the
result back into IR via a fresh intake — and asserts structural equality.
Same shape as the request-side roundtrip; the render's terminator bytes
are excluded from the round-trip target since the intake doesn't re-emit
them.

### Cross-impl streaming parity

`tests/test_lightllm_graph_sse_pipeline.py` exercises the persistent-loop
`SSEPipeline` against canonical fixtures:
* Anthropic → Anthropic same-format: render produces byte-equivalent SSE
  (after canonical normalization of random ids and `created` timestamps).
* Anthropic → OpenAI cross-format: render produces parseable OpenAI SSE
  whose IR re-parse matches the input.
* Chunk-boundary robustness: same wire output under 1-byte, 16-byte,
  64-byte, and all-at-once chunking.
* Concurrent independent pipelines on the same thread don't share state.

### Lossiness assertions

`tests/test_lightllm_graph_anthropic_dump.py` and
`tests/test_lightllm_graph_anthropic_load.py` (historical names
preserved; see Roundtrip section above) have tests ensuring the adapter
doesn't drop:

* `tool_name` populated for `ToolReturnPart` via two-pass lookup
* `BinaryContent.media_type` preserved
* Non-standard `cache_control.ttl` stashed in `raw_extras["cc:msg:N:block:M"]`
* Unknown content blocks stashed in `raw_extras["unknown_block:msg:N:idx:M"]`

Mirror these for any new provider's adapter.

---

## Visualization

Every built FSM in `lightllm/graph/` exposes a `.render()` mermaid
generator. Import the private module-level graph and print the diagram:

```python
from ccproxy.lightllm.graph.anthropic_intake import _intake_graph
print(_intake_graph.render(title="anthropic_intake", direction="LR"))
```

Produces (excerpt):

```
---
title: anthropic_intake
---
stateDiagram-v2
  direction LR
  frame_next_event
  state decision <<choice>>
  emit_done
  handle_content_block_delta
  handle_content_block_start
  handle_content_block_stop
  skip_ignored_event

  [*] --> frame_next_event
  frame_next_event --> decision
  decision --> emit_done
  decision --> handle_content_block_start
  decision --> handle_content_block_delta
  decision --> handle_content_block_stop
  decision --> skip_ignored_event
  handle_content_block_start --> frame_next_event
  handle_content_block_delta --> frame_next_event
  handle_content_block_stop --> frame_next_event
  skip_ignored_event --> frame_next_event
  emit_done --> [*]
```

The render-side graph lives at `_render_graph` in `anthropic_render.py`;
likewise `openai_intake._intake_graph`, `openai_render._render_graph`,
`google_intake._intake_graph`, `perplexity_intake._intake_graph`.

For the subgraph-composed intakes, the outer graph renders the composed
step as a single labelled node (`subgraph_pplx_event_dispatch:
dispatch_event` and `subgraph_google_chunk_dispatch: dispatch_chunk`).
The inner graphs are exposed at module scope and can be rendered
standalone:

```python
from ccproxy.lightllm.graph.perplexity_intake import _event_dispatch_graph
from ccproxy.lightllm.graph.google_intake import _chunk_dispatch_graph
print(_event_dispatch_graph.render(title="pplx_event_dispatch", direction="TB"))
print(_chunk_dispatch_graph.render(title="google_chunk_dispatch", direction="TB"))
```

Useful for debugging surprising routing, for code reviews, and for
keeping docs in sync.

---

## Troubleshooting

### `RuntimeError: This event loop is already running`

This should no longer occur on the request side — the adapters are pure
sync. If you see it on the response side, ensure you're using
`SSEPipeline` (which owns a persistent loop) instead of calling intake or
render FSMs directly from sync code.

### `UnsupportedUpstreamError: no outbound renderer for provider='X'`

Either the provider name is misspelled in `providers.X.provider` (config),
or you're trying to route to a provider that has no adapter. Add the
provider branch in `lightllm/graph/__init__.py:dispatch_dump_sync` and
create the adapter in `lightllm/adapters/`.

### `UnsupportedUpstreamError: no response intake for provider_type='X'`

Same diagnosis, but for the response side. Add a branch in
`dispatch_intake` plus the per-provider intake FSM module.

### `UnsupportedListenerError: no response render for inbound_format=X`

The listener format wasn't recognized by `dispatch_render`. Add a render
FSM module + a branch in `dispatch_render`.

### `ValueError: no IR parser for inbound_format=UNKNOWN`

The listener-format detection in `Context.from_flow` didn't match the
request path or headers. Check `_select_inbound_format` in
`pipeline/context.py`. Usual cause: a path that's neither
`/v1/messages` nor `/v1/chat/completions` and no `anthropic-version`
header.

### Lossiness regression test failed

A specific behavioral contract that's documented in the test docstring
just broke. Look at `tests/test_lightllm_graph_{anthropic,openai_chat}_{load,dump}.py`.
Restore the behavior — these are non-negotiable round-trip invariants.

### Streaming response is malformed / cut off

* Check `inspector/addon.py:_install_streaming_transformer` ran — search
  the logs for "SSEPipeline missing inbound_format / request_parameters".
  The pipeline only installs when both are stamped on the `TransformMeta`.
* Check the persistent loop is alive — `pipeline.close()` shouldn't have
  fired before EOS. `InspectorAddon.response` is the explicit-close
  callsite.
* Check `flow.response.stream` is the `SSEPipeline` instance, not
  overwritten by `GeminiAddon.responseheaders` (which has a back-off
  guard — investigate if the guard mis-fired).

### Buffered response is malformed

`transform_buffered_response_sync` failed silently — check the inspector
log for "Response transform failed, passing through raw response". Common
causes: synthesizing the per-block synthetic SSE for Anthropic when a
content block has an unexpected `type`; the buffered Gemini body wasn't a
`GenerateContentResponse` instance (cloudcode-pa returned an error
envelope without unwrap).

---

## File map

| Component | Path |
|---|---|
| Request envelope Protocol | `src/ccproxy/lightllm/adapters/__init__.py` (`LLMRenderInput`) |
| Test stub | `src/ccproxy/lightllm/parsed.py` (`ParsedRequest`) |
| Listener format enum | `src/ccproxy/lightllm/parsed.py` (`InboundFormat`) |
| Public dispatchers | `src/ccproxy/lightllm/graph/__init__.py` |
| Anthropic adapter | `src/ccproxy/lightllm/adapters/anthropic.py` |
| OpenAI Chat adapter | `src/ccproxy/lightllm/adapters/openai_chat.py` |
| Google adapter | `src/ccproxy/lightllm/adapters/google.py` |
| Perplexity adapter | `src/ccproxy/lightllm/adapters/perplexity.py` |
| Envelope helpers | `src/ccproxy/lightllm/adapters/_envelope.py`, `_anthropic_envelope.py`, `_openai_envelope.py` |
| Typed-tool wire-type mapping | `src/ccproxy/lightllm/adapters/_tool_kinds.py` |
| Shared FSM base classes | `src/ccproxy/lightllm/graph/_base.py` |
| Usage capture/projection helpers | `src/ccproxy/lightllm/graph/_usage.py` |
| `GraphBuilder.add_subgraph` patch | `src/ccproxy/lightllm/graph/_subgraph_patch.py` |
| Anthropic response FSMs | `src/ccproxy/lightllm/graph/anthropic_{intake,render}.py` |
| OpenAI response FSMs | `src/ccproxy/lightllm/graph/openai_{intake,render}.py` |
| Google response FSM | `src/ccproxy/lightllm/graph/google_intake.py` |
| Perplexity response FSM | `src/ccproxy/lightllm/graph/perplexity_intake.py` |
| Streaming response pipeline (persistent-loop bridge) | `src/ccproxy/lightllm/graph/sse_pipeline.py:SSEPipeline` |
| Buffered response transform | `src/ccproxy/lightllm/graph/buffered.py:transform_buffered_response_sync` |
| Inspector streaming call site | `src/ccproxy/inspector/addon.py:_install_streaming_transformer` |
| Inspector buffered call site | `src/ccproxy/inspector/routes/transform.py:handle_transform_response` |
| Inspector transform call site | `src/ccproxy/inspector/routes/transform.py:_handle_transform` |
| Tests (request side) | `tests/test_lightllm_graph_{anthropic,openai}_{load,dump}.py` + `_openai_responses_load.py` + `_google_dump.py` + `_perplexity_dump.py` + `_dispatch_sync.py` (historical file names — they exercise the adapters in ``src/ccproxy/lightllm/adapters/``) |
| Tests (response FSMs) | `tests/test_lightllm_graph_intake_*.py`, `test_lightllm_graph_render_*.py`, `test_lightllm_graph_buffered.py`, `test_lightllm_graph_sse_pipeline.py` |
| Perplexity Pro provider config + exceptions | `src/ccproxy/lightllm/pplx.py` |
| Perplexity business logic | `src/ccproxy/lightllm/pplx_steps.py`, `pplx_threads.py` |
| Provider registry | `src/ccproxy/lightllm/registry.py` |
| Hook results | `src/ccproxy/pipeline/results.py` (`HookResult` union) |
