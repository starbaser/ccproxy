# SDK Examples

This directory contains examples demonstrating how to use various Python SDKs with ccproxy for LLM request routing and monitoring.

## Overview

These examples show how to route SDK requests through ccproxy to leverage intelligent model routing, request classification, and observability features. All examples assume ccproxy is running locally on the default port (4000).

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

# Start ccproxy with debug logging
ccproxy start --detach
ccproxy logs -f
```

**Usage:**
```bash
# Run the example
uv run python docs/SDK/agent_sdk_caching_example.py

# Run multiple times to observe cache behavior
uv run python docs/SDK/agent_sdk_caching_example.py
uv run python docs/SDK/agent_sdk_caching_example.py
```

**Expected Cache Behavior:**
- **First run**: Creates cache with substantial context (>1024 tokens)
  - Look for `cache_creation_input_tokens` in usage stats
  - Subsequent requests can reuse this cached content
- **Subsequent runs**: Hit existing cache, reducing input token costs
  - Look for `cache_read_input_tokens` > 0 in usage stats
  - Monitor ccproxy logs for cache metrics

**Environment Variables:**
- `ANTHROPIC_BASE_URL`: Points to ccproxy (default: `http://localhost:4000`)
- `ANTHROPIC_API_KEY`: Your Anthropic API key (required for authentication)

---

### anthropic_sdk.py

Direct usage of the Anthropic SDK with ccproxy using credential forwarding.

**Purpose:**
- Demonstrate non-streaming and streaming requests via Anthropic SDK
- Show proxy-based authentication (no API key needed in script)
- Simple request/response pattern

**Prerequisites:**
```bash
# Install anthropic SDK
uv add anthropic

# Configure credentials in ~/.ccproxy/ccproxy.yaml
# Start ccproxy
ccproxy start --detach
```

**Usage:**
```bash
# Run both simple and streaming examples
uv run python docs/SDK/anthropic_sdk.py
```

**Features:**
- Uses dummy API key (`sk-proxy-dummy`) - proxy handles real authentication
- Base URL: `http://127.0.0.1:4000`
- Demonstrates both `messages.create()` and `messages.stream()` patterns

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
uv run python docs/SDK/litellm_sdk.py
```

**Features:**
- Uses `litellm.acompletion()` interface (works with proxies)
- Async/await patterns for concurrent requests
- Dummy API key with proxy authentication

**Note:** The `litellm.anthropic.messages` interface bypasses proxies, so this example uses the standard completion interface instead.

## Common Setup

All examples require ccproxy to be running:

```bash
# Start ccproxy in detached mode
ccproxy start --detach

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
- **Credentials**: Configured in `~/.ccproxy/ccproxy.yaml` or via environment variables
- **Models**: Defined in `~/.ccproxy/config.yaml` for LiteLLM proxy

## Troubleshooting

If examples fail:

1. **Verify ccproxy is running**: `ccproxy status`
2. **Check credentials**: Ensure API key is set in ccproxy configuration
3. **Review logs**: `ccproxy logs -f` for detailed error messages
4. **Verify port**: Default is 4000, ensure it's not blocked or in use

## Additional Resources

- [ccproxy Documentation](../../README.md)
- [Anthropic SDK Documentation](https://github.com/anthropics/anthropic-sdk-python)
- [LiteLLM Documentation](https://docs.litellm.ai/)
- [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python)
