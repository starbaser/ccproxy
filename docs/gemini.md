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
per process via `POST /v1internal:loadCodeAssist` and cached. On 401, refreshes
the OAuth token and retries.

### Response unwrapping

cloudcode-pa returns `{"response": {"candidates": [...]}}`. Standard Gemini SDK
clients expect `{"candidates": [...]}` at the top level. The addon's response
phase unwraps the envelope:

- **Buffered responses** — `_unwrap_gemini_response` in `inspector/addon.py` strips
  the outer `response` field.
- **Streaming responses** — `EnvelopeUnwrapStream` (in `hooks/gemini_cli.py`) is
  installed as `flow.response.stream` and unwraps each SSE chunk.

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

`oat_sources.gemini` resolves the OAuth token from
`~/.gemini/oauth_creds.json`:

```yaml
oat_sources:
  gemini:
    command: "jq -r '.access_token' ~/.gemini/oauth_creds.json"
    destinations: ["cloudcode-pa.googleapis.com"]
    user_agent: "GeminiCLI"
```

`forward_oauth` substitutes the sentinel key with the resolved token. On 401,
the addon retries once after refreshing the token.

## Configuration

Default `nix/defaults.nix` ships these transform routes:

```nix
inspector.transforms = [
  # WireGuard CLI flows already targeting cloudcode-pa — pass through unchanged
  { match_host = "cloudcode-pa.googleapis.com"; mode = "passthrough"; }

  # Gemini SDK pointed at ccproxy reverse proxy: /gemini/* → cloudcode-pa
  { match_path = "/gemini/"; mode = "redirect";
    dest_provider = "gemini";
    dest_host = "cloudcode-pa.googleapis.com";
    dest_api_key_ref = "gemini"; }

  # Native v1internal clients (Glass) — body already wrapped
  { match_path = "/v1internal"; mode = "redirect";
    dest_provider = "gemini";
    dest_host = "cloudcode-pa.googleapis.com";
    dest_api_key_ref = "gemini"; }
];

hooks.outbound = [
  "ccproxy.hooks.gemini_cli"            # envelope wrap, header masquerade
  "ccproxy.hooks.inject_mcp_notifications"
  "ccproxy.hooks.verbose_mode"
  "ccproxy.hooks.shape"                 # optional CLI-fingerprint shape
];
```

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
- The addon should install `EnvelopeUnwrapStream`. Check that `transform.provider == "gemini"` and `transform.is_streaming == True` are set on the flow record. If `transform` is `None`, the hook didn't fire — check `oauth_provider` metadata.

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
| Unified hook | `src/ccproxy/hooks/gemini_cli.py` |
| Project resolution | `src/ccproxy/hooks/_gemini_project.py` |
| Buffered response unwrap | `src/ccproxy/inspector/addon.py:_unwrap_gemini_response` |
| Streaming response unwrap | `src/ccproxy/hooks/gemini_cli.py:EnvelopeUnwrapStream` |
| Transform routes | `nix/defaults.nix` `inspector.transforms` |
| Tests | `tests/test_gemini_cli.py` |
