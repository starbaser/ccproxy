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

# 4. Test the providers[name].auth source manually (example for command-typed Anthropic)
jq -r '.claudeAiOauth.accessToken' ~/.claude/.credentials.json
# Should output a token

# 5. Inspect the most recent flow's pipeline-applied transformations
ccproxy flows list
ccproxy flows compare --jq 'map(.[-1])'   # client-vs-forwarded for the latest flow
```

---

## Error: "This credential is only authorized for use with Claude Code"

**Cause**: Anthropic's API checks that the system message starts with the Claude Code preamble. ccproxy supplies that preamble through shape replay — if there's no captured shape (or the shape is from an outdated CLI release), the preamble is missing and Anthropic rejects the request.

**Resolution**:

1. Confirm a shape file exists:

   ```bash
   ls -la ~/.config/ccproxy/shaping/shapes/anthropic.mflow
   ```

2. Capture (or refresh) a shape from a real Claude CLI run:

   ```bash
   ccproxy run --inspect -- claude -p "shape capture"
   ccproxy flows shape --provider anthropic
   ```

3. Verify the `shape` hook is in `hooks.outbound` in your `ccproxy.yaml`. Without it the shape is never replayed.

4. Verify the flow has a `TransformMeta` (i.e. matched a transform/redirect rule or resolved via sentinel-key). The `shape_guard` skips flows without a transform.

5. If the client sends a `list`-typed system prompt with its own content blocks, your `merge_strategies.system` controls how the shape's preamble is combined (`prepend_shape:N` is the canonical setting — see [`docs/shaping.md`](../../../docs/shaping.md)).

---

## Error: "OAuth is not supported" or "invalid x-api-key"

**Cause**: Anthropic's API requires `anthropic-beta: oauth-2025-04-20` to accept OAuth Bearer tokens. That header is supplied by the captured Anthropic shape — if the shape is missing or stale, the header isn't stamped.

**Resolution**:

1. Verify a shape exists and is recent — see steps under the previous error.
2. Inspect the forwarded request to see what headers actually went upstream:

   ```bash
   ccproxy flows list
   ccproxy flows dump --jq 'map(.[-1])' | jq '.log.entries[0].request.headers'
   ```

3. Compare client-vs-forwarded to confirm the shape ran:

   ```bash
   ccproxy flows compare --jq 'map(.[-1])'
   ```

   The "Body diff" section should show identity headers added on the forwarded side that the client never sent.

---

## Error: 401 Unauthorized / token errors

Multiple causes — work through in order.

### Token expired

OAuth tokens from `~/.claude/.credentials.json` expire. With `type: anthropic_oauth` (recommended), ccproxy refreshes them automatically. With `type: command`, it just reads whatever's on disk.

```bash
# Check token freshness
jq -r '.claudeAiOauth.expiresAt' ~/.claude/.credentials.json   # millis since epoch
# Compare with: date +%s%3N

# Test the providers[name].auth command manually
jq -r '.claudeAiOauth.accessToken' ~/.claude/.credentials.json
# Empty/null output = expired or missing credentials

# Force token refresh by signing into Claude Code
claude
```

ccproxy auto-retries on 401: `OAuthAddon.response()` detects HTTP 401 on flows where `forward_oauth` injected an OAuth token (`flow.metadata["ccproxy.oauth_injected"]`), calls `config.resolve_oauth_token(provider)`, and replays the request with whatever the resolver returns.

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

### providers[name].auth source failing

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

For OAuth sources (`anthropic_oauth`, `google_oauth`), the refresh round-trip is logged. Tail logs while reproducing:

```bash
ccproxy logs -f | grep -E 'OAuth|refresh'
```

### Auth header injection

`forward_oauth` injects auth via the configured header:

- Default: `Authorization: Bearer {token}`
- If `providers.{provider}.auth.header` is set: uses that header name with raw token value (e.g. `x-api-key: {token}`)

Check the forwarded request headers:

```bash
ccproxy flows list
ccproxy flows dump --jq 'map(.[-1])' | jq '.log.entries[0].request.headers'
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
- Startup readiness probe failed (`inspector.readiness.url` defaults to `https://1.1.1.1/`; set to `null` to skip in air-gapped environments)

---

## General diagnostics

With `log_level: DEBUG` in `ccproxy.yaml`, logs show each hook's execution and the OAuth/Gemini addon decisions:

```
ccproxy.pipeline:DEBUG: Executing hook forward_oauth
ccproxy.hooks.forward_oauth:INFO: OAuth token injected for provider 'anthropic' (sentinel)
ccproxy.pipeline:DEBUG: Executing hook shape
ccproxy.hooks.shape:INFO: Applied shape from <shape-id> for provider anthropic
ccproxy.inspector.oauth_addon:INFO: OAuth 401 for provider 'anthropic' — token refreshed, retrying request
```

If a hook is not firing:

- Check that it's in the `hooks.inbound` or `hooks.outbound` list in `ccproxy.yaml`
- Check the guard condition — e.g. `shape_guard` requires `ReverseMode` *or* `ccproxy.oauth_injected`, plus a `TransformMeta` on the record
- Check per-request overrides via the `x-ccproxy-hooks` header (`+hook,-other`)

### Verify transform routing

```bash
# List recent flows to see if they're being matched
ccproxy flows list

# Compare client vs forwarded for the latest flow
ccproxy flows compare --jq 'map(.[-1])'
```

If transforms are configured but not matching, check:

- `match_host` — regex matched against `pretty_host`, `Host` header, `X-Forwarded-Host`
- `match_path` — regex matched against the request path (default `.*`)
- `match_model` — regex matched against `glom(body, "model")`
- Rule order — first match wins

### Inspect the mitmweb UI

The inspector UI runs at `http://127.0.0.1:{inspector.port}/?token={web_token}`. The URL with token is printed to logs on startup.

- Select a flow to see full request/response headers and body
- Switch to the "Client-Request" content view to see the pre-pipeline snapshot
- Switch to the "Provider-Response" content view to see the raw upstream response (pre-unwrap for Gemini)
- Filter flows by host, path, or response code

---

## Provider-specific notes

### api.anthropic.com

- Requires `anthropic-beta` headers including `oauth-2025-04-20` for OAuth — supplied via shape replay
- Requires the "You are Claude Code" system prompt prefix for OAuth tokens — supplied via shape replay (`merge_strategies.system: prepend_shape:N`)
- Requires a fresh, signed `x-anthropic-billing-header` — re-signed per-request by the `regenerate_billing_header` shape inner-DAG hook (needs the salt + seed configured under `shaping.providers.anthropic.billing`)
- Both the shape itself and the billing constants must be set up — see [`docs/shaping.md`](../../../docs/shaping.md)
- OAuth tokens have `sk-ant-oat` prefix
- On 401: `OAuthAddon` re-resolves and retries automatically

### Google (Gemini / cloudcode-pa)

- cloudcode-pa flows are wrapped in the `v1internal` envelope by the `gemini_cli` outbound hook (not by shaping)
- Recommended auth is `type: google_oauth` so ccproxy owns refresh — `prewarm_project()` (which resolves the `cloudaicompanionProject`) needs a fresh token at startup; with `type: command` an expired token at startup means every Gemini request omits the `project` field
- Gemini OAuth tokens (`ya29.*`) flow as `Authorization: Bearer`; raw API keys (`AIza*`) can override via `providers.gemini.auth.header: "x-goog-api-key"`
- On 429/503 with `RESOURCE_EXHAUSTED` or `INTERNAL`, `GeminiAddon` runs the capacity-fallback chain — sticky retry on the original model, then walk `gemini_capacity.fallback_models`. See `gemini_capacity` in `ccproxy.yaml`.
- See [`docs/gemini.md`](../../../docs/gemini.md) for the full Gemini routing reference

### Other providers

- Each provider entry binds an auth source, a single destination (`host` + `path`), and a LiteLLM `provider` identifier (drives format dispatch)
- Provider resolution is sentinel-driven: `forward_oauth` parses the `sk-ant-oat-ccproxy-{name}` suffix and looks up `providers[name]`. With no sentinel it walks `config.providers` in dict insertion order and falls back to the first entry with a cached token. The transform handler then chooses `redirect` vs `transform` based on whether the incoming format matches the destination's `provider` field. (`inspector.provider_map` is unrelated — it maps hostnames to OTel `gen_ai.system` attributes for span attribution only.)
- Cross-provider format conversion happens via `lightllm` when `inspector.transforms` rule matches (or when sentinel-resolved Provider's `provider` field differs from the incoming format)
