# Inspect Mode

Inspect mode (`--inspect`) activates the full MITM stack with transparent network capture via WireGuard and Linux network namespaces. It intercepts all TCP/UDP traffic from a confined subprocess without requiring root or any modifications to the confined process.

This is distinct from the basic MITM approach (`HTTP_PROXY` injection) which only captures HTTP-aware clients. Inspect mode captures everything — including HTTP/2, raw TLS, or any other TCP traffic — because confinement happens at the network layer.

---

## Architecture

### Three mitmweb modes

`ccproxy start --inspect` launches mitmweb with three simultaneous proxy modes:

| Mode | Purpose |
|------|---------|
| `reverse@<port>` | Captures inbound client → LiteLLM traffic |
| `regular@<port>` | Captures LiteLLM → provider outbound traffic (via `HTTPS_PROXY`) |
| `wireguard@<wireguard_port>` | WireGuard server used as the tunnel endpoint for namespace-confined processes |

All three activate together. There is no partial-mode configuration — `--inspect` is the WireGuard stack or nothing.

### `ccproxy run --inspect -- claude` —

```
┌─ Host ────────────────────────────────────────────────────────┐
│                                                               │
│  ┌───────────┐   reverse   ┌──────────┐  HTTPS_PROXY   ┌───┐  │
│  │  mitmweb  │◀───────────▶│ LiteLLM  │───────────────▶│   │  │
│  │           │   @:4000    └──────────┘   @:8081       │ m │  │
│  │  WG srv   │                                         │ i │  │
│  │ @:51820   │   regular (outbound to providers)       │ t │  │
│  │           │◀───────────────────────────────────────▶│ m │  │
│  └─────▲─────┘                                         │ w │  │
│        │                                               │ e │  │
│        │ WireGuard UDP (via host network)              │ b │  │
│        │                                               └───┘  │
│  ┌─────┴───────────────────────────────────┐                  │
│  │ slirp4netns  (bridges namespace ↔ host) │                  │
│  │  host gateway: 10.0.2.2                 │                  │
│  └─────┬───────────────────────────────────┘                  │
│        │                                                      │
│  ┌─────┴── Network Namespace (user+net, no root) ─────────┐   │
│  │                                                        │   │
│  │  tap0 → 10.0.2.100/24  (slirp4netns --configure)       │   │
│  │  wg0  → 10.0.0.1/32   (WireGuard client)               │   │
│  │  Endpoint = 10.0.2.2:51820 (→ host mitmweb via slirp)  │   │
│  │  default route via wg0                                 │   │
│  │                                                        │   │
│  │  ┌──────────────────────┐                              │   │
│  │  │  <confined process>  │  all traffic → wg0           │   │
│  │  │  (e.g. claude CLI)   │  → mitmweb captures          │   │
│  │  └──────────────────────┘                              │   │
│  └────────────────────────────────────────────────────────┘   │
└───────────────────────────────────────────────────────────────┘
```

**Loop prevention**: mitmproxy's WireGuard server listens on the host network. The confined process sends WireGuard UDP packets to `10.0.2.2:51820` (the slirp4netns NAT gateway, which forwards to the host). These arrive at mitmproxy as ordinary UDP and are decrypted. mitmproxy then forwards the inner plaintext traffic out via the host's default route. mitmproxy's own outbound packets never enter the WireGuard tunnel.

---

## Prerequisites

### Kernel requirement

Unprivileged user namespaces must be enabled:

```
/proc/sys/kernel/unprivileged_userns_clone = 1
```

This is the default on mainline kernels. NixOS with kernel 6.18+ satisfies this by default.

### Required tools

| Tool | Package | Purpose |
|------|---------|---------|
| `slirp4netns` | `pkgs.slirp4netns` | Bridges network namespace to host |
| `unshare` | `pkgs.util-linux` | Creates user+net namespace |
| `nsenter` | `pkgs.util-linux` | Enters the namespace to run commands |
| `ip` | `pkgs.iproute2` | Configures WireGuard interface inside namespace |
| `wg` | `pkgs.wireguard-tools` | Sets WireGuard keys and config |
| WireGuard kernel module | Built into Linux 5.6+ | WireGuard tunnel in namespace |

All are standard on NixOS with the mainline kernel.

`ccproxy run --inspect` calls `check_namespace_capabilities()` at startup and hard-fails with a descriptive error for each missing prerequisite before attempting to create the namespace.

---

## Usage

### Starting the server

```bash
ccproxy start --inspect
```

This starts mitmweb (reverse + regular + wireguard modes) as a child process, then blocks on LiteLLM. After mitmweb is ready, the WireGuard client configuration is fetched from mitmweb's REST API and written to `{config_dir}/.inspector-wireguard-client.conf` for use by `ccproxy run --inspect`.

Ports opened:

| Port | Role |
|------|------|
| `4000` (default) | Reverse proxy entry point |
| `8081` (default) | Forward proxy for LiteLLM outbound traffic |
| `8083` (default) | mitmweb inspect UI |
| `51820` (default) | WireGuard UDP endpoint |

`ccproxy start` without `--inspect` runs LiteLLM only with no MITM at all.

### Running a confined subprocess

```bash
ccproxy run --inspect -- <command> [args...]
```

Examples:

```bash
ccproxy run --inspect -- curl https://api.anthropic.com/v1/models
ccproxy run --inspect -- claude
ccproxy run --inspect -- python my_script.py
```

The `-i` short flag is equivalent:

```bash
ccproxy run -i -- curl https://httpbin.org/get
```

### What happens

1. Prerequisite check — exits with error if any tool is missing
2. Reads `{config_dir}/.inspector-wireguard-client.conf` — exits with error if not present
3. Rewrites the WireGuard `Endpoint` to `10.0.2.2:{wireguard_port}` (the slirp4netns gateway)
4. Creates a user+net namespace via `unshare --user --map-root-user --net --pid --fork sleep infinity`
5. Starts slirp4netns with `--ready-fd` and `--exit-fd` for synchronised lifecycle
6. Waits for slirp4netns readiness signal on `ready-fd`
7. Runs WireGuard setup inside the namespace via `nsenter` (adds `wg0`, sets routes, replaces the default route with the WireGuard interface)
8. Executes the command in the namespace via `nsenter --net --user`
9. On exit (or Ctrl+C), tears down the namespace cleanly

The confined process receives no `HTTP_PROXY` or `HTTPS_PROXY` environment variables. It connects to providers normally — mitmweb intercepts transparently via the WireGuard tunnel.

### Verifying capture

Open the mitmweb UI at `http://localhost:8083` (default `port`). Traffic from the confined process appears in the flow list in real time. Filter by host or path to isolate provider API calls.

---

## Network Topology

### slirp4netns (host bridge)

`slirp4netns --configure` sets up the TAP device and default routing inside the namespace:

| Address | Role |
|---------|------|
| `10.0.2.100/24` | Namespace TAP interface (`tap0`) |
| `10.0.2.2` | Host gateway (all outbound traffic exits here) |
| `10.0.2.3` | Built-in DNS forwarder (libslirp) |

### WireGuard client (inside namespace)

After slirp4netns is ready, the WireGuard interface is configured on top:

| Address | Role |
|---------|------|
| `10.0.0.1/32` | WireGuard client address (`wg0`) |
| `10.0.0.53` | Virtual DNS provided by mitmproxy WireGuard mode |
| `10.0.2.2:51820` | Endpoint (rewritten from host IP to slirp gateway) |
| `0.0.0.0/0` | AllowedIPs (all traffic through tunnel) |

The namespace default route is replaced from `via 10.0.2.2` (slirp) to `dev wg0` (WireGuard). WireGuard's own UDP packets to `10.0.2.2:51820` are special-cased by the kernel as traffic to the gateway and exit via `tap0` rather than recursing through `wg0`.

---

## Configuration

These fields live under `ccproxy.inspector` in `ccproxy.yaml`:

The WireGuard keypair is auto-managed at `{config_dir}/wireguard.{pid}.conf` (PID-tagged for multi-instance isolation). Each `ccproxy start --inspect` gets its own WG server identity. Stale keypair files from dead processes are cleaned during preflight. The mitmproxy CA (in `cert_dir`/`confdir`) is shared across instances so clients only need to trust one CA.

---

## Lifecycle and Cleanup

### slirp4netns lifecycle

slirp4netns is started with two pipe file descriptors:

- `--ready-fd`: slirp4netns writes `"1"` when the TAP interface is configured and the namespace network is ready. `create_namespace` blocks on a read from this FD — no polling.
- `--exit-fd`: slirp4netns monitors this FD. When the parent closes the write end, slirp4netns detects HUP and exits cleanly (return code 0), removing its API socket.

The `NamespaceContext.exit_w` field holds the write end of the exit pipe. It remains open for the lifetime of the namespace.

### `cleanup_namespace`

Called in a `finally` block regardless of how the confined process exits:

1. Closes `exit_w` — triggers clean slirp4netns shutdown via exit-fd
2. Waits up to 2 seconds for slirp4netns to exit; SIGKILLs if it doesn't
3. SIGKILLs the namespace sentinel (`sleep infinity`) and reaps it with `waitpid`
4. Removes the temporary WireGuard config file
5. Removes the slirp4netns API socket if still present (only lingers if slirp was killed)

### `ccproxy start` shutdown

When `ccproxy start --inspect` receives SIGTERM or Ctrl+C, the `finally` block in `start_litellm` calls `_terminate_proc(mitm_proc)`, which sends SIGTERM to mitmweb and waits 5 seconds before escalating to SIGKILL. The PID-tagged WireGuard keypair file (`wireguard.{pid}.conf`) is removed on shutdown. The `.inspector-wireguard-client.conf` state file is deleted at the start of each `ccproxy start --inspect` and re-fetched from mitmweb after startup, preventing stale client configs from persisting across restarts. Preflight checks also clean orphaned `wireguard.*.conf` files for dead PIDs.

---

## Security Model

### What the jail provides

- **Network isolation**: The confined process has no direct access to the host network stack. All traffic exits through the WireGuard tunnel and is visible to mitmweb.
- **No root required**: User namespaces map the confined process's UID to a fake root inside the namespace (`--map-root-user`). No capabilities are granted on the host.
- **Hard failure**: `--inspect` never falls back to unconfined execution. If prerequisites are missing, the process does not run. This is a deliberate design choice — inspect mode is a security boundary. A silent fallback would defeat the purpose.

### What the jail does not provide

- **Filesystem isolation**: The confined process has full access to the host filesystem. Phase 4 (future work) may add mount namespace restrictions.
- **Syscall filtering**: No seccomp profile is applied. Phase 4 may add a seccomp allowlist.
- **Process isolation**: The confined process can see and signal host processes (though it cannot gain privileges via signals). A PID namespace is created for the sentinel but `nsenter` enters the net and user namespaces only.
- **MITM certificate trust**: If the confined process performs certificate pinning, mitmweb's TLS interception will fail for those connections. The mitmweb CA cert must be trusted by the confined process for TLS decryption to work.

---

## Troubleshooting

### `Error: Unprivileged user namespaces disabled`

```
/proc/sys/kernel/unprivileged_userns_clone = 0
```

Enable temporarily:

```bash
sudo sysctl -w kernel.unprivileged_userns_clone=1
```

Persist in NixOS:

```nix
boot.kernel.sysctl."kernel.unprivileged_userns_clone" = 1;
```

### `Error: slirp4netns not found`

```bash
nix profile install nixpkgs#slirp4netns
```

Or add `pkgs.slirp4netns` to the devShell packages in `flake.nix`.

### `Error: No WireGuard configuration found. Start ccproxy with --inspect first`

`ccproxy run --inspect` requires a running `ccproxy start --inspect` instance. Start the server first, then run the confined command. The state file `{config_dir}/.inspector-wireguard-client.conf` is written by `start_litellm` after mitmweb becomes ready.

### `Error: Namespace setup failed: slirp4netns failed to become ready`

slirp4netns exited before writing to `ready-fd`. Check for:
- Another process using the same network namespace PID (unlikely, but possible on rapid restart)
- `slirp4netns` version incompatibility (requires 0.4.0+ for `--ready-fd` and `--exit-fd` support)

### `Error: WireGuard setup failed in namespace: <stderr>`

The `nsenter` + `ip`/`wg` command sequence failed inside the namespace. The full stderr from the failed command is included in the error message. Common causes:
- WireGuard kernel module not loaded (`modprobe wireguard`)
- `ip` or `wg` not in PATH

### Traffic not appearing in mitmweb

- Confirm the confined process is connecting to a remote host (not localhost — loopback bypasses the WireGuard tunnel)
- Check that the confined process trusts mitmweb's CA certificate (`~/.mitmproxy/mitmproxy-ca-cert.pem`)
- Verify the WireGuard endpoint rewrite succeeded: the `.inspector-wireguard-client.conf` state file should contain `Endpoint = 10.0.2.2:51820`
- Check mitmweb logs for WireGuard handshake errors

### `Failed to retrieve WireGuard client config from mitmweb`

This warning appears in `ccproxy start --inspect` output when the mitmweb REST API (`GET /state`) does not return a `wireguard_conf` field within 15 seconds. Possible causes:
- mitmweb version does not support WireGuard mode (requires mitmproxy 10.3+)
- mitmweb started but WireGuard mode failed to initialise (check mitmweb logs at `{config_dir}/.inspector.log`)

Without the state file, `ccproxy run --inspect` will refuse to start.
