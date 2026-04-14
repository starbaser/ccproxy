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

Built-in CLI that wraps the REST API:

```bash
ccproxy flows list [--filter REGEX] [--json]    # List flows
ccproxy flows dump <id-prefix>                   # 1-page / 2-entry HAR 1.2 file
ccproxy flows diff <id1> <id2>                   # Unified diff of two request bodies
ccproxy flows clear                              # Clear all flows
```

`dump` emits HAR 1.2 JSON built server-side by the `ccproxy.dump` mitmproxy
command. One page per flow (`pages[0].id == flow.id`), two complete entries
by documented index:

- `entries[0] = [fwdreq, fwdres]` — the real flow, authoritative (forwarded
  request + upstream response).
- `entries[1] = [clireq, fwdres]` — clone with `.request` rebuilt from the
  pre-pipeline `ClientRequest` snapshot. Response is duplicated so the HAR
  pair stays schema-complete.

Query by index with jq:

```bash
ccproxy flows dump abc | jq '.log.pages[0].id'              # flow id
ccproxy flows dump abc | jq '.log.entries[0].request.url'   # forwarded URL
ccproxy flows dump abc | jq '.log.entries[1].request.url'   # pre-pipeline URL
ccproxy flows dump abc | jq '.log.entries[0].response.status'
ccproxy flows dump abc > /tmp/flow.har  # Open in Chrome DevTools / Charles / Fiddler
```

Flow ID prefixes: the list shows 8-character IDs; any unique prefix works for lookup.
