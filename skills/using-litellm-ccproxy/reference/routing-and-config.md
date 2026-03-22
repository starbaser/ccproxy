# Model Routing & Configuration

## Contents

- [How routing works](#how-routing-works)
- [config.yaml model definitions](#configyaml-model-definitions)
- [ccproxy.yaml hook pipeline](#ccproxyyaml-hook-pipeline)
- [OAuth token management](#oauth-token-management)
- [default_model_passthrough](#default_model_passthrough)
- [Rule system](#rule-system)

---

## How routing works

Request flow through the hook pipeline:

```
Client request (model: "claude-sonnet-4-5-20250929")
  │
  ▼
rule_evaluator
  Evaluates rules in order. First match wins.
  Sets metadata: ccproxy_alias_model, ccproxy_model_name
  │
  ▼
model_router
  Looks up ccproxy_model_name in config.yaml model_list.
  If passthrough + "default" label: keeps original model.
  Sets metadata: ccproxy_litellm_model, ccproxy_model_config
  │
  ▼
extract_session_id         [optional — for Langfuse/observability]
  Reads body.metadata.user_id (Claude Code format) or body.metadata.session_id.
  Sets metadata["session_id"] for Langfuse session grouping.
  │
  ▼
capture_headers
  Records configured client headers for tracing.
  │
  ▼
forward_oauth
  Detects provider from model_config (api_base, model name).
  Substitutes sentinel key with real OAuth token.
  Falls back to cached token if no auth header.
  Sets: Authorization header, clears x-api-key
  │
  ▼
add_beta_headers
  Only for Anthropic provider (detected same way as forward_oauth).
  Skips if model has its own api_key.
  Sets: anthropic-beta, anthropic-version headers
  │
  ▼
inject_claude_code_identity
  Only for api.anthropic.com + OAuth token detected.
  Prepends system message with required prefix.
  │
  ▼
inject_mcp_notifications   [optional — requires extract_session_id]
  Guard: only runs if session has buffered events.
  Drains NotificationBuffer for session_id.
  Inserts tool_use/tool_result pairs before final user message.
  │
  ▼
LiteLLM sends to provider API
```

---

## config.yaml model definitions

Models are defined in `~/.ccproxy/config.yaml`. Each entry has a `model_name` (alias) and `litellm_params` (how to reach the model).

### Minimum for Claude Code with OAuth

```yaml
model_list:
  # Rule aliases (routing targets)
  - model_name: default
    litellm_params:
      model: claude-sonnet-4-5-20250929

  - model_name: background
    litellm_params:
      model: claude-haiku-4-5-20251001

  - model_name: think
    litellm_params:
      model: claude-opus-4-5-20251101

  # Actual model deployments (no api_key = uses OAuth from pipeline)
  - model_name: claude-sonnet-4-5-20250929
    litellm_params:
      model: anthropic/claude-sonnet-4-5-20250929
      api_base: https://api.anthropic.com

  - model_name: claude-haiku-4-5-20251001
    litellm_params:
      model: anthropic/claude-haiku-4-5-20251001
      api_base: https://api.anthropic.com

  - model_name: claude-opus-4-5-20251101
    litellm_params:
      model: anthropic/claude-opus-4-5-20251101
      api_base: https://api.anthropic.com

litellm_settings:
  callbacks:
    - ccproxy.handler

general_settings:
  forward_client_headers_to_llm_api: true
```

Key points:
- **Rule aliases** (`default`, `background`, `think`) point to model names, not provider models
- **Deployments** have `api_base` and use `anthropic/` prefix in model field
- Omitting `api_key` from deployments means OAuth handles auth via pipeline hooks
- `forward_client_headers_to_llm_api: true` is required for hooks to receive client headers

### Adding models with their own API keys

```yaml
  # Model with its own API key (bypasses OAuth pipeline)
  - model_name: gpt-4o
    litellm_params:
      model: openai/gpt-4o
      api_key: os.environ/OPENAI_API_KEY

  # ZAI model with dedicated key
  - model_name: glm-4.7
    litellm_params:
      model: anthropic/glm-4.7
      api_base: https://api.z.ai/api/anthropic
      api_key: os.environ/ZAI_API_KEY
```

Models with `api_key` set:
- `forward_oauth` skips them (won't override configured key)
- `add_beta_headers` skips them (beta headers are for OAuth only)

---

## ccproxy.yaml hook pipeline

### Full OAuth pipeline

```yaml
ccproxy:
  debug: true
  handler: "ccproxy.handler:CCProxyHandler"

  oauth_ttl: 28800           # 8 hours
  oauth_refresh_buffer: 0.1  # Refresh at 90% of TTL

  oat_sources:
    anthropic: "jq -r '.claudeAiOauth.accessToken' ~/.claude/.credentials.json"

  hooks:
    - ccproxy.hooks.rule_evaluator
    - ccproxy.hooks.model_router
    - ccproxy.hooks.extract_session_id
    - ccproxy.hooks.capture_headers
    - ccproxy.hooks.forward_oauth
    - ccproxy.hooks.add_beta_headers
    - ccproxy.hooks.inject_claude_code_identity
    - ccproxy.hooks.inject_mcp_notifications

  default_model_passthrough: true
  rules: []
```

### API key pipeline (no OAuth)

```yaml
ccproxy:
  hooks:
    - ccproxy.hooks.rule_evaluator
    - ccproxy.hooks.model_router
    - ccproxy.hooks.forward_apikey
```

Choose ONE: `forward_oauth` (subscription) OR `forward_apikey` (API key).

### Hook parameters

Hooks accept params via dict form:

```yaml
hooks:
  # Simple (no params)
  - ccproxy.hooks.rule_evaluator

  # With params
  - hook: ccproxy.hooks.capture_headers
    params:
      headers: [user-agent, x-request-id, content-type]
```

### Hook dependency system

Hooks declare data dependencies via the `@hook` decorator. The `HookDAG` computes execution order via topological sort, guaranteeing a hook that reads key `X` runs after any hook that writes `X`.

```python
@hook(reads=["ccproxy_litellm_model", "authorization"], writes=["provider_specific_header"])
def forward_oauth(ctx, params): ...

@hook(reads=["proxy_server_request"], writes=["session_id", "trace_metadata"])
def extract_session_id(ctx, params): ...

@hook(reads=["messages", "session_id"], writes=["messages"])
def inject_mcp_notifications(ctx, params): ...
```

Dependency resolution:
- `inject_mcp_notifications` reads `session_id` → runs after `extract_session_id`
- `forward_oauth` reads `ccproxy_litellm_model` → runs after `model_router`
- `inject_claude_code_identity` reads `authorization` → runs after `forward_oauth`

YAML hook order still matters for readability but the DAG enforces correct execution order regardless.

---

## OAuth token management

### oat_sources configuration

**Simple form** (command string):
```yaml
oat_sources:
  anthropic: "jq -r '.claudeAiOauth.accessToken' ~/.claude/.credentials.json"
```

**Extended form** (with user_agent and destinations):
```yaml
oat_sources:
  anthropic:
    command: "jq -r '.claudeAiOauth.accessToken' ~/.claude/.credentials.json"
    user_agent: "ClaudeCode/1.0"
    destinations: ["api.anthropic.com"]

  zai:
    command: "jq -r '.accessToken' ~/.zai/credentials.json"
    user_agent: "MyApp/1.0"
    destinations: ["api.z.ai", "z.ai"]
```

Fields:
- `command` (required) — shell command that outputs the token
- `user_agent` (optional) — custom User-Agent header for this provider
- `destinations` (optional) — URL patterns for auto-matching api_base to provider

### Token refresh

Two automatic refresh triggers:
1. **TTL-based**: Background task every 30 minutes, refreshes at `oauth_ttl * (1 - oauth_refresh_buffer)`
2. **401-triggered**: Immediate refresh on authentication error, retries the failed request once

Default: 8h TTL, 10% buffer = refresh at ~7.2 hours.

### Destination matching

When `forward_oauth` and `add_beta_headers` need to determine which provider a request targets, they use this priority:

1. `custom_llm_provider` in model config (explicit)
2. `destinations` patterns in `oat_sources` (checks if api_base contains pattern)
3. LiteLLM's `get_llm_provider()` (model + api_base analysis)
4. Model name fallback ("claude" → anthropic, "gpt" → openai, "gemini" → gemini)

---

## default_model_passthrough

When `true` (default), requests that don't match any rule keep their original model name unchanged. The model must exist as a `model_name` in config.yaml.

When `false`, unmatched requests are routed to the `default` model_name in config.yaml.

```yaml
ccproxy:
  default_model_passthrough: true  # Keep original model if no rule matches
```

---

## Rule system

Rules are evaluated in order. First match sets the routing label.

### Built-in rules

| Rule | Params | Matches when |
|---|---|---|
| `ThinkingRule` | none | Request has `thinking` field |
| `MatchModelRule` | `model_name: str` | Request model contains the substring |
| `TokenCountRule` | `threshold: int` | Token count exceeds threshold |
| `MatchToolRule` | `tool_name: str` | Request tools contain the named tool |

### Example rules config

```yaml
rules:
  - name: think
    rule: ccproxy.rules.ThinkingRule

  - name: background
    rule: ccproxy.rules.MatchModelRule
    params:
      - model_name: haiku

  - name: large_context
    rule: ccproxy.rules.TokenCountRule
    params:
      - threshold: 60000

  - name: web_search
    rule: ccproxy.rules.MatchToolRule
    params:
      - tool_name: WebSearch
```

Each rule `name` must correspond to a `model_name` in config.yaml. If a request matches `think`, the model is rewritten to whatever `model_name: think` points to.

---

### MCP notification endpoint

ccproxy exposes `POST /mcp/notify` for ingesting terminal events from mcptty:

```json
{
  "task_id": "task-abc",
  "session_id": "session-uuid",
  "claude_session_id": "",
  "event": {"type": "terminal_change", "content": "..."}
}
```

Events are stored in `NotificationBuffer` keyed by `task_id`, up to 50 events per task with a 10-minute TTL. The `inject_mcp_notifications` hook drains the buffer for the current session on each request, converting events to synthetic `tool_use`/`tool_result` pairs inserted before the final user message.

The hook:
1. Checks guard conditions (session_id present, buffer has events)
2. Drains all events for the session from the buffer
3. Generates `tool_use` blocks with `name: "tasks_get"` and unique IDs (`toolu_notify_{hex}`)
4. Pairs each with a `tool_result` containing the event JSON
5. Inserts all pairs before `messages[-1]` (the final user message)
