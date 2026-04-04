    ┌─ Host ────────────────────────────────────────────────────────┐
    │                                                               │
    │  ┌───────────┐   reverse   ┌──────────┐  HTTPS_PROXY   ┌───┐ │
    │  │  mitmweb  │◀───────────▶│ LiteLLM  │───────────────▶│   │ │
    │  │           │   @:4000    └──────────┘   @:8081       │ m │ │
    │  │  WG srv   │                                         │ i │ │
    │  │ @:51820   │   regular (outbound to providers)       │ t │ │
    │  │           │◀───────────────────────────────────────▶│ m │ │
    │  └─────▲─────┘                                         │ w │ │
    │        │                                               │ e │ │
    │        │ WireGuard UDP (via host network)              │ b │ │
    │        │                                               └───┘ │
    │  ┌─────┴───────────────────────────────────┐                 │
    │  │ slirp4netns  (bridges namespace ↔ host) │                 │
    │  │  host gateway: 10.0.2.2                 │                 │
    │  └─────┬───────────────────────────────────┘                 │
    │        │                                                     │
    │  ┌─────┴── Network Namespace (user+net, no root) ─────────┐  │
    │  │                                                        │  │
    │  │  tap0 → 10.0.2.100/24  (slirp4netns --configure)       │  │
    │  │  wg0  → 10.0.0.1/32   (WireGuard client)              │  │
    │  │  Endpoint = 10.0.2.2:51820 (→ host mitmweb via slirp) │  │
    │  │  default route via wg0                                 │  │
    │  │                                                        │  │
    │  │  ┌──────────────────────┐                              │  │
    │  │  │  <confined process>  │  all traffic → wg0           │  │
    │  │  │  (e.g. claude CLI)   │  → mitmweb captures          │  │
    │  │  └──────────────────────┘                              │  │
    │  └────────────────────────────────────────────────────────┘  │
    └───────────────────────────────────────────────────────────────┘
