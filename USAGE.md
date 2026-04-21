# ccproxy Usage Guide

ccproxy is a transparent LLM API interceptor built on mitmproxy.
It embeds mitmweb in-process, intercepts HTTP traffic from any LLM client, and
feeds it through a configurable pipeline that can observe, rewrite, and re-route
requests between providers.
Two entry points serve different use cases: a reverse proxy for SDK clients and
a WireGuard tunnel for full transparent capture of arbitrary processes.

* * *

## 1. Getting Started

### Install configuration

```bash
ccproxy init              # writes ~/.config/ccproxy/ccproxy.yaml
ccproxy init --force      # overwrite existing config
```

Edit `~/.config/ccproxy/ccproxy.yaml` to configure transform rules, OAuth sources, and
hooks. The config directory can be overridden with `--config PATH` or the
`CCPROXY_CONFIG_DIR` environment variable.

### Start the server

```bash
ccproxy start
```

Runs in the foreground.
The server binds two listeners:

- **Reverse proxy** on the configured port (default `4000`) for SDK clients.
- **WireGuard UDP tunnel** on an auto-assigned port for namespace-jailed
  processes.

The mitmweb UI URL (with auth token) is printed at startup.
Use process-compose or systemd for background supervision.

### Check status

```bash
ccproxy status            # rich table: proxy, inspector, config, logs
ccproxy status --json     # machine-readable JSON
ccproxy status --proxy    # health check: exit 0 if proxy is up, 1 if down
ccproxy status --inspect  # health check: exit 0 if inspector is up, 2 if down
```

Health check flags use a bitmask: `--proxy --inspect` exits 0 only if both are
healthy, 3 if both are down.

### View logs

```bash
ccproxy logs              # auto-discovers: systemd journal, process-compose, or log file
ccproxy logs -f           # follow
ccproxy logs -n 50        # last 50 lines
```

* * *

## 2. Two Entry Points

Every flow enters ccproxy through one of two listeners.
The entry point determines how the flow is treated by the pipeline.

### Reverse proxy

SDK clients point their base URL at ccproxy:

```bash
ccproxy run -- my-tool          # sets ANTHROPIC_BASE_URL, OPENAI_BASE_URL, OPENAI_API_BASE
```

Or set the environment manually:

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:4000
export OPENAI_BASE_URL=http://127.0.0.1:4000
```

The client sends requests to ccproxy as if it were the provider.
Transform rules determine where the request actually goes.
Unmatched reverse proxy flows receive a `501` error — there is no default
upstream since the placeholder backend (`localhost:1`) is intentionally invalid.

### WireGuard namespace jail

For full transparent capture of all outbound traffic from a process:

```bash
ccproxy run --inspect -- claude --model haiku -p "hello"
ccproxy run -i -- aider --model claude-3-haiku
```

This creates a rootless Linux network namespace (no root required on Linux 5.6+
with unprivileged user namespaces enabled), routes all TCP/UDP traffic through a
WireGuard tunnel into mitmproxy, and injects a combined CA bundle so TLS
interception works transparently.
The confined process has no direct internet access — everything exits through
the WireGuard tunnel and passes through the full addon pipeline.

Unmatched WireGuard flows pass through to their original destination unchanged,
so the subprocess works normally even for traffic that ccproxy has no transform
rules for.

**Requirements**: `ccproxy start` must be running.
The following tools must be in PATH: `slirp4netns`, `unshare`, `nsenter`, `ip`,
`wg`. NixOS with kernel 6.18+ satisfies these by default.

### Key differences

|  | Reverse Proxy | WireGuard Namespace |
| --- | --- | --- |
| **How traffic arrives** | Client sets `base_url` to ccproxy | All traffic captured transparently |
| **Client modification** | Requires `base_url` env var | None — process is unaware of ccproxy |
| **Unmatched flows** | 501 error | Pass through unchanged |
| **Shaping observation** | Not observed (consumer of profiles) | Always observed (reference traffic) |
| **Shaping application** | Applied (when transform matched) | Not applied |
| **TLS** | Client connects via plain HTTP | mitmproxy intercepts and re-signs with its CA |

* * *

## 3. The Pipeline

Every request passes through a fixed five-stage addon chain:

```
┌────────────────┐
│  ReadySignal   │  Startup synchronization
└───────┬────────┘
        │
┌───────▼────────┐
│ InspectorAddon │  Flow capture, OTel spans, client request snapshot, SSE streaming
└───────┬────────┘
        │
┌───────▼────────┐
│ Inbound Hooks  │  OAuth token injection, session ID extraction
└───────┬────────┘
        │
┌───────▼────────┐
│   Transform    │  Route matching, provider dispatch (passthrough / redirect / transform)
└───────┬────────┘
        │
┌───────▼────────┐
│ Outbound Hooks │  MCP notification injection, verbose mode, shaping application
└───────┘────────┘
        │
        ▼
   Provider API
```

### InspectorAddon

The first real addon in the chain.
Before any hook touches the request, it captures a complete snapshot of the
original client request (method, URL, headers, body).
This snapshot is the ground truth of what the client sent and is used for:

- **Shaping observation** — learning what a reference client sends.
- **Client Request content view** — visible in the mitmweb UI under the
  "Client-Request" tab.
- **`ccproxy flows compare`** — diffing what the client sent vs what the
  pipeline forwarded.
- **HAR export** — each flow's HAR page includes both the forwarded and client
  request.

InspectorAddon also manages OTel span lifecycle and enables SSE streaming on
responses with `content-type: text/event-stream`.

### Inbound hooks

Run before the transform stage.
Default hooks:

- **`forward_oauth`** — Detects sentinel API keys (see
  [OAuth](#5-oauth-and-sentinel-keys)) and substitutes real tokens from
  configured credential sources.
- **`extract_session_id`** — Parses `metadata.user_id` from the request body and
  stores the session ID for downstream hooks (MCP notification injection).

### Transform

Matches the request against `inspector.transforms` rules (first match wins) and
dispatches in one of three modes.
See [Transform Rules](#4-transform-rules).

### Outbound hooks

Run after the transform stage.
Default hooks:

- **`inject_mcp_notifications`** — Drains buffered MCP terminal events for the
  current session and injects them as synthetic tool_use/tool_result message
  pairs.
- **`verbose_mode`** — Strips `redact-thinking-*` from the `anthropic-beta`
  header to enable full thinking block output from Anthropic models.
- **`apply_shaping`** — Stamps the learned shaping profile onto reverse
  proxy flows (headers, body envelope, system prompt).
  Only fires on flows that matched a transform rule.

### Hook execution

Hooks declare data dependencies (`reads` and `writes`) and are sorted into a DAG
via topological sort.
Hooks that don't depend on each other can run in parallel.
Errors in one hook don't block others — the sole exception is
`OAuthConfigError`, which is fatal and propagates through the pipeline.

Hooks can be configured per-request via the `x-ccproxy-hooks` header:

```
x-ccproxy-hooks: +extra_hook,-verbose_mode
```

`+` force-runs a hook, `-` force-skips it.

* * *

## 4. Transform Rules

Transform rules live under `inspector.transforms` in the config.
Each rule defines match criteria and a dispatch mode.
Rules are evaluated in order; first match wins.

### Matching

All match fields are optional and combined with AND logic:

- `match_host` — checked against the request's host, `Host` header, and
  `X-Forwarded-Host`.
- `match_path` — URL prefix match (default `/` matches everything).
- `match_model` — substring match on the `model` field in the JSON request body.

### Three modes

**`passthrough`** — Forward to the original destination unchanged.
The request is observed (logged, traced) but not modified.
Useful for WireGuard reference traffic that should flow through transparently.

```yaml
inspector:
  transforms:
    - mode: passthrough
      match_host: cloudcode-pa.googleapis.com
```

**`redirect`** — Rewrite the destination host/port/scheme/path and inject auth
credentials, but preserve the request body format.
For same-format routing where the body is already correct (e.g.
Anthropic-to-Anthropic, Gemini SDK-to-cloudcode-pa).

```yaml
inspector:
  transforms:
    - mode: redirect
      match_path: /v1internal
      dest_host: cloudcode-pa.googleapis.com
      dest_api_key_ref: gemini
```

**`transform`** — Full cross-provider rewrite via lightllm.
Changes the destination URL and rewrites the entire request body from one API
format to another (e.g. OpenAI format to Anthropic format).
The response is also transformed back to the client's expected format.

```yaml
inspector:
  transforms:
    - mode: transform
      match_path: /v1/chat/completions
      match_model: gpt-4o
      dest_provider: anthropic
      dest_model: claude-haiku-4-5-20251001
      dest_api_key_ref: anthropic
```

### Transform rule fields

| Field | Modes | Purpose |
| --- | --- | --- |
| `mode` | all | `passthrough`, `redirect`, or `transform` (default: `redirect`) |
| `match_host` | all | Hostname match (optional) |
| `match_path` | all | URL prefix match (default: `/`) |
| `match_model` | all | Model substring match (optional) |
| `dest_provider` | redirect, transform | Provider name (e.g. `anthropic`, `gemini`) |
| `dest_model` | transform | Destination model name |
| `dest_host` | redirect | Explicit destination host |
| `dest_path` | redirect | Override request path |
| `dest_api_key_ref` | redirect, transform | Provider name in `oat_sources` for auth |
| `dest_vertex_project` | transform | GCP project ID (Vertex AI) |
| `dest_vertex_location` | transform | GCP region (Vertex AI) |

### Response handling

- **Non-streaming responses** with a matched transform rule are converted back
  to OpenAI format before being sent to the client.
- **SSE streaming responses** use an `SseTransformer` that parses SSE events
  from the upstream provider and re-serializes them as OpenAI-format SSE chunks
  in real time.
- **Passthrough and redirect** responses are forwarded unchanged.

* * *

## 5. OAuth and Sentinel Keys

ccproxy uses sentinel API keys to trigger automatic token substitution.
A sentinel key is a special value that signals ccproxy to look up the real
credential from a configured source.

### Sentinel format

```
sk-ant-oat-ccproxy-{provider}
```

For example, `sk-ant-oat-ccproxy-anthropic` tells the `forward_oauth` hook to
resolve the real token from `oat_sources.anthropic`.

### Configuring token sources

```yaml
oat_sources:
  anthropic:
    command: "cat ~/.anthropic/oauth_token"
  gemini:
    file: "~/.config/gemini/oauth_token"
  openai:
    command: "op read 'op://vault/openai/api_key'"
    auth_header: "authorization"
```

Each source can be a shell `command` or a `file` path.
Optional fields:

- `auth_header` — target header name (default: `authorization` with `Bearer`
  prefix; set to `x-api-key` for raw injection).
- `user_agent` — custom User-Agent for requests using this token.
- `destinations` — URL patterns that should use this token.

### 401 retry

When a response returns 401 and the request used an OAuth-injected token,
ccproxy automatically re-resolves the credential source.
If the token has changed (e.g. refreshed externally), the request is retried
with the new token. If unchanged, the failure propagates — the credential is
genuinely stale.

* * *

## 6. Shaping Profiles

The shaping system passively learns the exact request shape that a reference
client (observed via WireGuard) sends to each provider, then stamps that shape
onto SDK requests arriving through the reverse proxy.

### Why

LLM providers increasingly enforce client identity.
Requests from Claude Code, for example, carry specific beta headers, system
prompt prefixes, body envelope fields, and session metadata.
When routing SDK traffic through ccproxy, these details are missing.
The shaping system observes what the real client sends, learns a stable
profile, and applies it to proxied requests so they are indistinguishable from
direct client traffic.

### How it works

1. **Observation** — WireGuard flows (and flows matching
   `shaping.reference_user_agents`) are analyzed.
   Headers, body fields, system prompts, and body wrapper structure are
   extracted.

2. **Accumulation** — Per `(provider, user_agent)` pair, features are collected
   across multiple observations (default: 3). Values that vary between
   observations (timestamps, session IDs) are automatically excluded.

3. **Finalization** — Once enough observations are collected, only features with
   identical values across all observations become stable profile features.

4. **Application** — The `apply_shaping` outbound hook applies the profile to
   reverse proxy flows.
   Five operations run in order:
   - **Headers**: add missing headers, union list-valued headers (e.g.
     `anthropic-beta`).
   - **Session metadata**: synthesize `device_id`/`account_uuid` from the
     profile.
   - **Body wrapping**: move the body into the correct wrapper field if the
     provider expects it.
   - **Body envelope fields**: add missing top-level fields (e.g.
     `user_prompt_id`).
   - **System prompt**: inject the profile's system prompt blocks.

### Initial shape

On first startup (when `shaping.seed_anthropic` is true), a hardcoded
Anthropic shape is created with the known beta headers and Claude Code system
prompt prefix. Learned profiles supersede it when they have a newer
timestamp.

### Profile storage

Profiles persist to `{config_dir}/shaping_profiles.json`. This file is
managed automatically — profiles are versioned and written atomically.

### Customizing the merger

The five application operations are implemented as methods on
`ShapingMerger`. To customize, subclass it and set `shaping.merger_class`
in config:

```yaml
shaping:
  merger_class: mypackage.custom_merger.MyMerger
```

* * *

## 7. Inspecting Flows

### mitmweb UI

The inspector UI is available at the URL printed at startup (includes an auth
token). It provides the standard mitmproxy flow list with two additions:

- **Client-Request content view** — a tab showing the pre-pipeline request
  snapshot (what the client originally sent, before any hooks or transforms
  modified it).
- **`ccproxy.clientrequest` command** — returns the client request snapshot as
  structured JSON.

### `ccproxy flows` CLI

All subcommands accept repeatable `--jq FILTER` flags.
Each filter is a jq expression that consumes and produces a JSON array.
Filters chain with `|`. Default filters from `flows.default_jq_filters` config
are applied first.

```bash
# List recent flows
ccproxy flows list
ccproxy flows list --json

# Filter to Anthropic traffic
ccproxy flows list --jq 'map(select(.request.host | endswith("anthropic.com")))'

# Export HAR (opens in Chrome DevTools, Charles, Fiddler)
ccproxy flows dump > all.har

# Diff consecutive request bodies (sliding window)
ccproxy flows diff

# Compare client request vs forwarded request per flow
ccproxy flows compare

# Clear flows
ccproxy flows clear          # clear filtered set
ccproxy flows clear --all    # clear everything
```

### HAR export

`ccproxy flows dump` produces a multi-page HAR 1.2 file.
Each flow becomes one page with two entries:

- **Entry 0** (even index): the forwarded request and response — what was
  actually sent to the provider.
- **Entry 1** (odd index): the client request (reconstructed from the
  pre-pipeline snapshot) paired with the same response.

This lets you compare what the client sent vs what the pipeline forwarded in any
HAR viewer.

### Default flow filters

Configure persistent filters in `ccproxy.yaml`:

```yaml
flows:
  default_jq_filters:
    - 'map(select(.request.host | endswith("anthropic.com")))'
```

* * *

## 8. MCP Notification Buffer

ccproxy exposes a `POST /mcp/notify` endpoint that accepts MCP terminal events:

```json
{"task_id": "...", "session_id": "...", "event": {...}}
```

Events are buffered per task (max 50, FIFO, 600s TTL). The
`inject_mcp_notifications` outbound hook drains the buffer for the current
session and injects events as synthetic tool_use/tool_result pairs before the
final user message in the conversation.
This allows external MCP servers to surface information into the LLM's context
window.

* * *

## 9. Wireshark Decryption

ccproxy exports keylogs for full packet capture decryption.

### Keylog files

At startup, ccproxy writes:

- `{config_dir}/tls.keylog` — TLS master secrets for all intercepted connections
  (inner TLS to provider APIs).
- `{config_dir}/wg.keylog` — WireGuard static private keys for the outer UDP
  tunnel.

### Capture and decrypt

```bash
# Capture traffic
sudo tcpdump -i any -w capture.pcap

# Open in Wireshark, then:
# 1. Decrypt WireGuard: Edit -> Preferences -> Protocols -> WireGuard -> Key log file -> wg.keylog
# 2. Decrypt TLS: Edit -> Preferences -> Protocols -> TLS -> (Pre)-Master-Secret log -> tls.keylog
```

With both keylogs loaded, the entire traffic path is visible: outer WireGuard
UDP packets, inner TLS handshakes, and plaintext HTTP request/response bodies.

* * *

## 10. OpenTelemetry

ccproxy emits OTel spans for every intercepted flow.
Three modes with graceful degradation:

| Mode | Condition | Behavior |
| --- | --- | --- |
| Real OTLP export | `otel.enabled: true` + packages installed | Spans exported via gRPC |
| No-op tracer | `enabled: false` + API packages present | Zero overhead |
| Stub | OTel packages absent | No imports, zero overhead |

### Configuration

```yaml
otel:
  enabled: true
  endpoint: "http://localhost:4317"
  service_name: "ccproxy"
```

### Span attributes

Each span includes HTTP semantics (`http.request.method`, `url.full`,
`server.address`), ccproxy-specific attributes (`ccproxy.proxy_direction`,
`ccproxy.session_id`), and GenAI semantic conventions (`gen_ai.system`,
`gen_ai.operation.name`) for flows to known provider hosts.

The Jaeger container in `compose.yaml` accepts OTLP gRPC on port 4317 and serves
the trace UI on port 16686.

* * *

## 11. WireGuard Namespace Internals

The namespace jail creates a fully isolated network environment routed through
mitmproxy. No root privileges are required.

### Network topology

```
  ┌─ Confined process ─────────────────────────────────┐
  │                                                     │
  │  wg0: 10.0.0.1/32          default route → wg0     │
  │  tap0: 10.0.2.100/24       gateway → 10.0.2.2      │
  │                             DNS → 10.0.2.3          │
  │                                                     │
  └──────────────────┬──────────────────────────────────┘
                     │ WireGuard UDP
                     │ Endpoint: 10.0.2.2:{wg_port}
                     ▼
  ┌─ slirp4netns NAT ──────────────────────────────────┐
  │  10.0.2.2 (gateway) ──────▶ host network           │
  └──────────────────┬──────────────────────────────────┘
                     │
                     ▼
  ┌─ mitmweb WireGuard listener ───────────────────────┐
  │  Decrypts tunnel → feeds into addon chain          │
  └────────────────────────────────────────────────────┘
```

| Address | Role |
| --- | --- |
| `10.0.0.1/32` | WireGuard client interface (`wg0`) |
| `10.0.2.100/24` | Namespace TAP interface (`tap0`) |
| `10.0.2.2` | Host gateway (slirp4netns NAT) — WireGuard endpoint |
| `10.0.2.3` | DNS forwarder (libslirp built-in) |

### Port forwarding

A background thread polls the namespace's `/proc/{pid}/net/tcp` every 0.5
seconds and dynamically forwards new listening ports via the slirp4netns API.
This allows tools that start local servers (e.g. OAuth callback listeners) to
receive connections from the host.

### Localhost routing

Inside the namespace, `127.0.0.1` is isolated loopback — host services are not
reachable there. iptables DNAT rules transparently redirect namespace localhost
traffic to the slirp4netns gateway (`10.0.2.2`), so tools with hardcoded
`127.0.0.1` base URLs work without modification.
When the running ccproxy port differs from the default (4000), a port remap rule
handles the translation.

### TLS trust

`ccproxy run --inspect` builds a combined CA bundle (mitmproxy's CA + system
CAs) and injects it into the subprocess environment via:

```
SSL_CERT_FILE          = <combined bundle>
REQUESTS_CA_BUNDLE     = <combined bundle>
CURL_CA_BUNDLE         = <combined bundle>
NODE_EXTRA_CA_CERTS    = <combined bundle>
```

This covers Python (`ssl`, `urllib3`, `httpx`, `requests`), `curl`, and Node.js
clients.

### Prerequisites

| Requirement | Check |
| --- | --- |
| Unprivileged user namespaces | `/proc/sys/kernel/unprivileged_userns_clone == 1` |
| `slirp4netns` | In PATH |
| `unshare` | In PATH |
| `nsenter` | In PATH |
| `ip` | In PATH |
| `wg` | In PATH |

* * *

## 12. Configuration Reference

Config file: `$CCPROXY_CONFIG_DIR/ccproxy.yaml` (default:
`~/.config/ccproxy/ccproxy.yaml`). Individual fields can be overridden via `CCPROXY_`
prefixed environment variables.

### Top-level

| Field | Default | Description |
| --- | --- | --- |
| `host` | `127.0.0.1` | Bind address |
| `port` | `4000` | Reverse proxy listener port |
| `log_level` | `INFO` | Root logger level (`LOG_LEVEL` env var overrides) |
| `log_file` | `ccproxy.log` | Daemon log file (relative to config dir; `null` disables) |
| `provider_timeout` | `null` | Timeout (seconds) for OAuth retry requests |
| `verify_readiness_on_startup` | `true` | Probe external host at startup |
| `readiness_probe_url` | `https://1.1.1.1/` | Canary URL for startup probe |
| `readiness_probe_timeout_seconds` | `5.0` | Timeout for startup probe |
| `use_journal` | `false` | Route daemon logs to systemd journal |

### `inspector`

| Field | Default | Description |
| --- | --- | --- |
| `port` | `8083` | mitmweb UI port |
| `cert_dir` | `null` | mitmproxy CA certificate store (default: `~/.mitmproxy`) |
| `provider_map` | *(see below)* | Hostname to OTel `gen_ai.system` mapping |
| `transforms` | `[]` | Transform rules (see [Transform Rules](#4-transform-rules)) |
| `mitmproxy` | *(object)* | mitmproxy option overrides |

Default `provider_map`:
```yaml
provider_map:
  api.anthropic.com: anthropic
  api.openai.com: openai
  generativelanguage.googleapis.com: google
  openrouter.ai: openrouter
```

### `inspector.mitmproxy`

| Field | Default | Description |
| --- | --- | --- |
| `confdir` | `null` | CA certificate store directory |
| `ssl_insecure` | `true` | Skip upstream TLS verification |
| `stream_large_bodies` | `1m` | Stream threshold (`512k`, `1m`, `10m`) |
| `body_size_limit` | `null` | Hard body size limit (`null` = unlimited) |
| `web_host` | `127.0.0.1` | mitmweb UI bind address |
| `web_password` | `null` | UI password (string, or `{command:}` / `{file:}` source) |
| `web_open_browser` | `false` | Auto-open browser on start |
| `ignore_hosts` | `[]` | Regex patterns for hosts to bypass |
| `allow_hosts` | `[]` | Regex patterns for hosts to intercept (exclusive) |
| `termlog_verbosity` | `warn` | mitmproxy terminal log level |
| `flow_detail` | `0` | Flow output verbosity (0-4) |

### `oat_sources`

```yaml
oat_sources:
  anthropic:
    command: "cat ~/.anthropic/oauth_token"
  gemini:
    file: "~/.config/gemini/oauth_token"
    auth_header: "x-api-key"
    user_agent: "my-tool/1.0"
    destinations:
      - "generativelanguage.googleapis.com"
```

### `hooks`

```yaml
hooks:
  inbound:
    - ccproxy.hooks.forward_oauth
    - ccproxy.hooks.extract_session_id
  outbound:
    - ccproxy.hooks.inject_mcp_notifications
    - ccproxy.hooks.verbose_mode
    - ccproxy.hooks.apply_shaping
```

Hooks can also be specified with parameters:

```yaml
hooks:
  inbound:
    - hook: ccproxy.hooks.forward_oauth
      params:
        strict: true
```

### `otel`

| Field | Default | Description |
| --- | --- | --- |
| `enabled` | `false` | Enable OTLP span export |
| `endpoint` | `http://localhost:4317` | OTLP gRPC endpoint |
| `service_name` | `ccproxy` | OTel resource service name |

### `shaping`

| Field | Default | Description |
| --- | --- | --- |
| `enabled` | `true` | Enable shaping observation and application |
| `min_observations` | `3` | Observations before profile finalization |
| `reference_user_agents` | `[]` | Additional UA patterns that trigger observation |
| `seed_anthropic` | `true` | Seed a hardcoded Anthropic shape on first run |
| `additional_header_exclusions` | `[]` | Extra headers to exclude from profiling |
| `additional_body_content_fields` | `[]` | Extra body fields to treat as content |
| `merger_class` | `ccproxy.shaping.merger.ShapingMerger` | Merger class path |

### `flows`

| Field | Default | Description |
| --- | --- | --- |
| `default_jq_filters` | `[]` | jq filters pre-applied to all `ccproxy flows` commands |

* * *

## 13. CLI Reference

```
ccproxy start                                  Start inspector server (foreground)
ccproxy init [--force]                         Initialize config files
ccproxy run [--inspect] -- <command> [args...]  Run command with proxy environment
ccproxy status [--json] [--proxy] [--inspect]  Show status / health check
ccproxy logs [-f] [-n N]                       View logs
ccproxy flows list [--json] [--jq FILTER]...   List flows
ccproxy flows dump [--jq FILTER]...            Export multi-page HAR
ccproxy flows diff [--jq FILTER]...            Sliding-window diff across flows
ccproxy flows compare [--jq FILTER]...         Per-flow client-vs-forwarded diff
ccproxy flows clear [--all] [--jq FILTER]...   Clear flows
```

Global options (before any subcommand):
- `--config PATH` — override config directory
- `-v` / `--verbose` — show INFO/DEBUG output on CLI commands

* * *

## 14. Smoke Test

The quickest end-to-end verification:

```bash
ccproxy start &                    # or via process-compose / systemd
ccproxy run --inspect -- claude --model haiku -p "what's 2+2"
```

This exercises: namespace creation, WireGuard tunnel, TLS interception, the full
hook pipeline, transform dispatch, upstream provider call, and SSE streaming
back to the client.
