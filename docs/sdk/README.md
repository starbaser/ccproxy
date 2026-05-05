# SDK Examples

This directory contains examples demonstrating how to use various Python SDKs with ccproxy for LLM request routing and monitoring.

## Overview

These examples show how to route SDK requests through ccproxy to leverage intelligent model routing, request classification, and observability features. All examples assume ccproxy is running locally on the default port (4000).

To install all SDK dependencies needed by these examples:

```bash
uv add claude-ccproxy[sdk]
```

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
1. Looks up the OAuth token for the specified provider from the `providers` map
2. Substitutes the sentinel with the real OAuth token (and routes the request to the matching `Provider`'s `host`/`path`)
3. If shaping is enabled, stamps captured compliance headers (beta flags, user-agent, etc.) onto the request

**Requirements:**
- A `providers` entry configured in `~/.config/ccproxy/ccproxy.yaml` for the sentinel suffix
- Pipeline hooks enabled: `forward_oauth`, `shape`

```bash
# Start ccproxy (foreground — use process-compose or systemd for background)
ccproxy start
```

## Examples

### anthropic_sdk.py

Direct usage of the Anthropic SDK with ccproxy using OAuth credential forwarding.

**Purpose:**
- Demonstrate non-streaming and streaming requests via Anthropic SDK
- Show proxy-based OAuth authentication using sentinel key
- Simple request/response pattern

**Prerequisites:**
```bash
# anthropic is a core dep of ccproxy — no extra install needed

# Configure OAuth credentials in ~/.config/ccproxy/ccproxy.yaml
# Start ccproxy
ccproxy start
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
# litellm is a core dep of ccproxy — no extra install needed

# Configure credentials in ~/.config/ccproxy/ccproxy.yaml
# Start ccproxy
ccproxy start
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
ccproxy start
```

**Usage:**
```bash
uv run python docs/sdk/zai_anthropic_sdk.py
```

**Features:**
- Routes through ccproxy at `http://127.0.0.1:4000`
- Model: `glm-4.7` (defined in ~/.config/ccproxy/config.yaml)
- Dummy API key - ccproxy handles real authentication

---

### gemini_sdk.py

google-genai SDK through ccproxy using the Gemini sentinel key.

**Purpose:**
- Demonstrate non-streaming and streaming content generation via google-genai SDK
- Show proxy-based OAuth authentication using the Gemini sentinel key
- The `gemini_cli` outbound hook wraps standard Gemini bodies in the v1internal envelope

**Prerequisites:**
```bash
# Install google-genai (included in ccproxy[sdk])
uv add claude-ccproxy[sdk]

# Ensure Gemini OAuth credentials exist
gemini -p ""

# Start ccproxy
ccproxy start
```

**Usage:**
```bash
uv run python docs/sdk/gemini_sdk.py
```

**Features:**
- Uses sentinel key `sk-ant-oat-ccproxy-gemini` — proxy substitutes real OAuth token
- Base URL: `http://127.0.0.1:4000/gemini`
- Demonstrates both `generate_content()` and `generate_content_stream()` patterns
- Same-format redirect — no body transformation needed

---

### deepseek_sdk.py

Anthropic SDK through ccproxy to DeepSeek using the sentinel key.

**Purpose:**
- Demonstrate using the Anthropic SDK with DeepSeek models
- DeepSeek exposes an Anthropic-compatible API — same wire format, same SDK
- ccproxy handles `x-api-key` header injection via `forward_oauth` hook

**Prerequisites:**
```bash
# anthropic is a core dep of ccproxy — no extra install needed

# Configure providers.deepseek in ccproxy.yaml
# Start ccproxy
ccproxy start
```

**Usage:**
```bash
uv run python docs/sdk/deepseek_sdk.py
```

**Features:**
- Uses sentinel key `sk-ant-oat-ccproxy-deepseek`
- Same SDK as `anthropic_sdk.py` — just a different sentinel key
- Same-format redirect — no body transformation needed
- Demonstrates both `messages.create()` and `messages.stream()` patterns

---

### lightllm_transform.py

Demonstrates ccproxy's lightllm cross-format transformation by using the OpenAI SDK
to call Anthropic and Gemini models through the transform pipeline.

**Purpose:**
- Show how ccproxy rewrites OpenAI-format requests into provider-native format
- Demonstrate the full lightllm pipeline: ``validate_environment → get_complete_url →
  transform_request → sign_request → transform_response``
- For Gemini: show the custom ``_transform_gemini`` code path that bypasses ``BaseConfig``
- Prove the same OpenAI SDK code can reach any provider ccproxy knows about

**Prerequisites:**
```bash
# Install openai (included in ccproxy[sdk])
uv add claude-ccproxy[sdk]

# Start ccproxy
ccproxy start
```

**Usage:**
```bash
uv run python docs/sdk/lightllm_transform.py
```

**Features:**
- Uses OpenAI SDK (`openai.OpenAI`) — single client, multiple backends
- Sentinel keys: `sk-ant-oat-ccproxy-anthropic` and `sk-ant-oat-ccproxy-gemini`
- ccproxy auto-detects OpenAI format from `/v1/chat/completions` path
- Format mismatch triggers transform automatically (no config needed)
- ``SseTransformer`` handles cross-provider streaming: parses provider-native SSE
  chunks, transforms each via ``ModelResponseIterator``, re-serializes as OpenAI SSE
- Demonstrates both non-streaming and streaming for each provider direction

## Common Setup

All examples require ccproxy to be running:

```bash
# Start ccproxy (foreground — use process-compose or systemd for background)
ccproxy start

# Monitor logs (optional)
ccproxy logs -f

# Check status
ccproxy status
```

## Configuration

Examples expect ccproxy running with:
- **Proxy port**: 4000 (default)
- **OAuth credentials**: Configured in `~/.config/ccproxy/ccproxy.yaml` under `providers`
- **Model routing**: Driven by sentinel-key resolution against `providers`. Use `inspector.transforms` (`TransformOverride` entries) only for edge cases — bypassing auth for a host or forcing a specific destination for a path/model combo.

### Example ccproxy.yaml Provider Configuration

```yaml
ccproxy:
  providers:
    anthropic:
      auth:
        type: command
        command: "jq -r '.claudeAiOauth.accessToken' ~/.claude/.credentials.json"
      host: api.anthropic.com
      path: /v1/messages
      provider: anthropic
```

## Troubleshooting

If examples fail:

1. **Verify ccproxy is running**: `ccproxy status`
2. **Check provider configuration**: Verify the relevant entry under `providers` in `~/.config/ccproxy/ccproxy.yaml`
3. **Review logs**: `ccproxy logs -f` for detailed error messages
4. **Check pipeline hooks**: Ensure `forward_oauth` and `shape` are enabled in hooks configuration
5. **Verify port**: Default is 4000, ensure it's not blocked or in use

### Common Errors

- **"This credential is only authorized for use with Claude Code"**: OAuth pipeline hooks not configured. Verify `forward_oauth` and `shape` hooks are enabled, and that you have a captured shape for the provider.
- **"invalid x-api-key"**: OAuth headers not being set correctly. Check `forward_oauth` hook configuration and logs.
- **Connection refused**: ccproxy not running. Check `ccproxy status`.
- **Transform returning unexpected format**: Verify the sentinel key resolves to a provider with a different wire format. Check `ccproxy flows compare` to see the pre-transform client request and post-transform forwarded request side-by-side.

## Additional Resources

- [ccproxy Documentation](../../README.md)
- [Anthropic SDK Documentation](https://github.com/anthropics/anthropic-sdk-python)
- [OpenAI SDK Documentation](https://github.com/openai/openai-python)
- [google-genai SDK Documentation](https://github.com/googleapis/python-genai)
- [LiteLLM Documentation](https://docs.litellm.ai/)
