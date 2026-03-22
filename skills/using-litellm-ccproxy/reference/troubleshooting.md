# Troubleshooting Guide

## Contents

- [Diagnostic checklist](#diagnostic-checklist)
- [Error: "This credential is only authorized for use with Claude Code"](#error-this-credential-is-only-authorized-for-use-with-claude-code)
- [Error: "OAuth is not supported" or "invalid x-api-key"](#error-oauth-is-not-supported-or-invalid-x-api-key)
- [Error: 401 Unauthorized / token errors](#error-401-unauthorized--token-errors)
- [Error: Connection refused / timeout](#error-connection-refused--timeout)
- [General diagnostics](#general-diagnostics)
- [LiteLLM internal behaviors](#litellm-internal-behaviors)
- [Provider-specific notes](#provider-specific-notes)

---

## Diagnostic checklist

Run these first for any authentication issue:

```bash
# 1. Is ccproxy running?
ccproxy status

# 2. Stream logs while reproducing the issue
ccproxy logs -f

# 3. Verify hook pipeline in ccproxy.yaml
grep -A 20 'hooks:' ~/.ccproxy/ccproxy.yaml

# 4. Verify oat_sources configured
grep -A 5 'oat_sources:' ~/.ccproxy/ccproxy.yaml

# 5. Test OAuth command manually
jq -r '.claudeAiOauth.accessToken' ~/.claude/.credentials.json
# Should output a token starting with "sk-ant-oat"
```

---

## Error: "This credential is only authorized for use with Claude Code"

**Cause**: Anthropic's API validates that OAuth tokens (from Claude Max/Team/Enterprise subscriptions) are only used by Claude Code. It checks that the system message starts with "You are Claude Code, Anthropic's official CLI for Claude."

**Resolution**:

1. Verify `inject_claude_code_identity` hook is enabled in `ccproxy.yaml`:
   ```yaml
   hooks:
     # ... other hooks ...
     - ccproxy.hooks.inject_claude_code_identity
   ```

2. Verify hook ordering — `inject_claude_code_identity` must come AFTER `forward_oauth` (the hook checks for OAuth token presence before injecting):
   ```yaml
   hooks:
     - ccproxy.hooks.rule_evaluator
     - ccproxy.hooks.model_router
     - ccproxy.hooks.forward_oauth              # Must be before identity injection
     - ccproxy.hooks.add_beta_headers
     - ccproxy.hooks.inject_claude_code_identity # Checks for "Bearer sk-ant-oat" in auth header
   ```

3. Check logs for the injection event:
   ```bash
   ccproxy logs -f
   # Look for: "Injected Claude Code identity for OAuth authentication"
   # If missing: hook is not triggering — check auth_header detection
   ```

4. The hook only injects for requests going to `api.anthropic.com`. If using a non-Anthropic api_base, the identity injection is skipped (ZAI and other compatible APIs don't require it).

5. If using a custom system message, verify the hook prepends rather than replaces. The hook behavior:
   - String system: prepends prefix with `\n\n` separator
   - List system: inserts `{"type": "text", "text": "You are Claude Code..."}` at index 0
   - No system: sets system to just the prefix string

---

## Error: "OAuth is not supported" or "invalid x-api-key"

**Cause**: Anthropic's API requires the `oauth-2025-04-20` beta header to accept OAuth Bearer tokens. Without it, the API sees an OAuth token where it expects an API key and rejects it.

**Resolution**:

1. Verify `add_beta_headers` hook is enabled:
   ```yaml
   hooks:
     - ccproxy.hooks.add_beta_headers
   ```

2. Verify it runs AFTER `model_router` (needs routing metadata to detect Anthropic provider):
   ```yaml
   hooks:
     - ccproxy.hooks.rule_evaluator
     - ccproxy.hooks.model_router       # Sets ccproxy_litellm_model and ccproxy_model_config
     - ccproxy.hooks.forward_oauth
     - ccproxy.hooks.add_beta_headers   # Reads ccproxy_litellm_model to detect provider
     - ccproxy.hooks.inject_claude_code_identity
   ```

3. Check logs for the beta headers event:
   ```bash
   ccproxy logs -f
   # Look for: "Added anthropic-beta headers for Claude Code impersonation"
   # If missing: provider detection failed — check model config has api_base
   ```

4. The hook skips beta headers if the model has its own `api_key` in config.yaml. Beta headers are only for OAuth, not for API key auth. Check:
   ```yaml
   # This model gets beta headers (no api_key — uses OAuth):
   - model_name: claude-sonnet-4-5-20250929
     litellm_params:
       model: anthropic/claude-sonnet-4-5-20250929
       api_base: https://api.anthropic.com

   # This model does NOT get beta headers (has its own api_key):
   - model_name: claude-sonnet-4-5-20250929
     litellm_params:
       model: anthropic/claude-sonnet-4-5-20250929
       api_key: sk-ant-api03-...
   ```

5. The hook merges with existing `anthropic-beta` headers from the original request. It does not clobber client-provided betas.

---

## Error: 401 Unauthorized / token errors

Multiple causes — work through in order:

### Token expired

OAuth tokens from `~/.claude/.credentials.json` expire (default TTL: 8 hours).

```bash
# Check token age — is Claude Code signed in?
ls -la ~/.claude/.credentials.json

# Test the oat_sources command manually
jq -r '.claudeAiOauth.accessToken' ~/.claude/.credentials.json
# Empty/null output = expired or missing credentials

# Force token refresh by signing into Claude Code
claude
# Then restart ccproxy
ccproxy restart --detach
```

ccproxy auto-refreshes tokens via:
- **TTL-based**: Background task checks every 30 minutes, refreshes at 90% of `oauth_ttl`
- **401-triggered**: Immediate refresh on authentication error, retries the request once

Config options:
```yaml
ccproxy:
  oauth_ttl: 28800           # Token lifetime (seconds), default 8 hours
  oauth_refresh_buffer: 0.1  # Refresh at 90% of TTL (10% buffer)
```

### Wrong sentinel key provider name

The provider name after `sk-ant-oat-ccproxy-` must exactly match a key in `oat_sources`:

```yaml
oat_sources:
  anthropic: "..."  # Matches: sk-ant-oat-ccproxy-anthropic
  zai: "..."        # Matches: sk-ant-oat-ccproxy-zai
```

Using `sk-ant-oat-ccproxy-claude` when the source is named `anthropic` will fail with a log warning:
```
Sentinel key for provider 'claude' but no OAuth token configured in oat_sources
```

### oat_sources command failing

```bash
# Copy your oat_sources command from ccproxy.yaml and run it directly:
jq -r '.claudeAiOauth.accessToken' ~/.claude/.credentials.json
# Should output a token starting with "sk-ant-oat"

# Common failures:
# - jq not installed
# - File doesn't exist: ~/.claude/.credentials.json
# - JSON path wrong (accessToken vs access_token)
# - Command timeout (ccproxy gives 5 seconds)
```

### x-api-key / Authorization header conflict

LiteLLM internally converts `Authorization: Bearer {token}` to `x-api-key: {token}` for Anthropic. The `forward_oauth` hook counteracts this by:
1. Setting `Authorization: Bearer {token}` in extra_headers
2. Setting `x-api-key: ""` (empty) in extra_headers

ccproxy also patches LiteLLM's `AnthropicModelInfo.validate_environment()` to preserve the empty `x-api-key` when OAuth mode is detected. If this patch fails, you'll see:
```
Failed to patch Anthropic validate_environment for OAuth header support
```

If patching fails, enable MITM mode as a fallback safety net:
```bash
ccproxy start --detach --mitm
```

---

## Error: Connection refused / timeout

```bash
# Check proxy status
ccproxy status

# Check if port 4000 is in use
ss -tlnp | grep 4000

# Start if not running
ccproxy start --detach

# Check for startup errors
ccproxy logs -n 30
```

Common causes:
- ccproxy not started
- Port 4000 already in use by another process
- LiteLLM failed to start (check logs for import errors)

---

## General diagnostics

### Verify hook pipeline execution

With `debug: true` in `ccproxy.yaml`, logs show each hook's execution:

```
ccproxy.hooks:DEBUG: forward_oauth: Detected provider 'anthropic' for model '...'
ccproxy.hooks:INFO: Forwarding request with OAuth authentication for provider 'anthropic'
ccproxy.hooks:INFO: Added anthropic-beta headers for Claude Code impersonation
ccproxy.hooks:INFO: Injected Claude Code identity for OAuth authentication
```

If any of these log lines are missing, the corresponding hook is either:
- Not in the hooks list
- Skipping due to a condition (model has api_key, provider not detected, no OAuth token)

### Verify model routing

Debug mode shows routing panels:
```
[ccproxy] Request Routed
├─ Type: PASSTHROUGH
├─ Model Name: default
├─ Original: claude-sonnet-4-5-20250929
└─ Routed to: claude-sonnet-4-5-20250929
```

If `Type: PASSTHROUGH` and the model doesn't exist in `config.yaml`, routing will fail.

### Check config files

```bash
# Verify both config files exist
ls -la ~/.ccproxy/ccproxy.yaml ~/.ccproxy/config.yaml

# Verify model definitions
grep 'model_name:' ~/.ccproxy/config.yaml

# Verify handler auto-generated
cat ~/.ccproxy/ccproxy.py
# Should contain: from ccproxy.handler import CCProxyHandler
```

---

## LiteLLM internal behaviors

These behaviors affect authentication and are handled by ccproxy's patches and hooks:

1. **Bearer-to-x-api-key conversion**: LiteLLM's Anthropic provider converts `Authorization: Bearer {token}` to `x-api-key: {token}`. The `forward_oauth` hook sets `x-api-key: ""` to prevent this, and ccproxy patches `AnthropicModelInfo.validate_environment` to preserve the empty value.

2. **Header merge order**: LiteLLM's `validate_environment()` merges headers as `{**user_headers, **provider_headers}`, meaning provider-hardcoded `x-api-key` overwrites user values. ccproxy's patch reverses this precedence when OAuth mode is detected.

3. **Health check failures**: Models using OAuth have no static API key, so LiteLLM health checks fail with `AuthenticationError`. ccproxy patches the health check to inject `mock_response` for models with `health_check_model` set.

4. **forward_client_headers_to_llm_api**: Must be `true` in `config.yaml`'s `general_settings` for client headers to reach the hooks:
   ```yaml
   general_settings:
     forward_client_headers_to_llm_api: true
   ```

---

## Provider-specific notes

### api.anthropic.com

- Requires ALL four beta headers (`oauth-2025-04-20`, `claude-code-20250219`, `interleaved-thinking-2025-05-14`, `fine-grained-tool-streaming-2025-05-14`)
- Requires "You are Claude Code" system message prefix
- OAuth tokens have `sk-ant-oat` prefix
- `x-api-key` must be empty (not absent) when using OAuth Bearer

### api.z.ai (ZAI)

- Does NOT require "You are Claude Code" system message (`inject_claude_code_identity` skips non-anthropic.com api_base)
- May require its own `oat_sources` entry with `destinations: ["api.z.ai"]`
- Use extended oat_sources form:
  ```yaml
  oat_sources:
    zai:
      command: "jq -r '.accessToken' ~/.zai/credentials.json"
      user_agent: "MyApp/1.0"
      destinations: ["api.z.ai"]
  ```

### Other providers (OpenAI, Gemini)

- Beta headers and system message injection only apply to Anthropic provider
- Other providers just need OAuth token forwarding via `forward_oauth`
- Provider detection: LiteLLM's `get_llm_provider()` → destination matching → model name fallback

---

## MITM mode (optional safety net)

MITM mode provides HTTP-layer redundancy for header injection. It is NOT required — the pipeline hooks handle everything. MITM is useful as a debugging tool or extra safety net.

```bash
# Start with MITM
ccproxy start --detach --mitm

# Architecture: client → reverse proxy (port 4000) → LiteLLM → forward proxy (port 8081) → provider API
```

The MITM addon independently:
- Removes `x-api-key` for OAuth requests
- Adds `anthropic-beta` headers
- Injects system message prefix

This means if a pipeline hook fails, MITM catches it at the HTTP layer.
