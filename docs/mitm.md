# MITM Traffic Capture

## Overview

The MITM (Man-in-the-Middle) feature captures all HTTP/HTTPS traffic passing through ccproxy using [mitmproxy](https://mitmproxy.org/). Traffic is stored in PostgreSQL for analysis and debugging.

**Key capabilities:**
- Capture requests/responses with headers and bodies
- Traffic classification (llm, mcp, web, other)
- Automatic body truncation and compression
- Asynchronous buffered writes
- Works transparently with `ccproxy run`

## Prerequisites

### Dependencies

```bash
# Required packages
uv add mitmproxy prisma

# Generate Prisma client
prisma generate
```

### PostgreSQL Database

Set the connection URL via environment variable:

```bash
export DATABASE_URL="postgresql://user:password@localhost:5432/ccproxy"
```

### Apply Schema

Run migrations to create the `CCProxy_HttpTraces` table:

```bash
prisma db push
```

## Configuration

Configure MITM in `~/.ccproxy/ccproxy.yaml`:

```yaml
ccproxy:
  mitm:
    enabled: true              # Enable traffic capture
    port: 8081                 # Mitmproxy listen port
    upstream_proxy: "http://localhost:4000"  # LiteLLM proxy URL
    max_body_size: 65536       # Max body bytes to capture (64KB)
    capture_bodies: true       # Store request/response bodies
    excluded_hosts: []         # Hosts to skip (optional)
    cert_dir: null             # Custom SSL cert directory (optional)
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
| `max_body_size` | int | `65536` | Maximum body size in bytes |
| `capture_bodies` | bool | `true` | Capture request/response bodies |
| `excluded_hosts` | list[str] | `[]` | Hosts to exclude from capture |
| `cert_dir` | Path\|None | `None` | Custom SSL certificate directory |
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
# 1. Start LiteLLM proxy
ccproxy start --detach

# 2. Start MITM capture
ccproxy mitm start --detach

# 3. Run commands through proxy
ccproxy run claude -p "hello world"

# 4. Check status
ccproxy mitm status

# 5. View logs
tail -f ~/.ccproxy/mitm.log

# 6. Query database
psql $DATABASE_URL -c "SELECT * FROM \"CCProxy_HttpTraces\" ORDER BY start_time DESC LIMIT 10;"

# 7. Stop MITM
ccproxy mitm stop
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
-- Top 10 slowest requests
SELECT url, duration_ms, status_code
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

-- Traffic breakdown
SELECT
  traffic_type,
  COUNT(*) AS requests,
  ROUND(AVG(duration_ms)::numeric, 2) AS avg_duration_ms
FROM "CCProxy_HttpTraces"
GROUP BY traffic_type
ORDER BY requests DESC;

-- Recent LLM API calls
SELECT
  host,
  method,
  status_code,
  duration_ms,
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
export CCPROXY_MITM_MAX_BODY_SIZE=65536
export DATABASE_URL=postgresql://...
```

These override `ccproxy.yaml` settings when running `mitm start`.

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
