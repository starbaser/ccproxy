# Configuration Guide

This guide covers `ccproxy`'s configuration system, including all configuration files and their purposes.

## Overview

`ccproxy` uses two main configuration files:

1. **`config.yaml`** - LiteLLM proxy configuration (models, API keys, etc.)
2. **`ccproxy.yaml`** - ccproxy-specific settings (rules, hooks, handler, debug options)

Additionally, `ccproxy.py` is automatically generated when you start the proxy based on the `handler` configuration in `ccproxy.yaml`.

## Installation

### Prerequisites

ccproxy requires LiteLLM to be installed in the same environment. This is handled automatically when using the recommended installation method:

```bash
# Install from PyPI
uv tool install claude-ccproxy --with 'litellm[proxy]'

# Or from GitHub (latest)
uv tool install git+https://github.com/starbased-co/ccproxy.git --with 'litellm[proxy]'
```

### Install Configuration Files

```bash
ccproxy install
```

This creates:
- `~/.ccproxy/ccproxy.yaml` - ccproxy configuration (rules, hooks, handler)
- `~/.ccproxy/config.yaml` - LiteLLM proxy configuration (models, API keys)

### Auto-Generated Files

When you start the proxy, ccproxy automatically generates:
- `~/.ccproxy/ccproxy.py` - Handler file that LiteLLM imports

**Do not edit `ccproxy.py` manually** - it's regenerated on every `ccproxy start` based on your `handler` configuration.

## Configuration Files

### `config.yaml` (LiteLLM Configuration)

This file configures the LiteLLM proxy server with model definitions and API settings.

```yaml
# LiteLLM model configuration
model_list:
  # Default model for regular use
  - model_name: default
    litellm_params:
      model: claude-sonnet-4-5-20250929

  # Background model for low-cost operations
  - model_name: background
    litellm_params:
      model: claude-haiku-4-5-20251001

  # Thinking model for complex reasoning
  - model_name: think
    litellm_params:
      model: claude-opus-4-5-20251101

  # Anthropic provided claude models, no `api_key` needed
  - model_name: claude-sonnet-4-5-20250929
    litellm_params:
      model: anthropic/claude-sonnet-4-5-20250929
      api_base: https://api.anthropic.com

  - model_name: claude-opus-4-5-20251101
    litellm_params:
      model: anthropic/claude-opus-4-5-20251101
      api_base: https://api.anthropic.com

  - model_name: claude-haiku-4-5-20251001
    litellm_params:
      model: anthropic/claude-haiku-4-5-20251001
      api_base: https://api.anthropic.com

# LiteLLM settings
litellm_settings:
  callbacks:
    - ccproxy.handler

general_settings:
  forward_client_headers_to_llm_api: true
```

Each `model_name` can be either:

- A configured LiteLLM model (e.g., `claude-sonnet-4-5-20250929`)
- The name of a rule configured in `ccproxy.yaml` (e.g., `default`, `background`, `think`)

Model names in `config.yaml` must correspond to rule names in `ccproxy.yaml`. When a rule matches, `ccproxy` routes to the model with the same `model_name`.

- **Minimum requirements for Claude Code**: For Claude Code to function properly, your `config.yaml` must include at minimum:
  - **Rule-based models**: `default`, `background`, and `think`
  - **Claude models**: `claude-sonnet-4-5-20250929`, `claude-haiku-4-5-20251001`, and `claude-opus-4-5-20251101` (all with `api_base: https://api.anthropic.com`)

See the [LiteLLM documentation](https://docs.litellm.ai/docs/proxy/configs) for more information.

### `ccproxy.yaml` (ccproxy Configuration)

This file configures `ccproxy`-specific behavior including routing rules and hooks.

```yaml
# LiteLLM proxy settings
litellm:
  host: 127.0.0.1
  port: 4000
  num_workers: 4
  debug: true
  detailed_debug: true

# ccproxy-specific configuration
ccproxy:
  debug: true

  # Handler class for LiteLLM callbacks (auto-generates ccproxy.py)
  # Format: "module.path:ClassName" or just "module.path" (defaults to CCProxyHandler)
  handler: "ccproxy.handler:CCProxyHandler"

  # Optional: Shell command to load oauth token on startup (for standalone mode)
  credentials: "jq -r '.claudeAiOauth.accessToken' ~/.claude/.credentials.json"

  # Processing hooks (executed in order)
  hooks:
    - ccproxy.hooks.rule_evaluator # Evaluates rules
    - ccproxy.hooks.model_router # Routes to models

    # Choose ONE:
    - ccproxy.hooks.forward_oauth # subscription account
    # - ccproxy.hooks.forward_apikey # api key

  # Routing rules (evaluated in order)
  rules:
    # Route high-token requests to large context model
    - name: token_count
      rule: ccproxy.rules.TokenCountRule
      params:
        - threshold: 60000

    # Route haiku model requests to background
    - name: background
      rule: ccproxy.rules.MatchModelRule
      params:
        - model_name: claude-haiku-4-5-20251001

    # Route thinking requests to reasoning model
    - name: think
      rule: ccproxy.rules.ThinkingRule

    # Route web search tool usage
    - name: web_search
      rule: ccproxy.rules.MatchToolRule
      params:
        - tool_name: WebSearch
```

- **`litellm`**: LiteLLM proxy server process (See `litellm --help`)
- **`ccproxy.credentials`**: Optional shell command to load credentials at startup for use as a standalone LiteLLM server
- **`ccproxy.hooks`**: A list of hooks that are executed in series during the `async_pre_call_hook`
- **`ccproxy.rules`**: Request routing rules (evaluated in order)

#### Built-in Rules

1. **TokenCountRule**: Routes based on token count threshold
2. **MatchModelRule**: Routes specific model requests
3. **ThinkingRule**: Routes requests with thinking fields
4. **MatchToolRule**: Routes based on tool usage

#### Built-in Hooks

1. **rule_evaluator**: Evaluates rules against the request to determine routing
2. **model_router**: Maps rule names to model configurations
3. **extract_session_id**: Extracts session_id from Claude Code's user_id for LangFuse session tracking
4. **capture_headers**: Captures HTTP headers with sensitive value redaction (supports `headers` param)
5. **forward_oauth**: Forwards OAuth tokens to Anthropic API (for subscription accounts with credentials fallback)
6. **forward_apikey**: Forwards x-api-key headers from incoming requests (for API key authentication)
7. **add_beta_headers**: Adds required `anthropic-beta` headers for Claude Code OAuth tokens
8. **inject_claude_code_identity**: Injects required system message prefix for Anthropic OAuth authentication

**Note**: Use either `forward_oauth` (subscription account) OR `forward_apikey` (API key), depending on your Claude Code authentication method.

#### Rule Parameters

Rules accept parameters in various formats:

```yaml
# Single positional parameter
params:
  - threshold: 60000

# Multiple parameters
params:
  - param1: value1
    param2: value2

# Mixed parameters
params:
  - "positional_value"
  - keyword: "keyword_value"
```

### Statusline Configuration

The `statusline` section configures the [ccstatusline](https://github.com/sirmalloc/ccstatusline) widget output. Uses Starship-style format strings with variable placeholders.

```yaml
ccproxy:
  statusline:
    format: "⸢$status⸥"    # Template with $status and $symbol variables
    symbol: ""             # Symbol/icon prefix (available as $symbol)
    on: "ccproxy: ON"      # Status text when proxy is active
    off: "ccproxy: OFF"    # Status text when proxy is inactive
    disabled: false        # Disable statusline output entirely
```

#### Format String Variables

| Variable | Description |
|----------|-------------|
| `$status` | Replaced with `on` or `off` value based on proxy state |
| `$symbol` | Replaced with `symbol` value |

#### Examples

**Default (Unicode brackets):**
```yaml
statusline:
  format: "⸢$status⸥"
  on: "ccproxy: ON"
  off: "ccproxy: OFF"
```
Output: `⸢ccproxy: ON⸥` or `⸢ccproxy: OFF⸥`

**With symbol:**
```yaml
statusline:
  format: "$symbol $status"
  symbol: ""
  on: "active"
  off: "inactive"
```
Output: ` active` or ` inactive`

**Emoji only:**
```yaml
statusline:
  format: "$status"
  on: "🟢"
  off: "🔴"
```
Output: `🟢` or `🔴`

**Hide when inactive:**
```yaml
statusline:
  format: "$symbol"
  symbol: ""
  on: "active"
  off: ""          # Empty = no output when inactive
```

**Disabled:**
```yaml
statusline:
  disabled: true
```

#### Installation

```bash
ccproxy statusline install [--force] [--use-bun]
```

This configures Claude Code's `statusLine` hook and adds a ccproxy widget to ccstatusline.

### ccproxy.py (Auto-Generated Handler)

**This file is auto-generated** by `ccproxy start` and should not be edited manually.

The handler file imports and instantiates the configured handler class for LiteLLM callbacks. The handler class is specified in `ccproxy.yaml` using the `handler` configuration field.

**Configuration:**
```yaml
ccproxy:
  handler: "ccproxy.handler:CCProxyHandler"  # module_path:ClassName
```

**Generated structure:**
```python
# Auto-generated - DO NOT EDIT
from ccproxy.handler import CCProxyHandler
handler = CCProxyHandler()
```

The file is referenced in `config.yaml` under `litellm_settings.callbacks` as `ccproxy.handler`.

**Custom Handlers:**

To use a custom handler class, update `ccproxy.yaml`:
```yaml
ccproxy:
  handler: "mypackage.custom:MyHandler"
```

Then run `ccproxy start` to regenerate the handler file with your custom handler.

## Request Routing Flow

1. **Request Received**: LiteLLM proxy receives request
2. **Hook Processing**: `ccproxy` hooks process the request in order:
   - `rule_evaluator`: Evaluates rules to determine routing
   - `model_router`: Maps rule name to model configuration
   - `forward_oauth`: Handles OAuth token forwarding
3. **Model Selection**: Request routed to appropriate model
4. **Response**: Response returned through LiteLLM proxy

## Credentials Management (OAuth Only)

The `credentials` field in `ccproxy.yaml` allows you to load OAuth tokens via shell command at startup. This is **only used with `forward_oauth` hook** for Claude Code subscription accounts.

**Note**: If using Claude Code with an Anthropic API key, use `forward_apikey` hook instead (no credentials field needed).

### Configuration

```yaml
ccproxy:
  credentials: "jq -r '.claudeAiOauth.accessToken' ~/.claude/.credentials.json"
```

### Behavior

- **Execution**: Shell command runs once during config initialization
- **Caching**: Result is cached for the lifetime of the proxy process
- **Validation**: Raises `RuntimeError` if command fails (fail-fast)
- **Usage**: OAuth token is used as fallback by `forward_oauth` hook

### Common Use Cases

**Claude Code with subscription account (OAuth):**

```yaml
credentials: "jq -r '.claudeAiOauth.accessToken' ~/.claude/.credentials.json"
hooks:
  - ccproxy.hooks.forward_oauth # Use forward_oauth for OAuth tokens
```

**Loading from custom script:**

```yaml
credentials: "~/bin/get-auth-token.sh"
```

### Hook Integration

The `credentials` field is used by the `forward_oauth` hook as a fallback when:

1. No authorization header exists in the incoming request
2. The request is targeting an Anthropic API endpoint
3. Credentials were successfully loaded at startup

This provides seamless OAuth token forwarding for Claude Code subscription accounts.

## Custom Rules

Create custom routing rules by implementing the `ClassificationRule` interface:

```python
from typing import Any
from ccproxy.rules import ClassificationRule
from ccproxy.config import CCProxyConfig

class CustomRule(ClassificationRule):
    def __init__(self, custom_param: str) -> None:
        self.custom_param = custom_param

    def evaluate(self, request: dict[str, Any], config: CCProxyConfig) -> bool:
        # Custom routing logic
        return True  # Return True to use this rule's model
```

Add to `ccproxy.yaml`:

```yaml
ccproxy:
  rules:
    - name: custom_model # Must match model_name in config.yaml
      rule: myproject.CustomRule # Python import path
      params:
        - custom_param: "value"
```

## Custom Hooks

`ccproxy` provides a hook system that allows you to extend and customize its behavior beyond the built-in rule routing system. Hooks are Python functions that can intercept and modify requests, implement custom logging, filtering, or integrate with external systems. The rule routing system is just itself a custom hook.

**Required for Claude Code**: Either `forward_oauth` (subscription account) OR `forward_apikey` (API key) is required, depending on your authentication method.

### Built-in Hook Details

#### forward_oauth

Forwards OAuth tokens to Anthropic API requests

**Use when:** Claude Code is configured with a subscription account

**Features:**

- Forwards existing authorization headers
- Falls back to `credentials` field if no header present
- Only activates for Anthropic API endpoints
- Automatically adds "Bearer" prefix if needed

**Configuration:**

```yaml
ccproxy:
  credentials: "jq -r '.claudeAiOauth.accessToken' ~/.claude/.credentials.json"
  hooks:
    - ccproxy.hooks.forward_oauth
```

#### forward_apikey

Forwards x-api-key headers from incoming requests to proxied requests.

**Use when:** Claude Code is configured with an Anthropic API key (not a subscription account)

**Features:**

- Forwards x-api-key header from request to proxied request
- No credentials fallback mechanism
- Simple header passthrough

**Configuration:**

```yaml
ccproxy:
  hooks:
    - ccproxy.hooks.forward_apikey
```

**Important**: Choose ONE of these hooks based on your Claude Code authentication method:

- **Subscription account** → Use `forward_oauth`
- **API key** → Use `forward_apikey`

### Example: Request Logging Hook

```python
# ~/.ccproxy/my_hooks.py
import logging
from typing import Any

logger = logging.getLogger(__name__)

def request_logger(data: dict[str, Any], user_api_key_dict: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Log detailed request information."""
    metadata = data.get("metadata", {})
    logger.info(f"Processing request for model: {data.get('model')}")
    return data
```

Add to `ccproxy.yaml`:

```yaml
ccproxy:
  hooks:
    - my_hooks.request_logger # Your custom hook
    - ccproxy.hooks.forward_oauth # For subscription account
    # - ccproxy.hooks.forward_apikey # Or this, for API key
```

### Hook Parameters

Hooks can accept parameters via the `hook:` + `params:` format:

```yaml
ccproxy:
  hooks:
    # Simple form (no params)
    - ccproxy.hooks.rule_evaluator

    # Dict form with params
    - hook: ccproxy.hooks.capture_headers
      params:
        headers: [user-agent, x-request-id, content-type]
```

Parameters are passed to the hook function via `**kwargs`:

```python
def my_hook(data: dict[str, Any], user_api_key_dict: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    # Access params from kwargs
    threshold = kwargs.get("threshold", 1000)
    return data
```

### Claude Code OAuth Support

For Claude Max subscription accounts using OAuth tokens, add these hooks to enable full Claude Code functionality:

```yaml
ccproxy:
  hooks:
    - ccproxy.hooks.rule_evaluator
    - ccproxy.hooks.model_router
    - ccproxy.hooks.forward_oauth
    - ccproxy.hooks.add_beta_headers           # Required for OAuth
    - ccproxy.hooks.inject_claude_code_identity # Required for OAuth
```

#### add_beta_headers

Adds `anthropic-beta` headers required for Claude Code feature access:

- `oauth-2025-04-20` - OAuth Bearer token authentication
- `claude-code-20250219` - Claude Code client identification
- `interleaved-thinking-2025-05-14` - Extended thinking support
- `fine-grained-tool-streaming-2025-05-14` - Tool streaming

#### inject_claude_code_identity

Injects required system message prefix for Anthropic OAuth tokens. Anthropic validates that OAuth tokens are used only with Claude Code by checking the system message starts with "You are Claude Code".

This hook automatically prepends the required prefix to requests using OAuth Bearer tokens (`sk-ant-oat-*`).

## Debugging

Enable debug output in `ccproxy.yaml`:

```yaml
litellm:
  debug: true
  detailed_debug: true

ccproxy:
  debug: true
```

This provides detailed logging for request processing and routing decisions.

## Common Patterns

### Token-Based Routing

Route expensive requests to cost-effective models:

```yaml
rules:
  - name: large_context
    rule: ccproxy.rules.TokenCountRule
    params:
      - threshold: 50000

  - name: default
    rule: ccproxy.rules.DefaultRule
```

### Tool-Based Routing

Route tool usage to specialized models:

```yaml
rules:
  - name: web_search
    rule: ccproxy.rules.MatchToolRule
    params:
      - tool_name: WebSearch

  - name: code_execution
    rule: ccproxy.rules.MatchToolRule
    params:
      - tool_name: CodeExecution
```

### Model-Specific Routing

Route specific model requests:

```yaml
rules:
  - name: background
    rule: ccproxy.rules.MatchModelRule
    params:
      - model_name: claude-haiku-4-5-20251001

  - name: reasoning
    rule: ccproxy.rules.MatchModelRule
    params:
      - model_name: claude-opus-4-5-20251101
```
