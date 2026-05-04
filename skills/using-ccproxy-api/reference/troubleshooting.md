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
cat $CCPROXY_CONFIG_DIR/ccproxy.yaml   # or: cat ~/.config/ccproxy/ccproxy.yaml

# 4. Test OAuth command manually
jq -r '.claudeAiOauth.accessToken' ~/.claude/.credentials.json
# Should output a token starting with "sk-ant-oat"

# 5. Check shaping profile status
uv run python scripts/shaping_status.py  # from ccproxy project root
```

---

## Error: "This credential is only authorized for use with Claude Code"

**Cause**: Anthropic's API validates that OAuth tokens are only used by Claude Code. It checks that the system message starts with "You are Claude Code, Anthropic's official CLI for Claude."

**Resolution**:

1. Check shaping profile status — the system prompt should be learned and stamped:
   ```bash
   uv run python scripts/shaping_status.py --provider anthropic
   # Verify has_system: true
   ```

2. If no learned profile exists yet, check if the v0 shape is active:
   ```bash
   uv run python scripts/shaping_status.py --shape-status
   ```
   The shape provides the system prompt prefix. If it's missing, verify `shaping.seed_anthropic: true` in config.

3. If a profile exists but the system prompt isn't being stamped, check the `apply_shaping` hook:
   - Is it in the `outbound` hooks list?
   - Does the flow have a `TransformMeta`? (requires a matching transform rule)
   - Is the flow coming through reverse proxy? (shaping only fires on reverse proxy, not WireGuard)

4. If the client sends a `list`-type system prompt (structured content blocks), shaping **skips** system injection — it assumes the client manages its own identity. Send `system` as a string or omit it.

5. To capture a fresh profile from real CLI traffic:
   ```bash
   ccproxy run --inspect -- claude
   # Make 3+ requests, then check:
   uv run python scripts/shaping_status.py --shape-status
   ```

---

## Error: "OAuth is not supported" or "invalid x-api-key"

**Cause**: Anthropic's API requires `anthropic-beta: oauth-2025-04-20` to accept OAuth Bearer tokens. Without it, the API rejects the OAuth token.

**Resolution**:

1. Check shaping profile headers:
   ```bash
   uv run python scripts/shaping_status.py --provider anthropic
   # Verify anthropic-beta header is in the profile
   ```

2. The v0 shape includes `anthropic-beta` with all required values. If it's not applying:
   - Verify `apply_shaping` is in `hooks.outbound`
   - Verify `shaping.enabled: true`
   - Verify `shaping.seed_anthropic: true`

3. Inspect the forwarded request to see what headers are actually being sent:
   ```bash
   ccproxy flows list
   ccproxy flows dump <flow-id> | jq '.log.entries[0].request.headers'    # Check for anthropic-beta header
   ```

4. Compare client vs forwarded to see if shaping stamped headers:
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

# Test the providers[name].auth command manually
jq -r '.claudeAiOauth.accessToken' ~/.claude/.credentials.json
# Empty/null output = expired or missing credentials

# Force token refresh by signing into Claude Code
claude
```

ccproxy auto-refreshes on 401: `InspectorAddon.response()` detects HTTP 401 with `x-ccproxy-oauth-injected: 1`, calls `refresh_oauth_token(provider)`, and retries with the new token if it changed.

### Wrong sentinel key provider name

The provider name after `sk-ant-oat-ccproxy-` must exactly match a key in `providers`:

```yaml
providers:
  anthropic:
    auth: "..."   # Matches: sk-ant-oat-ccproxy-anthropic
    host: api.anthropic.com
    path: /v1/messages
    provider: anthropic
  gemini:
    auth: "..."   # Matches: sk-ant-oat-ccproxy-gemini
    host: cloudcode-pa.googleapis.com
    path: "/v1internal:{action}"
    provider: gemini
```

Using `sk-ant-oat-ccproxy-claude` when the providers entry is named `anthropic` raises a fatal `OAuthConfigError`:
```
OAuthConfigError: Sentinel key for provider 'claude' but no matching providers entry. Add 'providers.claude' to ccproxy.yaml.
```

### providers[name].auth command failing

```bash
# Copy your providers[name].auth.command from ccproxy.yaml and run it directly:
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
- If `providers.{provider}.auth.header` is set: uses that header name with raw token value (e.g. `x-api-key: {token}`)

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
ccproxy.pipeline:DEBUG: Executing hook apply_shaping
ccproxy.shaping:INFO: Shaping: added header anthropic-beta
```

If a hook is not firing:
- Check that it's in the `hooks.inbound` or `hooks.outbound` list
- Check the guard condition (e.g. `apply_shaping` requires `ReverseMode` + `TransformMeta`)
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
- Both are handled automatically by the shaping system (initial shape or learned profile)
- OAuth tokens have `sk-ant-oat` prefix
- On 401: ccproxy auto-refreshes and retries once

### Google (Gemini / cloudcode-pa)

- cloudcode-pa flows use a body wrapper: `{model: X, request: {<body>}}` — handled by shaping `body_wrapper`
- Gemini OAuth tokens (`ya29.*`) flow as `Authorization: Bearer`; raw API keys (`AIza*`) can override via `providers.gemini.auth.header: "x-goog-api-key"`
- `providers.gemini.host` is a single destination (e.g. `cloudcode-pa.googleapis.com`); register a separate provider entry for `generativelanguage.googleapis.com` if you need to route both

### Other providers

- Shaping profiles are per-provider — each provider's contract is learned independently
- Provider resolution is sentinel-driven: `forward_oauth` parses the `sk-ant-oat-ccproxy-{name}` suffix and looks up `providers[name]`; with no sentinel it walks `config.providers` in dict order and falls back to the first entry with a cached token. The route handler then chooses `redirect` vs `transform` based on whether the incoming format matches the destination's `provider` field. `inspector.provider_map` is unrelated — it maps hostnames to OTel `gen_ai.system` attributes.
- Transform rules handle cross-provider format conversion via lightllm
