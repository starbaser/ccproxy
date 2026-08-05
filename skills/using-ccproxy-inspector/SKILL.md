---
name: using-ccproxy-inspector
description: >-
  Operates the ccproxy inspector MITM system for intercepting, inspecting, and
  transforming LLM API traffic. Covers running CLI tools through the reverse
  proxy or permissive WireGuard namespace capture path, checking namespace
  status and doctor output, inspecting flows with client-vs-forwarded request
  comparison, understanding the inbound/transform/outbound pipeline, capturing
  and auditing shape artifacts, applying the privacy guide, and diagnosing flow
  issues. Use when running CLI applications through ccproxy, inspecting
  intercepted flows, comparing client request vs forwarded request, checking
  shaping profile status, using WireGuard namespace capture, explaining privacy
  behavior, or debugging the hook pipeline.
---

# Using the ccproxy Inspector

The inspector intercepts LLM API traffic through mitmproxy and routes accepted
flows through the ccproxy addon chain:

```
InspectorAddon -> FingerprintCaptureAddon -> MultiHARSaver -> ShapeCaptureAddon
               -> inbound DAG -> transform router -> outbound DAG
               -> TransportOverrideAddon -> AuthAddon -> GeminiAddon
               -> PerplexityAddon -> EgressSanitizerAddon
```

Use the `using-ccproxy-api` skill for provider auth, sentinel keys, SDK base URL
configuration, native `ccproxy.yaml` setup, and sibling `config.yaml` model
routing when applicable.

The ccproxy plugin does not install or register the ccproxy FastMCP server for
the user. If an MCP-aware client should use ccproxy's flow-inspection tools,
the user must add their own MCP server config pointing at the running daemon's
`http://127.0.0.1:<mcp-port>/mcp` endpoint and supply its bearer token when
configured. Keep plugin installation separate from MCP server registration.

## Inspect First

Before debugging a flow, establish which process and config directory are in
play:

```bash
ccproxy status
ccproxy status --json
ccproxy status --proxy --inspect --mcp
```

For namespace work, also inspect the transparent capture path:

```bash
ccproxy namespace status
ccproxy namespace status --json
ccproxy namespace doctor
ccproxy namespace doctor --json
```

Interpretation:

- `namespace status` reports implementation facts: permissive mode, generated
  WireGuard config presence, slirp4netns topology, and required tool paths.
- `privacy_claim: false` is intentional. ccproxy reports observable runtime
  behavior; it does not claim that the namespace is a restrictive privacy
  firewall.
- `namespace doctor` runs a live probe through the same namespace execution path
  used by `ccproxy run --capture`.
- `namespace doctor` fails for DNS, public IPv4, or ccproxy-localhost
  reachability failures. IPv6 is reported but is not a failure.
- `ccproxy namespace wireguard-config` prints raw WireGuard client config and
  can expose private key material. Do not print or share it casually.

When the task concerns privacy, security language, namespace guarantees,
keylogs, flow exports, or sharing diagnostics, read `docs/privacy.md`.

## Running Tools Through ccproxy

### Reverse proxy: `ccproxy run`

Use this when the client honors SDK base URL environment variables:

```bash
ccproxy run -- claude
ccproxy run -- aider
ccproxy run -- python my_agent.py
```

This sets `ANTHROPIC_BASE_URL`, `OPENAI_BASE_URL`, and `OPENAI_API_BASE` to the
configured ccproxy reverse proxy listener. Only traffic addressed to ccproxy is
intercepted.

Use for lightweight SDK debugging and normal OpenAI/Anthropic-compatible
clients.

### WireGuard namespace capture: `ccproxy run --capture`

Use this when the tool hardcodes provider endpoints, when base URL injection is
not enough, or when you need reference traffic from a real provider CLI:

```bash
ccproxy start
ccproxy run --capture -- claude -p "hello"
ccproxy run --capture -- aider --model claude-sonnet-4-5-20250929
ccproxy run --capture -- python my_agent.py
```

The subprocess runs in a rootless Linux user+network namespace. ccproxy
configures a WireGuard client inside that namespace, routes the namespace
default route through mitmproxy, and injects a combined CA bundle via:

```bash
SSL_CERT_FILE
NODE_EXTRA_CA_CERTS
REQUESTS_CA_BUNDLE
CURL_CA_BUNDLE
```

Important behavior:

- The namespace path is permissive by default because ccproxy is a development
  tool.
- Unmatched WireGuard traffic passes through to its original destination.
- Namespace localhost is DNATed through the slirp4netns gateway so tools with
  hardcoded `127.0.0.1:4000` can still reach ccproxy.
- A port-forwarding monitor uses the slirp4netns API to expose namespace
  listeners back to the host, which supports OAuth callback workflows.
- Do not describe this path as a default deny privacy sandbox.

## Choosing A Capture Mode

| Scenario | Prefer |
| --- | --- |
| SDK client supports configurable base URL | `ccproxy run` |
| CLI hardcodes provider endpoints | `ccproxy run --capture` |
| Need native provider CLI reference traffic | `ccproxy run --capture` |
| Need minimum moving parts | `ccproxy run` |
| Need full local network capture for a tool | `ccproxy run --capture` |
| Need to explain privacy behavior | `docs/privacy.md` + `ccproxy namespace status --json` |

## Understanding Flow State

Every accepted reverse-proxy or WireGuard flow is `direction="inbound"`. The
pipeline stage names `inbound`, `transform`, and `outbound` describe processing
order, not traffic direction.

`InspectorAddon` stamps source metadata:

| Source | Meaning |
| --- | --- |
| `reverse` | Request entered through the reverse proxy listener |
| `wireguard` | Request entered through mitmproxy's WireGuard listener |
| `unknown` | Default before source is stamped |

Every flow has these useful views:

- **Client request**: pre-pipeline snapshot of what the client sent.
- **Forwarded request**: post-pipeline request ccproxy intended to send
  upstream.
- **Provider response**: raw provider response before response-side transform
  when captured.

Use these views to distinguish client behavior from ccproxy behavior.

## Pipeline Map

```
Client request snapshot
  |
  v
Inbound DAG
  inject_auth: sentinel key -> configured provider credential
  extract_session_id: body metadata -> ctx.metadata.session_id
  provider-specific inbound hooks
  |
  v
Transform router
  passthrough: keep destination/body
  redirect: rewrite destination/auth, preserve wire format
  transform: rewrite destination/auth and body via lightllm
  |
  v
Outbound DAG
  gemini_cli: cloudcode-pa envelope/path/header handling
  inject_mcp_notifications: buffered MCP events -> synthetic messages
  verbose_mode: strip redact-thinking beta header
  shape: replay packaged/local request shape and inner-DAG hooks
  commitbee_compat: compatibility shim
  |
  v
TransportOverrideAddon
  optional curl-cffi sidecar for configured fingerprint profiles
  |
  v
AuthAddon
  401 detect -> credential re-resolve -> replay when token changed
  |
  v
GeminiAddon / PerplexityAddon / EgressSanitizerAddon
  provider-specific response handling and ccproxy header cleanup
```

## Inspecting Flows

All `ccproxy flows` commands operate on a resolved flow set:

```
GET /flows -> config.flows.default_jq_filters -> CLI --jq filters -> final set
```

Use repeatable `--jq` filters. Each filter must consume and produce a JSON
array.

```bash
ccproxy flows list
ccproxy flows list --json
ccproxy flows list --jq 'map(select(.request.pretty_host == "api.anthropic.com"))'

ccproxy flows compare
ccproxy flows compare --jq 'map(.[-1])'

ccproxy flows diff
ccproxy flows diff --jq 'map(select(.response.status_code >= 400))'

ccproxy flows dump > all.har
ccproxy flows dump --jq 'map(.[-1])' > latest.har

ccproxy flows clear --all
ccproxy flows clear --jq 'map(select(.response.status_code >= 400))'
```

Privacy note: HAR dumps, request/response bodies, flow JSON, and packet
captures are sensitive. Prefer `flows compare` for local debugging and read
`docs/privacy.md` before sharing artifacts.

## Shape Artifacts

Shape replay uses provider-specific `.mflow` or patch artifacts to reproduce
known-good SDK request envelopes while injecting live request content.

Capture shape source traffic from a real CLI run:

```bash
ccproxy start
ccproxy run --capture -- claude -p "shape capture"
ccproxy flows list
ccproxy shapes save anthropic
ccproxy shapes save anthropic --mflow
```

Audit packaged shape invariants:

```bash
uv run ccproxy shapes audit
```

Shape guidance:

- Packaged `.mflow` files must be minimal request-only artifacts.
- Do not include responses, auth tokens, cookies, flow records, provider
  responses, client snapshots, or captured TLS fingerprint metadata in packaged
  defaults.
- Anthropic and Gemini packaged defaults are distribution artifacts; normal
  users should not need to capture their own shapes unless a provider SDK
  behavior changed before a fixed release exists.
- See `docs/shaping.md` for canonical shape behavior.

## Diagnosing Problems

```
Problem?
|
+- ccproxy not capturing?
|  -> ccproxy status --json
|  -> For transparent capture: ccproxy namespace status --json
|  -> For transparent capture: ccproxy namespace doctor --json
|  -> Check same CCPROXY_CONFIG_DIR for start/run/status
|
+- Provider returns 401/403?
|  -> ccproxy flows compare --jq 'map(.[-1])'
|  -> Check sentinel key: sk-ant-oat-ccproxy-{provider}
|  -> Check providers.{name}.auth resolves manually
|  -> Check ctx.metadata.auth_provider / auth_injected
|  -> Check ccproxy logs for AuthAddon refresh/replay
|
+- Request not transformed?
|  -> ccproxy flows list --json
|  -> Check lightllm.transforms match_host/match_path/match_model
|  -> Check sentinel key resolved to a Provider
|  -> ccproxy flows compare --jq 'map(.[-1])'
|
+- Shape not applied?
|  -> Check hooks.outbound contains ccproxy.hooks.shape
|  -> Check ccproxy shapes audit
|  -> Check transform metadata exists for the flow
|  -> Check flow source: reverse or auth-injected flows consume shapes
|
+- Gemini fails?
|  -> Check gemini_cli outbound hook
|  -> Check Google auth source refresh behavior
|  -> Check GeminiAddon capacity fallback logs
|  -> Inspect forwarded body for cloudcode-pa envelope fields
|
+- Privacy or artifact-sharing question?
   -> Read docs/privacy.md
   -> Prefer ccproxy namespace status --json over raw WireGuard config
   -> Treat tls.keylog, wg.keylog, HAR files, and .mflow captures as sensitive
```

## Reference Files

- `docs/privacy.md` - privacy model, sensitive artifacts, sharing guidance
- `docs/inspect.md` - inspector stack architecture
- `docs/shaping.md` - request shaping system
- `docs/lightllm.md` - request/response transformation internals
- `skills/using-ccproxy-inspector/reference/flow-api-reference.md` - mitmweb
  REST API endpoints, flow data model, content views, authentication
