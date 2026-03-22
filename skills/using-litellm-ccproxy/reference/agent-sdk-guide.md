# Claude Agent SDK Guide

Integration guide for `claude-agent-sdk` with ccproxy OAuth.

## Contents

- [Installation](#installation)
- [Environment setup](#environment-setup)
- [Message types](#message-types)
- [Basic usage](#basic-usage)
- [Caching example](#caching-example)
- [Options reference](#options-reference)
- [Troubleshooting](#troubleshooting)

---

## Installation

```bash
uv add claude-agent-sdk
```

The SDK depends on `anthropic` internally. Install in the same environment as your script.

---

## Environment setup

Set these before any import of `claude_agent_sdk` — the SDK reads them at module load time:

```python
import os
os.environ["ANTHROPIC_BASE_URL"] = "http://localhost:4000"
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-oat-ccproxy-anthropic"

# Must come after env var setup
from claude_agent_sdk import query, ClaudeAgentOptions, AssistantMessage, ResultMessage, TextBlock
```

Alternatively, set in shell:

```bash
export ANTHROPIC_BASE_URL="http://localhost:4000"
export ANTHROPIC_API_KEY="sk-ant-oat-ccproxy-anthropic"
uv run python my_script.py
```

Or use a `.env` file with direnv (see [per-project-setup.md](per-project-setup.md)).

---

## Message types

`query()` yields a stream of message objects:

| Type | When | Key fields |
|------|------|-----------|
| `AssistantMessage` | Each assistant turn | `model`, `content: list[Block]` |
| `ResultMessage` | Final message, always last | `subtype`, `session_id`, `num_turns`, `duration_ms`, `duration_api_ms`, `total_cost_usd`, `usage: dict`, `is_error` |
| `TextBlock` | Content item within `AssistantMessage.content` | `text: str` |

`ResultMessage.usage` dict keys: `input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`.

---

## Basic usage

```python
import asyncio, os

os.environ["ANTHROPIC_BASE_URL"] = "http://localhost:4000"
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-oat-ccproxy-anthropic"

from claude_agent_sdk import query, ClaudeAgentOptions, AssistantMessage, ResultMessage, TextBlock

async def main():
    async for message in query(
        prompt="List Python files in this directory, then summarize the project.",
        options=ClaudeAgentOptions(
            allowed_tools=["Read", "Glob"],
            permission_mode="default",
            cwd=os.getcwd(),
        ),
    ):
        if isinstance(message, AssistantMessage):
            print(f"\n[{message.model}]")
            for block in message.content:
                if isinstance(block, TextBlock):
                    print(block.text)

        elif isinstance(message, ResultMessage):
            print(f"\n--- Done in {message.num_turns} turns ({message.duration_ms}ms) ---")
            if message.total_cost_usd is not None:
                print(f"Cost: ${message.total_cost_usd:.6f}")
            if message.is_error:
                print(f"Error subtype: {message.subtype}")

asyncio.run(main())
```

---

## Caching example

A working example demonstrating prompt caching effectiveness:

```bash
cd ~/dev/projects/ccproxy
uv run python docs/sdk/agent_sdk_caching_example.py
```

The example:
- Creates a prompt with >1024 tokens of context (required to trigger caching)
- Reports `cache_creation_input_tokens` (first run) and `cache_read_input_tokens` (subsequent runs)
- Uses rich for formatted output of usage statistics

Run twice to observe cache hit behavior. On the second run, `cache_read_input_tokens` should be nonzero.

Monitor ccproxy logs during execution:
```bash
ccproxy logs -f
```

---

## Options reference

`ClaudeAgentOptions` fields:

| Field | Type | Notes |
|-------|------|-------|
| `allowed_tools` | `list[str]` | Tools the agent may use, e.g. `["Read", "Glob", "Bash"]` |
| `permission_mode` | `str` | `"default"` prompts for permission; `"auto"` allows all |
| `cwd` | `str` | Working directory for file operations |
| `max_turns` | `int` | Maximum conversation turns |
| `system_prompt` | `str` | Additional system prompt (ccproxy prepends Claude Code identity before this) |

---

## Troubleshooting

### `ModuleNotFoundError: No module named 'claude_agent_sdk'`

```bash
uv add claude-agent-sdk
```

### `AuthenticationError` or 401

Verify ccproxy is running and sentinel key matches an `oat_sources` entry:
```bash
ccproxy status
grep oat_sources ~/.ccproxy/ccproxy.yaml
```

### SDK ignores `ANTHROPIC_BASE_URL`

Env vars must be set **before** `from claude_agent_sdk import ...`. Setting them after import has no effect.

### Caching not activating

Prompts must exceed 1024 tokens for cache eligibility. Check `cache_creation_input_tokens` in `ResultMessage.usage`.
