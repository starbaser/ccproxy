# Configuration Guide

This guide covers ccproxy's configuration system, including all configuration files and their purposes.

## Overview

ccproxy uses three main configuration files:

1. **`config.yaml`** - LiteLLM proxy configuration (models, API keys, etc.)
2. **`ccproxy.yaml`** - ccproxy-specific settings (rules, hooks, debug options)
3. **`ccproxy.py`** - Handler instantiation for LiteLLM integration

## Installation

Install configuration templates to `~/.ccproxy/`:

```bash
ccproxy install
```

### Manual Setup

If you prefer to set up manually, download the template files:

```bash
# Create the ccproxy configuration directory
mkdir -p ~/.ccproxy

# Download the callback file
curl -o ~/.ccproxy/ccproxy.py \
  https://raw.githubusercontent.com/starbased-co/ccproxy/main/src/ccproxy/templates/ccproxy.py

# Download the LiteLLM config
curl -o ~/.ccproxy/config.yaml \
  https://raw.githubusercontent.com/starbased-co/ccproxy/main/src/ccproxy/templates/config.yaml

# Download ccproxy's config
curl -o ~/.ccproxy/ccproxy.yaml \
  https://raw.githubusercontent.com/starbased-co/ccproxy/main/src/ccproxy/templates/ccproxy.yaml
```

This creates the configuration files from the built-in templates.

## Configuration Files

### `config.yaml` (LiteLLM Configuration)

This file configures the LiteLLM proxy server with model definitions and API settings.

```yaml
# LiteLLM model configuration
model_list:
  # Default model for regular use
  - model_name: default
    litellm_params:
      model: claude-sonnet-4-20250514

  # Background model for low-cost operations
  - model_name: background
    litellm_params:
      model: claude-3-5-haiku-20241022

  # Thinking model for complex reasoning
  - model_name: think
    litellm_params:
      model: claude-opus-4-20250514

  # Large context model for >60k tokens
  - model_name: token_count
    litellm_params:
      model: gemini-2.5-pro

  # Web search model for tool usage
  - model_name: web_search
    litellm_params:
      model: gemini-2.5-flash

  # Anthropic provided claude models, no `api_key` needed
  - model_name: claude-sonnet-4-20250514
    litellm_params:
      model: claude-sonnet-4-20250514
      api_base: https://api.anthropic.com

  - model_name: claude-opus-4-20250514
    litellm_params:
      model: anthropic/claude-opus-4-20250514
      api_base: https://api.anthropic.com

  - model_name: claude-3-5-haiku-20241022
    litellm_params:
      model: anthropic/claude-3-5-haiku-20241022
      api_base: https://api.anthropic.com

  # Add any other provider/model supported by LiteLLM

  - model_name: gemini-2.5-pro
    litellm_params:
      model: gemini/gemini-2.5-pro
      api_base: https://generativelanguage.googleapis.com
      api_key: os.environ/GOOGLE_API_KEY

# LiteLLM settings
litellm_settings:
  callbacks:
    - ccproxy.handler

general_settings:
  forward_client_headers_to_llm_api: true
```

Each `model_name` can be either:

- A configured LiteLLM model (e.g., `claude-sonnet-4-20250514`)
- The name of a rule configured in `ccproxy.yaml` (e.g., `default`, `background`, `think`)

Model names in `config.yaml` must correspond to rule names in `ccproxy.yaml`. When a rule matches, ccproxy routes to the model with the same `model_name`.

- **Minimum requirements for Claude Code**: For Claude Code to function properly, your `config.yaml` must include at minimum:
  - **Rule-based models**: `default`, `background`, and `think`
  - **Claude models**: `claude-sonnet-4-20250514`, `claude-3-5-haiku-20241022`, and `claude-opus-4-20250514` (all with `api_base: https://api.anthropic.com`)

See the [LiteLLM documentation](https://docs.litellm.ai/docs/proxy/configs) for more information.

### `ccproxy.yaml` (ccproxy Configuration)

This file configures ccproxy-specific behavior including routing rules and hooks.

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

  # Processing hooks (executed in order)
  hooks:
    - ccproxy.hooks.rule_evaluator # Evaluates rules
    - ccproxy.hooks.model_router # Routes to models
    - ccproxy.hooks.forward_oauth # Forwards OAuth tokens

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
        - model_name: claude-3-5-haiku-20241022

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
- **`ccproxy.hooks`**: A list of hooks that are executed in series during the `async_pre_call_hook`
- **`ccproxy.rules`**: Request routing rules (evaluated in order)

#### Built-in Rules

1. **TokenCountRule**: Routes based on token count threshold
2. **MatchModelRule**: Routes specific model requests
3. **ThinkingRule**: Routes requests with thinking fields
4. **MatchToolRule**: Routes based on tool usage

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

### ccproxy.py (Handler Integration)

This file instantiates the ccproxy handler for LiteLLM integration.

```python
from ccproxy.handler import CCProxyHandler

# Create the instance that LiteLLM will use
handler = CCProxyHandler()
```

This file is referenced in `config.yaml` under `litellm_settings.callbacks`.

## Request Routing Flow

1. **Request Received**: LiteLLM proxy receives request
2. **Hook Processing**: ccproxy hooks process the request in order:
   - `rule_evaluator`: Evaluates rules to determine routing
   - `model_router`: Maps rule name to model configuration
   - `forward_oauth`: Handles OAuth token forwarding
3. **Model Selection**: Request routed to appropriate model
4. **Response**: Response returned through LiteLLM proxy

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

ccproxy provides a hook system that allows you to extend and customize its behavior beyond the built-in rule routing system. Hooks are Python functions that can intercept and modify requests, implement custom logging, filtering, or integrate with external systems. The rule routing system is just itself a custom hook.

Only the `forward_oauth` is required for Claude Code to function properly.

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
    - ccproxy.hooks.forward_oauth # Required for Claude Code
```

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
      - model_name: claude-3-5-haiku-20241022

  - name: reasoning
    rule: ccproxy.rules.MatchModelRule
    params:
      - model_name: claude-opus-4-20250514
```
