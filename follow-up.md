# Follow-up — SSE terminal signals and `finish_reason` propagation

Landed here: `7024db7` — "openai_conversations: idle-terminated turns carry a
typed terminal signal". A turn that hit the 120 s idle wait used to end its SSE
stream with no wire signal, and the intake's absent `finish_reason` rendered as
a false `"stop"` — a truncated turn was indistinguishable from a completed one.
Both give-up sites now emit a typed idle-timeout frame that the intake maps to
`finish_reason = "error"` (without overwriting a real completion), and the
timeout became one configured value (`OpenAIConversationsConfig.turn_idle_timeout_seconds`)
instead of two hardcoded literals.

Source spec: the P-6 section of
`~/dev/projects/alloy/plans/issues/eval/EVAL-007-first-party-hard-bound-ownership-audit.ledger.md`.

## Open item — larger than the proposal that surfaced it

### [High] `finish_reason` never reaches any streaming renderer — DONE

Fixed in `5c8d516`.

`SSEPipeline._drain_and_terminate()` now threads the intake's captured
`finish_reason` alongside `usage` / `raw_extras` into all three streaming render
`close()` implementations, and the per-listener projection lives in one place
(`lightllm/graph/_finish_reason.py`, the finish-reason twin of `_usage.py`) that
both the streaming terminators and the buffered assemblers call — so a
`stream: true` client and a `stream: false` client are told the same thing about
how a turn ended.

What the client now receives:

| IR reason | OpenAI Chat | Anthropic | OpenAI Responses |
| --- | --- | --- | --- |
| `length` | `finish_reason: "length"` | `stop_reason: "max_tokens"` | `response.incomplete` + `incomplete_details.reason: "max_output_tokens"` |
| `content_filter` | `finish_reason: "content_filter"` | `stop_reason: "refusal"` | `response.incomplete` + `incomplete_details.reason: "content_filter"` |
| `tool_call` | `finish_reason: "tool_calls"` | `stop_reason: "tool_use"` | `response.completed` |
| `error` | `finish_reason: "error"` | `stop_reason: "error"` | `response.failed` |

Beyond the three renderers, the same seam had gaps on the capture side that
would have left the threading half-dead for two providers:

- **Anthropic intake** never read `message_delta.delta.stop_reason` — although
  `_ANTHROPIC_MESSAGE_MODELED_KEYS` already listed `stop_reason` as *modeled*,
  nothing modelled it. It now maps into the funnel slot, with the raw wire value
  carried through `raw_extras["stop_reason"]` (so `pause_turn` / `compaction`,
  which the IR cannot express, are not dropped either).
- **Google intake** never read `candidate.finishReason`, so a Gemini
  `MAX_TOKENS` / `SAFETY` cut reached every listener as a clean completion.
  Capture runs *before* the contentless-candidate short-circuit, which is the
  exact frame a truncated Gemini turn arrives on.
- **Buffered Anthropic assembler** took a `stop_reason` parameter that no caller
  ever passed — a phantom knob, now plumbed through the shared projection.
- `_OPENAI_FINISH_BY_PART` in `buffered.py` was a constant no code path read;
  deleted. The OpenAI-Chat render state's `finish_reason` field (a five-member
  Literal only ever set to `"tool_calls"`) is now the boolean it always was,
  `tool_calls_rendered`, and the Responses render state's `finish_status` field
  (documented `"completed"`/`"incomplete"`/`"failed"`, never assigned) is gone in
  favour of the value actually threaded in.

Regressions: `tests/test_lightllm_graph_finish_reason.py` (39 tests). Eleven of
them fail on the pre-fix code, each crossing the exact former threshold —
verified by removing the `finish_reason=` argument from
`_drain_and_terminate()` and re-running: `stop` → `length`, `end_turn` →
`max_tokens`, `response.completed` → `response.incomplete`, `stop` → `error`.

## Owner decision — please review

**`"error"` on the OpenAI Chat and Anthropic wires is out-of-enum.** Neither
closed enum has a member for it (`stop|length|tool_calls|content_filter|function_call`;
`end_turn|max_tokens|stop_sequence|tool_use|pause_turn|compaction|refusal|model_context_window_exceeded`).
The alternatives were a fabricated completion (`stop` / `end_turn` — the exact
lie this whole item exists to remove) or a null/absent stop reason (truthful but
mute). Precedent decided it: the buffered OpenAI Chat body already emitted
`finish_reason: "error"` before this change, so the streaming path now agrees.
Verified that Stainless-generated SDKs parse response enums permissively —
`anthropic._models.construct_type` returns `stop_reason='error'` as a plain
string rather than raising — so no SDK client hard-fails on it.

If you would rather signal an abnormal Anthropic ending in-spec, the option is
the protocol's own `event: error` frame ahead of the terminator. That was left
out deliberately: it changes a truncated-but-readable turn into a client-side
raise (Claude Code would surface an API error instead of the partial answer),
which is a product decision, not a plumbing one.

## Gates

Run for this change, all green:

`nix develop --command uv run pytest` (2645 passed, 10 e2e deselected, coverage
86.90% against the 86% gate), `uv run ruff check .`,
`uv run ruff format --check .`, `uv run mypy src/ccproxy tests --no-incremental`,
`uv run ty check src tests`, `nix flake check`. This flake has no
pytest-running check derivation — the Python suite is the test vector.

## Rules for the lane

- **A retrievable domain is paginated, never truncated.** If the caller could
  ask for the rest, deliver continuation — Alloy owns that vocabulary (MCP
  cursor pagination, truthful `total`/`hasMore`, generation-bound section
  cursors). Do not reach for a `(+N more)` marker or a `truncated` flag on a
  surface that could have paged; that is silent truncation wearing a label.
- Markers and drop counters are the fallback for **genuinely lossy** surfaces
  only — a ring buffer that overwrote, an event channel that dropped on a slow
  subscriber, a rendered viewport. Where the loss is avoidable, remove the loss.
- Never enlarge a constant to make a symptom go away. Any surviving bound needs
  a normative spec, a physical or library fact, an authoritative upstream limit,
  a viewport that never bounds the underlying domain, an algorithmic safety
  guard with an explicit incomplete-result signal, or a named configurable
  budget that degrades through continuation rather than loss.
- A bound constant must be consumed and a documented knob must be plumbed —
  delete the phantom, or wire it end to end.
- Every regression must **cross** the former threshold and prove the complete
  domain is reachable (or, for a lossy surface, that the signal fires).
- Fix at the source owner, in this repo only. Planning and proposal identifiers
  never appear in code, comments, or test names. Run this repo's documented
  gates in full. Another session may hold files dirty here — defer any item
  whose target file is already modified, and say so.

### Lane notes

- `flake.lock` and `plans/.ignore` were already modified by a concurrent session
  when this lane started. Neither is a target of this item; both were left
  untouched and out of the commit. Nothing was deferred on their account.
