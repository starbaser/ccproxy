#!/usr/bin/env python3
"""Render src/ccproxy/templates/ccproxy.yaml from nix/defaults.nix.

Single source of truth for default values: nix/defaults.nix
This script adds the inline documentation layer for standalone installs.

Usage:
    nix eval --json .#defaultSettings.settings \
      | python3 scripts/render_template.py \
      > src/ccproxy/templates/ccproxy.yaml
"""

from __future__ import annotations

import json
import sys
from typing import Any


def _scalar(v: Any) -> str:
    """Format a Python value as a YAML scalar."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        needs_quote = any(c in v for c in ":{}[],\"'|>&*!%#`@\n")
        needs_quote = needs_quote or v in ("true", "false", "null", "yes", "no")
        return f'"{v}"' if needs_quote else v
    return str(v)


def render(s: dict[str, Any]) -> str:
    lines: list[str] = []

    def w(*args: str) -> None:
        lines.extend(args)

    def blank() -> None:
        lines.append("")

    def comment(text: str, indent: int = 2) -> None:
        prefix = " " * indent
        for line in text.split("\n"):
            lines.append(f"{prefix}# {line}" if line else f"{prefix}#")

    # ── top-level ──

    w("ccproxy:")
    w(f"  host: {s['host']}")
    w(f"  port: {s['port']}")
    blank()

    comment("Root Python logger level. DEBUG emits library internals (httpx,")
    comment("httpcore, mitmproxy); INFO is recommended for normal use.")
    comment("log_level: INFO")
    blank()
    comment("Daemon log file path. Relative to config dir, or absolute.")
    comment("Set to null to disable file logging. Only `ccproxy start` writes here.")
    comment("log_file: ccproxy.log")
    blank()
    comment("Route daemon logging to the systemd journal via JournalHandler.")
    comment("Applies only to `ccproxy start`. Requires the `journal` extra:")
    comment("  pip install claude-ccproxy[journal]")
    comment("Falls back to stderr with a warning when systemd-python is unavailable.")
    comment("use_journal: false")
    blank()
    comment("SYSLOG_IDENTIFIER for the journal handler when use_journal=true.")
    comment("Defaults derive from the config-dir basename:")
    comment("  ~/.config/ccproxy/            -> ccproxy")
    comment("  ~/dev/projects/foo/.ccproxy/  -> ccproxy-foo")
    comment("Override here, or via CCPROXY_JOURNAL_IDENTIFIER env var.")
    comment("journal_identifier: ccproxy-myproject")
    blank()

    # ── providers ──

    comment("Provider entries keyed by sentinel suffix. The sentinel key")
    comment("`sk-ant-oat-ccproxy-{name}` resolves to providers[name] for token")
    comment("injection and routing. Iteration order is load-bearing — the first")
    comment("provider with a cached token wins as the no-sentinel fallback.")
    w("  providers:")

    # Nix toJSON alphabetizes keys; preserve a logical priority ordering.
    provider_order = ["anthropic", "gemini", "deepseek"]
    provider_names = [n for n in provider_order if n in s["providers"]]
    provider_names += [n for n in s["providers"] if n not in provider_order]

    auth_key_order = [
        "type",
        "command",
        "file",
        "refresh_token_file",
        "client_id",
        "client_secret",
        "endpoint",
        "expiry_field",
        "header",
    ]

    for name in provider_names:
        entry = s["providers"][name]
        w(f"    {name}:")
        auth = entry.get("auth")
        if auth:
            w("      auth:")
            sorted_auth = sorted(
                auth.items(),
                key=lambda kv: auth_key_order.index(kv[0]) if kv[0] in auth_key_order else len(auth_key_order),
            )
            for k, v in sorted_auth:
                w(f"        {k}: {_scalar(v)}")
        if "host" in entry:
            w(f"      host: {_scalar(entry['host'])}")
        if "path" in entry:
            w(f"      path: {_scalar(entry['path'])}")
        if "provider" in entry:
            w(f"      provider: {_scalar(entry['provider'])}")
        blank()

    # ── hooks ──

    comment("Two-stage hook pipeline. Hooks are DAG-ordered within each stage.")
    comment("Each entry is a module path or {hook: <path>, params: <dict>}.")
    w("  hooks:")
    w("    inbound:")
    for hook in s["hooks"]["inbound"]:
        w(f"      - {hook}")

    w("    outbound:")
    for hook in s["hooks"]["outbound"]:
        w(f"      - {hook}")
    blank()

    # ── gemini_capacity ──

    if "gemini_capacity" in s:
        comment("Sticky-retry + fallback chain for Gemini RESOURCE_EXHAUSTED responses.")
        comment("Owned by GeminiAddon; no @hook entry. Disabled by default.")
        gc = s["gemini_capacity"]
        w("  gemini_capacity:")
        w(f"    enabled: {_scalar(gc['enabled'])}")
        if "fallback_models" in gc:
            w("    fallback_models:")
            for m in gc["fallback_models"]:
                w(f"      - {m}")
        for key in (
            "sticky_retry_attempts",
            "sticky_retry_max_delay_seconds",
            "terminal_delay_threshold_seconds",
            "total_retry_budget_seconds",
        ):
            if key in gc:
                w(f"    {key}: {_scalar(gc[key])}")
        blank()

    # ── otel ──

    comment("OpenTelemetry tracing. Requires a running collector (e.g. Jaeger).")
    w("  otel:")
    otel = s["otel"]
    w(f"    enabled: {_scalar(otel['enabled'])}")
    w(f"    endpoint: {_scalar(otel['endpoint'])}")
    w(f"    service_name: {_scalar(otel['service_name'])}")
    blank()

    # ── shaping ──

    comment("Request shaping — stamps a captured 'shape' flow onto outbound requests.")
    comment("Capture a shape: ccproxy flows shape --provider anthropic")
    shaping = s["shaping"]
    w("  shaping:")
    w(f"    enabled: {_scalar(shaping['enabled'])}")
    w(f"    shapes_dir: {_scalar(shaping['shapes_dir'])}")
    blank()
    comment("Per-provider shaping profiles.", indent=4)
    w("    providers:")

    for pname, prov in shaping["providers"].items():
        w(f"      {pname}:")

        w("        content_fields:")
        for field in prov["content_fields"]:
            w(f"          - {field}")

        if "merge_strategies" in prov:
            w("        merge_strategies:")
            for k, v in prov["merge_strategies"].items():
                w(f'          {k}: "{v}"')

        if "shape_hooks" in prov:
            w("        shape_hooks:")
            for hook in prov["shape_hooks"]:
                w(f"          - {hook}")

        if "preserve_headers" in prov:
            w("        preserve_headers:")
            for h in prov["preserve_headers"]:
                w(f"          - {h}")

        if "strip_headers" in prov:
            w("        strip_headers:")
            for h in prov["strip_headers"]:
                w(f"          - {h}")

        if "capture" in prov:
            w("        capture:")
            for k, v in prov["capture"].items():
                w(f'          {k}: "{v}"')

    blank()

    # ── inspector ──

    comment("Inspector settings (mitmweb UI and transform rules).")
    insp = s["inspector"]
    w("  inspector:")
    w(f"    port: {insp['port']}")
    if "cert_dir" in insp:
        w(f"    cert_dir: {_scalar(insp['cert_dir'])}")

    if "transforms" in insp:
        blank()
        comment("Optional regex-matched override rules layered on top of the", indent=4)
        comment("sentinel-driven providers map. Default is empty: most routing", indent=4)
        comment("comes from `providers` via forward_oauth's sentinel detection.", indent=4)
        comment("First match wins. Match fields are regex; actions are", indent=4)
        comment("passthrough | redirect | transform.", indent=4)
        if not insp["transforms"]:
            w("    transforms: []")
        else:
            w("    transforms:")
            key_order = [
                "match_host",
                "match_path",
                "match_model",
                "action",
                "dest_provider",
                "dest_host",
                "dest_path",
                "dest_model",
                "dest_vertex_project",
                "dest_vertex_location",
            ]
            for rule in insp["transforms"]:
                ordered = sorted(
                    rule.items(),
                    key=lambda kv: key_order.index(kv[0]) if kv[0] in key_order else len(key_order),
                )
                k0, v0 = ordered[0]
                w(f"      - {k0}: {_scalar(v0)}")
                for k, v in ordered[1:]:
                    w(f"        {k}: {_scalar(v)}")

    # trailing newline
    blank()
    return "\n".join(lines)


def main() -> None:
    settings = json.load(sys.stdin)
    sys.stdout.write(render(settings))


if __name__ == "__main__":
    main()
