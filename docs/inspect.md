# Inspector Stack Architecture

Inspect mode activates a full transparent MITM stack built on mitmproxy, WireGuard, and Linux
network namespaces. It intercepts all HTTP traffic through the ccproxy pipeline — from direct API
clients and namespace-jailed subprocesses — without modifying clients or injecting proxy
environment variables.

## 1. Overview

Two commands interact with the inspector:

```
ccproxy start               # Start server — always inspector mode
ccproxy run --inspect -- <command>  # Run subprocess in WireGuard namespace jail
```

`ccproxy start` launches mitmweb in-process via the `WebMaster` API. mitmweb binds two listeners:
a reverse proxy for direct HTTP clients and a WireGuard server for namespace-jailed subprocesses.

`ccproxy run --inspect -- <command>` starts the inspector (if not already running), creates a
rootless user+net namespace routed through the WireGuard listener, and executes the given command
inside. All traffic from the confined process is captured transparently — no `HTTPS_PROXY`, no
certificate injection, no client modifications required.

Inspect mode is all-or-nothing. If prerequisites for `ccproxy run --inspect` are missing,
the command hard-fails before any namespace is created.

---

## 2. Traffic Topology

### Two listeners

mitmweb binds exactly two proxy listeners, configured in `_build_opts()` in
`src/ccproxy/inspector/process.py`:

```python
opts = Options(
    mode=[
        f"reverse:http://localhost:1@{reverse_port}",
        f"wireguard:{wg_cli_conf_path}@{wg_cli_port}",
    ],
)
```

| Listener | Mode string | Purpose |
|----------|-------------|---------|
| Reverse proxy | `reverse:http://localhost:1@{reverse_port}` | Direct HTTP clients (SDK, curl). Placeholder backend (`localhost:1`) is overwritten per-flow by the transform handler. |
| WireGuard CLI | `wireguard:{wg_cli_conf_path}@{wg_cli_port}` | Namespace-jailed subprocesses (`ccproxy run --inspect`). UDP port auto-assigned at startup via `_find_free_udp_port()`. |

The WireGuard port is found by binding to UDP port 0 and reading the kernel-assigned port. This
value is passed to `_build_addons()` as `wg_cli_port` so the addon chain can reference it.

### Traffic flow diagram

```
  ┌─ SDK / curl ────────────────────────────────────────────────────┐
  │  Direct HTTP client (OpenAI-compatible)                         │
  └─────────────────────────────┬───────────────────────────────────┘
                                │ HTTP → reverse proxy listener
                                ▼
  ┌─ mitmweb (in-process) ──────────────────────────────────────────┐
  │  listener 1: reverse:http://localhost:1@{reverse_port}          │
  │  listener 2: wireguard:{wg_cli_conf_path}@{wg_cli_port}         │
  │                                                                 │
  │  addon chain:                                                   │
  │    ReadySignal                                                  │
  │    → InspectorAddon (OTel spans, flow records, SSE streaming)   │
  │    → ccproxy_inbound  (DAG: OAuth, session extraction)          │
  │    → ccproxy_transform (lightllm dispatch)                      │
  │    → ccproxy_outbound (DAG: beta headers, identity injection)   │
  └──────────┬──────────────────────────────────────────────────────┘
             │ transform rewrite: new host/port/body
             ▼
     provider API (Anthropic, Gemini, etc.)

  ┌─ CLI namespace ──────────────────────────────────────────────────┐
  │  confined process (e.g. claude)                                  │
  │    wg0 → 10.0.0.1/32   AllowedIPs 0.0.0.0/0                    │
  │    Endpoint → 10.0.2.2:{wg_cli_port}  (via slirp4netns NAT)     │
  └─────────────────────────────┬────────────────────────────────────┘
                                │ WireGuard UDP → host:{wg_cli_port}
                                ▼
                         WireGuard CLI listener
                         (decrypted, joins addon chain above)
```

Key:
- `{reverse_port}` — configured reverse proxy port (default: `inspector.reverse_port`)
- `{wg_cli_port}` — UDP port auto-assigned at startup

---

## 3. Addon Chain

The addon chain is built by `_build_addons()` in `src/ccproxy/inspector/process.py` and registered
on the `WebMaster` instance. Addons receive mitmproxy lifecycle events in list order.

```
ReadySignal → InspectorAddon → ccproxy_inbound → ccproxy_transform → ccproxy_outbound
```

| Addon | Type | Purpose |
|-------|------|---------|
| `ReadySignal` | Built-in class | Fires `asyncio.Event` when all listeners are bound (after mitmproxy's `RunningHook`). Lets `run_inspector()` block until ports are ready. |
| `InspectorAddon` | `InspectorAddon` | Direction detection, FlowRecord creation, OTel span lifecycle, SSE streaming setup. Must be first so spans open before any route handler mutates headers. |
| `ccproxy_inbound` | `InspectorRouter` (pipeline) | DAG executor for `hooks.inbound` entries — OAuth sentinel substitution, session ID extraction. Skipped if no inbound hooks configured. |
| `ccproxy_transform` | `InspectorRouter` (transform) | lightllm dispatch — matches transform rules, rewrites request to destination provider, handles non-streaming response transform. |
| `ccproxy_outbound` | `InspectorRouter` (pipeline) | DAG executor for `hooks.outbound` entries — beta header merge, Claude Code identity injection, verbose mode. Skipped if no outbound hooks configured. |

The pipeline routers are only added to the chain if the corresponding hook list is non-empty:

```python
if inbound_hooks:
    addons.append(_make_pipeline_router("ccproxy_inbound", inbound_hooks))
addons.append(_make_transform_router())
if outbound_hooks:
    addons.append(_make_pipeline_router("ccproxy_outbound", outbound_hooks))
```

---

## 4. Direction Model

**All flows are `"inbound"`.** There is no outbound direction concept in the inspector. The
"inbound/transform/outbound" naming in the addon chain refers to pipeline stages — processing
order — not traffic direction.

`InspectorAddon._get_direction()` accepts any `ReverseMode` or `WireGuardMode` flow as `"inbound"`,
and returns `None` for anything else (skipped):

```python
Direction = Literal["inbound"]

def _get_direction(self, flow: http.HTTPFlow) -> Direction | None:
    mode = flow.client_conn.proxy_mode
    if isinstance(mode, (ReverseMode, WireGuardMode)):
        return "inbound"
    return None
```

`FlowRecord.direction` is typed as `Literal["inbound"]`. The pipeline route handlers guard on
`flow.metadata.get(InspectorMeta.DIRECTION) != "inbound"` as a sanity check, but this check never
fails in practice since all accepted flows are inbound.

---

## 5. Flow State

### FlowStore

The flow store is a module-level `dict[str, tuple[FlowRecord, float]]` protected by
`threading.Lock`. TTL is 3600 seconds (1 hour). Expired entries are eagerly cleaned up on each
`create_flow_record()` call — no background thread.

Flow IDs propagate via the `x-ccproxy-flow-id` request header (`FLOW_ID_HEADER`). `InspectorAddon`
writes the header on the first pass; subsequent passes (if the flow is replayed or forwarded
internally) retrieve the existing record via `get_flow_record()`.

### FlowRecord

`FlowRecord` is the per-flow cross-phase state container (defined in
`src/ccproxy/flows/store.py`):

```python
@dataclass
class FlowRecord:
    direction: Literal["inbound"]
    auth: AuthMeta | None = None
    otel: OtelMeta | None = None
    client_request: HttpSnapshot | None = None
    provider_response: HttpSnapshot | None = None
    transform: TransformMeta | None = None
```

| Field | Written by | Read by |
|-------|------------|---------|
| `direction` | `InspectorAddon.request()` | Pipeline route guards |
| `auth` | `forward_oauth` hook | (logging context) |
| `otel` | `InspectorAddon.request()` via tracer | `InspectorAddon.response()` / `.error()` |
| `client_request` | `InspectorAddon.request()` | "Client Request" content view, `ccproxy.clientrequest` command |
| `provider_response` | `InspectorAddon.response()` | "Provider Response" content view, `ccproxy.dump` command |
| `transform` | `ccproxy_transform` REQUEST handler | `ccproxy_transform` RESPONSE handler, `responseheaders` |

### InspectorMeta keys

`InspectorMeta` provides string constants for `flow.metadata` dict keys:

```python
class InspectorMeta:
    RECORD    = "ccproxy.record"     # FlowRecord reference
    DIRECTION = "ccproxy.direction"  # "inbound"
```

### AuthMeta

Written by the `forward_oauth` hook when an OAuth sentinel key is detected:

```python
@dataclass
class AuthMeta:
    provider: str       # sentinel suffix (e.g. "anthropic")
    credential: str     # substituted OAuth token
    auth_header: str    # header name used ("authorization" or custom)
    injected: bool      # True once header was set on the request
    original_key: str   # the sentinel key value before substitution
```

### OtelMeta

Holds the OTel span object and its ended flag:

```python
@dataclass
class OtelMeta:
    span: Any = None
    ended: bool = False
```

### TransformMeta

Persisted on `FlowRecord` during the request phase by `ccproxy_transform`, consumed during the
response phase:

```python
@dataclass(frozen=True)
class TransformMeta:
    provider: str               # destination provider (e.g. "anthropic", "gemini")
    model: str                  # destination model name
    request_data: dict[str, Any] # full request body at transform time
    is_streaming: bool          # True if stream=True in the original request
    mode: Literal["redirect", "transform"] = "redirect"
```

### ClientRequest

Full snapshot of the client request before the addon pipeline mutates it. `HttpSnapshot` is a unified frozen dataclass for both request and response snapshots. `ClientRequest` is a type alias for `HttpSnapshot`. Captured by `InspectorAddon.request()` as the first addon in the chain.

```python
@dataclass(frozen=True)
class HttpSnapshot:
    headers: dict[str, str]
    body: bytes
    method: str | None = None
    url: str | None = None
    status_code: int | None = None

ClientRequest = HttpSnapshot  # type alias
```

Accessible via:
- **Content view**: `GET /flows/{id}/request/content/client%20request` — renders full request line, headers, and body
- **Command**: `POST /commands/ccproxy.clientrequest` with `{"arguments": ["@all"]}` — returns structured JSON

---

## 6. SSE Streaming

SSE streaming setup happens in `InspectorAddon.responseheaders()` — the mitmproxy hook that fires
after response headers arrive but before the body. `flow.response.stream` must be set here;
setting it in `response()` is too late (mitmproxy has already buffered the body).

xepor does not implement `responseheaders` — it lives entirely on `InspectorAddon`.

### Decision logic

```
responseheaders fires
  → content-type != text/event-stream  → no-op (buffered by mitmproxy)
  → content-type == text/event-stream
      → record.transform is not None and transform.is_streaming
            → make_sse_transformer(provider, model, optional_params)
            → flow.response.stream = SseTransformer(...)   [cross-provider]
      → else
            → flow.response.stream = True                  [passthrough]
```

**`SseTransformer`** (cross-provider transform): Stateful callable on `flow.response.stream`.
Parses SSE events from the upstream provider, transforms each chunk via LiteLLM's per-provider
`ModelResponseIterator.chunk_parser()`, re-serializes as OpenAI-format SSE.

**Passthrough** (`flow.response.stream = True`): Raw SSE bytes forwarded to the client unchanged —
used for same-provider flows or when no transform rule matched.

If `make_sse_transformer()` raises (e.g. unsupported provider), the handler logs a warning and
falls back to passthrough.

---

## 7. Route Handlers

### InspectorRouter

`InspectorRouter` (defined in `src/ccproxy/inspector/router.py`) is a thin subclass of xepor's
`InterceptedAPI` that adds two compatibility fixes for mitmproxy 12.x:

**1. `name` attribute** — mitmproxy's `AddonManager` uses addon names to detect collisions.
Multiple `InterceptedAPI` instances all share the same default name; the second would be rejected.
`InspectorRouter.__init__` accepts a `name: str` and assigns it directly.

**2. `remap_host` override** — mitmproxy 12.x made `Server` a `kw_only=True` dataclass. xepor's
upstream `remap_host()` calls `Server((dest, port))` with a positional argument. The fix calls
`Server(address=(dest, port))`.

**3. `find_handler` override** — upstream xepor skips routes with `host=None` because
`None != host` is always true. The override treats `h is None` as "match any host" (wildcard).

All routers are constructed with `request_passthrough=True, response_passthrough=True` so
unmatched flows pass through without being blocked.

Routes use `parse` library path templates (`{param}` syntax, not regex):

```python
@router.route("/{path}", rtype=RouteType.REQUEST)
def handle_transform(flow: HTTPFlow, **kwargs: object) -> None:
    ...
```

### Transform routes (`src/ccproxy/inspector/routes/transform.py`)

`register_transform_routes()` installs two handlers on the `ccproxy_transform` router.

**REQUEST handler (`handle_transform`):**

```
handle_transform (RouteType.REQUEST)
  → guard: direction == "inbound"
  → parse body as JSON
  → _resolve_transform_target(flow, body)
      → iterate config.inspector.transforms (first match wins)
      → match_host: checked against pretty_host, Host header, X-Forwarded-Host
      → match_path: prefix match against request path
      → match_model: substring match against body["model"]
  → target is None
      → ReverseMode flow: respond 501 (no default upstream)
      → WireGuard flow: pass through to original destination
  → target.mode == "passthrough"
      → _handle_passthrough(): forward unchanged, log only
  → target.mode == "transform"
      → _handle_transform(): call transform_to_provider() via lightllm
          → rewrites host, port, scheme, path, headers, body
          → persists TransformMeta on FlowRecord
```

**RESPONSE handler (`handle_transform_response`):**

```
handle_transform_response (RouteType.RESPONSE)
  → guard: record.transform is not None
  → guard: transform.is_streaming → return (handled by SseTransformer already)
  → guard: response status < 400
  → transform_to_openai(model, provider, MitmResponseShim(flow.response), ...)
      → MitmResponseShim duck-types httpx.Response for mitmproxy's flow.response
  → rewrite flow.response.content to OpenAI JSON
  → set content-type: application/json, strip content-encoding
```

### Pipeline routes (`src/ccproxy/inspector/pipeline.py`)

`register_pipeline_routes()` installs a single REQUEST handler on each pipeline router:

```
handle_pipeline (RouteType.REQUEST)
  → guard: direction == "inbound"
  → executor.execute(flow)   ← runs DAG-ordered hooks, calls ctx.commit() at end
```

The `PipelineExecutor` resolves hook dependencies via `HookDAG` (Kahn's algorithm), runs hooks in
topological order, and calls `ctx.commit()` to flush body mutations. Hook errors are isolated — one
failing hook does not block others. `OAuthConfigError` is the sole exception to this rule (it
propagates through the pipeline and is treated as fatal).

---

## 8. Namespace Jail

`ccproxy run --inspect -- <command>` confines a subprocess in a rootless user+net namespace, routed
entirely through mitmweb's WireGuard listener. All traffic from the subprocess is captured
transparently.

### Setup sequence (`create_namespace()`)

```
1. _rewrite_wg_endpoint(client_conf, gateway="10.0.2.2")
      → strip Address/DNS lines (wg-quick-only, not understood by wg setconf)
      → rewrite Endpoint host to 10.0.2.2 (slirp4netns NAT gateway), preserve port

2. Write modified config to tempfile

3. unshare --user --map-root-user --net --pid --fork sleep infinity
      → creates sentinel process in new user+net namespace
      → ns_pid = sentinel.pid

4. slirp4netns --configure --mtu=65520 --ready-fd=N --exit-fd=M
               --api-socket=<path> {ns_pid} tap0
      → bridges namespace tap0 to host network via NAT
      → blocks on ready-fd until TAP is configured

5. nsenter -t {ns_pid} --net --user --preserve-credentials -- sh -c "
      ip link add wg0 type wireguard &&
      wg setconf wg0 {conf_path} &&
      ip addr add 10.0.0.1/32 dev wg0 &&
      ip link set wg0 up &&
      ip route del default &&
      ip route add default dev wg0"
      → all namespace traffic exits via wg0

6. nsenter iptables DNAT rule on tap0
      → redirects slirp4netns hostfwd traffic to 127.0.0.1 (OAuth callbacks)

7. PortForwarder.start()
      → background thread polls /proc/{ns_pid}/net/tcp every 0.5s
      → calls slirp4netns add_hostfwd API for new LISTEN ports
```

### Namespace network topology

| Address | Role |
|---------|------|
| `10.0.2.100/24` | Namespace TAP interface (`tap0`) |
| `10.0.2.2` | Host gateway (slirp4netns NAT) — WireGuard endpoint rewritten to this |
| `10.0.2.3` | Built-in DNS forwarder (libslirp) |
| `10.0.0.1/32` | WireGuard client address (`wg0`) |

### Running inside the namespace

`run_in_namespace(ctx, command, env)` executes the command via `nsenter` into the sentinel's
network namespace:

```bash
nsenter -t {ns_pid} --net --user --preserve-credentials -- <command>
```

### Lifecycle and cleanup

`NamespaceContext` tracks all namespace resources:

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

`cleanup_namespace()` tears down in order:

1. `PortForwarder.stop()`
2. Close `exit_w` → slirp4netns detects HUP on `exit-fd`, exits cleanly
3. Wait up to 2s; SIGKILL slirp4netns if it hangs
4. SIGKILL sentinel, `waitpid`
5. Remove temp WireGuard config and slirp4netns API socket

### Prerequisites

`check_namespace_capabilities()` validates the runtime before namespace creation:

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

## 9. SSL/TLS

### TLS keylog

`mitmproxy.net.tls` reads `MITMPROXY_SSLKEYLOGFILE` at **module import time** (module-level
global). The env var must be set before any mitmproxy module import. ccproxy sets it at the top of
`_run_inspect()` in `cli.py`, before the `run_inspector()` call that triggers `WebMaster` import.

The keylog is written to `{config_dir}/tls.keylog` and contains TLS master secrets for all
connections mitmproxy intercepts (the inner TLS sessions to provider APIs).

### WireGuard keylog

`src/ccproxy/inspector/wg_keylog.py` exports WireGuard static private keys in Wireshark's
`wg.keylog_file` format to `{config_dir}/wg.keylog`, written after inspector startup. Format:

```
LOCAL_STATIC_PRIVATE_KEY = <base64>
```

This decrypts the outer WireGuard UDP tunnel. Combined with the TLS keylog, a full packet capture
can be completely decrypted in Wireshark.

### Combined CA bundle for ccproxy run --inspect

`_ensure_combined_ca_bundle()` in `cli.py` concatenates mitmproxy's CA cert with the system CA
bundle after mitmweb starts (ensuring the CA cert exists). The combined bundle path is set in the
subprocess environment:

```
SSL_CERT_FILE          = <combined bundle path>
REQUESTS_CA_BUNDLE     = <combined bundle path>
CURL_CA_BUNDLE         = <combined bundle path>
NODE_EXTRA_CA_CERTS    = <combined bundle path>
```

This covers Python `ssl` (urllib3, httpx), `requests`, `curl`, and Node.js clients. Falls back to
`/etc/ssl/certs/ca-certificates.crt` if the system bundle is absent.

### Wireshark decryption workflow

1. Capture traffic: `tcpdump -i any -w capture.pcap`
2. Open in Wireshark
3. Decrypt WireGuard outer tunnel: Edit → Preferences → Protocols → WireGuard → Key log file → `{config_dir}/wg.keylog`
4. Decrypt inner TLS: Edit → Preferences → Protocols → TLS → (Pre)-Master-Secret log filename → `{config_dir}/tls.keylog`

Both paths are logged at inspector startup.

---

## 10. OpenTelemetry Integration

`src/ccproxy/inspector/telemetry.py` implements OTel span emission with three-mode graceful
degradation:

| Mode | Condition | Behavior |
|------|-----------|----------|
| Real OTLP export | `ccproxy.otel.enabled=true` + packages installed | Spans exported via gRPC to configured endpoint |
| No-op tracer | `enabled=false` + API package present | Zero overhead, no exports |
| Stub | OTel packages absent | No imports, zero overhead |

### Span lifecycle

Spans are started in `InspectorAddon.request()` and ended in `InspectorAddon.response()` or
`InspectorAddon.error()`. The span object is stored in `FlowRecord.otel` (an `OtelMeta` instance).
For flows without a record, spans fall back to direct storage in `flow.metadata["ccproxy.otel_span"]`.

### Span attributes

Each span includes HTTP semantics attributes (`http.request.method`, `url.full`, `server.address`,
`server.port`), ccproxy-specific attributes (`ccproxy.proxy_direction`, `ccproxy.trace_id`,
`ccproxy.session_id` when present), and GenAI semantic convention attributes (`gen_ai.system`,
`gen_ai.operation.name`) for flows to known provider hosts.

### Configuration

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

## Source File Map

| Path | Role |
|------|------|
| `src/ccproxy/inspector/process.py` | `run_inspector()`, `_build_opts()`, `_build_addons()`, `ReadySignal`, `get_wg_client_conf()` |
| `src/ccproxy/inspector/addon.py` | `InspectorAddon` — direction detection, flow record lifecycle, SSE streaming setup, OTel delegation |
| `src/ccproxy/flows/store.py` | `FlowRecord`, `AuthMeta`, `OtelMeta`, `TransformMeta`, `HttpSnapshot`, `ClientRequest`, `InspectorMeta`, TTL store |
| `src/ccproxy/inspector/router.py` | `InspectorRouter` — xepor subclass with mitmproxy 12.x fixes and wildcard host support |
| `src/ccproxy/inspector/pipeline.py` | `build_executor()`, `register_pipeline_routes()` — DAG executor wiring |
| `src/ccproxy/inspector/routes/transform.py` | `register_transform_routes()` — REQUEST transform dispatch, RESPONSE format conversion |
| `src/ccproxy/inspector/namespace.py` | `create_namespace()`, `run_in_namespace()`, `cleanup_namespace()`, `PortForwarder`, `check_namespace_capabilities()` |
| `src/ccproxy/inspector/telemetry.py` | `InspectorTracer` — three-mode OTel span emission |
| `src/ccproxy/inspector/wg_keylog.py` | WireGuard keylog export for Wireshark |
| `src/ccproxy/inspector/shape_capturer.py` | `ShapeCapturer` — `ccproxy.shape` command for shape capture |
