#!/usr/bin/env python3
"""Inspect a single ccproxy flow: client request vs forwarded request.

Fetches the pre-pipeline client request snapshot and the post-pipeline
forwarded request, then computes a structured diff showing exactly what
the pipeline changed.

Usage:
    uv run python scripts/inspect_flow.py <flow-id-prefix>
    uv run python scripts/inspect_flow.py a1b2c3d4 --with-response
    uv run python scripts/inspect_flow.py a1b2c3d4 --json
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from typing import Any

import httpx


def _make_client():
    from ccproxy.config import CredentialSource, get_config
    from ccproxy.tools.flows import MitmwebClient

    cfg = get_config()
    inspector = cfg.inspector
    host = inspector.mitmproxy.web_host
    port = inspector.port

    web_password_cfg = inspector.mitmproxy.web_password
    if isinstance(web_password_cfg, str):
        token = web_password_cfg
    elif web_password_cfg is not None:
        source = (
            web_password_cfg
            if isinstance(web_password_cfg, CredentialSource)
            else CredentialSource(**web_password_cfg)
        )
        token = source.resolve("mitmweb web_password") or ""
    else:
        token = ""

    return MitmwebClient(host=host, port=port, token=token)


def _headers_dict(headers: list[list[str]]) -> dict[str, str]:
    return {pair[0].lower(): pair[1] for pair in headers}


def _parse_json_safe(raw: bytes) -> dict[str, Any] | None:
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _parse_client_request_text(text: str) -> dict[str, Any]:
    """Parse the Client-Request content view text into structured data."""
    result: dict[str, Any] = {"raw": text, "method": "", "url": "", "headers": {}, "body": None}

    lines = text.strip().split("\n")
    if not lines:
        return result

    # First line: METHOD scheme://host:port/path
    first_line = lines[0].strip()
    parts = first_line.split(" ", 1)
    if len(parts) >= 1:
        result["method"] = parts[0]
    if len(parts) >= 2:
        result["url"] = parts[1]

    in_headers = False
    in_body = False
    header_lines: list[str] = []
    body_lines: list[str] = []

    for line in lines[1:]:
        stripped = line.strip()
        if stripped == "--- Headers ---":
            in_headers = True
            in_body = False
            continue
        if stripped == "--- Body ---":
            in_headers = False
            in_body = True
            continue
        if in_headers and stripped:
            header_lines.append(stripped)
        elif in_body:
            body_lines.append(line)

    for hl in header_lines:
        if ": " in hl:
            k, v = hl.split(": ", 1)
            result["headers"][k.strip().lower()] = v.strip()

    body_text = "\n".join(body_lines).strip()
    if body_text:
        try:
            result["body"] = json.loads(body_text)
        except (json.JSONDecodeError, ValueError):
            result["body"] = body_text

    return result


def _compute_changes(
    client: dict[str, Any],
    forwarded_flow: dict[str, Any],
    forwarded_body: dict[str, Any] | None,
) -> list[dict[str, str]]:
    """Compute a list of changes between client and forwarded request."""
    changes: list[dict[str, str]] = []
    fwd_req = forwarded_flow["request"]

    # URL change
    fwd_url = f"{fwd_req['scheme']}://{fwd_req['pretty_host']}{fwd_req['path']}"
    client_url = client.get("url", "")
    if client_url and client_url != fwd_url:
        changes.append({
            "type": "url_rewrite",
            "description": "Request URL was rewritten by transform",
            "client": client_url,
            "forwarded": fwd_url,
        })

    # Header diff
    client_headers = client.get("headers", {})
    fwd_headers = _headers_dict(fwd_req.get("headers", []))

    added = {k: v for k, v in fwd_headers.items() if k not in client_headers}
    removed = {k: v for k, v in client_headers.items() if k not in fwd_headers}

    # Filter out transport/internal headers from diff
    skip = {"content-length", "host", "x-ccproxy-flow-id"}
    added = {k: v for k, v in added.items() if k not in skip}
    removed = {k: v for k, v in removed.items() if k not in skip}

    if added:
        changes.append({
            "type": "headers_added",
            "description": f"{len(added)} header(s) added by pipeline",
            "headers": json.dumps(added, indent=2),
        })
    if removed:
        changes.append({
            "type": "headers_removed",
            "description": f"{len(removed)} header(s) removed by pipeline",
            "headers": json.dumps(removed, indent=2),
        })

    # Auth injection
    if fwd_headers.get("x-ccproxy-oauth-injected"):
        changes.append({
            "type": "oauth_injected",
            "description": "OAuth token was injected by forward_oauth hook",
        })

    # Body format change
    client_body = client.get("body")
    if isinstance(client_body, dict) and isinstance(forwarded_body, dict):
        client_keys = set(client_body.keys())
        fwd_keys = set(forwarded_body.keys())

        # Detect API format transformation
        if "messages" in client_keys and "contents" in fwd_keys:
            changes.append({
                "type": "body_format_transform",
                "description": "Body transformed from OpenAI format (messages) to Gemini format (contents)",
            })
        elif "messages" in fwd_keys and "contents" in client_keys:
            changes.append({
                "type": "body_format_transform",
                "description": "Body transformed from Gemini format (contents) to Anthropic/OpenAI format (messages)",
            })

        # System prompt injection
        if "system" not in client_keys and "system" in fwd_keys:
            changes.append({
                "type": "system_injected",
                "description": "System prompt was injected (likely by compliance)",
            })
        elif "system" in client_keys and "system" in fwd_keys and client_body["system"] != forwarded_body["system"]:
            changes.append({
                "type": "system_modified",
                "description": "System prompt was modified (compliance prepended blocks)",
            })

        # Body wrapping
        new_keys = fwd_keys - client_keys
        for k in new_keys:
            if isinstance(forwarded_body.get(k), dict) and (
                "messages" in forwarded_body[k] or "contents" in forwarded_body[k]
            ):
                changes.append({
                    "type": "body_wrapped",
                    "description": f"Body was wrapped inside '{k}' field (compliance body_wrapper)",
                })

    if not changes:
        changes.append({
            "type": "no_changes",
            "description": "Client request and forwarded request are identical (passthrough)",
        })

    return changes


def _print_rich(
    client_parsed: dict[str, Any],
    forwarded_flow: dict[str, Any],
    forwarded_body: dict[str, Any] | None,
    response_body: Any,
    changes: list[dict[str, str]],
    flow_id: str,
) -> None:
    from rich.console import Console
    from rich.panel import Panel
    from rich.syntax import Syntax
    from rich.table import Table

    console = Console()

    # Client request
    client_text = client_parsed.get("raw", "")
    console.print(Panel(client_text, title=f"Client Request (pre-pipeline) -- {flow_id[:8]}"))

    # Forwarded request
    fwd_req = forwarded_flow["request"]
    fwd_url = f"{fwd_req['method']} {fwd_req['scheme']}://{fwd_req['pretty_host']}{fwd_req['path']}"
    fwd_parts = [fwd_url, ""]
    for pair in fwd_req.get("headers", []):
        fwd_parts.append(f"  {pair[0]}: {pair[1]}")
    if forwarded_body:
        fwd_parts.append("")
        fwd_parts.append(json.dumps(forwarded_body, indent=2)[:2000])
    console.print(Panel("\n".join(fwd_parts), title=f"Forwarded Request (post-pipeline) -- {flow_id[:8]}"))

    # Changes summary
    table = Table(title="Pipeline Changes", show_header=True, header_style="bold")
    table.add_column("Type", style="cyan", width=25)
    table.add_column("Description")
    for c in changes:
        table.add_row(c["type"], c["description"])
    console.print(table)

    # Response
    if response_body is not None:
        body_str = json.dumps(response_body, indent=2) if isinstance(response_body, dict) else str(response_body)
        console.print(Panel(
            Syntax(body_str[:3000], "json", theme="monokai", word_wrap=True),
            title=f"Response -- {flow_id[:8]}",
        ))


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect a ccproxy flow: client vs forwarded request")
    parser.add_argument("flow_id", help="Flow ID prefix (8+ chars from `ccproxy flows list`)")
    parser.add_argument("--with-response", action="store_true", help="Also fetch and display the response body")
    parser.add_argument("--json", action="store_true", help="Output as structured JSON")
    args = parser.parse_args()

    try:
        with _make_client() as client:
            flow_id = client.resolve_id(args.flow_id)

            # Fetch flow metadata
            flows = client.list_flows()
            flow = next((f for f in flows if f["id"] == flow_id), None)
            if flow is None:
                print(f"Error: Flow {flow_id} not found", file=sys.stderr)
                sys.exit(1)

            # Fetch client request (pre-pipeline)
            client_text = client.get_client_request(flow_id)
            client_parsed = _parse_client_request_text(client_text)

            # Fetch forwarded request body (post-pipeline)
            fwd_body_raw = client.get_request_body(flow_id)
            fwd_body = _parse_json_safe(fwd_body_raw)

            # Fetch response (optional)
            response_body = None
            if args.with_response:
                with contextlib.suppress(Exception):
                    res_raw = client.get_response_body(flow_id)
                    response_body = _parse_json_safe(res_raw)
                    if response_body is None:
                        response_body = res_raw.decode("utf-8", errors="replace")

            # Compute changes
            changes = _compute_changes(client_parsed, flow, fwd_body)

            if args.json:
                output = {
                    "flow_id": flow_id,
                    "client_request": {
                        "method": client_parsed.get("method"),
                        "url": client_parsed.get("url"),
                        "headers": client_parsed.get("headers"),
                        "body": client_parsed.get("body"),
                    },
                    "forwarded_request": {
                        "method": flow["request"]["method"],
                        "url": f"{flow['request']['scheme']}://{flow['request']['pretty_host']}{flow['request']['path']}",
                        "headers": _headers_dict(flow["request"].get("headers", [])),
                        "body": fwd_body,
                    },
                    "changes": changes,
                }
                if response_body is not None:
                    output["response"] = {
                        "status": (flow.get("response") or {}).get("status_code"),
                        "body": response_body,
                    }
                json.dump(output, sys.stdout, indent=2, default=str)
                print()
            else:
                _print_rich(client_parsed, flow, fwd_body, response_body, changes, flow_id)

    except httpx.ConnectError:
        print("Error: Cannot connect to mitmweb. Is ccproxy running? (ccproxy status)", file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
