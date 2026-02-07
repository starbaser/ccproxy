# ccproxy Agent Documentation

## Database Query Commands

### Quick Reference

```bash
# Basic query
ccproxy db sql "SELECT COUNT(*) FROM \"CCProxy_HttpTraces\""

# From file
ccproxy db sql --file query.sql

# Output formats
ccproxy db sql "SELECT * FROM \"CCProxy_HttpTraces\" LIMIT 10" --json
ccproxy db sql "SELECT * FROM \"CCProxy_HttpTraces\" LIMIT 10" --csv
```

### Key Table: `CCProxy_HttpTraces`

**Important Fields:**
- `proxy_direction` - 0=reverse (client→LiteLLM), 1=forward (LiteLLM→provider)
- `session_id` - Links related requests across proxy layers (extracted from `metadata.user_id`)
- `method`, `url`, `request_headers`, `response_headers`
- `request_body`, `response_body` - HTTP payload content
- `timestamp` - Request timestamp

**Common Queries:**

```sql
-- Filter by session
SELECT * FROM "CCProxy_HttpTraces" WHERE session_id = 'abc123';

-- Reverse proxy traffic only
SELECT * FROM "CCProxy_HttpTraces" WHERE proxy_direction = 0;

-- Forward proxy traffic only
SELECT * FROM "CCProxy_HttpTraces" WHERE proxy_direction = 1;

-- Recent traces with body content
SELECT timestamp, method, url, request_body
FROM "CCProxy_HttpTraces"
ORDER BY timestamp DESC
LIMIT 20;
```

**Database Connection:**
- Set via `CCPROXY_DATABASE_URL` environment variable
- Or configure in `ccproxy.yaml` under `litellm.environment`
- Current: `postgresql://ccproxy:test@localhost:5432/ccproxy_mitm`
