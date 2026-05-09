# Gemini Through ccproxy

Reference for routing Gemini traffic (CLI, SDK, native v1internal clients)
through ccproxy to `cloudcode-pa.googleapis.com`.

## The cloudcode-pa endpoint

The Gemini CLI does not talk to `generativelanguage.googleapis.com`. It talks
to `cloudcode-pa.googleapis.com/v1internal:{action}` — Google's "Code Assist"
endpoint. The body schema is wrapped in an envelope:

```
Standard Gemini API:
  POST /v1beta/models/{model}:generateContent
  { "contents": [...], "generationConfig": {...} }

cloudcode-pa v1internal:
  POST /v1internal:generateContent
  {
    "model": "gemini-3.1-pro-preview",
    "project": "principal-canopy-qxpwk",
    "request": { "contents": [...], "generationConfig": {...} },
    "user_prompt_id": "<uuid>"
  }
```

Why this endpoint matters: cloudcode-pa is what gets the Gemini Code Assist
tier rate limits and capacity. Standard `generativelanguage.googleapis.com`
uses different quota. The `Authorization: Bearer ya29.*` token from
`~/.gemini/oauth_creds.json` is scoped for cloudcode-pa, not the standard API.

## The sentinel-key contract

**Any client using the sentinel key `sk-ant-oat-ccproxy-gemini` MUST end up
sending v1internal envelope traffic to cloudcode-pa.** This is enforced by the
`gemini_cli` outbound hook regardless of how the client speaks.

```
client                          ccproxy                          upstream

Gemini SDK / Glass / OpenAI ──► forward_oauth ──► [transform] ──► gemini_cli ──► cloudcode-pa
  sentinel key                  resolves token   normalizes        wraps body,         v1internal
                                                  format            rewrites path
```

## The `gemini_cli` outbound hook

Single hook, three responsibilities:

1. **Header masquerade** — rewrites `user-agent` and `x-goog-api-client` to the
   Gemini CLI fingerprint. Capacity allocation by cloudcode-pa is fingerprint-
   sensitive; without this, traffic gets a different (lower) tier.
2. **Body envelope wrap** — `{contents, ...}` → `{model, project, request: {...}, user_prompt_id}`.
   Strips the Anthropic-style `metadata` field that Google rejects.
3. **Path/host rewrite** — `/v1beta/models/{m}:action` → `/v1internal:action`
   (with `?alt=sse` for `streamGenerateContent`); host → `cloudcode-pa.googleapis.com`.

The hook is **idempotent**: if the body is already in v1internal envelope shape
(Glass-style clients), it passes through unchanged.

### Trigger

Fires only when `flow.metadata["ccproxy.oauth_provider"] == "gemini"` — set by
`forward_oauth` after sentinel-key resolution. Other Gemini traffic (raw API
key, no sentinel) is not touched.

### Project resolution

The `project` field is the user's Cloud AI Companion project ID. Resolved once
per process by `prewarm_project()` via `POST /v1internal:loadCodeAssist` and
cached. The hook itself does not retry on 401 — it just logs a warning and
omits the `project` field from subsequent requests. Token freshness is the
job of `_load_credentials()` at startup: when the Gemini provider uses
`type: google_oauth`, the cached access token is refreshed (atomic write-back
to `~/.gemini/oauth_creds.json`) before `prewarm_project()` runs. With
`type: command`, no refresh happens — see configuration.md "Why Gemini wants
google_oauth".

### Response unwrapping

cloudcode-pa returns `{"response": {"candidates": [...]}}`. Standard Gemini SDK
clients expect `{"candidates": [...]}` at the top level. `GeminiAddon` owns the
response-side unwrap:

- **Buffered responses** — `unwrap_buffered()` in `hooks/gemini_envelope.py`
  strips the outer `response` field. Called from `GeminiAddon.response`.
- **Streaming responses** — `EnvelopeUnwrapStream` (also in
  `hooks/gemini_envelope.py`) is installed as `flow.response.stream` by
  `GeminiAddon.responseheaders` and unwraps each SSE chunk.

Both surfaces share the same primitive — the file is the single source of
truth for "strip the cloudcode-pa envelope."

## Three client scenarios

### 1. Gemini SDK (google-genai, native Gemini format)

```python
from google import genai

client = genai.Client(
    api_key="sk-ant-oat-ccproxy-gemini",
    http_options={"base_url": "http://127.0.0.1:4000/gemini"},
)

response = client.models.generate_content(
    model="gemini-3.1-pro-preview",
    contents="What is 2+2?",
)
print(response.text)
```

The SDK constructs `/v1beta/models/{model}:generateContent` paths and
`{contents, generationConfig}` bodies. ccproxy's `/gemini/` redirect strips the
prefix; the `gemini_cli` hook wraps the body and rewrites the path.

### 2. Native v1internal client (Glass)

```python
import urllib.request, json

req = urllib.request.Request(
    "http://127.0.0.1:4000/v1internal:generateContent",
    data=json.dumps({
        "model": "gemini-3.1-pro-preview",
        "project": "principal-canopy-qxpwk",
        "request": {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
    }).encode(),
    headers={"Content-Type": "application/json", "x-api-key": "sk-ant-oat-ccproxy-gemini"},
    method="POST",
)
```

Body is already in envelope shape. The hook detects this and passes the body
through unchanged (still does header masquerade and routing).

### 3. OpenAI-format client through transform mode

OpenAI-format `{messages: [...]}` → lightllm transforms to standard Gemini
`{contents, ...}` → `gemini_cli` hook wraps in v1internal envelope. Three
layers, each owning one transformation.

## Authentication

The recommended setup is `type: google_oauth` so ccproxy owns the in-process
refresh lifecycle (60s expiry headroom + atomic write-back). `prewarm_project()`
runs after `_load_credentials()` and depends on a fresh token to call
`loadCodeAssist`; with a static `command`/`file` source, an expired token at
startup means the `project` field is silently omitted from every Gemini request.

```yaml
providers:
  gemini:
    auth:
      type: google_oauth
      file_path: ~/.gemini/oauth_creds.json
      client_id: <gemini-cli installed-app client_id>
      client_secret: <gemini-cli installed-app client_secret>
      header: authorization
    host: cloudcode-pa.googleapis.com
    path: "/v1internal:{action}"
    provider: gemini
```

The `client_id` / `client_secret` are public installed-app values embedded in
the gemini-cli npm distribution — ccproxy does not vendor them; supply them in
your config.

`forward_oauth` substitutes the sentinel key with the resolved token and stamps
`flow.metadata["ccproxy.oauth_provider"] = "gemini"` so the `gemini_cli` hook
fires. On a 401 from upstream, `OAuthAddon` (not the gemini_cli hook itself)
re-resolves the credential source via `config.resolve_oauth_token("gemini")`
and replays the request.

## Capacity fallback (GeminiAddon)

`GeminiAddon` orchestrates Gemini-specific capacity handling for any flow
flagged with `flow.metadata["ccproxy.oauth_provider"] == "gemini"`. On a
429/503 carrying `RESOURCE_EXHAUSTED` or `INTERNAL` status, it sticky-retries
the original model up to `sticky_retry_attempts` times (honouring
`RetryInfo.retryDelay` per attempt, capped by
`sticky_retry_max_delay_seconds`), then walks `gemini_capacity.fallback_models`
in order. The whole chain is bounded by `total_retry_budget_seconds`.

Streaming flows defer their `EnvelopeUnwrapStream` install when the response
status is in `retry_status_codes` and fallback is enabled — that lets
mitmproxy buffer the error body so `_try_fallback_models` can read it for the
retry decision. Successful retry replaces `flow.response`; envelope unwrap
then runs against the (possibly replaced) response.

See [`configuration.md` § Gemini Capacity Fallback](configuration.md#gemini-capacity-fallback)
for the full field reference.

## Configuration

The Gemini route is driven by `providers.gemini` — the sentinel key
`sk-ant-oat-ccproxy-gemini` resolves to that entry for auth, host, and path.
`inspector.transforms` is empty by default; the SDK and Glass paths below
both ride sentinel-key resolution, not transform overrides.

```nix
providers.gemini = {
  auth = {
    type = "google_oauth";
    file_path = "~/.gemini/oauth_creds.json";
    client_id = "<gemini-cli installed-app client_id>";
    client_secret = "<gemini-cli installed-app client_secret>";
    header = "authorization";
  };
  host = "cloudcode-pa.googleapis.com";
  path = "/v1internal:{action}";
  provider = "gemini";
};

inspector.transforms = [];

hooks.outbound = [
  "ccproxy.hooks.gemini_cli"            # envelope wrap, header masquerade
  "ccproxy.hooks.inject_mcp_notifications"
  "ccproxy.hooks.verbose_mode"
  "ccproxy.hooks.shape"                 # optional CLI-fingerprint shape
];
```

WireGuard CLI flows (where the Gemini CLI talks to `cloudcode-pa.googleapis.com`
directly through the namespace jail) are handled by `gemini_cli`'s
sentinel-aware trigger and the Provider's path templating — no `passthrough`
override is required. Add a `TransformOverride` only when you need to bypass
auth or force a specific destination for a non-sentinel flow.

## Working examples

See `examples/gemini_sdk_via_ccproxy.py` (text) and
`examples/gemini_sdk_image_via_ccproxy.py` (multi-MB image payload).

## Troubleshooting

### 401 Unauthorized
- Check `~/.gemini/oauth_creds.json` exists and has a valid `access_token`
- Run `gemini -p ""` directly to force a token refresh
- `ccproxy logs -f` will show `OAuth token injected for provider 'gemini'`

### 429 Resource Exhausted
- cloudcode-pa rate limits are 25–40 second windows
- Verify the `gemini_cli` hook fired: log line `gemini_cli: <model> → cloudcode-pa.googleapis.com/v1internal:...`
- If user-agent is wrong, capacity gets cut. Check the masqueraded UA:
  `ccproxy flows compare` shows the forwarded request

### "Unknown name metadata"
- Google's API rejects unknown body fields. The hook strips `metadata` before
  wrapping. If you see this, check whether something is re-injecting it after
  the hook (shape hook config or another outbound hook).

### Streaming response shows `{"response": {...}}` envelope
- `GeminiAddon.responseheaders` should install `EnvelopeUnwrapStream`. Check
  that `flow.metadata["ccproxy.oauth_provider"] == "gemini"`,
  `transform.is_streaming == True`, and `transform.mode == "redirect"` are
  all set on the flow record. If `transform` is `None`, the `gemini_cli` hook
  didn't fire — check `oauth_provider` metadata.

### Inspecting flows

```bash
ccproxy flows list                       # all captured flows
ccproxy flows compare                    # client request vs forwarded request
ccproxy flows dump | jq '.log.entries'   # full HAR view
```

The `compare` view will show:
- Client request: `{contents: [...]}` (or `{model, project, request: {...}}` for Glass)
- Forwarded request: `{model, project, request: {contents: [...]}, user_prompt_id}`
- Provider response: `{response: {candidates: [...]}}`
- Client response: `{candidates: [...]}`

## File map

| Component | Path |
|-----------|------|
| Unified outbound hook | `src/ccproxy/hooks/gemini_cli.py` |
| Project resolution (`prewarm_project`) | `src/ccproxy/hooks/gemini_cli.py` |
| Buffered response unwrap (`unwrap_buffered`) | `src/ccproxy/hooks/gemini_envelope.py` |
| Streaming response unwrap (`EnvelopeUnwrapStream`) | `src/ccproxy/hooks/gemini_envelope.py` |
| Capacity fallback + envelope unwrap orchestrator | `src/ccproxy/inspector/gemini_addon.py` |
| 401 retry orchestrator | `src/ccproxy/inspector/oauth_addon.py` |
| Provider routing | `nix/defaults.nix` `providers.gemini` |
| Tests | `tests/test_gemini_cli.py`, `tests/test_gemini_addon_capacity.py` |
