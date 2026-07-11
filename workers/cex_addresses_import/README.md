# CEX addresses import

> Project context: [AGENTS.md](../../AGENTS.md) · [Supabase map](../../docs/SUPABASE.md) · [Architecture](../../docs/ARCHITECTURE.md)

Reference-data job: fetch the latest Dune CEX address list and upsert into `wallets.cex_addresses` via one SQL RPC. No wallet claim / eligibility loop.

## Pipeline

1. `GET https://api.dune.com/api/v1/query/{DUNE_QUERY_ID}/results` (paginated)
2. Fail if 0 rows (do not wipe good data)
3. `SELECT wallets.cex_addresses_upsert(rows::jsonb)`
4. Exit 0 on success, 1 on Dune/DB failure

Default query id: **7520736** (`source` written as `dune_query_7520736`).

If a single jsonb payload is too large for the pooler, call the same RPC in chunks of N rows (same upsert semantics). Start with one call.

## Schedule

GitHub Actions: days **1 and 16** at 00:00 UTC + `workflow_dispatch`.

## Env

| Variable | Required | Default | Role |
|---|---|---|---|
| `SUPABASE_DB_URL` | Yes | — | Postgres pooler DSN |
| `DUNE_KEY` | Yes | — | Dune API key |
| `DUNE_QUERY_ID` | No | `7520736` | Dune query id |
| `DUNE_PAGE_SIZE` | No | `10000` | Rows per Dune page |

## Local run

```powershell
cd workers/cex_addresses_import
copy .env.example .env
# Set SUPABASE_DB_URL and DUNE_KEY

uv sync
uv run python job.py
```

## Monitoring SQL

```sql
SELECT count(*) AS rows, max(updated_at) AS last_updated
FROM wallets.cex_addresses;

SELECT chain, count(*) AS n
FROM wallets.cex_addresses
GROUP BY 1
ORDER BY n DESC
LIMIT 10;

SELECT cex_name, count(*) AS n
FROM wallets.cex_addresses
GROUP BY 1
ORDER BY n DESC
LIMIT 10;
```

## Schema dependency

RPC lives in sibling repo **`gsa-supabase-schema`**: `wallets.cex_addresses_upsert(p_rows jsonb)`. Deploy that migration before relying on this worker.
