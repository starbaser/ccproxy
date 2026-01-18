# MITM Traffic Capture

## Overview

The MITM (Man-in-the-Middle) feature captures all HTTP/HTTPS traffic passing through ccproxy using [mitmproxy](https://mitmproxy.org/). Traffic is stored in PostgreSQL for analysis and debugging.

**Key capabilities:**
- Capture requests/responses with headers and bodies
- Traffic classification (llm, mcp, web, other)
- Proxy direction tracking (reverse vs forward)
- Session ID extraction from Claude Code metadata
- Automatic body truncation and compression
- Asynchronous buffered writes
- Works transparently with `ccproxy run`

**Recent Changes:**
- Dedicated `ccproxy-db` PostgreSQL container for MITM traces (port 5432)
- LiteLLM database (`litellm-db`) now optional and commented out by default
- New `proxy_direction` field to distinguish client→LiteLLM vs LiteLLM→provider traffic
- New `session_id` field to link related requests across proxy layers

## Prerequisites

### Dependencies

```bash
# Required packages
uv add mitmproxy prisma

# Generate Prisma client
prisma generate
```

### PostgreSQL Database

The MITM traces use a **dedicated database container** (`ccproxy-db`):

- **MITM traces database**: `postgresql://ccproxy:test@localhost:5432/ccproxy` (dedicated container: `ccproxy-db`)
- **LiteLLM database** (optional): `postgresql://ccproxy:test@localhost:5433/litellm` (commented out by default in `compose.yaml`)

Set the connection URL via environment variable:

```bash
# MITM database (preferred)
export CCPROXY_DATABASE_URL="postgresql://ccproxy:test@localhost:5432/ccproxy"

# Falls back to DATABASE_URL if CCPROXY_DATABASE_URL is not set
export DATABASE_URL="postgresql://ccproxy:test@localhost:5432/ccproxy"
```

> **Note:** The docker compose creates a dedicated `ccproxy-db` PostgreSQL container for MITM traces. The LiteLLM database (`litellm-db`) is commented out by default and can be enabled if needed.

### Apply Schema

Start the database container and apply the schema:

```bash
# Start database container
docker compose up -d

# Apply schema to create the CCProxy_HttpTraces table
DATABASE_URL="postgresql://ccproxy:test@localhost:5432/ccproxy" prisma db push
```

## Configuration

Configure MITM in `~/.ccproxy/ccproxy.yaml`:

```yaml
ccproxy:
  mitm:
    enabled: true              # Enable traffic capture
    port: 8081                 # Mitmproxy listen port
    upstream_proxy: "http://localhost:4000"  # LiteLLM proxy URL
    database_url: "postgresql://ccproxy:test@localhost:5432/ccproxy"  # MITM database URL
    max_body_size: 0              # Max body bytes to capture (0 = unlimited)
    capture_bodies: true       # Store request/response bodies
    excluded_hosts: []         # Hosts to skip (optional)
    cert_dir: null             # Custom SSL cert directory (optional)
    debug: false               # Enable debug logging
    llm_hosts:                 # Additional LLM provider hosts
      - "api.anthropic.com"
      - "api.openai.com"
      - "generativelanguage.googleapis.com"
```

### MitmConfig Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `false` | Enable MITM capture |
| `port` | int | `8081` | Mitmproxy listening port |
| `upstream_proxy` | str | `"http://localhost:4000"` | Upstream proxy (LiteLLM) |
| `database_url` | str\|None | `None` | PostgreSQL connection URL for traces |
| `max_body_size` | int | `0` | Maximum body size in bytes (0 = unlimited) |
| `capture_bodies` | bool | `true` | Capture request/response bodies |
| `excluded_hosts` | list[str] | `[]` | Hosts to exclude from capture |
| `cert_dir` | Path\|None | `None` | Custom SSL certificate directory |
| `debug` | bool | `false` | Enable debug logging |
| `llm_hosts` | list[str] | (see config) | LLM provider hosts for classification |

## CLI Commands

### Start with MITM Capture

```bash
# Start LiteLLM proxy with MITM capture enabled
ccproxy start --mitm --detach

# This starts the dual-proxy architecture:
# - MITM reverse proxy on :4000 (receives client requests)
# - LiteLLM on random internal port
# - MITM forward proxy on :8081 (captures outbound API calls)
```

**Options:**
- `--mitm`: Enable MITM traffic capture
- `--detach` / `-d`: Run in background

**Process management:**
- LiteLLM PID file: `~/.ccproxy/litellm.lock`
- MITM reverse PID file: `~/.ccproxy/.mitm-reverse.lock`
- MITM forward PID file: `~/.ccproxy/.mitm-forward.lock`
- Log files: `~/.ccproxy/litellm.log`, `~/.ccproxy/mitm-*.log`

### Stop All Proxies

```bash
ccproxy stop  # Stops LiteLLM and both MITM proxies
```

Sends `SIGTERM` for graceful shutdown, falls back to `SIGKILL` if needed.

### Check Status

```bash
# Human-readable output
ccproxy status

# JSON output
ccproxy status --json
```

## Database Schema

### CCProxy_HttpTraces Table

```sql
-- Request data
trace_id              TEXT PRIMARY KEY  -- UUID
proxy_direction       INT               -- 0=reverse (client→LiteLLM), 1=forward (LiteLLM→provider)
session_id            TEXT              -- Claude Code session ID (extracted from metadata.user_id)
method                TEXT              -- HTTP method (GET, POST, etc.)
url                   TEXT              -- Full URL
host                  TEXT              -- Hostname
path                  TEXT              -- URL path
request_headers       JSONB             -- Request headers as JSON
request_body          BYTEA             -- Base64-encoded body (truncated)
request_body_size     INT               -- Original body size
request_content_type  TEXT              -- Content-Type header

-- Response data
status_code           INT               -- HTTP status code (null if error)
response_headers      JSONB             -- Response headers as JSON
response_body         BYTEA             -- Base64-encoded body (truncated)
response_body_size    INT               -- Original body size
response_content_type TEXT              -- Content-Type header

-- Timing
start_time            TIMESTAMP         -- Request start
end_time              TIMESTAMP         -- Response received
duration_ms           FLOAT             -- Request duration in milliseconds

-- Connection metadata
client_ip             TEXT              -- Client IP address
server_ip             TEXT              -- Server IP address
server_port           INT               -- Server port
is_https              BOOLEAN           -- TLS connection

-- Error handling
error_message         TEXT              -- Error description (if any)
error_type            TEXT              -- Error type/category

-- Classification
traffic_type          TEXT              -- llm | mcp | web | other

-- Audit
created_at            TIMESTAMP         -- Record creation time
```

**Indexes:**
- `start_time` - Query by time range
- `host` - Filter by hostname
- `traffic_type` - Filter by classification
- `created_at` - Sort by creation
- `status_code` - Filter by status
- `proxy_direction` - Filter by proxy direction
- `session_id` - Filter by Claude Code session
- `(session_id, start_time)` - Composite index for session-based queries

## Session ID Extraction

The MITM addon automatically extracts Claude Code session IDs from the request body's `metadata.user_id` field. This allows you to:

- Link reverse proxy (client→LiteLLM) and forward proxy (LiteLLM→provider) requests by session
- Track complete request flows across both proxy layers
- Filter and analyze traffic per Claude Code session

**Session ID Format:**

Claude Code embeds session information in the `metadata.user_id` field with the format:

```
user_{hash}_account_{uuid}_session_{uuid}
```

The addon extracts the final UUID after `_session_` and stores it in the `session_id` column.

**Example:**

```json
{
  "metadata": {
    "user_id": "user_abc123_account_def456_session_789xyz"
  }
}
```

Extracted `session_id`: `789xyz`

## Traffic Classification

Traffic is automatically classified based on host and path patterns:

### Classification Logic

```
┌─────────────────────────────────────────┐
│          Request Received               │
└─────────────┬───────────────────────────┘
              ↓
      ┌───────────────┐
      │ Extract host  │
      │ and path      │
      └───────┬───────┘
              ↓
     ┌────────────────────┐
     │ Check LLM patterns │──yes──▶ llm
     └────────┬───────────┘
              │no
              ↓
     ┌────────────────────┐
     │ Check MCP patterns │──yes──▶ mcp
     └────────┬───────────┘
              │no
              ↓
     ┌────────────────────┐
     │ Check if localhost │──yes──▶ other
     └────────┬───────────┘
              │no
              ↓
            web
```

### Classification Types

**llm** - LLM API requests:
- `api.anthropic.com` - Claude API
- `api.openai.com` - OpenAI API
- `generativelanguage.googleapis.com` - Gemini API
- `api.cohere.ai` - Cohere API
- `bedrock` - AWS Bedrock
- `azure.com/openai` - Azure OpenAI

**mcp** - Model Context Protocol:
- Host or path contains "mcp"

**web** - External web requests:
- Any non-localhost HTTP/HTTPS traffic

**other** - Internal/proxy traffic:
- `localhost`, `127.0.0.1`, `::1`

## Usage Workflows

### Basic Workflow

```bash
# 1. Start database
docker compose up -d

# 2. Apply schema
DATABASE_URL="postgresql://ccproxy:test@localhost:5432/ccproxy" prisma db push

# 3. Start proxy with MITM enabled
ccproxy start --mitm --detach

# 4. Run commands through proxy
ccproxy run claude -p "hello world"

# 5. Check status
ccproxy status

# 6. View logs
tail -f ~/.ccproxy/mitm-reverse.log
tail -f ~/.ccproxy/mitm-forward.log

# 7. Query database
psql postgresql://ccproxy:test@localhost:5432/ccproxy -c "SELECT * FROM \"CCProxy_HttpTraces\" ORDER BY start_time DESC LIMIT 10;"

# 8. Stop all proxies
ccproxy stop
```

### Integration with `ccproxy run`

When MITM is running, `ccproxy run` automatically routes traffic through mitmproxy:

```bash
# Automatic routing detection
ccproxy run claude -p "test"

# Environment variables set:
# - HTTPS_PROXY=http://localhost:8081
# - HTTP_PROXY=http://localhost:8081
# - ANTHROPIC_BASE_URL=http://localhost:8081
```

**Dual-proxy traffic flow:**

```
┌────────┐     ┌───────────┐     ┌──────────┐     ┌───────────┐     ┌────────┐
│ Client │────▶│ MITM Rev. │────▶│ LiteLLM  │────▶│ MITM Fwd. │────▶│  LLM   │
│        │     │   :4000   │     │ (random) │     │   :8081   │     │  API   │
└────────┘     └─────┬─────┘     └──────────┘     └─────┬─────┘     └────────┘
                     │                                   │
                     └──────────────┬────────────────────┘
                                    ↓
                              ┌──────────┐
                              │PostgreSQL│
                              │  Traces  │
                              └──────────┘
```

The dual-proxy architecture captures traffic at both ends:
- **MITM Reverse** (:4000): Captures incoming client requests before LiteLLM processing
- **MITM Forward** (:8081): Captures outbound API calls to LLM providers

### Debugging Workflow

```bash
# 1. Enable detailed logging
export PYTHONBREAKPOINT=pdbp.set_trace
ccproxy mitm start  # foreground mode for logs

# 2. In another terminal, run test
ccproxy run curl https://api.anthropic.com/v1/messages

# 3. Query specific traffic
psql $DATABASE_URL -c "
  SELECT method, url, status_code, duration_ms
  FROM \"CCProxy_HttpTraces\"
  WHERE traffic_type = 'llm'
  ORDER BY start_time DESC
  LIMIT 5;
"
```

### Analysis Queries

```sql
-- View recent traces with direction and session
SELECT trace_id, proxy_direction, session_id, method, url, start_time
FROM "CCProxy_HttpTraces"
ORDER BY start_time DESC
LIMIT 10;

-- Link reverse and forward proxy requests by session
SELECT
  proxy_direction,
  method,
  url,
  status_code,
  duration_ms,
  start_time
FROM "CCProxy_HttpTraces"
WHERE session_id = 'your-session-uuid'
ORDER BY start_time;

-- Top 10 slowest requests
SELECT url, duration_ms, status_code, proxy_direction
FROM "CCProxy_HttpTraces"
ORDER BY duration_ms DESC NULLS LAST
LIMIT 10;

-- Error rate by host
SELECT
  host,
  COUNT(*) FILTER (WHERE status_code >= 400) AS errors,
  COUNT(*) AS total,
  ROUND(100.0 * COUNT(*) FILTER (WHERE status_code >= 400) / COUNT(*), 2) AS error_rate
FROM "CCProxy_HttpTraces"
GROUP BY host
ORDER BY error_rate DESC;

-- Traffic breakdown by direction
SELECT
  CASE proxy_direction
    WHEN 0 THEN 'reverse (client→LiteLLM)'
    WHEN 1 THEN 'forward (LiteLLM→provider)'
  END AS direction,
  traffic_type,
  COUNT(*) AS requests,
  ROUND(AVG(duration_ms)::numeric, 2) AS avg_duration_ms
FROM "CCProxy_HttpTraces"
GROUP BY proxy_direction, traffic_type
ORDER BY proxy_direction, requests DESC;

-- Recent LLM API calls with session tracking
SELECT
  host,
  method,
  status_code,
  duration_ms,
  session_id,
  proxy_direction,
  start_time
FROM "CCProxy_HttpTraces"
WHERE traffic_type = 'llm'
ORDER BY start_time DESC
LIMIT 20;
```

## Advanced Configuration

### Custom SSL Certificates

For enterprise environments with custom CA certificates:

```yaml
ccproxy:
  mitm:
    cert_dir: /path/to/custom/certs
```

### Exclude Sensitive Hosts

Prevent capturing traffic to specific hosts:

```yaml
ccproxy:
  mitm:
    excluded_hosts:
      - "internal-api.company.com"
      - "metrics.internal"
```

### Body Truncation

Control storage size by adjusting `max_body_size`:

```yaml
ccproxy:
  mitm:
    max_body_size: 131072  # 128KB
    capture_bodies: true
```

Set `capture_bodies: false` to skip bodies entirely (headers only).

## Environment Variables

**Runtime configuration:**

```bash
# Set via CLI start command or environment
export CCPROXY_MITM_PORT=8081
export CCPROXY_MITM_UPSTREAM=http://localhost:4000
export CCPROXY_MITM_MAX_BODY_SIZE=0
export CCPROXY_MITM_MODE=reverse  # or "forward" for LiteLLM→provider direction

# MITM database (dedicated ccproxy-db container)
export CCPROXY_DATABASE_URL=postgresql://ccproxy:test@localhost:5432/ccproxy
# Falls back to DATABASE_URL if CCPROXY_DATABASE_URL not set
export DATABASE_URL=postgresql://ccproxy:test@localhost:5432/ccproxy

# Debug mode
export CCPROXY_DEBUG=true
```

These override `ccproxy.yaml` settings when running `mitm start`.

**Proxy Direction:**

The `CCPROXY_MITM_MODE` environment variable determines which direction the MITM proxy captures:

- `reverse` (default): Captures client→LiteLLM traffic (incoming requests before processing)
- `forward`: Captures LiteLLM→provider traffic (outbound API calls to LLM providers)

The dual-proxy architecture uses both modes simultaneously to capture traffic at both ends.

## Troubleshooting

### Database Connection Failed

```
ERROR: Failed to connect storage: connection refused
```

**Solution:**
```bash
# Verify DATABASE_URL is set
echo $DATABASE_URL

# Test connection
psql $DATABASE_URL -c "SELECT 1;"

# Run migrations
prisma db push
```

### Mitmproxy Not Found

```
Error: mitmdump not found at /path/to/bin/mitmdump
```

**Solution:**
```bash
# Install mitmproxy in same environment
uv add mitmproxy

# Verify installation
which mitmdump
```

### SSL Certificate Errors

```
SSL verification failed
```

**Solution:**
```bash
# Install mitmproxy CA certificate
# Follow: https://docs.mitmproxy.org/stable/concepts-certificates/

# Or disable SSL verification (development only)
export CURL_CA_BUNDLE=""
export REQUESTS_CA_BUNDLE=""
```

### Port Already in Use

```
Error: Address already in use
```

**Solution:**
```bash
# Find process using port
lsof -i :8081

# Use different port
ccproxy mitm start --port 8082
```

### Prisma OpenSSL 3.6.x Compatibility (Arch Linux)

```
Error: Unable to load shared library 'libssl.so.3'
```

On Arch Linux with OpenSSL 3.6.x, Prisma engine binaries may not find the correct library.

**Solution:**
```bash
# Find the Prisma binaries directory
cd ~/.cache/prisma-python/binaries/

# Symlink the 3.0.x binary name to 3.6.x
# (exact path depends on your Prisma version)
ln -s /usr/lib/libssl.so.3 libssl.so.3.0
ln -s /usr/lib/libcrypto.so.3 libcrypto.so.3.0
```

## Performance Considerations

**Buffered writes:** Traffic data is queued asynchronously with a buffer size of 1000 operations. Under high load, the queue may delay writes.

**Body truncation:** Bodies larger than `max_body_size` are truncated. Increase this value if you need full bodies, but monitor database growth.

**Indexes:** The schema includes indexes on common query fields. Add custom indexes for specific analysis patterns.

**Database cleanup:** Implement periodic cleanup to manage database size:

```sql
-- Delete traces older than 30 days
DELETE FROM "CCProxy_HttpTraces"
WHERE created_at < NOW() - INTERVAL '30 days';
```
