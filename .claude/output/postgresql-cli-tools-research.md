---
agent: perplexity
source: perplexity-research
date: 2026-01-17
topic: PostgreSQL CLI and Non-Interactive Database Access Tools
query: Research CLI and non-interactive tooling for programmatic PostgreSQL access without raw SQL
tools_used: [search]
---

# PostgreSQL CLI Tools for Non-Interactive Database Access

Research on CLI tools and non-interactive approaches for accessing PostgreSQL databases programmatically, avoiding raw SQL queries where possible.

## Context

- PostgreSQL database with HTTP trace data (table: `CCProxy_HttpTraces`)
- Using Prisma ORM with existing schema
- Need command-line / scriptable / automation-friendly tools
- Want to avoid writing raw SQL where possible

## Key Findings

### 1. Prisma Client - Native ORM Approach

**Recommendation**: ⭐ **BEST FOR YOUR USE CASE** - Already using Prisma

**Description**: Prisma Client is a type-safe query builder generated from your schema that enables programmatic database queries in JavaScript/TypeScript without raw SQL.

**Pros**:
- ✅ Already integrated into your project
- ✅ Type-safe queries (zero-SQL for basic CRUD)
- ✅ Excellent for scripting and automation
- ✅ Full programmatic API
- ✅ Handles migrations via `prisma migrate`

**Cons**:
- ❌ Requires Node.js/TypeScript runtime
- ❌ Complex aggregations may still need raw SQL
- ❌ Not a standalone CLI tool

**Usage Example**:
```javascript
const { PrismaClient } = require('@prisma/client');
const prisma = new PrismaClient();

async function main() {
  // Query CCProxy_HttpTraces without SQL
  const traces = await prisma.cCProxy_HttpTraces.findMany({
    where: {
      proxy_direction: 1,
      session_id: { not: null }
    },
    orderBy: { created_at: 'desc' },
    take: 100
  });

  console.log(JSON.stringify(traces, null, 2));
}

main();
```

**Installation**: Already available
**Docs**: https://www.prisma.io/docs/orm/reference/prisma-client-reference

---

### 2. Harlequin - Terminal SQL IDE

**Recommendation**: ⭐⭐⭐ **BEST TUI EXPERIENCE**

**Description**: Terminal-based SQL IDE written in Python with PostgreSQL adapter, VS Code-inspired keybindings, and rich data exploration features.

**Pros**:
- ✅ Beautiful TUI with syntax highlighting and autocomplete
- ✅ PostgreSQL adapter available
- ✅ Export results to CSV/JSON
- ✅ Query history and tabs
- ✅ Scriptable via Python
- ✅ Mouse + keyboard navigation
- ✅ Data catalog for schema exploration

**Cons**:
- ❌ Still requires writing SQL queries
- ❌ Python dependency (but uses `pip install`)
- ❌ Interactive-first (though scriptable)

**Installation**:
```bash
pip install harlequin harlequin-postgres
# or
uv tool install harlequin --with harlequin-postgres
```

**Usage**:
```bash
# Interactive
harlequin postgres://user:pass@localhost:5432/ccproxy_db

# Export query result
harlequin -e "SELECT * FROM CCProxy_HttpTraces LIMIT 100" --format json > traces.json
```

**Docs**: https://github.com/tconbeer/harlequin

---

### 3. rainfrog - Vim-like PostgreSQL TUI

**Recommendation**: ⭐⭐ **BEST FOR VIM USERS**

**Description**: Rust-based TUI for PostgreSQL with vim-like keybindings, quick table browsing, and spreadsheet-like editing.

**Pros**:
- ✅ Vim-like navigation (hjkl, search)
- ✅ Fast Rust implementation
- ✅ Quick schema/table browsing
- ✅ Session history and query favorites
- ✅ Syntax highlighting
- ✅ Manual row editing
- ✅ Supports DATABASE_URL env var

**Cons**:
- ❌ Still requires SQL for queries
- ❌ Limited export formats
- ❌ Interactive-focused (not ideal for scripting)

**Installation**:
```bash
# Via cargo
cargo install rainfrog

# Via package manager (check availability)
```

**Usage**:
```bash
# Connect via DATABASE_URL
export DATABASE_URL="postgres://user:pass@localhost:5432/ccproxy_db"
rainfrog

# Or via CLI
rainfrog --url postgres://user:pass@localhost:5432/ccproxy_db
```

**Docs**: https://github.com/achristmascarl/rainfrog

---

### 4. dsq - SQL on Files and Databases

**Recommendation**: ⭐⭐⭐ **BEST FOR FILE + DB HYBRID**

**Description**: CLI tool from DataStation for running SQL queries on JSON/CSV/Excel files AND PostgreSQL databases.

**Pros**:
- ✅ Query JSON/CSV/Parquet files directly
- ✅ Connect to PostgreSQL
- ✅ Pipe output to `jq` for further processing
- ✅ Handles nested JSON with path syntax
- ✅ Scriptable and automation-friendly
- ✅ Uses SQLite backend with extensions

**Cons**:
- ❌ Still requires SQL syntax
- ❌ Less mature than established tools
- ❌ Limited PostgreSQL-specific optimizations

**Installation**:
```bash
# From GitHub releases
# https://github.com/multiprocessio/dsq
```

**Usage**:
```bash
# Query JSON file
dsq api-results.json 'SELECT * FROM {0, "data.data"} ORDER BY id DESC' | jq

# Query PostgreSQL
dsq --database postgresql://user:pass@localhost:5432/ccproxy_db \
  "SELECT * FROM CCProxy_HttpTraces WHERE proxy_direction = 1"

# Query CSV
dsq traces.csv "SELECT COUNT(1) FROM {}"
```

**Docs**: https://datastation.multiprocess.io/blog/2022-03-23-dsq-0.9.0.html

---

### 5. usql - Universal Database CLI

**Recommendation**: ⭐⭐ **BEST FOR MULTI-DB ENVIRONMENTS**

**Description**: Universal command-line client for PostgreSQL, MySQL, SQLite, and many other databases with consistent syntax.

**Pros**:
- ✅ Single CLI for multiple database types
- ✅ PostgreSQL support with full features
- ✅ Scriptable with `-c` flag
- ✅ JSON/CSV output formats
- ✅ Active development

**Cons**:
- ❌ Still requires SQL queries
- ❌ Not a query builder
- ❌ Primarily a `psql` replacement

**Installation**:
```bash
# Via package manager or GitHub releases
# https://github.com/xo/usql
```

**Usage**:
```bash
# Interactive
usql postgres://user:pass@localhost:5432/ccproxy_db

# Scripting with JSON output
usql -c "SELECT * FROM CCProxy_HttpTraces LIMIT 10" \
  --format json \
  postgres://user:pass@localhost:5432/ccproxy_db > traces.json
```

**Docs**: https://github.com/xo/usql

---

### 6. Steampipe - SQL for APIs (Bonus)

**Recommendation**: ⭐ **SPECIALIZED USE CASE**

**Description**: Zero-ETL tool that translates SQL queries into API calls. Not directly for PostgreSQL querying, but interesting for API integration.

**Pros**:
- ✅ Query APIs using SQL syntax
- ✅ 450+ predefined API tables
- ✅ PostgreSQL wire protocol
- ✅ Export to CSV/JSON
- ✅ Multi-threading and caching

**Cons**:
- ❌ Not for querying existing PostgreSQL databases
- ❌ Designed for cloud API access
- ❌ Requires plugins for different services

**Use Case**: If you need to combine PostgreSQL data with cloud API data (AWS, GitHub, etc.)

**Installation**:
```bash
# Via package manager or website
# https://steampipe.io/downloads
```

**Docs**: https://steampipe.io/docs

---

## Other Tools Mentioned

### GUI Tools (Not CLI-focused)
- **DBeaver**: Open-source with scripting via automation
- **pgAdmin**: CLI mode via `pgadmin4-cli`
- **DataGrip**: JetBrains IDE with query builder

### Lesser-Known CLI Tools
- **gobang**: Cross-platform TUI (Rust, alpha stage)
- **lazysql**: TUI database tool (Go)
- **termdbms**: TUI for database files

---

## PostgreSQL Native JSON Output

For pure PostgreSQL scripting without third-party tools, use native JSON functions:

```sql
-- Generate JSON from query
SELECT json_agg(row_to_json(t))
FROM (
  SELECT * FROM CCProxy_HttpTraces LIMIT 100
) t;

-- Nested JSON with aggregation
SELECT json_build_object(
  'session_id', session_id,
  'traces', json_agg(row_to_json(t))
)
FROM CCProxy_HttpTraces
GROUP BY session_id;
```

Pipe to `jq` for further processing:
```bash
psql -t -A -c "SELECT json_agg(row_to_json(t)) FROM (...) t" | jq '.[] | select(.proxy_direction == 1)'
```

---

## Recommendations by Use Case

### For Your Project (ccproxy with Prisma)

1. **Primary**: **Prisma Client** - Already integrated, type-safe, best for automation
   ```javascript
   // scripts/query-traces.js
   const { PrismaClient } = require('@prisma/client');
   const prisma = new PrismaClient();

   const traces = await prisma.cCProxy_HttpTraces.findMany({
     where: { /* conditions */ }
   });
   ```

2. **Interactive Exploration**: **Harlequin** - Best TUI experience with export
   ```bash
   uv tool install harlequin --with harlequin-postgres
   harlequin postgres://localhost:5432/ccproxy_db
   ```

3. **Quick Scripts**: **psql + jq** - Native PostgreSQL JSON + command-line processing
   ```bash
   psql -t -A postgres://... -c "SELECT json_agg(...)" | jq '.[]'
   ```

### By Priority

**High Priority**:
- Prisma Client (already have it, type-safe)
- Harlequin (best TUI for exploration)

**Medium Priority**:
- rainfrog (vim users, fast exploration)
- dsq (if working with JSON/CSV files too)

**Low Priority**:
- usql (only if managing multiple DB types)
- Steampipe (only for API integration)

---

## Installation Quick Reference

```bash
# Prisma Client (already installed)
# Just use it in Node.js scripts

# Harlequin (recommended)
uv tool install harlequin --with harlequin-postgres

# rainfrog (vim users)
cargo install rainfrog

# dsq (file + DB hybrid)
# Download from: https://github.com/multiprocessio/dsq/releases

# usql (multi-DB environments)
# Download from: https://github.com/xo/usql/releases
```

---

## Conclusion

**For ccproxy project**:
- ✅ Use **Prisma Client** for all programmatic access (type-safe, no SQL)
- ✅ Install **Harlequin** for interactive exploration with export
- ✅ Use **psql + jq** for quick one-off queries in shell scripts
- ✅ Consider **rainfrog** if you prefer vim-like navigation

**Avoid**: GUI tools (DBeaver, pgAdmin) since requirement is CLI/non-interactive.

**Key Insight**: Most CLI tools still require SQL. True "no SQL" access requires an ORM (Prisma Client) or native application code. For CLI work, focus on tools with good output formats (JSON/CSV) and pipe to processing tools like `jq`.
