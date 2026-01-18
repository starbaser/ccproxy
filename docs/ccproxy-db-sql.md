# ccproxy db sql

Execute SQL queries against the ccproxy MITM HTTP traces database.

## Synopsis

```bash
ccproxy db sql "<query>"
ccproxy db sql --file <path>
echo "<query>" | ccproxy db sql
```

## Options

| Option | Alias | Description |
|--------|-------|-------------|
| `--file` | `-f` | Read SQL from file |
| `--json` | `-j` | Output as JSON |
| `--csv` | `-c` | Output as CSV |

## Database Configuration

The command reads the database URL from (in order):
1. `CCPROXY_DATABASE_URL` environment variable
2. `DATABASE_URL` environment variable
3. `ccproxy.yaml` → `litellm.environment.CCPROXY_DATABASE_URL`

Current production URL: `postgresql://ccproxy:test@localhost:5432/ccproxy_mitm`

## Schema: CCProxy_HttpTraces

```sql
CREATE TABLE "CCProxy_HttpTraces" (
    trace_id              TEXT PRIMARY KEY,
    method                TEXT NOT NULL,
    url                   TEXT NOT NULL,
    host                  TEXT NOT NULL,
    path                  TEXT NOT NULL,
    request_headers       JSONB DEFAULT '{}',
    request_body          BYTEA,
    request_body_size     INTEGER DEFAULT 0,
    request_content_type  TEXT,
    status_code           INTEGER,
    response_headers      JSONB DEFAULT '{}',
    response_body         BYTEA,
    response_body_size    INTEGER DEFAULT 0,
    response_content_type TEXT,
    start_time            TIMESTAMP(3) NOT NULL,
    end_time              TIMESTAMP(3),
    duration_ms           DOUBLE PRECISION,
    client_ip             TEXT,
    server_ip             TEXT,
    server_port           INTEGER,
    is_https              BOOLEAN DEFAULT FALSE,
    error_message         TEXT,
    error_type            TEXT,
    traffic_type          TEXT DEFAULT 'unknown',
    created_at            TIMESTAMP(3) DEFAULT CURRENT_TIMESTAMP,
    proxy_direction       INTEGER DEFAULT 0,  -- 0=reverse, 1=forward
    session_id            TEXT
);
```

### Key Fields

| Field | Description |
|-------|-------------|
| `proxy_direction` | 0 = reverse (client→LiteLLM), 1 = forward (LiteLLM→provider) |
| `session_id` | Claude Code session ID (from `metadata.user_id`) |
| `traffic_type` | `llm`, `mcp`, `web`, `other`, `unknown` |
| `duration_ms` | Request duration in milliseconds |
| `host` | Target host (e.g., `api.anthropic.com`, `localhost`) |

### Indexes

- `created_at` - For time-range queries
- `start_time` - For duration analysis
- `host` - For filtering by provider
- `status_code` - For error analysis
- `traffic_type` - For traffic categorization
- `proxy_direction` - For direction filtering
- `session_id` - For session correlation

## Common Queries

### Count total traces
```bash
ccproxy db sql 'SELECT COUNT(*) FROM "CCProxy_HttpTraces"'
```

### Recent traces
```bash
ccproxy db sql 'SELECT trace_id, method, host, status_code, duration_ms
FROM "CCProxy_HttpTraces" ORDER BY created_at DESC LIMIT 10'
```

### Errors only
```bash
ccproxy db sql 'SELECT trace_id, host, status_code, error_message
FROM "CCProxy_HttpTraces" WHERE status_code >= 400 ORDER BY created_at DESC'
```

### By provider
```bash
ccproxy db sql 'SELECT COUNT(*), host FROM "CCProxy_HttpTraces"
GROUP BY host ORDER BY count DESC'
```

### Forward proxy only (LiteLLM→providers)
```bash
ccproxy db sql 'SELECT * FROM "CCProxy_HttpTraces"
WHERE proxy_direction = 1 ORDER BY created_at DESC LIMIT 10'
```

### Slow requests (>5s)
```bash
ccproxy db sql 'SELECT trace_id, host, path, duration_ms
FROM "CCProxy_HttpTraces" WHERE duration_ms > 5000 ORDER BY duration_ms DESC'
```

### By session
```bash
ccproxy db sql 'SELECT COUNT(*), session_id FROM "CCProxy_HttpTraces"
WHERE session_id IS NOT NULL GROUP BY session_id'
```

### Traffic type breakdown
```bash
ccproxy db sql 'SELECT traffic_type, COUNT(*) as count,
AVG(duration_ms) as avg_duration FROM "CCProxy_HttpTraces"
GROUP BY traffic_type ORDER BY count DESC'
```

### Time range (last hour)
```bash
ccproxy db sql "SELECT * FROM \"CCProxy_HttpTraces\"
WHERE created_at > NOW() - INTERVAL '1 hour' ORDER BY created_at DESC"
```

### Request/response body (with size check)
```bash
ccproxy db sql 'SELECT trace_id, request_body_size, response_body_size,
encode(request_body, '"'"'escape'"'"') as req_preview
FROM "CCProxy_HttpTraces"
WHERE request_body_size < 1000 AND request_body IS NOT NULL
LIMIT 5'
```

## Output Formats

### Table (default)
```
╭───────────────────────────┬────────┬───────────────────┬─────────────╮
│ trace_id                  │ method │ host              │ status_code │
├───────────────────────────┼────────┼───────────────────┼─────────────┤
│ abc123...                 │ POST   │ api.anthropic.com │ 200         │
╰───────────────────────────┴────────┴───────────────────┴─────────────╯
```

### JSON (`--json`)
```json
[{"trace_id": "abc123", "method": "POST", "host": "api.anthropic.com"}]
```

### CSV (`--csv`)
```csv
trace_id,method,host,status_code
abc123,POST,api.anthropic.com,200
```

## Notes

- Table name requires double quotes: `"CCProxy_HttpTraces"`
- JSONB fields (`request_headers`, `response_headers`) can be queried with `->` and `->>`
- Body fields are `BYTEA` - use `encode(field, 'escape')` to view as text
- `--json` and `--csv` are mutually exclusive
