# ccproxy MCP Notification Injection — Implementation Specification

**Version**: 1.0
**Status**: Contract for implementation
**Producer**: mcptty (Go MCP server)
**Consumer**: ccproxy (LiteLLM proxy with hook pipeline)

## Overview

mcptty wraps terminal applications in PTYs and exposes them via MCP tools. Its polling observer (`observe_start` / `tasks_get` / `observe_stop`) buffers terminal change events that an AI model can poll. This spec defines how ccproxy **automatically injects** those events into the conversation so the model doesn't need to manually poll.

```
Claude Code  ──MCP stdio──▶  mcptty
                                │  observe_start → polling observer running
                                │  terminal changes → DamageEvents buffered
                                │
                                │  POST /mcp/notify  (fire-and-forget)
                                ▼
Claude Code  ──API HTTP───▶  ccproxy
                               │  hook: inject_mcp_notifications
                               │  drain buffer → build tool_use/tool_result
                               │  inject at conversation TAIL
                               ▼
                            Anthropic API
```

---

## 1. Notification Receive Endpoint

### `POST /mcp/notify`

Receives fire-and-forget event notifications from mcptty's `NotifyClient`.

**Request body**:
```json
{
  "task_id": "string (UUID)",
  "session_id": "string (e.g. 'main')",
  "claude_session_id": "string (optional, Claude Code session ID)",
  "event": {
    "timestamp": "2026-03-01T12:34:56.789Z",
    "frame_index": 42,
    "tier": 2,
    "summary": "content: 5 cells changed in 1 region",
    "report": {
      "change_type": "partial",
      "regions": [
        {
          "bounds": {"x": 0, "y": 5, "w": 40, "h": 2},
          "type": "content",
          "old_text": "$ _",
          "new_text": "$ ls\nfile1.txt  file2.txt"
        }
      ],
      "stats": {
        "content_changes": 5,
        "style_only_changes": 0,
        "cells_changed": 80
      }
    },
    "screen_text": null
  }
}
```

**Field reference**:

| Field | Type | Present | Description |
|-------|------|---------|-------------|
| `task_id` | string | Always | UUID identifying the observer task |
| `session_id` | string | Always | Terminal session ID (e.g. "main") |
| `claude_session_id` | string | Optional | Claude Code session ID (defaults to empty string) |
| `event.timestamp` | RFC3339 | Always | When the change was detected |
| `event.frame_index` | int | Always | Monotonic frame counter |
| `event.tier` | int | Always | 1=style, 2=content, 3=layout shift |
| `event.summary` | string | Always | Human-readable change description |
| `event.report` | object/null | Tier 2+ | Full damage report with regions and stats |
| `event.screen_text` | string/null | Tier 3 only | Complete terminal screen content |

**Tier sizes**:
- Tier 1: ~50 bytes (style-only: cursor blinks, color changes)
- Tier 2: ~500 bytes (content changes with region details)
- Tier 3: ~4KB (layout shift with full screen text)

**Response**: `200 OK` (body ignored — mcptty is fire-and-forget)

**Error handling**: Return 200 even on internal errors. mcptty swallows all HTTP errors. Logging is sufficient.

---

## 2. Buffer Management

### Storage

In-memory dict keyed by `task_id`. Each entry holds:

```python
@dataclass
class TaskBuffer:
    task_id: str
    session_id: str
    events: list  # capped at max_events, oldest dropped on overflow
    last_seen: float  # time.time()
```

### Constraints

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Max events per task | 50 | Prevents unbounded growth |
| Overflow strategy | Drop oldest | Matches mcptty's internal buffer |
| TTL | 600 seconds (10 min) | Auto-cleanup stale tasks |
| Cleanup interval | 60 seconds | Background sweep |

### Operations

- **Write** (`POST /mcp/notify`): Append event to task's list. Update `last_seen`. If list exceeds max_events, oldest are dropped.
- **Drain** (hook injection): Atomically drain all tasks matching the current session_id. Returns `{task_id: events}` dict. Thread-safe via lock.
- **Expire**: Background thread removes entries where `time.time() - last_seen > ttl`.

---

## 3. Hook: `inject_mcp_notifications`

### Pipeline Position

```
ccproxy hook pipeline:
  1. forward_oauth
  2. gemini_cli_compat
  3. reroute_gemini
  4. extract_session_id
  ── transform (lightllm) ──
  5. inject_mcp_notifications   <── HERE (outbound, before forwarding)
  6. verbose_mode
  7. shape
```

### Signature

```python
@hook(writes=["messages"])
def inject_mcp_notifications(request, context):
```

### Logic

```
1. IF request has no "messages" field → return (skip non-chat requests)
2. IF notification buffer is empty → return (no-op, zero overhead)
3. FOR each task_id with buffered events:
   a. Drain all events atomically
   b. Apply coalescing rules (Section 4)
   c. IF coalesced result is trivial (e.g., "2 cursor blinks") → skip
   d. Build synthetic tasks_get response JSON
   e. Generate tool_use_id: "toolu_notify_<8-char-uuid>"
   f. Create assistant message (tool_use block)
   g. Create user message (tool_result block)
4. Find insertion point: BEFORE the final user message
5. Insert all generated message pairs at that point
```

### Insertion Point

```
messages = [
  system,           # cached — DO NOT TOUCH
  user,             # cached
  assistant,        # cached
  ...               # cached conversation history
  ─── injection point ───
  assistant(tool_use: tasks_get),    # INJECTED
  user(tool_result: events),         # INJECTED
  ─── end injection ───
  user              # final user message (current turn)
]
```

**CRITICAL**: Never inject into or before cached content. The system prompt and early conversation turns are prompt-cached. Injecting there busts the cache and wastes tokens.

---

## 4. Injection Format

### Assistant Message (tool_use)

```json
{
  "role": "assistant",
  "content": [
    {
      "type": "tool_use",
      "id": "toolu_notify_a1b2c3d4",
      "name": "tasks_get",
      "input": {
        "taskId": "abc-123-def-456"
      }
    }
  ]
}
```

### User Message (tool_result)

```json
{
  "role": "user",
  "content": [
    {
      "type": "tool_result",
      "tool_use_id": "toolu_notify_a1b2c3d4",
      "content": "{\"task_id\":\"abc-123-def-456\",\"status\":\"watching\",\"session_id\":\"main\",\"events\":[...],\"events_count\":3}"
    }
  ]
}
```

The `content` string is JSON matching `tasks_get`'s return schema:

```json
{
  "task_id": "abc-123-def-456",
  "status": "watching",
  "session_id": "main",
  "events": [
    {
      "timestamp": "2026-03-01T12:34:56.789Z",
      "frame_index": 42,
      "tier": 2,
      "summary": "content: 5 cells changed in 1 region",
      "report": { ... },
      "screen_text": null
    }
  ],
  "events_count": 1
}
```

### Why This Format Works

`tasks_get` is a real MCP tool registered on the mcptty server. The model has seen its schema in the tool list. Injected `tool_use`/`tool_result` pairs are indistinguishable from the model having called the tool itself. The model processes the events naturally as part of conversation flow.

---

## 5. Event Coalescing

Applied during drain, before injection. Reduces token cost.

### Rules

| Rule | Condition | Action |
|------|-----------|--------|
| Tier 1 collapse | Multiple tier 1 events | Replace all with: `{"tier": 1, "summary": "N style-only changes detected", "frame_index": <latest>}` |
| Tier 3 supersede | Tier 3 present | Drop ALL prior tier 1 and tier 2 events for same task. Tier 3 contains full screen. |
| Tier 2 dedup | Consecutive tier 2 with identical region bounds | Keep only the latest |
| Trivial skip | After coalescing, only tier 1 summary with count <= 3 | Skip injection entirely |

### Token Budget

| Budget | Limit |
|--------|-------|
| Max per injection | ~8KB (~2000 tokens) |
| If over budget | Drop all tier 1, keep last 5 tier 2, keep latest tier 3 |

### Priority (when trimming)

```
Tier 3 (keep latest)  >  Tier 2 (keep last 5)  >  Tier 1 (collapse to count)
```

---

## 6. Configuration

### ccproxy.yaml

```yaml
hooks:
  # ... existing hooks ...
  - ccproxy.hooks.inject_mcp_notifications

# Optional — defaults shown
mcp_notifications:
  max_events_per_task: 50
  max_injection_tokens: 2000
  ttl_seconds: 600
  coalesce_tier1: true
```

### Feature Toggle

When `inject_mcp_notifications` is not in the hooks list, the `/mcp/notify` endpoint should still accept and buffer events (allows enabling the hook without restarting mcptty), but the hook never fires.

Alternatively, if the endpoint itself should be gated:

```yaml
mcp_notifications:
  enabled: false  # disables both endpoint and hook
```

---

## 7. Edge Cases

| Case | Handling |
|------|----------|
| `tool_use_id` format | Must start with `toolu_` (Anthropic API requirement). Use `toolu_notify_<8-hex-chars>`. |
| Request without messages | Hook checks for `messages` key; skips embeddings, completions, etc. |
| Concurrent API requests | Lock on buffer drain. Each request gets whatever is buffered at that moment. |
| ccproxy restart | Buffer lost. mcptty continues POSTing. Buffer rebuilds from next event. |
| mcptty not running | No events arrive. Hook is permanent no-op. Zero overhead. |
| Multiple task_ids | Each gets independent tool_use/tool_result pair. Multiple pairs injected. |
| Empty events after coalescing | Skip injection (don't inject empty tool_result). |
| Multiple CC instances | Single-tenant for now. Future: route by session_id or API key. |

---

## 8. Testing Contract

### Unit Tests

| Test | Input | Expected |
|------|-------|----------|
| Endpoint accepts tier 1 | POST tier 1 event | 200 OK, event in buffer |
| Endpoint accepts tier 2 | POST tier 2 event with report | 200 OK, event in buffer |
| Endpoint accepts tier 3 | POST tier 3 event with screen_text | 200 OK, event in buffer |
| Buffer overflow | POST 55 events to same task | Buffer has 50, oldest 5 dropped |
| TTL expiry | POST event, wait >TTL | Buffer empty after cleanup |
| Hook no-op | Empty buffer, call hook | Messages unchanged |
| Hook injects pair | Buffer 3 events, call hook | 2 messages inserted before final user msg |
| Coalesce tier 1 | Buffer 10 tier 1 events | Single summary event in injection |
| Tier 3 supersede | Buffer tier 2 then tier 3 | Only tier 3 in injection |
| Cache safety | Verify injection index | Inserted AFTER all prior assistant/user turns, BEFORE final user |
| Concurrent drain | Drain from two threads | Each gets disjoint events, no duplicates |

### Integration Test Sequence

```
1. Start mcptty: ./bin/mcptty -- bash
2. Call observe_start → task_id
3. Type command in terminal (triggers damage events)
4. mcptty POSTs events to ccproxy /mcp/notify
5. Claude Code sends API request through ccproxy
6. Verify: response messages include injected tasks_get result
7. Verify: model response acknowledges terminal changes
8. Call observe_stop → cleanup
```

---

## 9. Graceful Degradation Matrix

| Infrastructure | Behavior | Model Experience |
|---|---|---|
| mcptty only | Model calls `tasks_get` manually when it wants updates | Explicit polling |
| mcptty + ccproxy | ccproxy auto-injects poll results | Automatic awareness |
| Native MCP Tasks client (future) | Full spec-compliant async push | Real-time streaming |

---

## 10. mcptty-Side Change Required

Extend `NotifyClient` POST body to include `session_id` (currently missing):

```go
// notify.go — extend payload struct
payload := struct {
    TaskID    string      `json:"task_id"`
    SessionID string      `json:"session_id"`
    Event     DamageEvent `json:"event"`
}{
    TaskID:    taskID,
    SessionID: sessionID,
    Event:     event,
}
```

This requires threading `sessionID` through the `Send` method signature. Trivial change.
