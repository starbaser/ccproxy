# Inspector Stack Architecture

Inspect mode activates a full transparent MITM stack built on mitmproxy, WireGuard, and Linux network
namespaces. It intercepts and observes all HTTP traffic through the ccproxy pipeline — from CLI clients
and HTTP API consumers through LiteLLM to upstream providers — without modifying the clients or injecting
proxy environment variables.

## 1. Overview

Two commands activate inspect mode:

```
ccproxy start --inspect
ccproxy run --inspect -- <command>
```

`ccproxy start --inspect` launches mitmweb alongside LiteLLM. mitmweb binds three proxy listeners: a
reverse proxy for direct HTTP clients, and two WireGuard servers — one for CLI client confinement
(WG-CLI, port A) and one for gateway-side capture of LiteLLM's outbound provider traffic
(WG-Gateway, port B). Both WireGuard ports are auto-assigned from available UDP ports at startup.

`ccproxy run --inspect -- <command>` creates a rootless user+net namespace, routes it through the WG-CLI
tunnel, and executes the given command inside. All traffic from the confined process is captured by
mitmweb transparently — no `HTTPS_PROXY`, no certificate injection, no client modifications required.

Inspect mode is all-or-nothing. There is no partial activation. If prerequisites are missing,
`ccproxy run --inspect` hard-fails before creating any namespace.

---

## 2. Architecture

### Full traffic topology

```
  ┌─ CLI namespace ──────────────────────────────────────────────┐
  │  confined process (e.g. claude, curl)                        │
  │    wg0 → 10.0.0.1/32   AllowedIPs 0.0.0.0/0                 │
  │    Endpoint 10.0.2.2:A  (slirp4netns gateway rewrite)        │
  └─────────────────────────────┬────────────────────────────────┘
                                │ WireGuard UDP → host port A
                                ▼
  ┌─ mitmweb ────────────────────────────────────────────────────┐
  │  listener 1: reverse:http://localhost:L@R  (inbound HTTP)    │
  │  listener 2: wireguard:keypair-cli@A       (WIREGUARD_CLI)   │
  │  listener 3: wireguard:keypair-gw@B        (WIREGUARD_GW)    │
  │                                                              │
  │  addon chain:                                                │
  │    InspectorAddon (OTel spans)                                │
  │    → inbound InspectorRouter  (OAuth sentinel detection)     │
  │    → outbound InspectorRouter (beta headers, auth failures)  │
  └──────────────┬─────────────────────────────────────────────-┘
                 │ forwarded to localhost:L (inbound flows)
                 │ provider API calls (outbound flows)
                 ▼
  ┌─ LiteLLM namespace ──────────────────────────────────────────┐
  │  LiteLLM binds port L                                        │
  │    wg0 → 10.0.0.1/32   AllowedIPs 0.0.0.0/0                 │
  │    Endpoint 10.0.2.2:B  (slirp4netns gateway rewrite)        │
  │    --port-map L:L/tcp   (LAN-accessible via host port L)     │
  │                                                              │
  │  all outbound provider calls exit via wg0 → WG-Gateway       │
  └──────────────────────────────────────────────────────────────┘

  External HTTP client
    → reverse proxy listener @R → LiteLLM (inbound, no WireGuard)
```

Key:
- `L` — LiteLLM port (default 4001 dev, 4000 prod)
- `R` — reverse proxy port (default 4002)
- `A` — WG-CLI UDP port (auto-assigned at startup)
- `B` — WG-Gateway UDP port (auto-assigned at startup)

### mitmweb process launch

`start_inspector()` in `src/ccproxy/inspector/process.py` launches mitmweb with:

```
mitmweb
  --mode reverse:http://localhost:L@R
  --mode wireguard:<keypair-cli-path>@A
  --mode wireguard:<keypair-gw-path>@B
  -s <inspector/script.py>
  --web-port <UI port>
  ...
```

Both WireGuard ports are found via `_find_free_udp_port()` (binds UDP port 0, reads the assigned port,
closes the socket). The auto-assigned ports are passed to the addon subprocess via env vars
`CCPROXY_INSPECTOR_WG_CLI_PORT` and `CCPROXY_INSPECTOR_WG_GATEWAY_PORT`.

---

## 3. Traffic Direction Model

Every HTTP flow through mitmweb is classified as `"inbound"` or `"outbound"` by
`InspectorAddon._get_direction()`. This determines which route handlers fire and which direction
metadata is attached.

### Detection logic

Direction is derived from `flow.client_conn.proxy_mode` using `isinstance` checks against mitmproxy's
concrete mode dataclasses:

```
ReverseMode                                 → "inbound"
WireGuardMode, port != wg_gateway_port      → "inbound"   (WIREGUARD_CLI)
WireGuardMode, port == wg_gateway_port      → "outbound"  (WIREGUARD_GW)
anything else                               → None (flow ignored)
```

The listen port is read from `mode.custom_listen_port` — a typed dataclass field on `WireGuardMode`.
The gateway port is the value of `CCPROXY_INSPECTOR_WG_GATEWAY_PORT` received at addon load time.

### Direction type

Direction is typed as `Literal["inbound", "outbound"]` (see `addon.py` line 33). There is no enum.
The string value is stored in `flow.metadata[InspectorMeta.DIRECTION]` for route handlers to read.

### Direction semantics

| Direction | Source flows | Route handling |
|-----------|--------------|----------------|
| `"inbound"` | CLI via WireGuard (WIREGUARD_CLI) | OAuth sentinel detection, token substitution |
| `"inbound"` | Direct HTTP client via reverse proxy | OAuth sentinel detection, token substitution |
| `"outbound"` | LiteLLM → provider (WIREGUARD_GW) | Beta header merge, auth failure observation |

---

## 4. xepor Routing Framework

Route handlers are registered on `InspectorRouter` instances using a Flask-style decorator API.
xepor is vendored at version 0.6.0 with two compatibility fixes applied.

### InspectorRouter

`InspectorRouter` is a subclass of xepor's `InterceptedAPI` defined in
`src/ccproxy/inspector/router.py`. It adds three things:

**1. `name` attribute** — mitmproxy's `AddonManager` uses addon names to detect collisions.
Multiple `InterceptedAPI` instances would all have the same default name, causing the second
instance to be rejected. `InspectorRouter.__init__` accepts `name: str` and assigns it.

**2. `find_handler` override** — upstream xepor's route lookup uses `h != host` to skip non-matching
host entries. Routes registered with `host=None` (wildcard) are skipped by this check because
`None != host` is always true. The override treats `h is None` as "match any host":

```python
for h, parser, handler in routes:
    if h is not None and h != host:
        continue
    ...
```

**3. `remap_host` override** — mitmproxy 12.x made `Server` a `kw_only=True` dataclass. xepor calls
`Server((dest, port))` with a positional argument, which raises `TypeError`. The fix calls
`Server(address=(dest, port))`.

### Addon chain

The addon chain is built by `_build_addons()` in `src/ccproxy/inspector/process.py`:

```python
addons = [
    InspectorAddon(...),        # OTel span lifecycle — must fire first
    _make_inbound_router(),     # OAuth sentinel detection (request phase)
    _make_outbound_router(),    # Beta headers + auth failure (request+response phases)
]
```

Each addon receives mitmproxy lifecycle events in list order. `InspectorAddon` must be first so
that OTel spans are started before route handlers mutate headers.

### Route registration

Routes are registered with the `parse` library for path matching. The `parse` library uses Python
format-string syntax (`{param}` captures), not regex. A wildcard catch-all is registered for all
paths:

```python
@router.route("/{path}", rtype=RouteType.REQUEST)
def handle_inbound(flow: HTTPFlow, **kwargs: object) -> None:
    ...
```

Both routers are constructed with `request_passthrough=True` and `response_passthrough=True` so
unmatched flows pass through without being blocked.

---

## 5. Flow Store and Cross-Pass State

A single logical request from a CLI client traverses mitmweb twice — once inbound
(client → LiteLLM) and once outbound (LiteLLM → provider). These are two separate `HTTPFlow`
objects with no shared identity in mitmproxy. The flow store bridges them.

### FlowRecord

`FlowRecord` is the primary cross-pass state container (defined in
`src/ccproxy/inspector/flow_store.py`):

```python
@dataclass
class FlowRecord:
    direction: Literal["inbound", "outbound"]
    auth: AuthMeta | None = None
    otel: OtelMeta | None = None
    original_headers: dict[str, str] = field(default_factory=dict)
```

- `auth` — filled by inbound OAuth route handler, read by outbound auth failure handler
- `otel` — span lifecycle (start/end) tracked per logical request
- `original_headers` — request headers at inbound time, before any mutation

### AuthMeta

Written by the inbound route handler when an OAuth sentinel key is detected:

```python
@dataclass
class AuthMeta:
    provider: str       # sentinel suffix (e.g. "anthropic")
    credential: str     # substituted OAuth token
    key_field: str      # header name used ("authorization" or custom)
    injected: bool      # True once header was set on the request
    original_key: str   # the sentinel key value before substitution
```

The outbound route handler reads `record.auth.provider` to include provider context in auth failure
log entries.

### OtelMeta

Holds the OTel span object and its ended flag for a flow:

```python
@dataclass
class OtelMeta:
    span: Any = None
    ended: bool = False
```

### InspectorMeta keys

`InspectorMeta` is a class with two string constants that serve as `flow.metadata` dict keys,
mirroring xepor's own `FlowMeta` enum pattern:

```python
class InspectorMeta:
    RECORD    = "ccproxy.record"     # FlowRecord reference
    DIRECTION = "ccproxy.direction"  # "inbound" or "outbound"
```

### Flow ID propagation

A UUID flow ID is created when a new `FlowRecord` is created, and written into the request as
header `x-ccproxy-flow-id` (the constant `FLOW_ID_HEADER`). LiteLLM passes this header through to
the provider request without stripping it. When the outbound flow fires, the outbound route handler
reads `x-ccproxy-flow-id` from the outbound request headers and calls `get_flow_record()` to
retrieve the same `FlowRecord` that was populated on the inbound pass.

### Store implementation

The store is a module-level `dict[str, tuple[FlowRecord, float]]` protected by a `threading.Lock`.
TTL is 120 seconds. Expired entries are cleaned up eagerly on each `create_flow_record()` call —
no background thread required for a workload of this volume.

```
inbound flow fires
  → create_flow_record("inbound") → UUID, FlowRecord
  → flow.request.headers[FLOW_ID_HEADER] = UUID
  → LiteLLM makes provider call, header preserved
outbound flow fires
  → get_flow_record(UUID) → same FlowRecord
  → record.auth.provider available for logging
```

---

## 6. OAuth Dual-Layer Architecture

OAuth handling runs at two independent layers. The mitmproxy layer is the primary handler in
inspect mode. The LiteLLM layer is the fallback for non-inspect mode.

### mitmproxy layer (inbound route handler)

Handles OAuth for ALL inbound flows regardless of client type. Sentinel key detection runs on
both WIREGUARD_CLI flows and reverse-proxy HTTP flows.

The sentinel key scheme: SDK clients configure `sk-ant-oat-ccproxy-{provider}` as their API key.
The inbound handler detects the `OAUTH_SENTINEL_PREFIX` prefix, extracts the provider suffix,
looks up the cached OAuth token from `oat_sources` config, and substitutes the real credential
before the request reaches LiteLLM.

After substitution:
- `x-ccproxy-oauth-injected: 1` is set on the request
- `AuthMeta` is written to the `FlowRecord`

### LiteLLM layer (forward_oauth hook)

The `forward_oauth` pipeline hook performs the same OAuth substitution at the LiteLLM hook
pipeline level. It checks for the `x-ccproxy-oauth-injected` header first:
- Header present → skip (mitmproxy layer already handled it)
- Header absent → run full OAuth pipeline (non-inspect mode fallback)

### Provider model

Both layers are provider-agnostic. No provider hostnames or paths are hardcoded. Provider identity
is determined entirely by the sentinel key suffix and the corresponding `oat_sources` entry in
`ccproxy.yaml`. The target auth header name per provider is configurable via `auth_header` in the
oat_sources config.

---

## 7. Route Handlers

### Inbound routes (`src/ccproxy/inspector/routes/inbound.py`)

One handler covers all paths on all hosts (`/{path}`, `host=None` wildcard):

```
handle_inbound (RouteType.REQUEST)
  ├── guard: flow must be inbound (ReverseMode or WireGuardMode)
  ├── read x-api-key header
  ├── check prefix == OAUTH_SENTINEL_PREFIX
  ├── extract provider from suffix
  ├── look up OAuth token from config.oat_sources
  ├── write AuthMeta to FlowRecord
  ├── substitute token into request headers
  └── set x-ccproxy-oauth-injected: 1
```

If the sentinel key is present but no token is found in `oat_sources`, the handler raises
`OAuthConfigError` with a descriptive message rather than silently passing the sentinel key
to the provider.

If `auth_header` is configured for the provider, the token is written to that header directly
(e.g. `x-api-key = <token>`). Otherwise, `authorization: Bearer <token>` is used and
`x-api-key` is cleared.

### Outbound routes (`src/ccproxy/inspector/routes/outbound.py`)

Two handlers cover the outbound leg. Both are guarded by a direction check:
`flow.metadata[InspectorMeta.DIRECTION] == "outbound"`.

**ensure_beta_headers (RouteType.REQUEST)**

Idempotent `anthropic-beta` header merge. If the header is absent entirely, the handler
does nothing (the LiteLLM-side `add_beta_headers` hook already set it). If the header is
present, the handler merges the configured `ANTHROPIC_BETA_HEADERS` list with the existing
value, deduplicates while preserving order, and writes the merged list back.

**observe_auth_failure (RouteType.RESPONSE)**

Watches for 401 and 403 responses. When detected, logs a structured warning with provider
context from `record.auth.provider` (read via `InspectorMeta.RECORD` from the flow metadata,
which was populated by `ensure_beta_headers` in the same flow).

---

## 8. TLS Key Log

mitmproxy natively supports the [NSS Key Log format](https://firefox-source-docs.mozilla.org/security/nss/legacy/key_log_format/index.html)
via the `MITMPROXY_SSLKEYLOGFILE` environment variable. ccproxy sets this automatically when
`--inspect` is active, writing TLS master secrets to `{config_dir}/tls.keylog`.

### Mechanism

`mitmproxy.net.tls` reads `MITMPROXY_SSLKEYLOGFILE` at module import time (module-level global).
The env var must be set before any mitmproxy module that triggers `mitmproxy.net.tls` is imported.
ccproxy sets it at the top of `_run_inspect()` in `cli.py`, before the `run_inspector()` call
which triggers `WebMaster` import.

`MITMPROXY_SSLKEYLOGFILE` is preferred over the generic `SSLKEYLOGFILE` to avoid affecting
Python's `ssl` module, browsers, or other TLS libraries.

### Scope

In WireGuard mode, the TLS sessions mitmproxy intercepts are the inner TLS connections (e.g.,
to `api.anthropic.com`). Combined with the WireGuard keylog (`wg.keylog`) that decrypts the
outer tunnel, a complete packet capture can be fully decrypted in Wireshark.

### Wireshark usage

1. Capture traffic (e.g., `tcpdump -i any -w capture.pcap`)
2. Open in Wireshark
3. Decrypt outer WireGuard: Edit → Preferences → Protocols → WireGuard → Key log file → `{config_dir}/wg.keylog`
4. Decrypt inner TLS: Edit → Preferences → Protocols → TLS → (Pre)-Master-Secret log filename → `{config_dir}/tls.keylog`

Both paths are printed to stdout at inspector startup.

---

## 9. WireGuard Keylog Export

`src/ccproxy/inspector/wg_keylog.py` exports WireGuard static private keys in Wireshark's
`wg.keylog_file` format so that packet captures of the outer WireGuard tunnel layer can be
decrypted.

### Format

```
LOCAL_STATIC_PRIVATE_KEY = <base64>
LOCAL_STATIC_PRIVATE_KEY = <base64>   (client key, if present)
```

mitmproxy writes its WireGuard keypair to `wireguard.{pid}.conf` as JSON. `write_wg_keylog()`
reads `server_key` (and optionally `client_key`) from that file and writes the Wireshark keylog
format to `{config_dir}/wg.keylog`. The output path is logged at inspector startup.

### Scope

This decrypts only the outer WireGuard UDP tunnel. Inner TLS sessions are separately decrypted
via the TLS keylog at `{config_dir}/tls.keylog` (see Section 8).

---

## 10. OpenTelemetry Integration

`src/ccproxy/inspector/telemetry.py` implements OTel span emission for inspector flows with
three-mode graceful degradation:

| Mode | Condition | Behavior |
|------|-----------|----------|
| Real OTLP export | `ccproxy.otel.enabled=true` + packages installed | Spans exported via gRPC |
| No-op tracer | `enabled=false` + API package present | Zero overhead, no exports |
| Stub | OTel packages absent | No imports, zero overhead |

### Span lifecycle

`InspectorScript` initializes `InspectorTracer` in the `running()` hook (async, after mitmweb is
fully started). Spans are started in `InspectorAddon.request()` and ended in
`InspectorAddon.response()` or `InspectorAddon.error()`.

The tracer stores spans in `FlowRecord.otel` (an `OtelMeta` instance) when a `FlowRecord` is
present in `flow.metadata`. For flows without a record, spans fall back to direct storage in
`flow.metadata["ccproxy.otel_span"]`. The `_get_span()` and `_mark_ended()` methods implement
this dual dispatch:

```python
def _get_span(self, flow):
    record = flow.metadata.get(InspectorMeta.RECORD)
    if record and record.otel:
        return record.otel.span, record.otel.ended
    return flow.metadata.get("ccproxy.otel_span"), ...
```

### Span attributes

Each span includes HTTP semantics attributes (`http.request.method`, `url.full`, `server.address`,
`server.port`, `url.path`, `url.scheme`), ccproxy-specific attributes
(`ccproxy.proxy_direction`, `ccproxy.trace_id`, `ccproxy.session_id` when extracted from
`metadata.user_id`), and GenAI semantic convention attributes (`gen_ai.system`,
`gen_ai.operation.name`) for flows to known provider hosts.

### Configuration

OTel config lives under `ccproxy.otel` in `ccproxy.yaml` and is loaded in `InspectorScript.load()`:

```yaml
ccproxy:
  otel:
    enabled: true
    endpoint: "http://localhost:4317"
    service_name: "ccproxy"
```

The Jaeger container in `compose.yaml` accepts OTLP gRPC on port 4317 and serves the trace UI
on port 16686.

---

## 11. Network Namespace Confinement

### CLI namespace

`create_namespace()` in `src/ccproxy/inspector/namespace.py` creates a rootless network namespace
for confining CLI clients such as `claude`. Steps:

1. Write a modified WireGuard client config with the endpoint host rewritten from the mitmweb
   listen address to `10.0.2.2` (the slirp4netns NAT gateway), preserving the port.
   `Address` and `DNS` lines are stripped (wg-quick extensions not understood by `wg setconf`).
2. Start a sentinel process (`sleep infinity`) via `unshare --user --map-root-user --net --pid --fork`.
3. Start `slirp4netns --configure --mtu=65520 --ready-fd=N --exit-fd=M --api-socket=<path> <ns_pid> tap0`.
   This creates a TAP device in the namespace (`10.0.2.100/24`) and NATs it to the host network.
4. Block on `ready-fd` until slirp4netns signals the TAP interface is ready.
5. Run WireGuard setup inside the namespace via `nsenter`:
   ```
   ip link add wg0 type wireguard
   wg setconf wg0 <conf_path>
   ip addr add 10.0.0.1/32 dev wg0
   ip link set wg0 up
   ip route del default
   ip route add default dev wg0
   ```
6. Install iptables DNAT rule on `tap0` to redirect slirp4netns hostfwd traffic to `127.0.0.1`
   (enables OAuth callback servers inside the namespace to receive connections forwarded from the host).
7. Start `PortForwarder` — polls `/proc/{ns_pid}/net/tcp` every 500ms and calls the slirp4netns
   API to forward newly-appearing LISTEN ports from the namespace to the host.

### Gateway namespace

`create_gateway_namespace()` confines LiteLLM rather than a CLI client. It differs from
`create_namespace()` in two ways:

- Adds `--port-map=L:L/tcp` to the slirp4netns command, making LiteLLM's port available on the
  host for external HTTP clients and direct health probes.
- Does not start `PortForwarder` — LiteLLM's port is known upfront.

LiteLLM's outbound provider calls exit the namespace via `wg0 → 10.0.2.2:B → mitmweb`, where
`B` is the WG-Gateway port. This eliminates the `HTTPS_PROXY` environment variable previously
required for LiteLLM outbound capture.

### Slirp4netns network topology

| Address | Role |
|---------|------|
| `10.0.2.100/24` | Namespace TAP interface (`tap0`) |
| `10.0.2.2` | Host gateway (slirp4netns NAT) |
| `10.0.2.3` | Built-in DNS forwarder (libslirp) |
| `10.0.0.1/32` | WireGuard client address (`wg0`) |

### Loop prevention

WireGuard's UDP packets from inside the namespace are destined for `10.0.2.2:A` (or `10.0.2.2:B`
for the gateway namespace). slirp4netns routes these to the host's loopback or network stack
as ordinary UDP — they reach the mitmweb WireGuard listener on the host. mitmweb then forwards
the decrypted inner traffic out the host's normal network. mitmweb's own outbound packets never
re-enter any WireGuard tunnel.

### Lifecycle management

Both `create_namespace()` and `create_gateway_namespace()` return a `NamespaceContext`:

```python
@dataclasses.dataclass
class NamespaceContext:
    ns_pid: int                        # sentinel process PID
    slirp_proc: subprocess.Popen      # slirp4netns bridge
    exit_w: int                        # write end of exit-fd pipe
    wg_conf_path: Path                 # temp WireGuard config file
    api_socket: Path | None            # slirp4netns API socket
    port_forwarder: PortForwarder | None
```

`cleanup_namespace()` tears down resources in order:

1. Stop `PortForwarder` if active
2. Close `exit_w` — slirp4netns detects HUP on `exit-fd` and exits cleanly
3. Wait up to 2 seconds; SIGKILL slirp4netns if it doesn't exit
4. SIGKILL the sentinel and reap with `waitpid`
5. Remove the temp WireGuard config file
6. Remove the slirp4netns API socket if still present

### Prerequisites

`check_namespace_capabilities()` validates the runtime environment before namespace creation:

| Requirement | Check |
|-------------|-------|
| Unprivileged user namespaces | `/proc/sys/kernel/unprivileged_userns_clone == 1` |
| `slirp4netns` | `shutil.which("slirp4netns")` |
| `unshare` | `shutil.which("unshare")` |
| `nsenter` | `shutil.which("nsenter")` |
| `ip` | `shutil.which("ip")` |
| `wg` | `shutil.which("wg")` |

All are rootless on Linux 5.6+ with unprivileged user namespaces enabled. NixOS with kernel
6.18+ satisfies these requirements by default.

---

## 12. SSL/TLS Certificate Handling

### Combined CA bundle

The confined CLI client and the gateway namespace (LiteLLM) both need to trust mitmproxy's CA
so that TLS interception succeeds. The combined CA bundle is built **after** mitmweb starts
(to ensure the mitmproxy CA cert exists) by concatenating the mitmproxy CA cert with the system
CA bundle.

The combined bundle is then applied inside the gateway namespace by setting four environment
variables before launching LiteLLM:

```
SSL_CERT_FILE          = <combined bundle path>
REQUESTS_CA_BUNDLE     = <combined bundle path>
CURL_CA_BUNDLE         = <combined bundle path>
NODE_EXTRA_CA_CERTS    = <combined bundle path>
```

This covers Python `ssl` (urllib3, httpx), `requests`, `curl`, and Node.js clients.

### Reverse proxy leg

Direct HTTP clients connecting to mitmweb's reverse proxy listener on port `R` use plain HTTP
over localhost. No TLS is involved on that leg — the reverse proxy terminates at mitmweb and
mitmweb forwards to LiteLLM on `localhost:L` over plain HTTP.

### SSL_CERT_FILE validation

On startup, ccproxy validates that `SSL_CERT_FILE` points to an existing file. If the path does
not exist (stale venv after a Python upgrade, for example), it falls back in order to:
`certifi.where()`, then `/etc/ssl/certs/ca-certificates.crt`.

---

## Source File Map

| Path | Role |
|------|------|
| `src/ccproxy/inspector/addon.py` | `InspectorAddon` — direction detection, flow store integration, OTel delegation |
| `src/ccproxy/inspector/flow_store.py` | `FlowRecord`, `AuthMeta`, `OtelMeta`, `InspectorMeta`, TTL store |
| `src/ccproxy/inspector/router.py` | `InspectorRouter` — xepor subclass with mitmproxy 12.x fixes |
| `src/ccproxy/inspector/routes/inbound.py` | OAuth sentinel detection and token substitution |
| `src/ccproxy/inspector/routes/outbound.py` | Beta header merge, auth failure observation |
| `src/ccproxy/inspector/wg_keylog.py` | WireGuard keylog export for Wireshark |
| `src/ccproxy/inspector/namespace.py` | Network namespace confinement, `PortForwarder`, lifecycle |
| `src/ccproxy/inspector/process.py` | mitmweb process launch and env construction |
| `src/ccproxy/inspector/telemetry.py` | OTel span emission, three-mode degradation |
| `stubs/xepor/__init__.pyi` | xepor type stub — API surface for `InterceptedAPI` |

---

## Troubleshooting

### Unprivileged user namespaces disabled

```
Error: Unprivileged user namespaces disabled (kernel.unprivileged_userns_clone=0)
```

Enable temporarily:

```bash
sudo sysctl -w kernel.unprivileged_userns_clone=1
```

Persist in NixOS:

```nix
boot.kernel.sysctl."kernel.unprivileged_userns_clone" = 1;
```

### Missing tools

```bash
nix profile install nixpkgs#slirp4netns nixpkgs#util-linux nixpkgs#iproute2 nixpkgs#wireguard-tools
```

Or add to the devShell packages in `flake.nix`.

### Traffic not appearing in mitmweb

- Confirm the confined process connects to remote hosts — loopback traffic bypasses the WireGuard
  tunnel
- Verify the combined CA bundle is being used by the confined process — check `SSL_CERT_FILE`
  in the namespace environment
- Check mitmweb logs for WireGuard handshake errors (look for `[inspector]` prefixed lines)
- For Wireshark analysis: use `{config_dir}/wg.keylog` to decrypt the outer WireGuard tunnel
  and `{config_dir}/tls.keylog` to decrypt inner TLS sessions (both paths printed at startup)

### OAuth token not substituted

If `x-ccproxy-oauth-injected` is absent from LiteLLM-bound requests, the inbound route handler
did not fire or found no matching `oat_sources` entry. Check:

- The request `x-api-key` header starts with `sk-ant-oat-ccproxy-`
- The provider suffix matches an `oat_sources` key in `ccproxy.yaml`
- The flow direction resolves to `"inbound"` — check `flow.metadata["ccproxy.direction"]` in
  mitmweb flow details

### WireGuard setup failed in namespace

```
RuntimeError: WireGuard setup failed in namespace: <stderr>
```

The `nsenter` + `ip`/`wg` command sequence failed. The full stderr is included in the message.
Common causes: WireGuard kernel module not loaded (`modprobe wireguard`), or `ip`/`wg` not in
PATH inside the namespace. Verify tools are available before `ccproxy run --inspect`.
