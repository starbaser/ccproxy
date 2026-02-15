# SDK Examples

This directory contains examples demonstrating how to use various Python SDKs with ccproxy for LLM request routing and monitoring.

## Overview

These examples show how to route SDK requests through ccproxy to leverage intelligent model routing, request classification, and observability features. All examples assume ccproxy is running locally on the default port (4000).

## OAuth Sentinel Key

ccproxy supports a **sentinel API key** that triggers automatic OAuth token substitution. This allows SDK clients to use ccproxy's cached OAuth credentials without needing a real API key.

**Format:** `sk-ant-oat-ccproxy-{provider}`

**Example for Anthropic:**
```python
import anthropic

client = anthropic.Anthropic(
    api_key="sk-ant-oat-ccproxy-anthropic",  # Sentinel key
    base_url="http://localhost:4000",
)
```

When ccproxy sees this sentinel key, it:
1. Looks up the OAuth token for the specified provider from `oat_sources` config
2. Substitutes the sentinel with the real OAuth token
3. Adds required headers (`anthropic-beta`, etc.)
4. Injects the "You are Claude Code" system message prefix (for OAuth compliance)

**Requirements:**
- OAuth credentials configured in `~/.ccproxy/ccproxy.yaml` under `oat_sources`
- Pipeline hooks enabled: `inject_claude_code_identity`, `add_beta_headers`, `forward_oauth`
- (Optional) MITM mode provides redundant safety net for header injection at HTTP layer

```bash
# Start ccproxy
ccproxy start --detach
```

## Examples

### agent_sdk_caching_example.py

Demonstrates Claude Agent SDK integration with ccproxy for prompt caching monitoring.

**Purpose:**
- Monitor prompt caching effectiveness via usage statistics
- Show cache creation and hit metrics through ccproxy
- Demonstrate Agent SDK `query()` with tool permissions

**Prerequisites:**
```bash
# Install claude-agent-sdk
uv add claude-agent-sdk

# Start ccproxy
ccproxy start --detach
ccproxy logs -f
```

**Usage:**
```bash
# Run the example
uv run python docs/sdk/agent_sdk_caching_example.py

# Run multiple times to observe cache behavior
uv run python docs/sdk/agent_sdk_caching_example.py
```

**Expected Cache Behavior:**
- **First run**: Creates cache with substantial context (>1024 tokens)
  - Look for `cache_creation_input_tokens` in usage stats
- **Subsequent runs**: Hit existing cache, reducing input token costs
  - Look for `cache_read_input_tokens` > 0 in usage stats

**Environment Variables:**
- `ANTHROPIC_BASE_URL`: Points to ccproxy (default: `http://localhost:4000`)
- `ANTHROPIC_API_KEY`: Use sentinel key `sk-ant-oat-ccproxy-anthropic` for OAuth

---

### anthropic_sdk.py

Direct usage of the Anthropic SDK with ccproxy using OAuth credential forwarding.

**Purpose:**
- Demonstrate non-streaming and streaming requests via Anthropic SDK
- Show proxy-based OAuth authentication using sentinel key
- Simple request/response pattern

**Prerequisites:**
```bash
# Install anthropic SDK
uv add anthropic

# Configure OAuth credentials in ~/.ccproxy/ccproxy.yaml
# Start ccproxy
ccproxy start --detach
```

**Usage:**
```bash
# Run both simple and streaming examples
uv run python docs/sdk/anthropic_sdk.py
```

**Features:**
- Uses sentinel API key (`sk-ant-oat-ccproxy-anthropic`) - proxy substitutes real OAuth token
- Base URL: `http://localhost:4000`
- Demonstrates both `messages.create()` and `messages.stream()` patterns
- Pipeline hooks inject required headers and system message for OAuth compliance

---

### litellm_sdk.py

Using LiteLLM's Python SDK with async completion API.

**Purpose:**
- Show async request patterns with `litellm.acompletion()`
- Demonstrate streaming and non-streaming modes
- Illustrate proxy-based credential handling

**Prerequisites:**
```bash
# Install litellm
uv add litellm

# Configure credentials in ~/.ccproxy/ccproxy.yaml
# Start ccproxy
ccproxy start --detach
```

**Usage:**
```bash
# Run both simple and streaming examples
uv run python docs/sdk/litellm_sdk.py
```

**Features:**
- Uses `litellm.acompletion()` interface (works with proxies)
- Async/await patterns for concurrent requests
- Sentinel key with proxy authentication

**Note:** The `litellm.anthropic.messages` interface bypasses proxies, so this example uses the standard completion interface instead.

---

### zai_anthropic_sdk.py

Using Anthropic SDK to access Z.AI GLM models via ccproxy.

**Purpose:**
- Demonstrate Anthropic SDK with GLM-4.7 routed through ccproxy
- Show non-streaming and streaming patterns with messages API
- Proxy handles authentication via `os.environ/ZAI_API_KEY` in config.yaml

**Prerequisites:**
```bash
# Ensure ZAI_API_KEY is in environment (for config.yaml)
export ZAI_API_KEY="your-api-key"

# Start ccproxy
ccproxy start --detach
```

**Usage:**
```bash
uv run python docs/sdk/zai_anthropic_sdk.py
```

**Features:**
- Routes through ccproxy at `http://127.0.0.1:4000`
- Model: `glm-4.7` (defined in ~/.ccproxy/config.yaml)
- Dummy API key - ccproxy handles real authentication

## Common Setup

All examples require ccproxy to be running:

```bash
# Start ccproxy
ccproxy start --detach

# Optional: Enable MITM for redundant HTTP-layer safety net
ccproxy start --detach --mitm

# Monitor logs (optional)
ccproxy logs -f

# Check status
ccproxy status

# Stop when done
ccproxy stop
```

## Configuration

Examples expect ccproxy running with:
- **Proxy port**: 4000 (default)
- **OAuth credentials**: Configured in `~/.ccproxy/ccproxy.yaml` under `oat_sources`
- **Models**: Defined in `~/.ccproxy/config.yaml` for LiteLLM proxy
- **MITM mode**: Optional (provides HTTP-layer redundancy for header injection)

### Example ccproxy.yaml OAuth Configuration

```yaml
ccproxy:
  oat_sources:
    anthropic:
      command: "jq -r '.claudeAiOauth.accessToken' ~/.claude/.credentials.json"
      user_agent: "anthropic"

  mitm:
    enabled: true
    port: 8081
```

## Troubleshooting

If examples fail:

1. **Verify ccproxy is running**: `ccproxy status`
2. **Check OAuth credentials**: Verify `oat_sources` in `~/.ccproxy/ccproxy.yaml`
3. **Review logs**: `ccproxy logs -f` for detailed error messages
4. **Check pipeline hooks**: Ensure `inject_claude_code_identity`, `add_beta_headers`, and `forward_oauth` are enabled in hooks configuration
5. **Optional MITM verification**: If using `--mitm`, status should show `mitm: reverse on 4000`
6. **Verify port**: Default is 4000, ensure it's not blocked or in use

### Common Errors

- **"This credential is only authorized for use with Claude Code"**: OAuth pipeline hooks not configured. Verify `inject_claude_code_identity` and `add_beta_headers` hooks are enabled in `ccproxy.yaml`. Optionally enable MITM mode for redundant safety.
- **"invalid x-api-key"**: OAuth headers not being set correctly. Check `forward_oauth` hook configuration and logs.
- **Connection refused**: ccproxy not running. Check `ccproxy status`.

## Additional Resources

- [ccproxy Documentation](../../README.md)
- [Anthropic SDK Documentation](https://github.com/anthropics/anthropic-sdk-python)
- [LiteLLM Documentation](https://docs.litellm.ai/)
- [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python)
