# Flow API Reference

## Contents

- [mitmweb REST API](#mitmweb-rest-api)
- [Flow data model](#flow-data-model)
- [Content views](#content-views)
- [Authentication](#authentication)

---

## mitmweb REST API

All endpoints are on the inspector UI port (default 8083).

| Method | Endpoint | Purpose |
|--------|----------|---------|
| `GET` | `/flows` | List all captured flows (JSON array) |
| `GET` | `/flows/{id}/request/content.data` | Raw request body bytes (post-pipeline) |
| `GET` | `/flows/{id}/response/content.data` | Raw response body bytes |
| `GET` | `/flows/{id}/request/content/{view-name}` | Content view output for request |
| `POST` | `/clear` | Clear all flows (requires XSRF) |

### XSRF for POST

`POST /clear` requires a synthetic XSRF pair:
- Cookie: `_xsrf={random_hex}`
- Header: `X-XSRFToken={same_hex}`

---

## Flow data model

Each flow in `GET /flows` returns:

```json
{
  "id": "uuid-string",
  "request": {
    "method": "POST",
    "scheme": "https",
    "host": "api.anthropic.com",
    "port": 443,
    "path": "/v1/messages",
    "pretty_host": "api.anthropic.com",
    "headers": [["Header-Name", "value"], ...],
    "contentLength": 1234,
    "timestamp_start": 1234567890.123
  },
  "response": {
    "status_code": 200,
    "reason": "OK",
    "headers": [["Header-Name", "value"], ...],
    "contentLength": 5678,
    "timestamp_start": 1234567891.456
  },
  "client_conn": {
    "timestamp_start": 1234567890.0
  }
}
```

Headers are arrays of `[name, value]` pairs (not objects). Multiple headers with the same name appear as separate entries.

**Note**: `request` fields reflect the **post-pipeline** state (after hooks and transform). To see the pre-pipeline state, use the Client-Request content view.

---

## Content views

### Client-Request view

The custom `Client-Request` content view shows the pre-pipeline request snapshot captured by `InspectorAddon.request()` before any hook mutations.

**Endpoint**: `GET /flows/{id}/request/content/client-request`

**Response format**: `[[label, text], ...]` — extract `data[0][1]` for the text.

**Text format**:
```
POST https://api.anthropic.com:443/v1/messages

--- Headers ---
  content-type: application/json
  x-api-key: sk-ant-oat-ccproxy-anthropic
  anthropic-version: 2023-06-01
  user-agent: claude-code/1.0.42

--- Body ---
{
  "model": "claude-sonnet-4-5-20250929",
  "messages": [...]
}
```

This view is also accessible in the mitmweb UI by selecting a flow and switching to the "Client-Request" content view tab.

---

## Authentication

All REST API calls require:

```
Authorization: Bearer <web_password>
```

The token is:
- `inspector.mitmproxy.web_password` from config (if set as a string)
- Resolved from a `CredentialSource` (if set as `command`/`file`)
- Auto-generated on startup (if not set) — printed to logs with the mitmweb URL

The helper scripts (`list_flows.py`, `inspect_flow.py`) resolve the token automatically from config via `get_config()`.

---

## ccproxy flows CLI

Built-in CLI that wraps the REST API. All subcommands operate on a filtered **set** of flows. The `--jq` flag is repeatable; each filter consumes and produces a JSON array.

```bash
ccproxy flows list [--json] [--jq FILTER]...     # List flow set
ccproxy flows dump [--jq FILTER]...              # Multi-page HAR of flow set
ccproxy flows diff [--jq FILTER]...              # Sliding-window diff across set
ccproxy flows compare [--jq FILTER]...           # Per-flow client-vs-forwarded diff
ccproxy flows clear [--all] [--jq FILTER]...     # Clear flow set (--all bypasses filters)
```

`dump` emits multi-page HAR 1.2 JSON built server-side by the `ccproxy.dump` mitmproxy command. One page per flow, two entries per page:

- `entries[2i]` — forwarded request + raw provider response (authoritative).
- `entries[2i+1]` — pre-pipeline client request + post-transform client response.

Query with jq:

```bash
ccproxy flows dump | jq '.log.pages | length'              # page count
ccproxy flows dump | jq '.log.entries[0].request.url'      # first forwarded URL
ccproxy flows dump | jq '.log.entries[1].request.url'      # first pre-pipeline URL
ccproxy flows dump > all.har   # Open in Chrome DevTools / Charles / Fiddler
```

Filter examples:

```bash
ccproxy flows list --jq 'map(select(.request.path | startswith("/v1/messages")))'
ccproxy flows compare --jq 'map(select(.request.pretty_host == "api.anthropic.com"))'
```
