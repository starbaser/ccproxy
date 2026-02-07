# pgdump Script Fix Summary

## Problem

The original `pgdump` script used `pgclimb` for PostgreSQL JSON export, which failed with authentication error:

```
pq: unknown authentication response: 10
```

This error occurs because pgclimb doesn't support SCRAM-SHA-256 authentication used by modern PostgreSQL installations.

## Solution

Replaced `pgclimb` with native `psql` JSON export:

1. **Removed pgclimb dependency** - No longer requires external tool
2. **Docker support** - Automatically detects and uses `docker exec` if PostgreSQL client not installed locally
3. **Quoted table names** - Properly handles mixed-case table names (e.g., `CCProxy_HttpTraces`)
4. **JSON array to JSONL** - Uses `psql` with `json_agg(row_to_json(t))` piped to `jq -c '.[]'`

## Key Changes

### Authentication Fix

```bash
# Before (pgclimb with unsupported auth)
pgclimb --host localhost --port 5432 --dbname ccproxy_mitm ...

# After (psql with standard auth or docker exec)
psql -h localhost -p 5432 -d ccproxy_mitm ...
# OR
docker exec -i litellm-db psql -h localhost -p 5432 -d ccproxy_mitm ...
```

### Table Name Handling

```sql
-- Before (fails with mixed case)
SELECT * FROM CCProxy_HttpTraces WHERE created_at > '2026-01-18T01:15:00Z'

-- After (properly quoted)
SELECT * FROM "CCProxy_HttpTraces" WHERE created_at > '2026-01-18T01:15:00Z'
```

### JSON Export

```bash
# Query produces JSON array, jq converts to JSONL
psql -t -A -c "SELECT json_agg(row_to_json(t)) FROM (SELECT * FROM \"table\") t" \
  | jq -c '.[]' > output.jsonl
```

## Usage

### Basic Export

```bash
./scripts/pgdump \
  -d ccproxy_mitm \
  -U ccproxy \
  -h localhost \
  -p 5432 \
  -O /tmp/mitm_dump \
  --column created_at \
  "CCProxy_HttpTraces"
```

### Incremental Export (since timestamp)

```bash
./scripts/pgdump \
  -d ccproxy_mitm \
  -U ccproxy \
  -h localhost \
  -p 5432 \
  -O /tmp/mitm_dump \
  --since '2026-01-18T01:15:00Z' \
  --column created_at \
  -v \
  "CCProxy_HttpTraces"
```

### Incremental Export (using state file)

After first export, state is tracked in `$OUTPUT_DIR/.pgdump/last_export.tsv`:

```bash
# First export
./scripts/pgdump -d ccproxy_mitm -U ccproxy -O /tmp/mitm_dump --column created_at "CCProxy_HttpTraces"

# Subsequent exports only fetch new rows
./scripts/pgdump -d ccproxy_mitm -U ccproxy -O /tmp/mitm_dump --column created_at "CCProxy_HttpTraces"
```

### Full Export (ignore state)

```bash
./scripts/pgdump \
  -d ccproxy_mitm \
  -U ccproxy \
  -O /tmp/mitm_dump \
  --full \
  --column created_at \
  "CCProxy_HttpTraces"
```

## Output Format

**JSONL** - One JSON object per line:

```json
{"trace_id":"f94abaf3-ffd3-493b-bf65-bb7bcd70855d","method":"POST","url":"https://api.z.ai/...","status_code":200,...}
{"trace_id":"a1b2c3d4-e5f6-7890-abcd-ef1234567890","method":"GET","url":"https://api.z.ai/...","status_code":200,...}
```

## Dependencies

- **psql** - PostgreSQL client (or docker with litellm-db container)
- **jq** - JSON processor for array to JSONL conversion

## Docker Support

Script automatically detects and uses docker if:

1. `psql` not found in PATH
2. Docker is available
3. Container `litellm-db` is running

Can override container name with environment variable:

```bash
DOCKER_CONTAINER=my-postgres-container ./scripts/pgdump ...
```

## Environment Variables

```bash
# Connection
DB_HOST=localhost
DB_PORT=5432
DB_NAME=ccproxy_mitm
DB_USER=ccproxy
DB_PASS=secret

# Incremental column
INC_COLUMN=created_at

# Docker container
DOCKER_CONTAINER=litellm-db
```

## Files Modified

- `/home/starbased/dev/projects/ccproxy/scripts/pgdump`
  - Removed pgclimb dependency
  - Added docker exec support
  - Fixed table name quoting
  - Changed from pgclimb to psql + jq JSON export
