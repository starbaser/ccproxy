# Claude CLI vs glmaude Request Comparison

This document compares requests from Claude CLI (to Anthropic API) and glmaude (to Z.AI API) to understand prompt caching behavior.

## Executive Summary

| Aspect | Claude CLI (Anthropic) | glmaude (Z.AI) |
|--------|------------------------|----------------|
| **Endpoint** | `api.anthropic.com` | `api.z.ai` |
| **Request Size** | 134,770 bytes | 147,462 bytes |
| **Tools Count** | 20 | 20 |
| **System Blocks** | 3 | 2 |
| **Cache Read** | 15,883 tokens | 512 tokens |
| **Cache Creation** | 18,119 | N/A |

**Key Finding:** Z.AI caches only ~512 tokens (fixed tool definitions) while Anthropic caches much more (~15K+ tokens including system prompt).

---

## 1. HTTP Headers

### Claude CLI → Anthropic
```
anthropic-beta: oauth-2025-04-20,claude-code-20250219,interleaved-thinking-2025-05-14,advanced-tool-use-2025-11-20
anthropic-version: 2023-06-01
user-agent: claude-cli/2.1.12 (external, cli)
content-type: application/json
```

### glmaude → Z.AI
```
anthropic-beta: claude-code-20250219,interleaved-thinking-2025-05-14,advanced-tool-use-2025-11-20
anthropic-version: 2023-06-01
user-agent: claude-cli/2.1.12 (external, cli)
content-type: application/json
```

### Header Differences

| Header | Claude CLI | glmaude |
|--------|-----------|---------|
| `anthropic-beta` | `oauth-2025-04-20,claude-code-20250219,interleaved-thinking-2...` | `claude-code-20250219,interleaved-thinking-2025-05-14,advance...` |
| `user-agent` | `claude-cli/2.1.12 (external, cli)` | `claude-cli/2.1.12 (external, cli)` |
| Path | `/v1/messages?beta=true` | `/api/anthropic/v1/messages?beta=true` |

---

## 2. Request Structure

### Top-Level Keys

| Key | Claude CLI | glmaude |
|-----|-----------|---------|
| model | `claude-opus-4-5-20251101` | `glm-4.7` |
| max_tokens | `32000` | `32000` |
| stream | `True` | `True` |
| tools | ✅ (20) | ✅ (20) |
| system | ✅ (3 blocks) | ✅ (2 blocks) |
| messages | ✅ (1) | ✅ (1) |
| metadata | `['user_id']` | `['user_id']` |

---

## 3. System Prompt Structure

### Claude CLI System Blocks

| Block | Size | cache_control | Preview |
|-------|------|---------------|---------|
| 0 | 57 chars | ❌ | `You are Claude Code, Anthropic's official CLI for Claude....` |
| 1 | 62 chars | ✅ | `You are a Claude agent, built on Anthropic's Claude Agent SDK....` |
| 2 | 14,028 chars | ✅ | ` You are an interactive CLI tool that helps users with software engineering tasks. Use the instructi...` |

### glmaude System Blocks

| Block | Size | cache_control | Preview |
|-------|------|---------------|---------|
| 0 | 62 chars | ✅ | `You are a Claude agent, built on Anthropic's Claude Agent SDK....` |
| 1 | 13,900 chars | ✅ | ` You are an interactive CLI tool that helps users with software engineering tasks. Use the instructi...` |

---

## 4. Tools Comparison

### Summary

| Category | Count |
|----------|-------|
| Common tools | 20 |
| Claude CLI only | 0 |
| glmaude only | 0 |

### Common Tools (20)

Both Claude CLI and glmaude share these tools:

- `AskUserQuestion`
- `Bash`
- `Edit`
- `EnterPlanMode`
- `ExitPlanMode`
- `Glob`
- `Grep`
- `KillShell`
- `ListMcpResourcesTool`
- `MCPSearch`
- `NotebookEdit`
- `Read`
- `ReadMcpResourceTool`
- `Skill`
- `Task`
- `TaskOutput`
- `TodoWrite`
- `WebFetch`
- `WebSearch`
- `Write`

### Claude CLI Only (0)

(none)

### glmaude Only (0)

(none)

---

## 5. Cache Statistics

### Response Usage Comparison

| Metric | Claude CLI (Anthropic) | glmaude (Z.AI) |
|--------|------------------------|----------------|
| input_tokens | 3 | 0 |
| output_tokens | 4 | 0 |
| cache_read_input_tokens | 15,883 | 512 |
| cache_creation_input_tokens | 18,119 | N/A |

### Analysis

**Anthropic (Claude CLI):**
- Caches **15,883 tokens** (529433.3% of total input)
- Creates **18,119** new cache tokens
- Caches significant portions of the system prompt

**Z.AI (glmaude):**
- Caches only **512 tokens** (fixed amount)
- No cache creation reported
- Likely caches only tool definitions, not custom system prompts

---

## 6. Key Differences Summary

| Difference | Impact |
|------------|--------|
| **Cache amount** | Anthropic: ~15,883 tokens vs Z.AI: fixed 512 |
| **Cache creation** | Anthropic reports cache_creation; Z.AI doesn't |
| **Tool overlap** | 20/20 Claude tools are also in glmaude |
| **Beta header** | Different beta feature flags |

---

## 7. Implications for SDK/ccproxy

For an SDK to get caching benefits:

1. **Tools are required** - Both APIs only cache when tools are present
2. **Z.AI caches less** - Only ~512 tokens (tool definitions), not custom prompts
3. **Anthropic caches more** - Significant system prompt caching possible

### Recommendation for ccproxy

To enable caching for requests routed to Z.AI:
- Include at least one tool definition in requests
- Expect ~512 token savings (fixed, regardless of prompt size)
- Consider adding a hook to inject minimal tools for Z.AI-bound requests

### Test Verification

To verify caching works, the request must include:
- `tools` array with at least one tool
- `?beta=true` query parameter (Z.AI requirement)
- `anthropic-beta` header with appropriate flags
- `cache_control: {"type": "ephemeral"}` on system blocks

---

*Generated from MITM traces captured on 2026-01-17 17:43*
