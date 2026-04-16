#!/usr/bin/env python3
"""Inspect a single ccproxy flow: client request vs forwarded request.

Fetches the page-grouped HAR 1.2 dump produced by the `ccproxy.dump`
mitmproxy command and computes a structured diff showing exactly what
the pipeline changed between the pre-pipeline client request and the
forwarded request.

Usage:
    uv run python scripts/inspect_flow.py <flow-id-prefix>
    uv run python scripts/inspect_flow.py a1b2c3d4 --with-response
    uv run python scripts/inspect_flow.py a1b2c3d4 --json
"""

from __future__ import annotations

import argparse
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
            web_password_cfg if isinstance(web_password_cfg, CredentialSource) else CredentialSource(**web_password_cfg)
        )
        token = source.resolve("mitmweb web_password") or ""
    else:
        token = ""

    return MitmwebClient(host=host, port=port, token=token)


def _har_headers_to_dict(headers: list[dict[str, str]]) -> dict[str, str]:
    """Convert HAR [{name, value}, ...] to a lower-cased dict."""
    return {h["name"].lower(): h["value"] for h in headers}


def _har_headers_to_pairs(headers: list[dict[str, str]]) -> list[list[str]]:
    """Convert HAR [{name, value}, ...] to mitmweb-style [[name, value], ...]."""
    return [[h["name"], h["value"]] for h in headers]


def _parse_body_text(text: str | None) -> dict[str, Any] | str | None:
    """Try to parse a body string as JSON; fall back to the raw string."""
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return text


def _client_entry_to_parsed(entry: dict[str, Any]) -> dict[str, Any]:
    """Adapt the HAR client-request entry to the shape the rest of the script expects."""
    req = entry["request"]
    headers = _har_headers_to_dict(req.get("headers", []))
    post_data = req.get("postData") or {}
    body = _parse_body_text(post_data.get("text"))

    raw_lines = [f"{req['method']} {req['url']}", ""]
    raw_lines.append("--- Headers ---")
    for name, value in headers.items():
        raw_lines.append(f"  {name}: {value}")
    raw_lines.append("")
    raw_lines.append("--- Body ---")
    if isinstance(body, dict):
        raw_lines.append(json.dumps(body, indent=2))
    elif body:
        raw_lines.append(str(body))
    else:
        raw_lines.append("(empty)")

    return {
        "raw": "\n".join(raw_lines),
        "method": req["method"],
        "url": req["url"],
        "headers": headers,
        "body": body,
    }


def _forwarded_entry_to_flow(entry: dict[str, Any]) -> dict[str, Any]:
    """Adapt the HAR forwarded entry to the mitmweb-style flow dict expected by
    _compute_changes / _print_rich."""
    req = entry["request"]
    # HAR url is a fully-qualified URL; split into scheme/host/path for the legacy view.
    from urllib.parse import urlsplit

    parts = urlsplit(req["url"])
    host = parts.netloc
    path = parts.path
    if parts.query:
        path = f"{path}?{parts.query}"

    flow: dict[str, Any] = {
        "request": {
            "method": req["method"],
            "scheme": parts.scheme,
            "pretty_host": host,
            "path": path,
            "headers": _har_headers_to_pairs(req.get("headers", [])),
            "http_version": req.get("httpVersion", "HTTP/1.1"),
        },
    }
    if entry.get("response"):
        flow["response"] = {
            "status_code": entry["response"].get("status"),
            "reason": entry["response"].get("statusText", ""),
            "headers": _har_headers_to_pairs(entry["response"].get("headers", [])),
        }
    return flow


def _compute_changes(
    client: dict[str, Any],
    forwarded_flow: dict[str, Any],
    forwarded_body: dict[str, Any] | None,
) -> list[dict[str, str]]:
    """Compute a list of changes between client and forwarded request."""
    changes: list[dict[str, str]] = []
    fwd_req = forwarded_flow["request"]

    fwd_url = f"{fwd_req['scheme']}://{fwd_req['pretty_host']}{fwd_req['path']}"
    client_url = client.get("url", "")
    if client_url and client_url != fwd_url:
        changes.append(
            {
                "type": "url_rewrite",
                "description": "Request URL was rewritten by transform",
                "client": client_url,
                "forwarded": fwd_url,
            }
        )

    client_headers = client.get("headers", {})
    fwd_headers = {pair[0].lower(): pair[1] for pair in fwd_req.get("headers", [])}

    added = {k: v for k, v in fwd_headers.items() if k not in client_headers}
    removed = {k: v for k, v in client_headers.items() if k not in fwd_headers}

    skip = {"content-length", "host", "x-ccproxy-flow-id"}
    added = {k: v for k, v in added.items() if k not in skip}
    removed = {k: v for k, v in removed.items() if k not in skip}

    if added:
        changes.append(
            {
                "type": "headers_added",
                "description": f"{len(added)} header(s) added by pipeline",
                "headers": json.dumps(added, indent=2),
            }
        )
    if removed:
        changes.append(
            {
                "type": "headers_removed",
                "description": f"{len(removed)} header(s) removed by pipeline",
                "headers": json.dumps(removed, indent=2),
            }
        )

    if fwd_headers.get("x-ccproxy-oauth-injected"):
        changes.append(
            {
                "type": "oauth_injected",
                "description": "OAuth token was injected by forward_oauth hook",
            }
        )

    client_body = client.get("body")
    if isinstance(client_body, dict) and isinstance(forwarded_body, dict):
        client_keys = set(client_body.keys())
        fwd_keys = set(forwarded_body.keys())

        if "messages" in client_keys and "contents" in fwd_keys:
            changes.append(
                {
                    "type": "body_format_transform",
                    "description": "Body transformed from OpenAI format (messages) to Gemini format (contents)",
                }
            )
        elif "messages" in fwd_keys and "contents" in client_keys:
            changes.append(
                {
                    "type": "body_format_transform",
                    "description": (
                        "Body transformed from Gemini format (contents) to Anthropic/OpenAI format (messages)"
                    ),
                }
            )

        if "system" not in client_keys and "system" in fwd_keys:
            changes.append(
                {
                    "type": "system_injected",
                    "description": "System prompt was injected (likely by compliance)",
                }
            )
        elif "system" in client_keys and "system" in fwd_keys and client_body["system"] != forwarded_body["system"]:
            changes.append(
                {
                    "type": "system_modified",
                    "description": "System prompt was modified (compliance prepended blocks)",
                }
            )

        new_keys = fwd_keys - client_keys
        for k in new_keys:
            val = forwarded_body.get(k)
            if isinstance(val, dict) and ("messages" in val or "contents" in val):
                changes.append(
                    {
                        "type": "body_wrapped",
                        "description": f"Body was wrapped inside '{k}' field (compliance body_wrapper)",
                    }
                )

    if not changes:
        changes.append(
            {
                "type": "no_changes",
                "description": "Client request and forwarded request are identical (passthrough)",
            }
        )

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

    client_text = client_parsed.get("raw", "")
    console.print(Panel(client_text, title=f"Client Request (pre-pipeline) -- {flow_id[:8]}"))

    fwd_req = forwarded_flow["request"]
    fwd_url = f"{fwd_req['method']} {fwd_req['scheme']}://{fwd_req['pretty_host']}{fwd_req['path']}"
    fwd_parts = [fwd_url, ""]
    for pair in fwd_req.get("headers", []):
        fwd_parts.append(f"  {pair[0]}: {pair[1]}")
    if forwarded_body:
        fwd_parts.append("")
        fwd_parts.append(json.dumps(forwarded_body, indent=2)[:2000])
    console.print(Panel("\n".join(fwd_parts), title=f"Forwarded Request (post-pipeline) -- {flow_id[:8]}"))

    table = Table(title="Pipeline Changes", show_header=True, header_style="bold")
    table.add_column("Type", style="cyan", width=25)
    table.add_column("Description")
    for c in changes:
        table.add_row(c["type"], c["description"])
    console.print(table)

    if response_body is not None:
        body_str = json.dumps(response_body, indent=2) if isinstance(response_body, dict) else str(response_body)
        console.print(
            Panel(
                Syntax(body_str[:3000], "json", theme="monokai", word_wrap=True),
                title=f"Response -- {flow_id[:8]}",
            )
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect a ccproxy flow: client vs forwarded request")
    parser.add_argument("flow_id", help="Flow ID prefix (8+ chars from `ccproxy flows list`)")
    parser.add_argument("--with-response", action="store_true", help="Also fetch and display the response body")
    parser.add_argument("--json", action="store_true", help="Output as structured JSON")
    args = parser.parse_args()

    try:
        with _make_client() as client:
            flow_id = client.resolve_id(args.flow_id)

            # Fetch the page-grouped HAR from the ccproxy.dump mitmproxy command.
            har = json.loads(client.dump_har(flow_id))
            entries = har["log"]["entries"]
            forwarded_entry = entries[0]  # [fwdreq, fwdres]
            client_entry = entries[1]  # [clireq, fwdres]

            client_parsed = _client_entry_to_parsed(client_entry)
            forwarded_flow = _forwarded_entry_to_flow(forwarded_entry)

            fwd_post = forwarded_entry["request"].get("postData") or {}
            fwd_body = _parse_body_text(fwd_post.get("text"))

            response_body: Any = None
            if args.with_response:
                res_content = forwarded_entry.get("response", {}).get("content") or {}
                response_body = _parse_body_text(res_content.get("text"))

            changes = _compute_changes(client_parsed, forwarded_flow, fwd_body if isinstance(fwd_body, dict) else None)

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
                        "method": forwarded_flow["request"]["method"],
                        "url": (
                            f"{forwarded_flow['request']['scheme']}://"
                            f"{forwarded_flow['request']['pretty_host']}"
                            f"{forwarded_flow['request']['path']}"
                        ),
                        "headers": {pair[0].lower(): pair[1] for pair in forwarded_flow["request"].get("headers", [])},
                        "body": fwd_body,
                    },
                    "changes": changes,
                }
                if response_body is not None:
                    output["response"] = {
                        "status": (forwarded_flow.get("response") or {}).get("status_code"),
                        "body": response_body,
                    }
                json.dump(output, sys.stdout, indent=2, default=str)
                print()
            else:
                _print_rich(
                    client_parsed,
                    forwarded_flow,
                    fwd_body if isinstance(fwd_body, dict) else None,
                    response_body,
                    changes,
                    flow_id,
                )

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
