#!/usr/bin/env python3
"""List and filter ccproxy inspector flows with structured JSON output.

Uses MitmwebClient directly for enriched flow data beyond what
`ccproxy flows list` provides. Supports filtering by provider, model,
status code, and URL pattern.

Usage:
    uv run python scripts/list_flows.py
    uv run python scripts/list_flows.py --filter "anthropic"
    uv run python scripts/list_flows.py --provider anthropic --status 200
    uv run python scripts/list_flows.py --model claude --latest 5
    uv run python scripts/list_flows.py --table
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

import httpx


def _make_client():
    """Create MitmwebClient from current config."""
    from ccproxy.config import CredentialSource, get_config

    cfg = get_config()
    inspector = cfg.inspector
    host = inspector.mitmproxy.web_host
    port = inspector.port

    web_password_cfg = inspector.mitmproxy.web_password
    if isinstance(web_password_cfg, str):
        token = web_password_cfg
    elif web_password_cfg is not None:
        source = (
            web_password_cfg if isinstance(web_password_cfg, CredentialSource) else CredentialSource(**web_password_cfg)
        )
        token = source.resolve("mitmweb web_password") or ""
    else:
        token = ""

    from ccproxy.flows import MitmwebClient

    return MitmwebClient(host=host, port=port, token=token)


def _header_value(headers: list[list[str]], name: str) -> str:
    for pair in headers:
        if pair[0].lower() == name.lower():
            return pair[1]
    return ""


def _extract_model(body_bytes: bytes) -> str | None:
    try:
        data = json.loads(body_bytes)
        if isinstance(data, dict):
            return data.get("model")
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    return None


def _build_provider_map() -> dict[str, str]:
    try:
        from ccproxy.config import get_config

        return get_config().inspector.provider_map
    except Exception:
        return {}


def _enrich_flow(client, flow: dict[str, Any], *, fetch_model: bool = False) -> dict[str, Any]:
    """Extract structured fields from a raw mitmweb flow dict."""
    req = flow["request"]
    res = flow.get("response") or {}
    flow_id = flow["id"]

    record: dict[str, Any] = {
        "id": flow_id,
        "id_short": flow_id[:8],
        "method": req["method"],
        "status": res.get("status_code"),
        "host": req["pretty_host"],
        "path": req["path"],
        "user_agent": _header_value(req.get("headers", []), "user-agent"),
        "content_type": _header_value(req.get("headers", []), "content-type"),
        "oauth_injected": bool(_header_value(req.get("headers", []), "x-ccproxy-oauth-injected")),
        "timestamp": flow.get("client_conn", {}).get("timestamp_start"),
    }

    if fetch_model:
        try:
            body = client.get_request_body(flow_id)
            record["model"] = _extract_model(body)
        except Exception:
            record["model"] = None
    else:
        record["model"] = None

    return record


def _print_table(flows: list[dict[str, Any]]) -> None:
    from rich.console import Console
    from rich.table import Table

    console = Console()
    if not flows:
        console.print("[dim]No flows.[/dim]")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("ID", width=8)
    table.add_column("Method", width=7)
    table.add_column("Code", width=5, justify="right")
    table.add_column("Host", max_width=35)
    table.add_column("Path", max_width=50)
    table.add_column("Model", max_width=30)
    table.add_column("OAuth", width=5)

    for f in flows:
        code = str(f["status"] or "-")
        code_style = "green" if code.startswith("2") else "red" if code != "-" else "dim"
        oauth = "[green]yes[/green]" if f["oauth_injected"] else "[dim]-[/dim]"
        model = f.get("model") or "[dim]-[/dim]"

        table.add_row(
            f["id_short"],
            f["method"],
            f"[{code_style}]{code}[/{code_style}]",
            f["host"],
            f["path"][:50],
            str(model)[:30],
            oauth,
        )

    console.print(table)


def main() -> None:
    parser = argparse.ArgumentParser(description="List and filter ccproxy inspector flows")
    parser.add_argument("--filter", help="Regex filter on host+path")
    parser.add_argument("--provider", help="Filter by provider name (matches against inspector.provider_map)")
    parser.add_argument("--model", help="Filter by model substring (fetches request bodies)")
    parser.add_argument("--status", type=int, help="Filter by HTTP status code")
    parser.add_argument("--latest", type=int, help="Show only the N most recent flows")
    parser.add_argument("--table", action="store_true", help="Rich table output (default: JSON)")
    parser.add_argument("--json", action="store_true", default=True, help="JSON output (default)")
    args = parser.parse_args()

    fetch_model = bool(args.model)

    try:
        with _make_client() as client:
            raw_flows = client.list_flows()

            # URL regex filter
            if args.filter:
                pat = re.compile(args.filter, re.IGNORECASE)
                raw_flows = [f for f in raw_flows if pat.search(f["request"]["pretty_host"] + f["request"]["path"])]

            # Provider filter
            if args.provider:
                provider_map = _build_provider_map()
                provider_hosts = {host for host, prov in provider_map.items() if prov == args.provider}
                raw_flows = [f for f in raw_flows if f["request"]["pretty_host"] in provider_hosts]

            # Status filter
            if args.status is not None:
                raw_flows = [f for f in raw_flows if (f.get("response") or {}).get("status_code") == args.status]

            # Latest N
            if args.latest:
                raw_flows = raw_flows[-args.latest :]

            # Enrich
            enriched = [_enrich_flow(client, f, fetch_model=fetch_model) for f in raw_flows]

            # Model filter (post-enrichment)
            if args.model:
                enriched = [f for f in enriched if f.get("model") and args.model.lower() in f["model"].lower()]

            if args.table:
                _print_table(enriched)
            else:
                json.dump(enriched, sys.stdout, indent=2, default=str)
                print()

    except httpx.ConnectError:
        print("Error: Cannot connect to mitmweb. Is ccproxy running? (ccproxy status)", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
