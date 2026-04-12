---
name: using-ccproxy-api
description: >-
  Guides users through ccproxy as an OpenAI-compatible and Anthropic-compatible LLM API server
  with SDK integration, OAuth authentication, sentinel key substitution, model routing, and
  troubleshooting. Use when configuring SDK clients (Anthropic, OpenAI, LiteLLM, Agent SDK)
  against ccproxy, debugging authentication errors, setting up OAuth token forwarding,
  or understanding the hook pipeline, beta headers, and sentinel key mechanism.
---

# Using ccproxy as an LLM API Server

ccproxy exposes an OpenAI-compatible and Anthropic-compatible API on `http://localhost:4000`. Any SDK or HTTP client that supports custom `base_url` can use it.

## Quick start

```python
# Anthropic SDK (OAuth via sentinel key)
import anthropic
client = anthropic.Anthropic(
    api_key="sk-ant-oat-ccproxy-anthropic",
    base_url="http://localhost:4000",
)

# OpenAI SDK
from openai import OpenAI
client = OpenAI(
    api_key="sk-ant-oat-ccproxy-anthropic",
    base_url="http://localhost:4000",
)
```

## How authentication works

ccproxy supports two authentication modes:

**OAuth mode** (subscription accounts — Claude Max, Team, Enterprise):
1. Client sends sentinel key `sk-ant-oat-ccproxy-{provider}` as API key
2. `forward_oauth` hook detects sentinel prefix, looks up real token from `oat_sources`
3. `apply_compliance` hook stamps learned headers (`anthropic-beta`, `anthropic-version`), system prompt, and body envelope fields from a compliance profile
4. Request reaches provider API with valid OAuth Bearer token and full compliance contract

**API key mode** (direct API keys):
1. Client sends real API key via `x-api-key` or `Authorization` header
2. Key passes through to the provider unchanged

### Sentinel key format

```
sk-ant-oat-ccproxy-{provider}
```

Where `{provider}` matches a key in `oat_sources` config. Common values:
- `sk-ant-oat-ccproxy-anthropic` — uses `oat_sources.anthropic` token
- `sk-ant-oat-ccproxy-zai` — uses `oat_sources.zai` token
- `sk-ant-oat-ccproxy-gemini` — uses `oat_sources.gemini` token

### Default hooks

```yaml
hooks:
  inbound:
    - ccproxy.hooks.forward_oauth
    - ccproxy.hooks.extract_session_id
  outbound:
    - ccproxy.hooks.inject_mcp_notifications
    - ccproxy.hooks.verbose_mode
    - ccproxy.hooks.apply_compliance
```

- `forward_oauth` — substitutes sentinel key with real token, sets `Authorization: Bearer {token}`, clears `x-api-key`
- `extract_session_id` — parses `metadata.user_id` for MCP notification routing
- `inject_mcp_notifications` — injects buffered MCP terminal events as tool_use/tool_result pairs
- `verbose_mode` — strips `redact-thinking-*` from `anthropic-beta` to enable full thinking output
- `apply_compliance` — stamps learned compliance headers, body fields, and system prompt (see below)

### Compliance-based headers and identity

Instead of explicit hooks for beta headers and identity injection, ccproxy uses a **compliance learning system**. It passively observes legitimate CLI traffic (via WireGuard) and learns the exact headers, body fields, and system prompt that constitute a compliant request. This learned profile is then stamped onto SDK requests by `apply_compliance`.

The compliance system automatically handles `anthropic-beta`, `anthropic-version`, system prompt injection, and body envelope fields. An Anthropic v0 seed profile provides baseline coverage on first startup before any real traffic is observed.

See the `using-ccproxy-inspector` skill for details on seeding and inspecting compliance profiles.

## SDK integration

### Anthropic Python SDK

```python
import anthropic

client = anthropic.Anthropic(
    api_key="sk-ant-oat-ccproxy-anthropic",
    base_url="http://localhost:4000",
)

response = client.messages.create(
    model="claude-sonnet-4-5-20250929",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Hello"}],
)
```

No extra headers needed — the pipeline hooks handle `anthropic-beta`, `anthropic-version`, and system message injection automatically.

Streaming:
```python
with client.messages.stream(
    model="claude-sonnet-4-5-20250929",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Hello"}],
) as stream:
    for text in stream.text_stream:
        print(text, end="")
```

### OpenAI Python SDK

```python
from openai import OpenAI

client = OpenAI(
    api_key="sk-ant-oat-ccproxy-anthropic",
    base_url="http://localhost:4000",
)

response = client.chat.completions.create(
    model="claude-sonnet-4-5-20250929",
    messages=[{"role": "user", "content": "Hello"}],
)
```

LiteLLM translates OpenAI format to Anthropic format internally.

### LiteLLM SDK

```python
import asyncio, litellm

async def main():
    response = await litellm.acompletion(
        model="claude-sonnet-4-5-20250929",
        messages=[{"role": "user", "content": "Hello"}],
        api_base="http://127.0.0.1:4000",
        api_key="sk-ant-oat-ccproxy-anthropic",
    )
    print(response.choices[0].message.content)

asyncio.run(main())
```

**Note**: `litellm.anthropic.messages` bypasses proxies. Always use `litellm.acompletion()`.

### Claude Agent SDK

```python
import os
os.environ["ANTHROPIC_BASE_URL"] = "http://localhost:4000"
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-oat-ccproxy-anthropic"

from claude_agent_sdk import query, ClaudeAgentOptions

async for message in query(
    prompt="Your prompt here",
    options=ClaudeAgentOptions(
        allowed_tools=["Read", "Glob"],
        permission_mode="default",
        cwd=os.getcwd(),
    ),
):
    # Handle AssistantMessage, ResultMessage, etc.
    pass
```

### Environment variables (any SDK)

```bash
export ANTHROPIC_BASE_URL="http://localhost:4000"
export ANTHROPIC_API_KEY="sk-ant-oat-ccproxy-anthropic"
# OpenAI compat
export OPENAI_BASE_URL="http://localhost:4000"
export OPENAI_API_BASE="http://localhost:4000"
```

### curl (raw HTTP)

```bash
# Anthropic /v1/messages endpoint
curl http://localhost:4000/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: sk-ant-oat-ccproxy-anthropic" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "claude-sonnet-4-5-20250929",
    "max_tokens": 100,
    "messages": [{"role": "user", "content": "Hello"}]
  }'
```

## Model routing

Model routing is configured via `inspector.transforms` in `ccproxy.yaml`. Each transform rule matches by `match_host`, `match_path`, and/or `match_model`, then rewrites to `dest_provider`/`dest_model` via the lightllm dispatch. First match wins. Unmatched flows pass through unchanged.

See [reference/routing-and-config.md](reference/routing-and-config.md) for transform configuration patterns.

## Troubleshooting

Authentication failures are the most common issue. Follow this decision tree:

```
Error message?
│
├─ "This credential is only authorized for use with Claude Code"
│  ▶ See: Missing identity injection
│
├─ "OAuth is not supported" / "invalid x-api-key"
│  ▶ See: Missing beta headers
│
├─ 401 Unauthorized / "authentication" / token errors
│  ▶ See: Token issues
│
├─ Connection refused / timeout
│  ▶ See: Connectivity
│
└─ Other / unclear
   ▶ See: General diagnostics
```

See [reference/troubleshooting.md](reference/troubleshooting.md) for the full diagnostic guide with resolution steps for each branch.

### Quick diagnostic commands

```bash
ccproxy status              # Verify proxy is running
ccproxy status --json       # Machine-readable status with URL
ccproxy logs -f             # Stream logs in real-time
ccproxy logs -n 50          # Last 50 lines
```

## Reference files

- [reference/troubleshooting.md](reference/troubleshooting.md) — Full diagnostic decision tree with error-specific resolution steps
- [reference/routing-and-config.md](reference/routing-and-config.md) — Model routing, config.yaml patterns, hook pipeline details
