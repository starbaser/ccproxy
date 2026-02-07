# Plan: `ccproxy db sql` Command

## Summary

Add a `ccproxy db sql` command that executes SQL queries against the MITM traces database, reading the connection string from config automatically.

## Architecture

```
ccproxy db sql <query|--file|stdin>
        │
        ▼
┌───────────────────┐
│  DbSql Command    │  (Tyro dataclass in cli.py)
└────────┬──────────┘
         │
         ▼
┌───────────────────┐
│  get_database_url │  (reads from CCProxyConfig.mitm.database_url)
└────────┬──────────┘
         │
         ▼
┌───────────────────┐
│   asyncpg pool    │  (direct SQL execution, no Prisma ORM)
└────────┬──────────┘
         │
         ▼
┌───────────────────┐
│   Format Output   │  (table, json, csv)
└───────────────────┘
```

## Dependencies

**None required** - `asyncpg>=0.31.0` is already in `pyproject.toml`.

## CLI Interface (Tyro Dataclass)

```python
@attrs.define
class DbSql:
    """Execute SQL queries against the MITM traces database."""

    query: Annotated[str | None, tyro.conf.Positional] = None
    """SQL query to execute (inline)."""

    file: Annotated[Path | None, tyro.conf.arg(aliases=["-f"])] = None
    """Read SQL from file."""

    json: Annotated[bool, tyro.conf.arg(aliases=["-j"])] = False
    """Output results as JSON."""

    csv: Annotated[bool, tyro.conf.arg(aliases=["-c"])] = False
    """Output results as CSV."""
```

## Usage Examples

```bash
# Inline query
ccproxy db sql "SELECT COUNT(*) FROM \"CCProxy_HttpTraces\""

# From file
ccproxy db sql --file queries/recent_requests.sql

# From stdin (pipe)
echo "SELECT * FROM \"CCProxy_HttpTraces\" LIMIT 5" | ccproxy db sql

# JSON output for LLM consumption
ccproxy db sql "SELECT * FROM \"CCProxy_HttpTraces\" LIMIT 10" --json

# CSV export
ccproxy db sql "SELECT method, url, status_code FROM \"CCProxy_HttpTraces\"" --csv > traces.csv
```

## Implementation Steps

### Phase 1: Core Infrastructure
- Add `DbSql` dataclass to `cli.py`
- Add to `Command` union type
- Add entry_point rewrite for `db sql` → `db-sql`
- Implement `get_database_url()`

### Phase 2: SQL Execution
- Implement `execute_sql()` with asyncpg
- Implement `resolve_sql_input()` (inline, file, stdin)

### Phase 3: Output Formatting
- Implement `format_table()` using Rich
- Implement `format_json()`
- Implement `format_csv()`

### Phase 4: Integration
- Implement `handle_db_sql()`
- Add handler to `main()`

### Phase 5: Testing
- Unit tests for input resolution
- Unit tests for output formatters
- Integration tests with mocked asyncpg

## Key Functions

```python
def get_database_url(config_dir: Path) -> str | None:
    """Get database URL from ccproxy config with env var fallback.

    Priority:
    1. ccproxy.yaml -> ccproxy.mitm.database_url
    2. CCPROXY_DATABASE_URL environment variable
    3. DATABASE_URL environment variable
    """

async def execute_sql(database_url: str, query: str) -> tuple[list[dict], list[str]]:
    """Execute SQL query and return results with column names."""

def resolve_sql_input(cmd: DbSql) -> str:
    """Resolve SQL query from inline, file, or stdin."""

def handle_db_sql(config_dir: Path, cmd: DbSql) -> None:
    """Handle the db sql command."""
```

## Error Handling

| Error Scenario | Handling |
|----------------|----------|
| No SQL input provided | Print error, show usage hint, exit 1 |
| No database_url configured | Print error explaining config location, exit 1 |
| Database connection failure | Print error with connection details (no password), exit 1 |
| SQL syntax error | Print PostgreSQL error message, exit 1 |
| File not found (--file) | Print error with path, exit 1 |
| Both --json and --csv | Print error (mutually exclusive), exit 1 |

## Files to Modify

| File | Changes |
|------|---------|
| `src/ccproxy/cli.py` | Add DbSql dataclass, handlers, formatters |
| `tests/test_db_sql.py` | New test file |

## Verification

1. Start the ccproxy-db container: `docker compose up -d`
2. Apply schema: `DATABASE_URL="postgresql://ccproxy:test@localhost:5432/ccproxy" uv run prisma db push`
3. Test inline query: `ccproxy db sql "SELECT COUNT(*) FROM \"CCProxy_HttpTraces\""`
4. Test JSON output: `ccproxy db sql "SELECT * FROM \"CCProxy_HttpTraces\" LIMIT 1" --json`
5. Test file input: Create a `.sql` file and run `ccproxy db sql --file test.sql`
6. Run tests: `uv run pytest tests/test_db_sql.py -v`
