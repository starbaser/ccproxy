"""Query mitmweb flows REST API for debugging LLM request pipelines."""

from __future__ import annotations

import contextlib
import difflib
import json
import re
import sys
from pathlib import Path
from typing import Annotated, Any

import attrs
import httpx
import tyro
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table


class MitmwebClient:
    """Sync client for the mitmweb REST API."""

    def __init__(self, host: str, port: int, token: str) -> None:
        self._base = f"http://{host}:{port}"
        self._client = httpx.Client(
            base_url=self._base,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10.0,
        )
        self._xsrf: str | None = None

    def list_flows(self) -> list[dict[str, Any]]:
        resp = self._client.get("/flows")
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    def get_request_body(self, flow_id: str) -> bytes:
        resp = self._client.get(f"/flows/{flow_id}/request/content.data")
        resp.raise_for_status()
        return resp.content

    def get_response_body(self, flow_id: str) -> bytes:
        resp = self._client.get(f"/flows/{flow_id}/response/content.data")
        resp.raise_for_status()
        return resp.content

    def get_client_request(self, flow_id: str) -> str:
        resp = self._client.get(f"/flows/{flow_id}/request/content/client-request")
        resp.raise_for_status()
        data = resp.json()
        # contentview returns [[label, text], ...] — extract the text
        if isinstance(data, list) and data:
            return str(data[0][1]) if isinstance(data[0], list) else str(data[0])
        return resp.text

    def _post(self, path: str) -> httpx.Response:
        """POST with synthetic XSRF token pair (cookie + header)."""
        import secrets as _secrets

        if not self._xsrf:
            self._xsrf = _secrets.token_hex(16)
        self._client.cookies.set("_xsrf", self._xsrf)
        resp = self._client.post(path, headers={"X-XSRFToken": self._xsrf})
        resp.raise_for_status()
        return resp

    def clear(self) -> None:
        self._post("/clear")

    def resolve_id(self, prefix: str) -> str:
        """Find first flow whose id starts with prefix. Raises ValueError if no match."""
        for flow in self.list_flows():
            if flow["id"].startswith(prefix):
                return flow["id"]  # type: ignore[no-any-return]
        raise ValueError(f"No flow matching prefix {prefix!r}")

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> MitmwebClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


@attrs.define
class Flows:
    """Query mitmweb flows for debugging request pipelines."""

    args: Annotated[list[str] | None, tyro.conf.Positional] = None
    """Subcommand and flow IDs: [list|req|res|client|diff] [id1] [id2]"""

    json: bool = False
    """Raw JSON output (list action only)."""

    filter: str | None = None
    """Filter list by URL regex pattern."""

    clear: bool = False
    """Clear all flows."""



def _make_client() -> MitmwebClient:
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
            web_password_cfg
            if isinstance(web_password_cfg, CredentialSource)
            else CredentialSource(**web_password_cfg)
        )
        token = source.resolve("mitmweb web_password") or ""
    else:
        token = ""

    return MitmwebClient(host=host, port=port, token=token)



def _header_value(headers: list[list[str]], name: str) -> str:
    """Extract a header value from the mitmweb headers array [[name, value], ...]."""
    for pair in headers:
        if pair[0].lower() == name.lower():
            return pair[1]
    return ""


def _do_list(
    console: Console,
    client: MitmwebClient,
    *,
    json_output: bool = False,
    filter_pat: str | None = None,
) -> None:
    flows = client.list_flows()

    if filter_pat:
        pat = re.compile(filter_pat, re.IGNORECASE)
        flows = [
            f for f in flows
            if pat.search(f["request"]["pretty_host"] + f["request"]["path"])
        ]

    if json_output:
        console.print_json(json.dumps(flows, indent=2))
        return

    if not flows:
        console.print("[dim]No flows.[/dim]")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("ID", width=8)
    table.add_column("Method", width=7)
    table.add_column("Code", width=5, justify="right")
    table.add_column("Host", max_width=35)
    table.add_column("Path", max_width=60)
    table.add_column("UA", max_width=30)

    for f in flows:
        req = f["request"]
        res = f.get("response") or {}
        code = str(res.get("status_code", "-"))
        code_style = "green" if code.startswith("2") else "red" if code != "-" else "dim"
        ua = _header_value(req.get("headers", []), "user-agent")

        table.add_row(
            f["id"][:8],
            req["method"],
            f"[{code_style}]{code}[/{code_style}]",
            req["pretty_host"],
            req["path"][:60],
            ua[:30] if ua else "[dim]-[/dim]",
        )

    console.print(table)


def _format_headers_table(headers: list[list[str]]) -> Table:
    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
    table.add_column("Header", style="cyan")
    table.add_column("Value")
    for name, value in headers:
        table.add_row(name, value)
    return table


def _format_body(raw: bytes) -> Syntax | str:
    text = raw.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text)
        pretty = json.dumps(parsed, indent=2)
        return Syntax(pretty, "json", theme="monokai", word_wrap=True)
    except (json.JSONDecodeError, ValueError):
        return text if text else "(empty)"


def _do_inspect(
    console: Console,
    client: MitmwebClient,
    *,
    action: str,
    id_prefix: str,
) -> None:
    flow_id = client.resolve_id(id_prefix)

    flows = client.list_flows()
    flow = next((f for f in flows if f["id"] == flow_id), None)
    if flow is None:
        console.print(f"[red]Flow {flow_id} not found[/red]")
        sys.exit(1)

    if action == "client":
        text = client.get_client_request(flow_id)
        console.print(Panel(text, title=f"Client Request (pre-pipeline) — {flow_id[:8]}"))
        return

    if action == "req":
        req = flow["request"]
        headers = req.get("headers", [])
        title = f"{req['method']} {req['scheme']}://{req['pretty_host']}{req['path']}"
        console.print(Panel(_format_headers_table(headers), title=title))
        body = client.get_request_body(flow_id)
        if body:
            console.print(Panel(_format_body(body), title="Request Body"))

    elif action == "res":
        res = flow.get("response")
        if not res:
            console.print("[yellow]No response yet.[/yellow]")
            return
        headers = res.get("headers", [])
        title = f"HTTP {res['status_code']} {res.get('reason', '')}"
        console.print(Panel(_format_headers_table(headers), title=title))
        body = client.get_response_body(flow_id)
        if body:
            console.print(Panel(_format_body(body), title="Response Body"))


def _do_diff(
    console: Console,
    client: MitmwebClient,
    prefix_a: str,
    prefix_b: str,
) -> None:
    id_a = client.resolve_id(prefix_a)
    id_b = client.resolve_id(prefix_b)

    body_a = client.get_request_body(id_a).decode("utf-8", errors="replace")
    body_b = client.get_request_body(id_b).decode("utf-8", errors="replace")

    with contextlib.suppress(json.JSONDecodeError, ValueError):
        body_a = json.dumps(json.loads(body_a), indent=2)
    with contextlib.suppress(json.JSONDecodeError, ValueError):
        body_b = json.dumps(json.loads(body_b), indent=2)

    diff_lines = list(difflib.unified_diff(
        body_a.splitlines(keepends=True),
        body_b.splitlines(keepends=True),
        fromfile=f"flow:{id_a[:8]}",
        tofile=f"flow:{id_b[:8]}",
    ))

    if not diff_lines:
        console.print("[green]Bodies are identical.[/green]")
        return

    diff_text = "".join(diff_lines)
    console.print(Syntax(diff_text, "diff", theme="monokai", word_wrap=True))



def handle_flows(cmd: Flows, _config_dir: Path) -> None:
    """Dispatch flows subcommand actions."""
    console = Console()
    args = cmd.args or []
    action = args[0] if args else "list"
    ids = args[1:]

    if cmd.clear:
        try:
            with _make_client() as client:
                client.clear()
            console.print("Flows cleared.")
        except httpx.HTTPError as e:
            console.print(f"[red]Failed to clear: {e}[/red]")
            sys.exit(1)
        if not args:
            return

    try:
        with _make_client() as client:
            if action == "list":
                _do_list(console, client, json_output=cmd.json, filter_pat=cmd.filter)

            elif action in ("req", "res", "client"):
                if not ids:
                    console.print(f"[red]{action} requires a flow ID prefix[/red]")
                    sys.exit(1)
                _do_inspect(console, client, action=action, id_prefix=ids[0])

            elif action == "diff":
                if len(ids) < 2:
                    console.print("[red]diff requires two flow ID prefixes[/red]")
                    sys.exit(1)
                _do_diff(console, client, ids[0], ids[1])

            else:
                console.print(f"[red]Unknown action: {action!r}[/red]")
                console.print("Actions: list, req, res, client, diff")
                sys.exit(1)

    except httpx.ConnectError:
        console.print("[red]Cannot connect to mitmweb. Is ccproxy running?[/red]")
        sys.exit(1)
    except httpx.HTTPStatusError as e:
        console.print(f"[red]HTTP {e.response.status_code}: {e.response.text[:200]}[/red]")
        sys.exit(1)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)
