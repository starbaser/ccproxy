"""Query mitmweb flows REST API for debugging LLM request pipelines.

CLI subcommands:

    ccproxy flows list [--json] [--filter PAT]    Tabular listing
    ccproxy flows dump <id-prefix>                One-page HAR via ccproxy.dump
    ccproxy flows diff <id-a> <id-b>              Unified diff of two request bodies
    ccproxy flows clear                           Clear all captured flows

HAR output from `dump` is built server-side by the `ccproxy.dump` mitmproxy
command (registered by `MultiHARSaver` in `ccproxy.inspector.multi_har_saver`).
It delegates to `mitmproxy.addons.savehar.SaveHar.make_har()` — no parallel
HAR construction in ccproxy itself.
"""

from __future__ import annotations

import contextlib
import difflib
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import httpx
import humanize
import tyro
from pydantic import BaseModel
from rich.console import Console
from rich.syntax import Syntax
from rich.table import Table


class MitmwebClient:
    """Sync client for the mitmweb REST API."""

    def __init__(self, host: str, port: int, token: str) -> None:
        self._base = f"http://{host}:{port}"
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = httpx.Client(
            base_url=self._base,
            headers=headers,
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

    def resolve_id(self, prefix: str) -> str:
        """Find first flow whose id starts with prefix. Raises ValueError if no match."""
        for flow in self.list_flows():
            if flow["id"].startswith(prefix):
                return flow["id"]  # type: ignore[no-any-return]
        raise ValueError(f"No flow matching prefix {prefix!r}")

    def dump_har(self, flow_id: str) -> str:
        """Invoke the `ccproxy.dump` mitmproxy command; returns a JSON string."""
        resp = self._post(
            "/commands/ccproxy.dump",
            json_body={"arguments": [flow_id]},
        )
        payload = resp.json()
        if "error" in payload:
            raise ValueError(payload["error"])
        return str(payload["value"])

    def clear(self) -> None:
        self._post("/clear")

    def _post(
        self,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """POST with synthetic XSRF token pair (cookie + header), optional JSON body."""
        import secrets as _secrets

        if not self._xsrf:
            self._xsrf = _secrets.token_hex(16)
        self._client.cookies.set("_xsrf", self._xsrf)
        resp = self._client.post(
            path,
            headers={"X-XSRFToken": self._xsrf},
            json=json_body,
        )
        resp.raise_for_status()
        return resp

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> MitmwebClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


# --- CLI subcommand classes ---


class FlowsList(BaseModel):
    """Tabular listing of captured flows."""

    json_output: Annotated[bool, tyro.conf.arg(name="json")] = False
    """Emit raw JSON instead of a rendered table."""

    filter: str | None = None
    """Filter by URL regex pattern (case-insensitive, matched against host+path)."""


class FlowsDump(BaseModel):
    """Dump a flow as a page-grouped HAR 1.2 file.

    Output contains one page (the flow) with two complete HAR entries:

      entries[0]  [fwdreq, fwdres]  real flow — forwarded request + upstream response
      entries[1]  [clireq, fwdres]  clone — pre-pipeline client request (response duplicated)

    Pipe to a file and open in Chrome DevTools / Charles / Fiddler, or query
    with jq by index:

      ccproxy flows dump abc > flow.har
      ccproxy flows dump abc | jq '.log.entries[0].request.url'   # forwarded URL
      ccproxy flows dump abc | jq '.log.entries[1].request.url'   # pre-pipeline URL
      ccproxy flows dump abc | jq '.log.entries[0].response.status'
    """

    id_prefix: Annotated[str, tyro.conf.Positional]
    """Flow ID prefix (e.g. `abc123`)."""


class FlowsDiff(BaseModel):
    """Unified diff of two flow request bodies."""

    id_a: Annotated[str, tyro.conf.Positional]
    """First flow ID prefix."""

    id_b: Annotated[str, tyro.conf.Positional]
    """Second flow ID prefix."""


class FlowsClear(BaseModel):
    """Clear all captured flows from mitmweb."""


Flows = Annotated[
    Annotated[FlowsList, tyro.conf.subcommand(name="list")]
    | Annotated[FlowsDump, tyro.conf.subcommand(name="dump")]
    | Annotated[FlowsDiff, tyro.conf.subcommand(name="diff")]
    | Annotated[FlowsClear, tyro.conf.subcommand(name="clear")],
    tyro.conf.subcommand(
        name="flows",
        description="Inspect mitmweb flows for debugging the request pipeline.",
    ),
]


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
            web_password_cfg if isinstance(web_password_cfg, CredentialSource) else CredentialSource(**web_password_cfg)
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


def _dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=UTC)


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
        flows = [f for f in flows if pat.search(f["request"]["pretty_host"] + f["request"]["path"])]

    if json_output:
        for f in flows:
            ts = f["request"].get("timestamp_start")
            if ts:
                f["time"] = _dt(ts).strftime("%Y-%m-%d %H:%M:%S UTC")
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
    table.add_column("Time", width=12)

    for f in flows:
        req = f["request"]
        res = f.get("response") or {}
        code = str(res.get("status_code", "-"))
        code_style = "green" if code.startswith("2") else "red" if code != "-" else "dim"
        ua = _header_value(req.get("headers", []), "user-agent")
        ts = req.get("timestamp_start")
        rel_time = humanize.naturaltime(_dt(ts)) if ts else "-"

        table.add_row(
            f["id"][:8],
            req["method"],
            f"[{code_style}]{code}[/{code_style}]",
            req["pretty_host"],
            req["path"][:60],
            ua[:30] if ua else "[dim]-[/dim]",
            f"[dim]{rel_time}[/dim]",
        )

    console.print(table)


def _do_dump(client: MitmwebClient, *, id_prefix: str) -> None:
    """Resolve the flow id prefix and print the HAR JSON returned by ccproxy.dump."""
    flow_id = client.resolve_id(id_prefix)
    print(client.dump_har(flow_id))


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

    diff_lines = list(
        difflib.unified_diff(
            body_a.splitlines(keepends=True),
            body_b.splitlines(keepends=True),
            fromfile=f"flow:{id_a[:8]}",
            tofile=f"flow:{id_b[:8]}",
        )
    )

    if not diff_lines:
        console.print("[green]Bodies are identical.[/green]")
        return

    diff_text = "".join(diff_lines)
    console.print(Syntax(diff_text, "diff", theme="monokai", word_wrap=True))


def handle_flows(
    cmd: FlowsList | FlowsDump | FlowsDiff | FlowsClear,
    _config_dir: Path,
) -> None:
    """Dispatch flows subcommand actions by isinstance."""
    console = Console()
    try:
        with _make_client() as client:
            if isinstance(cmd, FlowsList):
                _do_list(
                    console,
                    client,
                    json_output=cmd.json_output,
                    filter_pat=cmd.filter,
                )
            elif isinstance(cmd, FlowsDump):
                _do_dump(client, id_prefix=cmd.id_prefix)
            elif isinstance(cmd, FlowsDiff):
                _do_diff(console, client, cmd.id_a, cmd.id_b)
            elif isinstance(cmd, FlowsClear):
                client.clear()
                console.print("Flows cleared.")
    except httpx.ConnectError:
        console.print("[red]Cannot connect to mitmweb. Is ccproxy running?[/red]")
        sys.exit(1)
    except httpx.HTTPStatusError as e:
        console.print(f"[red]HTTP {e.response.status_code}: {e.response.text[:200]}[/red]")
        sys.exit(1)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)
