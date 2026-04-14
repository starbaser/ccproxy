"""Query mitmweb flows REST API for debugging LLM request pipelines."""

from __future__ import annotations

import base64
import contextlib
import difflib
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

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

    def get_response_body(self, flow_id: str) -> bytes:
        resp = self._client.get(f"/flows/{flow_id}/response/content.data")
        resp.raise_for_status()
        return resp.content

    def get_client_request(self, flow_id: str) -> dict[str, Any]:
        """Fetch the pre-pipeline client request as a structured dict.

        Returns ``{method, url, headers: [{name, value}, ...], body_text}``.
        """
        resp = self._client.get(f"/flows/{flow_id}/request/content/client-request")
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and "text" in data:
            text = str(data["text"])
        elif isinstance(data, list) and data:
            text = str(data[0][1]) if isinstance(data[0], list) else str(data[0])
        else:
            text = resp.text
        return _parse_client_request_text(text)

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


class Flows(BaseModel):
    """Inspect mitmweb flows for debugging the request pipeline.

    Subcommands:
      list                       Tabular listing of captured flows (use --json for raw).
      req <id-prefix>            Dump forwarded request + response as a HAR 1.2 file.
      res <id-prefix>            Alias for `req` — same HAR output.
      client <id-prefix>         HAR with the pre-pipeline client request as the
                                 request side (original URL/headers/body before
                                 OAuth substitution or lightllm transform).
      diff <id1> <id2>           Unified diff of two request bodies.

    HAR output is standard HTTP Archive 1.2 JSON — pipe to a file and open in
    Chrome DevTools / Charles / Fiddler, or query with jq:
      ccproxy flows req abc | jq '.log.entries[0].request.url'
      ccproxy flows req abc > flow.har
    """

    args: Annotated[list[str] | None, tyro.conf.Positional] = None
    """Subcommand and flow IDs, e.g. `list`, `req abc123`, `diff a1 b2`."""

    json_output: Annotated[bool, tyro.conf.arg(name="json")] = False
    """Emit raw JSON for `list` (no-op for other subcommands — they are HAR JSON)."""

    filter: str | None = None
    """Filter `list` output by URL regex pattern (case-insensitive)."""

    clear: bool = False
    """Clear all captured flows from mitmweb."""



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
        flows = [
            f for f in flows
            if pat.search(f["request"]["pretty_host"] + f["request"]["path"])
        ]

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


_CLIENT_REQUEST_HEADERS_MARKER = "--- Headers ---"
_CLIENT_REQUEST_BODY_MARKER = "--- Body ---"


def _parse_client_request_text(text: str) -> dict[str, Any]:
    """Parse the rendered pre-pipeline client request text into structured fields.

    Input format (produced by ``ClientRequestContentview``)::

        {METHOD} {scheme}://{host}:{port}{path}

        --- Headers ---
          {name}: {value}
          ...

        --- Body ---
        {body or "(empty)"}
    """
    method = ""
    url = ""
    headers: list[dict[str, str]] = []
    body_text = ""

    lines = text.splitlines()
    if lines:
        first = lines[0].strip()
        if " " in first:
            method, url = first.split(" ", 1)
        else:
            url = first

    section: str | None = None
    body_lines: list[str] = []
    for line in lines[1:]:
        stripped = line.strip()
        if stripped == _CLIENT_REQUEST_HEADERS_MARKER:
            section = "headers"
            continue
        if stripped == _CLIENT_REQUEST_BODY_MARKER:
            section = "body"
            continue
        if section == "headers":
            if not stripped:
                continue
            if ":" in stripped:
                name, value = stripped.split(":", 1)
                headers.append({"name": name.strip(), "value": value.strip()})
        elif section == "body":
            body_lines.append(line)

    if body_lines:
        body_text = "\n".join(body_lines)
        if body_text == "(empty)":
            body_text = ""

    return {"method": method, "url": url, "headers": headers, "body_text": body_text}


def _safe_fetch(fetch: Any, flow_id: str) -> bytes:
    """Fetch a flow body, swallowing 5xx (e.g. SSE streams that can't be replayed)."""
    try:
        return fetch(flow_id)  # type: ignore[no-any-return]
    except httpx.HTTPStatusError:
        return b""


def _headers_to_har(headers: list[list[str]]) -> list[dict[str, str]]:
    return [{"name": pair[0], "value": pair[1]} for pair in headers]


def _query_string(path: str) -> list[dict[str, str]]:
    parsed = urlsplit(path)
    if not parsed.query:
        return []
    out: list[dict[str, str]] = []
    for kv in parsed.query.split("&"):
        if "=" in kv:
            k, v = kv.split("=", 1)
        else:
            k, v = kv, ""
        out.append({"name": k, "value": v})
    return out


def _body_to_har_text(raw: bytes) -> tuple[str, str | None]:
    """Decode body bytes for HAR. Returns (text, encoding) where encoding is 'base64' for binary."""
    if not raw:
        return "", None
    try:
        return raw.decode("utf-8"), None
    except UnicodeDecodeError:
        return base64.b64encode(raw).decode("ascii"), "base64"


def _ms_delta(later: float | None, earlier: float | None) -> float:
    if later is None or earlier is None:
        return -1.0
    return 1000.0 * (later - earlier)


def _build_timings(req: dict[str, Any], res: dict[str, Any] | None, server_conn: dict[str, Any]) -> dict[str, float]:
    connect = _ms_delta(server_conn.get("timestamp_tcp_setup"), server_conn.get("timestamp_start"))
    ssl = _ms_delta(server_conn.get("timestamp_tls_setup"), server_conn.get("timestamp_tcp_setup"))

    req_end = req.get("timestamp_end")
    req_start = req.get("timestamp_start")
    send = _ms_delta(req_end, req_start)
    if send < 0:
        send = 0.0

    if res and req_end is not None:
        wait_v = _ms_delta(res.get("timestamp_start"), req_end)
        wait = wait_v if wait_v >= 0 else 0.0
    else:
        wait = 0.0

    if res:
        receive_v = _ms_delta(res.get("timestamp_end"), res.get("timestamp_start"))
        receive = receive_v if receive_v >= 0 else 0.0
    else:
        receive = 0.0

    return {"connect": connect, "ssl": ssl, "send": send, "wait": wait, "receive": receive}


def _build_har_request(
    flow: dict[str, Any],
    body: bytes,
    *,
    client_req: dict[str, Any] | None,
) -> dict[str, Any]:
    req = flow["request"]

    if client_req:
        method = client_req["method"]
        url = client_req["url"]
        headers_har = client_req["headers"]
        body_text = client_req["body_text"]
        body_encoding: str | None = None
        body_size = len(body_text.encode("utf-8")) if body_text else 0
    else:
        method = req["method"]
        url = f"{req['scheme']}://{req['pretty_host']}{req['path']}"
        headers_har = _headers_to_har(req.get("headers", []))
        body_text, body_encoding = _body_to_har_text(body)
        body_size = len(body)

    mime_type = next((h["value"] for h in headers_har if h["name"].lower() == "content-type"), "")

    request_entry: dict[str, Any] = {
        "method": method,
        "url": url,
        "httpVersion": req.get("http_version", "HTTP/1.1"),
        "cookies": [],
        "headers": headers_har,
        "queryString": _query_string(url) or _query_string(req.get("path", "")),
        "headersSize": -1,
        "bodySize": body_size,
    }

    if method in {"POST", "PUT", "PATCH"} or body_text or body_encoding:
        post_data: dict[str, Any] = {"mimeType": mime_type, "text": body_text, "params": []}
        if body_encoding:
            post_data["encoding"] = body_encoding
        request_entry["postData"] = post_data

    return request_entry


def _build_har_response(flow: dict[str, Any], body: bytes) -> dict[str, Any]:
    res = flow.get("response")
    if not res:
        return {
            "status": 0,
            "statusText": "",
            "httpVersion": "",
            "cookies": [],
            "headers": [],
            "content": {"size": 0, "mimeType": "", "text": ""},
            "redirectURL": "",
            "headersSize": -1,
            "bodySize": -1,
        }

    headers_har = _headers_to_har(res.get("headers", []))
    mime_type = next((h["value"] for h in headers_har if h["name"].lower() == "content-type"), "")
    redirect_url = next((h["value"] for h in headers_har if h["name"].lower() == "location"), "")

    body_text, body_encoding = _body_to_har_text(body)
    content: dict[str, Any] = {
        "size": len(body),
        "mimeType": mime_type,
        "text": body_text,
    }
    if body_encoding:
        content["encoding"] = body_encoding

    return {
        "status": res.get("status_code", 0),
        "statusText": res.get("reason", ""),
        "httpVersion": res.get("http_version", "HTTP/1.1"),
        "cookies": [],
        "headers": headers_har,
        "content": content,
        "redirectURL": redirect_url,
        "headersSize": -1,
        "bodySize": len(body),
    }


def _build_har_entry(
    flow: dict[str, Any],
    req_body: bytes,
    res_body: bytes,
    *,
    client_req: dict[str, Any] | None = None,
) -> dict[str, Any]:
    req = flow["request"]
    res = flow.get("response")
    server_conn = flow.get("server_conn") or {}

    timings = _build_timings(req, res, server_conn)
    started = req.get("timestamp_start")
    started_iso = (
        _dt(started).isoformat() if started is not None else datetime.now(UTC).isoformat()
    )
    total_time = sum(v for v in timings.values() if v >= 0)

    entry: dict[str, Any] = {
        "startedDateTime": started_iso,
        "time": total_time,
        "request": _build_har_request(flow, req_body, client_req=client_req),
        "response": _build_har_response(flow, res_body),
        "cache": {},
        "timings": timings,
    }

    peername = server_conn.get("peername")
    if isinstance(peername, list) and peername:
        entry["serverIPAddress"] = str(peername[0])

    return entry


def _build_har(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "log": {
            "version": "1.2",
            "creator": {"name": "ccproxy", "version": "dev"},
            "entries": [entry],
        }
    }


def _do_inspect(
    client: MitmwebClient,
    *,
    action: str,
    id_prefix: str,
) -> None:
    flow_id = client.resolve_id(id_prefix)

    flows = client.list_flows()
    flow = next((f for f in flows if f["id"] == flow_id), None)
    if flow is None:
        print(f"error: flow {flow_id} not found", file=sys.stderr)
        sys.exit(1)

    req_body = _safe_fetch(client.get_request_body, flow_id)
    res_body = _safe_fetch(client.get_response_body, flow_id)

    if action == "client":
        client_req = client.get_client_request(flow_id)
        entry = _build_har_entry(flow, req_body, res_body, client_req=client_req)
    else:
        entry = _build_har_entry(flow, req_body, res_body)

    print(json.dumps(_build_har(entry), indent=2))


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
                _do_list(console, client, json_output=cmd.json_output, filter_pat=cmd.filter)

            elif action in ("req", "res", "client"):
                if not ids:
                    console.print(f"[red]{action} requires a flow ID prefix[/red]")
                    sys.exit(1)
                _do_inspect(client, action=action, id_prefix=ids[0])

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
