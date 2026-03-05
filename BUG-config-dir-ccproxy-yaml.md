# Bug: `--config-dir` flag only affects `config.yaml`, not `ccproxy.yaml`

## Summary

When using `ccproxy --config-dir ./project/.ccproxy start`, the `config.yaml` is correctly loaded from the specified directory, but `ccproxy.yaml` is always loaded from the global `~/.ccproxy/` fallback. The `CCPROXY_CONFIG_DIR` environment variable is also ignored entirely.

## Expected Behavior

Both `config.yaml` and `ccproxy.yaml` should be resolved from the directory specified by `--config-dir` (or `CCPROXY_CONFIG_DIR` env var).

## Actual Behavior

- `config.yaml` → loaded from `--config-dir` path (correct)
- `ccproxy.yaml` → always loaded from `~/.ccproxy/` (incorrect)
- `CCPROXY_CONFIG_DIR` env var → ignored by both `start` and `status` subcommands

## Reproduction

```bash
# Create a per-project config directory
mkdir -p /tmp/test-ccproxy
cat > /tmp/test-ccproxy/config.yaml <<'EOF'
model_list:
  - model_name: default
    litellm_params:
      model: anthropic/claude-sonnet-4-6
      api_base: https://api.anthropic.com
litellm_settings:
  callbacks: [ccproxy.handler, langfuse]
  success_callback: [langfuse]
general_settings:
  forward_client_headers_to_llm_api: true
EOF

cat > /tmp/test-ccproxy/ccproxy.yaml <<'EOF'
ccproxy:
  handler: "ccproxy.handler:CCProxyHandler"
  oat_sources:
    anthropic:
      file: "~/.opnix/secrets/claude-code-oauth-token"
      destinations:
        - "api.anthropic.com"
  hooks:
    - ccproxy.hooks.rule_evaluator
    - ccproxy.hooks.model_router
    - ccproxy.hooks.extract_session_id
    - ccproxy.hooks.forward_oauth
    - ccproxy.hooks.add_beta_headers
    - ccproxy.hooks.inject_claude_code_identity
  default_model_passthrough: true
  rules: []
litellm:
  host: 127.0.0.1
  port: 4010
EOF

# Test 1: --config-dir flag
ccproxy --config-dir /tmp/test-ccproxy status
# Shows config.yaml from /tmp/test-ccproxy (correct)
# Shows ccproxy.yaml from ~/.ccproxy/ (wrong — should be /tmp/test-ccproxy)

# Test 2: CCPROXY_CONFIG_DIR env var
CCPROXY_CONFIG_DIR=/tmp/test-ccproxy ccproxy status
# Shows both from ~/.ccproxy/ (completely ignored)

# Test 3: start with --config-dir
ccproxy --config-dir /tmp/test-ccproxy start
# Loads hooks from global ~/.ccproxy/ccproxy.yaml (e.g. capture_headers present even though not in project ccproxy.yaml)
# Uses port from global ccproxy.yaml (4000) instead of project ccproxy.yaml (4010)
# BUT loads model_list from project config.yaml (correct — only config.yaml is redirected)
```

## Evidence

Hook list from `start` output shows hooks only present in global `~/.ccproxy/ccproxy.yaml`:
```
Pipeline initialized with 9 hooks: capture_headers → extract_session_id → forward_apikey → ...
```

The project `ccproxy.yaml` only defines 6 hooks (no `capture_headers`, no `forward_apikey`, no `inject_mcp_notifications`).

Port binds to 4000 (global `litellm.port`) instead of 4010 (project `litellm.port`).

## Impact

Per-project ccproxy instances cannot use different hooks, ports, or OAuth sources. The per-project setup documented in the skill reference (`reference/per-project-setup.md`) is broken for `ccproxy.yaml` settings — only `config.yaml` (model definitions, callbacks) works correctly.

## Context

Discovered while setting up a per-project ccproxy instance for the kitstore project with:
- Dedicated port (4010) to avoid conflict with global instance (4000)
- Subset of hooks (no capture_headers, no forward_apikey)
- Project-specific Langfuse keys via `.env`
- devenv process management via `devenv up --detached`
