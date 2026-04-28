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
        needs_quote = any(c in v for c in ':{}[],"\'|>&*!%#`@\n')
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

    # ── oat_sources ──

    comment("OAuth token sources — shell commands that output tokens.")
    comment("Sentinel key sk-ant-oat-ccproxy-{name} triggers lookup.")
    w("  oat_sources:")

    # Nix toJSON alphabetizes keys; preserve a logical ordering.
    oat_order = ["anthropic", "gemini", "deepseek"]
    oat_names = [n for n in oat_order if n in s["oat_sources"]]
    oat_names += [n for n in s["oat_sources"] if n not in oat_order]

    for name in oat_names:
        src = s["oat_sources"][name]
        w(f"    {name}:")
        w(f'      command: "{src["command"]}"')
        if "destinations" in src:
            w("      destinations:")
            for dest in src["destinations"]:
                w(f"        - {_scalar(dest)}")
        if "user_agent" in src:
            w(f"      user_agent: {_scalar(src['user_agent'])}")
        if "auth_header" in src:
            w(f"      auth_header: {_scalar(src['auth_header'])}")
        blank()

    # ── hooks ──

    comment("Two-stage hook pipeline. Hooks are DAG-ordered within each stage.")
    comment("Each entry is a module path or {hook: <path>, params: <dict>}.")
    w("  hooks:")
    w("    inbound:")
    for hook in s["hooks"]["inbound"]:
        w(f"      - {hook}")

    comment("Uncomment to work around google-gemini/gemini-cli#21691 —", indent=6)
    comment("the Gemini CLI wipes its own refresh_token during access_token", indent=6)
    comment("refresh, causing 'No refresh token is set' errors after ~1hr.", indent=6)
    comment("- ccproxy.hooks.gemini_oauth_refresh", indent=6)

    w("    outbound:")
    for hook in s["hooks"]["outbound"]:
        w(f"      - {hook}")
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
        comment("Transform rules — first match wins.", indent=4)
        comment("Modes: passthrough (forward unchanged), redirect (rewrite host),", indent=4)
        comment("  transform (cross-format via lightllm).", indent=4)
        comment("Matching: match_host, match_path (prefix), match_model (substring).", indent=4)
        w("    transforms:")
        # Nix toJSON alphabetizes keys; reorder so match_* leads, mode next, dest_* last.
        key_order = [
            "match_host", "match_path", "match_model",
            "mode",
            "dest_provider", "dest_host", "dest_path", "dest_api_key_ref",
            "dest_vertex_project", "dest_vertex_location",
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
