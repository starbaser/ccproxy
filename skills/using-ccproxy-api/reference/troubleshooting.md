# Troubleshooting Guide

## Contents

- [Diagnostic checklist](#diagnostic-checklist)
- [Error: "This credential is only authorized for use with Claude Code"](#error-this-credential-is-only-authorized-for-use-with-claude-code)
- [Error: "OAuth is not supported" or "invalid x-api-key"](#error-oauth-is-not-supported-or-invalid-x-api-key)
- [Error: 401 Unauthorized / token errors](#error-401-unauthorized--token-errors)
- [Error: Connection refused / timeout](#error-connection-refused--timeout)
- [General diagnostics](#general-diagnostics)
- [Provider-specific notes](#provider-specific-notes)

---

## Diagnostic checklist

Run these first for any issue:

```bash
# 1. Is ccproxy running?
ccproxy status

# 2. Stream logs while reproducing the issue
ccproxy logs -f

# 3. Verify config
cat $CCPROXY_CONFIG_DIR/ccproxy.yaml   # or: cat ~/.ccproxy/ccproxy.yaml

# 4. Test OAuth command manually
jq -r '.claudeAiOauth.accessToken' ~/.claude/.credentials.json
# Should output a token starting with "sk-ant-oat"

# 5. Check compliance profile status
uv run python scripts/compliance_status.py  # from ccproxy project root
```

---

## Error: "This credential is only authorized for use with Claude Code"

**Cause**: Anthropic's API validates that OAuth tokens are only used by Claude Code. It checks that the system message starts with "You are Claude Code, Anthropic's official CLI for Claude."

**Resolution**:

1. Check compliance profile status — the system prompt should be learned and stamped:
   ```bash
   uv run python scripts/compliance_status.py --provider anthropic
   # Verify has_system: true
   ```

2. If no learned profile exists yet, check if the v0 seed is active:
   ```bash
   uv run python scripts/compliance_status.py --seed-status
   ```
   The seed provides the system prompt prefix. If it's missing, verify `compliance.seed_anthropic: true` in config.

3. If a profile exists but the system prompt isn't being stamped, check the `apply_compliance` hook:
   - Is it in the `outbound` hooks list?
   - Does the flow have a `TransformMeta`? (requires a matching transform rule)
   - Is the flow coming through reverse proxy? (compliance only fires on reverse proxy, not WireGuard)

4. If the client sends a `list`-type system prompt (structured content blocks), compliance **skips** system injection — it assumes the client manages its own identity. Send `system` as a string or omit it.

5. To seed a fresh profile from real CLI traffic:
   ```bash
   ccproxy run --inspect -- claude
   # Make 3+ requests, then check:
   uv run python scripts/compliance_status.py --seed-status
   ```

---

## Error: "OAuth is not supported" or "invalid x-api-key"

**Cause**: Anthropic's API requires `anthropic-beta: oauth-2025-04-20` to accept OAuth Bearer tokens. Without it, the API rejects the OAuth token.

**Resolution**:

1. Check compliance profile headers:
   ```bash
   uv run python scripts/compliance_status.py --provider anthropic
   # Verify anthropic-beta header is in the profile
   ```

2. The v0 seed profile includes `anthropic-beta` with all required values. If it's not applying:
   - Verify `apply_compliance` is in `hooks.outbound`
   - Verify `compliance.enabled: true`
   - Verify `compliance.seed_anthropic: true`

3. Inspect the forwarded request to see what headers are actually being sent:
   ```bash
   ccproxy flows list
   ccproxy flows dump <flow-id> | jq '.log.entries[0].request.headers'    # Check for anthropic-beta header
   ```

4. Compare client vs forwarded to see if compliance stamped headers:
   ```bash
   uv run python scripts/inspect_flow.py <flow-id>
   ```

---

## Error: 401 Unauthorized / token errors

Multiple causes — work through in order:

### Token expired

OAuth tokens from `~/.claude/.credentials.json` expire.

```bash
# Check token age — is Claude Code signed in?
ls -la ~/.claude/.credentials.json

# Test the oat_sources command manually
jq -r '.claudeAiOauth.accessToken' ~/.claude/.credentials.json
# Empty/null output = expired or missing credentials

# Force token refresh by signing into Claude Code
claude
```

ccproxy auto-refreshes on 401: `InspectorAddon.response()` detects HTTP 401 with `x-ccproxy-oauth-injected: 1`, calls `refresh_oauth_token(provider)`, and retries with the new token if it changed.

### Wrong sentinel key provider name

The provider name after `sk-ant-oat-ccproxy-` must exactly match a key in `oat_sources`:

```yaml
oat_sources:
  anthropic: "..."  # Matches: sk-ant-oat-ccproxy-anthropic
  gemini: "..."     # Matches: sk-ant-oat-ccproxy-gemini
```

Using `sk-ant-oat-ccproxy-claude` when the source is named `anthropic` raises a fatal `OAuthConfigError`:
```
OAuthConfigError: Sentinel key for provider 'claude' but no OAuth token configured in oat_sources
```

### oat_sources command failing

```bash
# Copy your oat_sources command from ccproxy.yaml and run it directly:
jq -r '.claudeAiOauth.accessToken' ~/.claude/.credentials.json
# Should output a token

# Common failures:
# - jq not installed
# - File doesn't exist: ~/.claude/.credentials.json
# - JSON path wrong (accessToken vs access_token)
# - Command returns empty string or null
```

### Auth header injection

`forward_oauth` injects auth via the configured header:
- Default: `Authorization: Bearer {token}`
- If `oat_sources.{provider}.auth_header` is set: uses that header name with raw token value (e.g. `x-goog-api-key: {token}`)

Check the forwarded request headers:
```bash
ccproxy flows dump <flow-id> | jq '.log.entries[0].request.headers'
# Verify Authorization or x-api-key header is present and non-empty
```

---

## Error: Connection refused / timeout

```bash
# Check proxy status
ccproxy status

# Check ports
ss -tlnp | grep 4000    # proxy port
ss -tlnp | grep 8083    # inspector UI port

# Start if not running
ccproxy start            # foreground
just up                  # or: process-compose up --detached

# Check for startup errors
ccproxy logs -n 30
```

Common causes:
- ccproxy not started
- Port already in use (check for another ccproxy instance or stale process)
- Startup failure in mitmproxy (check logs for import errors or port conflicts)

---

## General diagnostics

With `debug: true` in `ccproxy.yaml`, logs show each hook's execution:

```
ccproxy.pipeline:DEBUG: Executing hook forward_oauth
ccproxy.hooks:INFO: Forwarding request with OAuth for provider 'anthropic'
ccproxy.pipeline:DEBUG: Executing hook apply_compliance
ccproxy.compliance:INFO: Compliance: added header anthropic-beta
```

If a hook is not firing:
- Check that it's in the `hooks.inbound` or `hooks.outbound` list
- Check the guard condition (e.g. `apply_compliance` requires `ReverseMode` + `TransformMeta`)
- Check per-request overrides via `x-ccproxy-hooks` header

### Verify transform routing

```bash
# List recent flows to see if they're being matched
ccproxy flows list

# Check if a flow was transformed
ccproxy flows dump <id> | jq '.log.entries[1].request.url'   # Pre-pipeline URL
ccproxy flows dump <id> | jq '.log.entries[0].request.url'   # Post-pipeline URL (should differ if transformed)
```

If transforms are configured but not matching, check:
- `match_host` — matches against `pretty_host`, `Host` header, `X-Forwarded-Host`
- `match_path` — prefix match (must start with the same path)
- `match_model` — substring match on the `model` field in the JSON body
- Rule order — first match wins

### Inspect the mitmweb UI

The inspector UI runs at `http://127.0.0.1:{inspector.port}/?token={web_token}`. The URL with token is printed to logs on startup.

- Select a flow to see full request/response headers and body
- Switch to "Client-Request" content view to see the pre-pipeline snapshot
- Filter flows by host, path, or response code

---

## Provider-specific notes

### api.anthropic.com

- Requires `anthropic-beta` headers including `oauth-2025-04-20` for OAuth
- Requires "You are Claude Code" system prompt prefix for OAuth tokens
- Both are handled automatically by the compliance system (seed or learned profile)
- OAuth tokens have `sk-ant-oat` prefix
- On 401: ccproxy auto-refreshes and retries once

### Google (Gemini / cloudcode-pa)

- cloudcode-pa flows use a body wrapper: `{model: X, request: {<body>}}` — handled by compliance `body_wrapper`
- Gemini auth uses `x-goog-api-key` header — set via `oat_sources.gemini.auth_header: "x-goog-api-key"` or let `forward_oauth` handle it
- Configure `destinations` to include both `generativelanguage.googleapis.com` and `cloudcode-pa.googleapis.com`

### Other providers

- Compliance profiles are per-provider — each provider's contract is learned independently
- Provider detection uses `oat_sources.*.destinations` (substring match) then `inspector.provider_map` (exact hostname)
- Transform rules handle cross-provider format conversion via lightllm
