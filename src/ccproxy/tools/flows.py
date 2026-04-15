"""Query mitmweb flows REST API for debugging LLM request pipelines.

All ``flows`` subcommands operate on a **set** of flows built by:

    GET /flows → config.flows.default_jq_filters → CLI --jq filters → final set

CLI subcommands:

    ccproxy flows list     [--json] [--jq FILTER]...
    ccproxy flows dump              [--jq FILTER]...
    ccproxy flows diff              [--jq FILTER]...
    ccproxy flows compare           [--jq FILTER]...
    ccproxy flows clear    [--all]  [--jq FILTER]...

HAR output from ``dump`` is built server-side by the ``ccproxy.dump`` mitmproxy
command (registered by ``MultiHARSaver`` in ``ccproxy.inspector.multi_har_saver``).
"""

from __future__ import annotations

import contextlib
import difflib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import httpx
import humanize
import tyro
from pydantic import BaseModel, Field
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
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

    def dump_har(self, flow_ids: list[str]) -> str:
        """Invoke ``ccproxy.dump`` with one or more flow ids; returns HAR JSON string."""
        if not flow_ids:
            raise ValueError("dump_har: flow_ids must be non-empty")
        resp = self._post(
            "/commands/ccproxy.dump",
            json_body={"arguments": [",".join(flow_ids)]},
        )
        payload = resp.json()
        if "error" in payload:
            raise ValueError(payload["error"])
        return str(payload["value"])

    def delete_flow(self, flow_id: str) -> None:
        """DELETE /flows/{id} — remove a single flow from mitmweb."""
        resp = self._client.delete(f"/flows/{flow_id}")
        resp.raise_for_status()

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


class _FlowsBase(BaseModel):
    """Shared fields for every ``flows`` subcommand."""

    jq_filter: Annotated[list[str], tyro.conf.arg(name="jq")] = Field(
        default_factory=list,
    )
    """Repeatable jq filter expression. Each must consume and produce a JSON array."""


class FlowsList(_FlowsBase):
    """Tabular listing of the resolved flow set."""

    json_output: Annotated[bool, tyro.conf.arg(name="json")] = False
    """Emit raw JSON instead of a rendered table."""


class FlowsDump(_FlowsBase):
    """Dump the resolved flow set as a multi-page HAR 1.2 file.

    Output contains one page per flow (pageref = flow.id), each page
    containing two HAR entries:

      entries[2i]     [fwdreq, fwdres]  real forwarded request + upstream response
      entries[2i+1]   [clireq, fwdres]  clone with .request from ClientRequest snapshot

    Pipe to a file and open in Chrome DevTools / Charles / Fiddler:

        ccproxy flows dump > all.har
        ccproxy flows dump --jq 'map(select(.id | startswith("abc")))' > one.har
    """


class FlowsDiff(_FlowsBase):
    """Sliding-window unified diff over the resolved flow set.

    For a set [f0, f1, f2, f3], emits 3 diffs: f0->f1, f1->f2, f2->f3.
    Narrow to exactly 2 flows for a classic pairwise diff.
    """


class FlowsCompare(_FlowsBase):
    """Per-flow client-request vs forwarded-request diff.

    For each flow in the set, shows what the ccproxy pipeline changed:
    diffs the pre-pipeline client request against the post-pipeline
    forwarded request.

    Supports 1+ flows. Each flow produces one diff panel.

        ccproxy flows compare
        ccproxy flows compare --jq 'map(select(.id | startswith("abc")))'
    """


class FlowsClear(_FlowsBase):
    """Clear the resolved flow set (or everything with --all)."""

    all: Annotated[bool, tyro.conf.arg(name="all")] = False
    """Bypass the filter pipeline and clear every flow."""


Flows = Annotated[
    Annotated[FlowsList, tyro.conf.subcommand(name="list")]
    | Annotated[FlowsDump, tyro.conf.subcommand(name="dump")]
    | Annotated[FlowsDiff, tyro.conf.subcommand(name="diff")]
    | Annotated[FlowsCompare, tyro.conf.subcommand(name="compare")]
    | Annotated[FlowsClear, tyro.conf.subcommand(name="clear")],
    tyro.conf.subcommand(
        name="flows",
        description="Inspect mitmweb flows. All commands operate on a set "
        "narrowed by --jq filters + config default_jq_filters.",
    ),
]


# --- Helpers ---


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


# --- JQ filter pipeline ---


def _run_jq(
    flows: list[dict[str, Any]],
    filter_str: str,
) -> list[dict[str, Any]]:
    """Run a jq filter over a flows list. Filter must produce a JSON array."""
    proc = subprocess.run(  # noqa: S603
        ["jq", "-c", filter_str],  # noqa: S607
        input=json.dumps(flows).encode(),
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise ValueError(f"jq filter failed: {proc.stderr.decode().strip()}")
    try:
        output = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise ValueError(f"jq output is not valid JSON: {e}") from e
    if not isinstance(output, list):
        raise ValueError(
            f"jq filter must produce a JSON array, got {type(output).__name__}",
        )
    return output  # type: ignore[no-any-return]


def _resolve_flow_set(
    client: MitmwebClient,
    cmd: _FlowsBase,
    flows_cfg: Any,
) -> list[dict[str, Any]]:
    """Build the operating set: raw -> default filters -> CLI filters."""
    raw = client.list_flows()
    filters = [*flows_cfg.default_jq_filters, *cmd.jq_filter]
    if not filters:
        return raw
    return _run_jq(raw, " | ".join(filters))


# --- Per-command handlers ---


def _do_list(
    console: Console,
    flow_set: list[dict[str, Any]],
    *,
    json_output: bool = False,
) -> None:
    """Render a pre-resolved flow set as a table or JSON."""
    if json_output:
        for f in flow_set:
            ts = f["request"].get("timestamp_start")
            if ts:
                f["time"] = _dt(ts).strftime("%Y-%m-%d %H:%M:%S UTC")
        console.print_json(json.dumps(flow_set, indent=2))
        return

    if not flow_set:
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

    for f in flow_set:
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


def _do_dump(client: MitmwebClient, flow_set: list[dict[str, Any]]) -> None:
    """Dump all flows in the set as a multi-page HAR."""
    if not flow_set:
        print("No flows in set.", file=sys.stderr)
        sys.exit(1)
    flow_ids = [f["id"] for f in flow_set]
    print(client.dump_har(flow_ids))


def _format_body(text: str | None) -> str:
    """Try to pretty-format a body string as JSON; fall back to raw."""
    if not text:
        return ""
    with contextlib.suppress(json.JSONDecodeError, ValueError):
        return json.dumps(json.loads(text), indent=2)
    return text


def _do_diff(
    console: Console,
    client: MitmwebClient,
    flow_set: list[dict[str, Any]],
) -> None:
    """Sliding-window diff over the set."""
    if len(flow_set) < 2:
        console.print(
            f"[yellow]diff needs at least 2 flows in the set (got {len(flow_set)})[/yellow]",
        )
        sys.exit(1)

    for i in range(len(flow_set) - 1):
        a, b = flow_set[i], flow_set[i + 1]
        id_a, id_b = a["id"], b["id"]

        body_a = client.get_request_body(id_a).decode("utf-8", errors="replace")
        body_b = client.get_request_body(id_b).decode("utf-8", errors="replace")

        body_a = _format_body(body_a) or body_a
        body_b = _format_body(body_b) or body_b

        diff_lines = list(
            difflib.unified_diff(
                body_a.splitlines(keepends=True),
                body_b.splitlines(keepends=True),
                fromfile=f"flow:{id_a[:8]}",
                tofile=f"flow:{id_b[:8]}",
            )
        )

        if i > 0:
            console.print(Rule())

        if not diff_lines:
            console.print(f"[green]{id_a[:8]} → {id_b[:8]}: bodies are identical.[/green]")
            continue

        diff_text = "".join(diff_lines)
        console.print(Syntax(diff_text, "diff", theme="monokai", word_wrap=True))


def _do_compare(
    console: Console,
    client: MitmwebClient,
    flow_set: list[dict[str, Any]],
) -> None:
    """Per-flow client-request vs forwarded-request diff."""
    if not flow_set:
        console.print("[yellow]No flows in set[/yellow]")
        sys.exit(1)

    flow_ids = [f["id"] for f in flow_set]
    har = json.loads(client.dump_har(flow_ids))
    entries = har["log"]["entries"]

    for i in range(0, len(entries), 2):
        fwd_entry = entries[i]
        cli_entry = entries[i + 1]
        flow_id = har["log"]["pages"][i // 2]["id"]

        fwd_url = fwd_entry["request"]["url"]
        cli_url = cli_entry["request"]["url"]
        fwd_body = _format_body(fwd_entry["request"].get("postData", {}).get("text"))
        cli_body = _format_body(cli_entry["request"].get("postData", {}).get("text"))

        if i > 0:
            console.print(Rule())

        if cli_url != fwd_url:
            console.print(
                Panel(
                    f"[red]- {cli_url}[/red]\n[green]+ {fwd_url}[/green]",
                    title=f"URL change — {flow_id[:8]}",
                )
            )

        diff_lines = list(
            difflib.unified_diff(
                cli_body.splitlines(keepends=True),
                fwd_body.splitlines(keepends=True),
                fromfile=f"client:{flow_id[:8]}",
                tofile=f"forwarded:{flow_id[:8]}",
            )
        )

        if not diff_lines:
            console.print(f"[green]{flow_id[:8]}: request bodies are identical.[/green]")
            continue

        diff_text = "".join(diff_lines)
        console.print(
            Panel(
                Syntax(diff_text, "diff", theme="monokai", word_wrap=True),
                title=f"Body diff — {flow_id[:8]}",
            )
        )


def _do_clear(
    console: Console,
    client: MitmwebClient,
    flow_set: list[dict[str, Any]],
    *,
    clear_all: bool,
) -> None:
    """Clear the set (or everything if --all)."""
    if clear_all:
        client.clear()
        console.print("All flows cleared.")
        return
    if not flow_set:
        console.print("No flows in set.")
        return
    for flow in flow_set:
        client.delete_flow(flow["id"])
    console.print(f"Cleared {len(flow_set)} flow(s).")


# --- Dispatch ---


def handle_flows(
    cmd: FlowsList | FlowsDump | FlowsDiff | FlowsCompare | FlowsClear,
    _config_dir: Path,
) -> None:
    """Dispatch flows subcommand actions by isinstance."""
    from ccproxy.config import get_config

    console = Console()
    config = get_config()
    try:
        with _make_client() as client:
            flow_set = _resolve_flow_set(client, cmd, config.flows)
            if isinstance(cmd, FlowsList):
                _do_list(console, flow_set, json_output=cmd.json_output)
            elif isinstance(cmd, FlowsDump):
                _do_dump(client, flow_set)
            elif isinstance(cmd, FlowsDiff):
                _do_diff(console, client, flow_set)
            elif isinstance(cmd, FlowsCompare):
                _do_compare(console, client, flow_set)
            elif isinstance(cmd, FlowsClear):
                _do_clear(console, client, flow_set, clear_all=cmd.all)
    except httpx.ConnectError:
        console.print("[red]Cannot connect to mitmweb. Is ccproxy running?[/red]")
        sys.exit(1)
    except httpx.HTTPStatusError as e:
        console.print(f"[red]HTTP {e.response.status_code}: {e.response.text[:200]}[/red]")
        sys.exit(1)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)
